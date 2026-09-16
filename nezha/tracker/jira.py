"""Jira Cloud tracker adapter (read-only).

STATUS: verified against a live Jira Cloud site -- ``fetch_all`` executed a real
authenticated request and returned real issues. Paging (``nextPageToken`` /
``isLast``), ``fields.status.name``, and ADF-or-null descriptions all match.

Server/Data Center is NOT supported: those deploy ``/rest/api/2/search`` with
``startAt``/``total`` paging and wiki-markup descriptions instead of ADF.

Two auth modes:

``basic`` (default) -- ``email`` + API token against the site host::

    tracker:
      kind: jira
      provider:
        site_url: https://your-org.atlassian.net
        email: ${JIRA_EMAIL}
        token: ${JIRA_API_TOKEN}
        jql: project = ABC AND assignee = currentUser()

``bearer`` -- an OAuth 2.0 (3LO) access token with ``read:jira-work``, against
the ``api.atlassian.com`` gateway. The gateway is mandatory here: the same token
returns 401 on the site host. ``token_command`` runs a command whose stdout is
the token, so short-lived tokens can be refreshed without editing config::

    tracker:
      kind: jira
      provider:
        site_url: https://your-org.atlassian.net   # browse links only
        auth: bearer
        cloud_id: 00000000-0000-0000-0000-000000000000
        token_command: examples/atlassian-mcp-token.sh
        jql: assignee = currentUser()

``token_command`` executes a shell command from config. That is no worse than
the surrounding file, which already dictates what the agent is told to do, but
treat WORKFLOW.md as trusted input.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from .base import Issue, Tracker, TrackerError

_MAX_RESULTS = 100
_MAX_PAGES = 50

# Node types that end a line once their children are flattened.
_BLOCK_TYPES = ("paragraph", "heading", "codeBlock", "blockquote", "panel", "rule")


def _link_href(node: Dict[str, Any]) -> str:
    for mark in node.get("marks") or []:
        if isinstance(mark, dict) and mark.get("type") == "link":
            href = (mark.get("attrs") or {}).get("href")
            if href:
                return str(href)
    return ""


def _adf_to_text(node: Any, depth: int = 0) -> str:
    """Flatten Atlassian Document Format into plain text (best effort).

    Leaf nodes that carry meaning only in ``attrs`` (media, cards, mentions)
    are rendered as placeholders; dropping them silently turned real tickets
    into empty descriptions.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n, depth) for n in node)
    if not isinstance(node, dict):
        return ""

    kind = node.get("type")
    attrs = node.get("attrs") or {}

    if kind == "text":
        text = node.get("text", "")
        href = _link_href(node)
        return "%s (%s)" % (text, href) if href and href != text else text
    if kind == "hardBreak":
        return "\n"
    if kind in ("inlineCard", "blockCard", "embedCard"):
        url = attrs.get("url")
        if not url and isinstance(attrs.get("data"), dict):
            url = attrs["data"].get("url")
        return str(url) if url else ""
    if kind == "media":
        label = attrs.get("alt") or attrs.get("id") or "attachment"
        return "[media: %s]" % label
    if kind == "mention":
        return str(attrs.get("text") or attrs.get("id") or "@unknown")
    if kind == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind == "rule":
        return "---\n"
    if kind == "date":
        return str(attrs.get("timestamp") or "")

    body = _adf_to_text(node.get("content"), depth + 1)
    if kind == "listItem":
        indent = "  " * max(depth - 2, 0)
        return "%s- %s\n" % (indent, body.strip())
    if kind in ("mediaSingle", "mediaGroup"):
        return body + "\n" if body else ""
    if kind in _BLOCK_TYPES:
        return body + "\n"
    return body


