"""Tracker adapters: normalize provider payloads into a stable issue model.

By design, trackers here are *read-only*. Ticket writes
(state transitions, comments, PR links) are performed by the coding agent via
provider-native MCP tools, not by the orchestrator.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class TrackerError(RuntimeError):
    """Raised when a tracker cannot be reached or returns an unusable payload."""


class Issue(object):
    """Normalized issue model shared by every adapter."""

    __slots__ = ("id", "identifier", "title", "description", "state", "labels", "url", "raw")

    def __init__(self, id: str, identifier: str, title: str, state: str,
                 description: str = "", labels: Optional[List[str]] = None,
                 url: str = "", raw: Optional[Dict[str, Any]] = None):
        self.id = str(id)
        self.identifier = str(identifier)
        self.title = title
        self.description = description or ""
        self.state = state
        self.labels = list(labels or [])
        self.url = url
        self.raw = raw or {}

    def as_context(self) -> Dict[str, Any]:
        """Shape exposed to the prompt template as ``issue.*``."""
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "state": self.state,
            "labels": self.labels,
            "url": self.url,
        }

    def __repr__(self) -> str:
        return "Issue(%s, state=%r)" % (self.identifier, self.state)


class Tracker(object):
    """Adapter interface. Subclasses must implement :meth:`fetch_all`."""

    kind = "base"

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.active_states = list(config.get("active_states") or [])
        self.terminal_states = list(config.get("terminal_states") or [])
        self.required_labels = list(config.get("required_labels") or [])

    def fetch_all(self) -> List[Issue]:
        raise NotImplementedError

    # -- derived queries (shared by all adapters) --------------------------
    def _eligible(self, issue: Issue) -> bool:
        if issue.state not in self.active_states:
            return False
        if self.required_labels and not set(self.required_labels).issubset(set(issue.labels)):
            return False
        return True

    def fetch_active(self) -> List[Issue]:
        return [i for i in self.fetch_all() if self._eligible(i)]

    def fetch_terminal(self) -> List[Issue]:
        return [i for i in self.fetch_all() if i.state in self.terminal_states]

    def fetch_states(self, issue_ids: List[str]) -> Dict[str, str]:
        wanted = set(issue_ids)
        return {i.id: i.state for i in self.fetch_all() if i.id in wanted}


def build_tracker(config: Dict[str, Any]) -> Tracker:
    """Factory keyed on ``tracker.kind`` from WORKFLOW.md."""
    kind = (config.get("kind") or "file").lower()
    if kind == "file":
        from .file import FileTracker
        return FileTracker(config)
    if kind == "jira":
        from .jira import JiraTracker
        return JiraTracker(config)
    raise TrackerError(
        "unknown tracker.kind %r (available: file, jira)" % kind
    )
