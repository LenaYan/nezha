"""File-backed tracker: a JSON board on disk.

Purpose: run and verify the full orchestration loop with zero external
credentials. Also useful as a dry-run harness before pointing Nezha at Jira.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .base import Issue, Tracker, TrackerError


class FileTracker(Tracker):
    kind = "file"

    def __init__(self, config: Dict[str, Any]):
        super(FileTracker, self).__init__(config)
        provider = config.get("provider") or {}
        path = provider.get("path")
        if not path:
            raise TrackerError("tracker.provider.path is required for kind=file")
        self.path = os.path.abspath(os.path.expanduser(path))

    def _read(self) -> Dict[str, Any]:
        if not os.path.isfile(self.path):
            raise TrackerError("board file not found: %s" % self.path)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except ValueError as exc:
            raise TrackerError("board file %s is not valid JSON: %s" % (self.path, exc))

    def fetch_all(self) -> List[Issue]:
        payload = self._read()
        rows = payload.get("issues")
        if not isinstance(rows, list):
            raise TrackerError("board file %s must contain an 'issues' array" % self.path)
        issues: List[Issue] = []
        for row in rows:
            if not isinstance(row, dict):
                raise TrackerError("board file %s: each issue must be an object" % self.path)
            missing = [k for k in ("id", "title", "state") if k not in row]
            if missing:
                raise TrackerError(
                    "board file %s: issue missing required keys %s" % (self.path, missing)
                )
            issues.append(Issue(
                id=row["id"],
                identifier=row.get("identifier", row["id"]),
                title=row["title"],
                state=row["state"],
                description=row.get("description", ""),
                labels=row.get("labels") or [],
                url=row.get("url", "file://%s" % self.path),
                raw=row,
            ))
        return issues

    def set_state(self, issue_id: str, state: str) -> None:
        """Test helper for the ``nezha board`` command. Not used by the orchestrator."""
        payload = self._read()
        for row in payload.get("issues", []):
            if str(row.get("id")) == str(issue_id):
                row["state"] = state
                break
        else:
            raise TrackerError("issue %s not found in %s" % (issue_id, self.path))
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, self.path)
