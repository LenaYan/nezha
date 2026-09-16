import json

import pytest

from nezha.tracker import build_tracker
from nezha.tracker.base import TrackerError


def cfg(board, **overrides):
    base = {
        "kind": "file",
        "provider": {"path": str(board)},
        "active_states": ["Todo", "In Progress"],
        "terminal_states": ["Done"],
        "required_labels": [],
    }
    base.update(overrides)
    return base


def test_fetch_all_normalizes(board):
    issues = build_tracker(cfg(board)).fetch_all()
    assert [i.identifier for i in issues] == ["T-1", "T-2", "T-3", "T-4"]
    assert issues[0].description == "do the thing"
    assert issues[0].as_context()["labels"] == ["nezha"]


def test_active_excludes_terminal_and_unknown_states(board):
    active = build_tracker(cfg(board)).fetch_active()
    assert sorted(i.identifier for i in active) == ["T-1", "T-4"]


def test_required_labels_filter(board):
    active = build_tracker(cfg(board, required_labels=["nezha"])).fetch_active()
    assert [i.identifier for i in active] == ["T-1"]


def test_terminal_and_states(board):
    tracker = build_tracker(cfg(board))
    assert [i.identifier for i in tracker.fetch_terminal()] == ["T-2"]
    assert tracker.fetch_states(["T-1", "T-2"]) == {"T-1": "Todo", "T-2": "Done"}


def test_set_state_roundtrip(board):
    tracker = build_tracker(cfg(board))
    tracker.set_state("T-1", "Done")
    assert json.loads(board.read_text())["issues"][0]["state"] == "Done"
    assert [i.identifier for i in tracker.fetch_active()] == ["T-4"]


def test_set_state_unknown_issue(board):
    with pytest.raises(TrackerError, match="not found"):
        build_tracker(cfg(board)).set_state("NOPE", "Done")


def test_missing_board_file(tmp_path):
    tracker = build_tracker(cfg(tmp_path / "absent.json"))
    with pytest.raises(TrackerError, match="not found"):
        tracker.fetch_all()


def test_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(TrackerError, match="not valid JSON"):
        build_tracker(cfg(bad)).fetch_all()


def test_missing_issue_keys(tmp_path):
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"issues": [{"id": "X"}]}), encoding="utf-8")
    with pytest.raises(TrackerError, match="missing required keys"):
        build_tracker(cfg(bad)).fetch_all()


def test_issues_must_be_array(tmp_path):
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"issues": {}}), encoding="utf-8")
    with pytest.raises(TrackerError, match="'issues' array"):
        build_tracker(cfg(bad)).fetch_all()


def test_path_required():
    with pytest.raises(TrackerError, match="provider.path is required"):
        build_tracker({"kind": "file"})


def test_unknown_kind():
    with pytest.raises(TrackerError, match="unknown tracker.kind"):
        build_tracker({"kind": "trello"})


def test_jira_requires_provider_fields():
    with pytest.raises(TrackerError, match="provider.site_url is required"):
        build_tracker({"kind": "jira", "provider": {}})


def test_jira_adf_flattening():
    from nezha.tracker.jira import _adf_to_text
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "world"}]},
    ]}
    assert _adf_to_text(doc).split() == ["hello", "world"]
    assert _adf_to_text(None) == ""


# --- Jira network layer -------------------------------------------------
#
# Fixtures below mirror the node types and paging fields observed on a live
# Jira Cloud site (mediaGroup, hardBreak, inlineCard, bulletList, null
# descriptions, nextPageToken + isLast). Content is synthetic on purpose.

JIRA_PROVIDER = {
    "base_url": "https://example.atlassian.net/",
    "email": "bot@example.com",
    "token": "s3cr3t",
    "jql": "project = ABC",
}


def _jira(**overrides):
    provider = dict(JIRA_PROVIDER)
    provider.update(overrides)
    return build_tracker({"kind": "jira", "provider": provider})


