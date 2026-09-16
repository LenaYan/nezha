"""Workspace manager: deterministic issue -> git worktree mapping.

A worktree gives *collision* isolation (independent branch + working directory),
not *privilege* isolation. Privilege isolation is :mod:`nezha.sandbox`'s job.

Note: worktrees share the main repository's object store and config. A hostile
agent inside a worktree can still corrupt the parent repo. Use a dedicated
clone as ``workspace.repo`` for untrusted work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_SLUG_LEN = 80


class WorkspaceError(RuntimeError):
    pass


def slugify(identifier: str) -> str:
    """Map an identifier to a filesystem-safe, *injective* slug.

    Sanitizing alone is many-to-one (``PROJ/123`` and ``PROJ-123`` both become
    ``PROJ-123``), which would let two live issues share one worktree, branch
    and state file. When sanitizing is lossy -- or the identifier is truncated
    -- a short digest of the raw identifier is appended to keep the mapping
    unique. Identifiers that are already slug-safe keep their readable form.
    """
    raw = str(identifier)
    slug = SLUG_RE.sub("-", raw).strip("-.")
    if not slug:
        raise WorkspaceError("issue identifier %r produced an empty slug" % identifier)
    if slug == raw and len(slug) <= MAX_SLUG_LEN:
        return slug
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (slug[:MAX_SLUG_LEN - 9], digest)


def ensure_within(root: str, candidate: str) -> str:
    """Reject any path that escapes ``root`` (SPEC: Path Safety)."""
    root_real = os.path.realpath(root)
    cand_real = os.path.realpath(candidate)
    if cand_real != root_real and not cand_real.startswith(root_real + os.sep):
        raise WorkspaceError("path %s escapes workspace root %s" % (cand_real, root_real))
    return cand_real


def _run(args: List[str], cwd: Optional[str] = None, timeout: float = 300) -> str:
    proc = subprocess.run(args, cwd=cwd, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise WorkspaceError("command failed (%s): %s\n%s"
                             % (proc.returncode, " ".join(args), output.strip()))
    return output


class Workspace(object):
    """One issue's worktree plus its orchestrator-owned state file."""

    def __init__(self, issue_id: str, identifier: str, path: str, branch: str, state_path: str):
        self.issue_id = issue_id
        self.identifier = identifier
        self.path = path
        self.branch = branch
        self.state_path = state_path

    # -- state (kept OUTSIDE the worktree so the agent cannot forge it) ----
    def load_state(self) -> Dict[str, Any]:
        if not os.path.isfile(self.state_path):
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except ValueError:
            return {}

    def save_state(self, **updates: Any) -> Dict[str, Any]:
        state = self.load_state()
        state.update(updates)
        state["issue_id"] = self.issue_id
        state["identifier"] = self.identifier
        state["path"] = self.path
        state["branch"] = self.branch
        state["updated_at"] = time.time()
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, self.state_path)
        return state

    def __repr__(self) -> str:
        return "Workspace(%s -> %s)" % (self.identifier, self.path)