class JiraTracker(Tracker):
    kind = "jira"

    def __init__(self, config: Dict[str, Any]):
        super(JiraTracker, self).__init__(config)
        provider = config.get("provider") or {}
        self.auth_mode = str(provider.get("auth", "basic")).lower()
        if self.auth_mode not in ("basic", "bearer"):
            raise TrackerError(
                "tracker.provider.auth must be 'basic' or 'bearer', got %r" % provider.get("auth")
            )

        site_url = provider.get("site_url") or provider.get("base_url")
        if not site_url:
            raise TrackerError("tracker.provider.site_url is required for kind=jira")
        self.site_url = str(site_url).rstrip("/")

        if not provider.get("jql"):
            raise TrackerError("tracker.provider.jql is required for kind=jira")
        self.jql = provider["jql"]
        self.timeout = float(provider.get("timeout_sec", 30))
        self._token_command = provider.get("token_command")
        self._static_token = provider.get("token")
        self._token_ttl = float(provider.get("token_ttl_sec", 60))
        self._token_cache = None
        self._token_cache_until = 0.0
        if not self._token_command and not self._static_token:
            raise TrackerError(
                "tracker.provider.token (or token_command) is required for kind=jira"
            )

        if self.auth_mode == "basic":
            if not provider.get("email"):
                raise TrackerError("tracker.provider.email is required for auth=basic")
            self.email = provider["email"]
            # Basic auth works directly against the site host.
            self.api_root = self.site_url
        else:
            self.email = None
            cloud_id = provider.get("cloud_id")
            if not cloud_id:
                raise TrackerError(
                    "tracker.provider.cloud_id is required for auth=bearer "
                    "(GET /oauth/token/accessible-resources lists it)"
                )
            self.cloud_id = str(cloud_id)
            # A 3LO token is rejected by the site host; only the gateway accepts it.
            self.api_root = "https://api.atlassian.com/ex/jira/%s" % self.cloud_id

    def _token(self) -> str:
        if not self._token_command:
            return str(self._static_token)
        # fetch_all() pages, so without a cache the command would be spawned
        # once per page. The TTL keeps a daemon picking up rotated tokens.
        now = time.time()
        if self._token_cache and now < self._token_cache_until:
            return self._token_cache
        try:
            out = subprocess.run(
                self._token_command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise TrackerError("tracker.provider.token_command timed out after %ss" % self.timeout)
        except OSError as exc:
            raise TrackerError("tracker.provider.token_command failed to start: %s" % exc)
        if out.returncode != 0:
            raise TrackerError("tracker.provider.token_command exited %d: %s" % (
                out.returncode, out.stderr.decode("utf-8", "replace").strip()[:300]))
        token = out.stdout.decode("utf-8", "replace").strip()
        if not token:
            raise TrackerError("tracker.provider.token_command produced no token")
        self._token_cache = token
        self._token_cache_until = now + self._token_ttl
        return token

    def _auth_header(self) -> str:
        token = self._token()
        if self.auth_mode == "bearer":
            return "Bearer " + token
        credential = "%s:%s" % (self.email, token)
        return "Basic " + base64.b64encode(credential.encode("utf-8")).decode("ascii")

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = "%s%s?%s" % (self.api_root, path, urllib.parse.urlencode(params))
        request = urllib.request.Request(url, headers={
            "Authorization": self._auth_header(),
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise TrackerError("Jira %s -> HTTP %s: %s" % (path, exc.code, detail))
        except urllib.error.URLError as exc:
            raise TrackerError("Jira %s unreachable: %s" % (path, exc.reason))
        except ValueError as exc:
            raise TrackerError("Jira %s returned non-JSON: %s" % (path, exc))

    def fetch_all(self) -> List[Issue]:
        issues: List[Issue] = []
        next_token = None
        seen_tokens = set()
        for _ in range(_MAX_PAGES):
            params: Dict[str, Any] = {
                "jql": self.jql,
                "maxResults": _MAX_RESULTS,
                "fields": "summary,status,labels,description",
            }
            if next_token:
                params["nextPageToken"] = next_token
            payload = self._get("/rest/api/3/search/jql", params)
            if not isinstance(payload, dict):
                raise TrackerError("Jira search returned %s, expected an object" % type(payload).__name__)
            for row in payload.get("issues") or []:
                fields = row.get("fields") or {}
                status = (fields.get("status") or {}).get("name", "")
                issues.append(Issue(
                    id=row.get("key", row.get("id", "")),
                    identifier=row.get("key", ""),
                    title=fields.get("summary", ""),
                    state=status,
                    description=_adf_to_text(fields.get("description")).strip(),
                    labels=fields.get("labels") or [],
                    url="%s/browse/%s" % (self.site_url, row.get("key", "")),
                    raw=row,
                ))
            if payload.get("isLast"):
                break
            next_token = payload.get("nextPageToken")
            # A server that keeps handing back the same token would otherwise
            # spin forever inside the daemon loop.
            if not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
        else:
            raise TrackerError(
                "Jira search exceeded %d pages; narrow tracker.provider.jql" % _MAX_PAGES
            )
        return issues