class _FakeResponse:
    def __init__(self, body):
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, pages, calls=None):
    """Serve `pages` (list of dicts or Exceptions) in order, recording URLs."""
    from nezha.tracker import jira as jira_mod

    state = {"i": 0}

    def fake_urlopen(request, timeout=None):
        index = min(state["i"], len(pages) - 1)
        state["i"] += 1
        if calls is not None:
            calls.append({
                "url": request.full_url,
                "headers": dict(request.headers),
                "timeout": timeout,
            })
        page = pages[index]
        if isinstance(page, Exception):
            raise page
        if isinstance(page, str):
            return _FakeResponse(page)
        return _FakeResponse(json.dumps(page))

    monkeypatch.setattr(jira_mod.urllib.request, "urlopen", fake_urlopen)
    return state


def _row(key, status="Open", labels=None, description=None, summary="Do the thing"):
    return {
        "id": "1" + key.split("-")[-1],
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status, "statusCategory": {"key": "new"}},
            "labels": labels if labels is not None else [],
            "description": description,
        },
    }


def test_jira_request_shape(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, [{"issues": [_row("ABC-1")], "isLast": True}], calls)
    issues = _jira(timeout_sec=7).fetch_all()

    assert len(calls) == 1
    url = calls[0]["url"]
    # base_url trailing slash must not produce a double slash
    assert url.startswith("https://example.atlassian.net/rest/api/3/search/jql?")
    assert "jql=project+%3D+ABC" in url
    assert "maxResults=100" in url
    assert "fields=summary%2Cstatus%2Clabels%2Cdescription" in url
    assert "nextPageToken" not in url
    assert calls[0]["timeout"] == 7
    # urllib title-cases header names
    assert calls[0]["headers"]["Authorization"] == "Basic Ym90QGV4YW1wbGUuY29tOnMzY3IzdA=="
    assert issues[0].url == "https://example.atlassian.net/browse/ABC-1"
    assert issues[0].id == "ABC-1"
    assert issues[0].state == "Open"


def test_jira_paginates_until_is_last(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, [
        {"issues": [_row("ABC-1")], "isLast": False, "nextPageToken": "tok1"},
        {"issues": [_row("ABC-2")], "isLast": False, "nextPageToken": "tok2"},
        {"issues": [_row("ABC-3")], "isLast": True, "nextPageToken": "tok3"},
    ], calls)

    issues = _jira().fetch_all()

    assert [i.identifier for i in issues] == ["ABC-1", "ABC-2", "ABC-3"]
    assert len(calls) == 3
    assert "nextPageToken=tok1" in calls[1]["url"]
    assert "nextPageToken=tok2" in calls[2]["url"]


def test_jira_stops_when_token_missing(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, [
        {"issues": [_row("ABC-1")], "isLast": False},
        {"issues": [_row("ABC-2")], "isLast": False},
    ], calls)
    assert [i.identifier for i in _jira().fetch_all()] == ["ABC-1"]
    assert len(calls) == 1


def test_jira_breaks_on_repeated_token(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, [
        {"issues": [_row("ABC-1")], "isLast": False, "nextPageToken": "same"},
        {"issues": [_row("ABC-2")], "isLast": False, "nextPageToken": "same"},
    ], calls)
    issues = _jira().fetch_all()
    assert [i.identifier for i in issues] == ["ABC-1", "ABC-2"]
    assert len(calls) == 2


def test_jira_page_cap_raises(monkeypatch):
    from nezha.tracker import jira as jira_mod

    state = {"n": 0}

    def fake_urlopen(request, timeout=None):
        state["n"] += 1
        return _FakeResponse(json.dumps({
            "issues": [_row("ABC-%d" % state["n"])],
            "isLast": False,
            "nextPageToken": "tok%d" % state["n"],
        }))

    monkeypatch.setattr(jira_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TrackerError, match="exceeded 50 pages"):
        _jira().fetch_all()
    assert state["n"] == jira_mod._MAX_PAGES