class WorkspaceManager(object):
    def __init__(self, config: Dict[str, Any], logger=None):
        self.root = os.path.abspath(os.path.expanduser(config.get("root")))
        repo = config.get("repo") or os.getcwd()
        self.repo = os.path.abspath(os.path.expanduser(repo))
        self.base_ref = config.get("base_ref") or "origin/main"
        self.branch_prefix = config.get("branch_prefix") or "nezha/"
        state_root = config.get("state_root") or "~/.local/state/nezha"
        self.state_root = os.path.abspath(os.path.expanduser(state_root))
        self.hooks: Dict[str, Any] = {}
        self.log = logger

    def _emit(self, level: str, message: str, **fields: Any) -> None:
        if self.log is not None:
            getattr(self.log, level)(message, **fields)

    def verify_repo(self) -> None:
        if not os.path.isdir(os.path.join(self.repo, ".git")) and not os.path.isfile(
                os.path.join(self.repo, ".git")):
            raise WorkspaceError("workspace.repo is not a git repository: %s" % self.repo)

    def path_for(self, identifier: str) -> str:
        return os.path.join(self.root, slugify(identifier))

    def branch_for(self, identifier: str) -> str:
        return self.branch_prefix + slugify(identifier)

    def state_path_for(self, identifier: str) -> str:
        return os.path.join(self.state_root, slugify(identifier) + ".json")

    def describe(self, issue_id: str, identifier: str) -> Workspace:
        return Workspace(issue_id, identifier, self.path_for(identifier),
                         self.branch_for(identifier), self.state_path_for(identifier))

    def _existing_worktrees(self) -> Dict[str, str]:
        out = _run(["git", "-C", self.repo, "worktree", "list", "--porcelain"])
        trees: Dict[str, str] = {}
        current = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                current = line[len("worktree "):].strip()
                trees[os.path.realpath(current)] = ""
            elif line.startswith("branch ") and current:
                trees[os.path.realpath(current)] = line[len("branch "):].strip()
        return trees

    def ensure(self, issue_id: str, identifier: str) -> Workspace:
        """Create the worktree if absent; reuse it (preserving state) otherwise."""
        self.verify_repo()
        os.makedirs(self.root, exist_ok=True)
        ws = self.describe(issue_id, identifier)
        ensure_within(self.root, ws.path)

        if os.path.isdir(ws.path):
            if os.path.realpath(ws.path) in self._existing_worktrees():
                # Defence in depth behind the injective slug: never hand a
                # worktree that belongs to another issue to this run.
                owner = ws.load_state().get("issue_id")
                if owner is not None and str(owner) != str(issue_id):
                    raise WorkspaceError(
                        "%s already belongs to issue %s; refusing to reuse it for %s"
                        % (ws.path, owner, issue_id))
                self._emit("info", "workspace.reuse", identifier=identifier, path=ws.path)
                return ws
            raise WorkspaceError(
                "%s exists but is not a registered worktree; remove it manually" % ws.path
            )

        args = ["git", "-C", self.repo, "worktree", "add"]
        if self._branch_exists(ws.branch):
            args += [ws.path, ws.branch]
        else:
            args += ["-b", ws.branch, ws.path, self.base_ref]
        _run(args)
        self._emit("info", "workspace.create", identifier=identifier,
                   path=ws.path, branch=ws.branch)
        ws.save_state(created_at=time.time(), attempts=0)
        self.run_hook("after_create", ws)
        return ws

    def _branch_exists(self, branch: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.returncode == 0

    def run_hook(self, name: str, ws: Workspace, timeout: float = 900) -> None:
        script = (self.hooks or {}).get(name)
        if not script:
            return
        self._emit("info", "workspace.hook.start", identifier=ws.identifier, hook=name)
        env = dict(os.environ)
        env.update({
            "NEZHA_WORKSPACE": ws.path,
            "NEZHA_BRANCH": ws.branch,
            "NEZHA_ISSUE_ID": ws.issue_id,
            "NEZHA_ISSUE_IDENTIFIER": ws.identifier,
        })
        proc = subprocess.run(["bash", "-lc", script], cwd=ws.path, env=env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = proc.stdout.decode("utf-8", "replace")
        if proc.returncode != 0:
            raise WorkspaceError("hook %s failed (%s):\n%s"
                                 % (name, proc.returncode, output.strip()[-2000:]))
        self._emit("info", "workspace.hook.done", identifier=ws.identifier, hook=name)

    def remove(self, ws: Workspace, force: bool = True) -> None:
        ensure_within(self.root, ws.path)
        if os.path.isdir(ws.path):
            try:
                self.run_hook("before_remove", ws)
            except WorkspaceError as exc:
                self._emit("warning", "workspace.hook.failed",
                           identifier=ws.identifier, error=str(exc))
            args = ["git", "-C", self.repo, "worktree", "remove", ws.path]
            if force:
                args.insert(-1, "--force")
            try:
                _run(args)
            except WorkspaceError:
                shutil.rmtree(ws.path, ignore_errors=True)
                _run(["git", "-C", self.repo, "worktree", "prune"])
        if os.path.isfile(ws.state_path):
            os.remove(ws.state_path)
        self._emit("info", "workspace.remove", identifier=ws.identifier, path=ws.path)

    def list_states(self) -> List[Dict[str, Any]]:
        if not os.path.isdir(self.state_root):
            return []
        rows: List[Dict[str, Any]] = []
        for name in sorted(os.listdir(self.state_root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.state_root, name), "r", encoding="utf-8") as handle:
                    rows.append(json.load(handle))
            except ValueError:
                continue
        return rows