def test_jira_http_error_is_tracker_error(monkeypatch):
    import io
    import urllib.error

    err = urllib.error.HTTPError(
        "https://example.atlassian.net", 401, "Unauthorized", {},
        io.BytesIO(b'{"errorMessages":["Client must be authenticated"]}'),
    )
    _patch_urlopen(monkeypatch, [err])
    with pytest.raises(TrackerError, match="HTTP 401"):
        _jira().fetch_all()


def test_jira_unreachable_is_tracker_error(monkeypatch):
    import urllib.error

    _patch_urlopen(monkeypatch, [urllib.error.URLError("nodename nor servname provided")])
    with pytest.raises(TrackerError, match="unreachable"):
        _jira().fetch_all()


def test_jira_non_json_is_tracker_error(monkeypatch):
    _patch_urlopen(monkeypatch, ["<html>proxy login</html>"])
    with pytest.raises(TrackerError, match="non-JSON"):
        _jira().fetch_all()


def test_jira_non_object_payload_is_tracker_error(monkeypatch):
    _patch_urlopen(monkeypatch, ["[]"])
    with pytest.raises(TrackerError, match="expected an object"):
        _jira().fetch_all()


def test_jira_tolerates_sparse_rows(monkeypatch):
    _patch_urlopen(monkeypatch, [{"issues": [
        {"key": "ABC-9", "fields": {"summary": "no status", "labels": None, "description": None}},
    ], "isLast": True}])
    issue = _jira().fetch_all()[0]
    assert issue.state == ""
    assert issue.labels == []
    assert issue.description == ""


def test_jira_adf_real_world_nodes():
    from nezha.tracker.jira import _adf_to_text

    # A description whose only content is an image previously flattened to "".
    media_only = {"type": "doc", "version": 1, "content": [
        {"type": "mediaGroup", "content": [
            {"type": "media", "attrs": {"type": "file", "id": "abc-123", "collection": ""}},
        ]},
        {"type": "paragraph", "content": []},
    ]}
    assert _adf_to_text(media_only).strip() == "[media: abc-123]"

    alt = {"type": "mediaSingle", "content": [
        {"type": "media", "attrs": {"id": "x", "alt": "screenshot.png"}},
    ]}
    assert "[media: screenshot.png]" in _adf_to_text(alt)

    # hardBreak carries the line structure of multi-line acceptance criteria.
    breaks = {"type": "paragraph", "content": [
        {"type": "text", "text": "line one"},
        {"type": "hardBreak"},
        {"type": "text", "text": "line two"},
    ]}
    assert _adf_to_text(breaks).strip().splitlines() == ["line one", "line two"]

    # inlineCard is a bare URL reference with no text child.
    card = {"type": "paragraph", "content": [
        {"type": "text", "text": "blocked by "},
        {"type": "inlineCard", "attrs": {"url": "https://example.atlassian.net/browse/ABC-1"}},
    ]}
    assert _adf_to_text(card).strip() == "blocked by https://example.atlassian.net/browse/ABC-1"

    bullets = {"type": "bulletList", "content": [
        {"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "first"}]}]},
        {"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "second"}]}]},
    ]}
    assert [l.strip() for l in _adf_to_text(bullets).strip().splitlines()] == ["- first", "- second"]

    linked = {"type": "text", "text": "the design", "marks": [
        {"type": "link", "attrs": {"href": "https://example.com/d"}},
    ]}
    assert _adf_to_text(linked) == "the design (https://example.com/d)"

    # Formatting-only marks must not leak into the text.
    strong = {"type": "text", "text": "urgent", "marks": [{"type": "strong"}]}
    assert _adf_to_text(strong) == "urgent"

    assert _adf_to_text({"type": "mention", "attrs": {"text": "@alice", "id": "1"}}) == "@alice"
    assert _adf_to_text({"type": "rule"}).strip() == "---"
    assert _adf_to_text({"type": "unknownFutureNode", "content": [
        {"type": "text", "text": "kept"}]}) == "kept"


# --- Jira auth modes ----------------------------------------------------

BEARER_PROVIDER = {
    "auth": "bearer",
    "cloud_id": "cafe-1234",
    "site_url": "https://example.atlassian.net",
    "token": "oauth-token",
    "jql": "project = ABC",
}


def test_jira_base_url_is_accepted_as_site_url_alias(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, [{"issues": [_row("ABC-1")], "isLast": True}], calls)
    tracker = build_tracker({"kind": "jira", "provider": dict(JIRA_PROVIDER)})
    tracker.fetch_all()
    assert calls[0]["url"].startswith("https://example.atlassian.net/rest/api/3/")


def test_jira_bearer_uses_gateway_and_site_browse_links(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, [{"issues": [_row("ABC-1")], "isLast": True}], calls)
    tracker = build_tracker({"kind": "jira", "provider": dict(BEARER_PROVIDER)})
    issues = tracker.fetch_all()

    # A 3LO token is 401 on the site host; it must go through the gateway.
    assert calls[0]["url"].startswith(
        "https://api.atlassian.com/ex/jira/cafe-1234/rest/api/3/search/jql?")
    assert calls[0]["headers"]["Authorization"] == "Bearer oauth-token"
    # ...but the gateway URL is not browsable, so links stay on the site host.
    assert issues[0].url == "https://example.atlassian.net/browse/ABC-1"


def test_jira_bearer_requires_cloud_id():
    provider = dict(BEARER_PROVIDER)
    del provider["cloud_id"]
    with pytest.raises(TrackerError, match="cloud_id is required for auth=bearer"):
        build_tracker({"kind": "jira", "provider": provider})


def test_jira_basic_requires_email():
    provider = dict(JIRA_PROVIDER)
    del provider["email"]
    with pytest.raises(TrackerError, match="email is required for auth=basic"):
        build_tracker({"kind": "jira", "provider": provider})


def test_jira_rejects_unknown_auth_mode():
    provider = dict(BEARER_PROVIDER, auth="oauth2")
    with pytest.raises(TrackerError, match="must be 'basic' or 'bearer'"):
        build_tracker({"kind": "jira", "provider": provider})


def test_jira_requires_a_token_source():
    provider = dict(JIRA_PROVIDER)
    del provider["token"]
    with pytest.raises(TrackerError, match="token .*is required"):
        build_tracker({"kind": "jira", "provider": provider})


def test_jira_token_command_is_used_and_cached(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, [
        {"issues": [_row("ABC-1")], "isLast": False, "nextPageToken": "t1"},
        {"issues": [_row("ABC-2")], "isLast": True},
    ], calls)
    provider = dict(BEARER_PROVIDER)
    del provider["token"]
    provider["token_command"] = "printf from-command"
    tracker = build_tracker({"kind": "jira", "provider": provider})

    spawns = {"n": 0}

    import nezha.tracker.jira as jira_mod
    original = jira_mod.subprocess.run

    def counting_run(*args, **kwargs):
        spawns["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(jira_mod.subprocess, "run", counting_run)
    tracker.fetch_all()

    assert [c["headers"]["Authorization"] for c in calls] == ["Bearer from-command"] * 2
    # Two pages, but the command must only be spawned once.
    assert spawns["n"] == 1


def test_jira_token_command_ttl_expiry(monkeypatch):
    provider = dict(BEARER_PROVIDER, token_ttl_sec=0)
    del provider["token"]
    provider["token_command"] = "printf tok"
    tracker = build_tracker({"kind": "jira", "provider": provider})
    assert tracker._token() == "tok"
    assert tracker._token() == "tok"
    assert tracker._token_cache == "tok"


def test_jira_token_command_failure_is_tracker_error():
    provider = dict(BEARER_PROVIDER)
    del provider["token"]
    provider["token_command"] = "echo boom >&2; exit 3"
    tracker = build_tracker({"kind": "jira", "provider": provider})
    with pytest.raises(TrackerError, match="token_command exited 3: boom"):
        tracker.fetch_all()


def test_jira_token_command_empty_output_is_tracker_error():
    provider = dict(BEARER_PROVIDER)
    del provider["token"]
    provider["token_command"] = "true"
    tracker = build_tracker({"kind": "jira", "provider": provider})
    with pytest.raises(TrackerError, match="produced no token"):
        tracker.fetch_all()
