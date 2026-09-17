"""Orchestrator: poll, dispatch with bounded concurrency, retry, reconcile.

Single authoritative in-memory state (``self.runs``) guarded by one lock.
Durable-enough recovery comes from the tracker plus per-workspace state files,
so there is no database (SPEC 2.1).
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from .log import StructuredLogger
from .runner import CopilotRunner, RunResult
from .sandbox import build_sandbox
from .tracker import Issue, Tracker, build_tracker
from .workflow import Workflow
from .workspace import WorkspaceManager

IDLE = "idle"
RUNNING = "running"
CANCELLING = "cancelling"
RETRY_WAIT = "retry_wait"
EXHAUSTED = "exhausted"
SUCCEEDED = "succeeded"

# A run occupies a concurrency slot until its child process has actually exited.
ACTIVE_STATUSES = (RUNNING, CANCELLING)


class RunState(object):
    __slots__ = ("issue_id", "identifier", "status", "attempts", "next_attempt_at",
                 "session_id", "last_result", "proc", "cancelled", "started_at")

    def __init__(self, issue_id: str, identifier: str):
        self.issue_id = issue_id
        self.identifier = identifier
        self.status = IDLE
        self.attempts = 0
        self.next_attempt_at = 0.0
        self.session_id = None      # type: Optional[str]
        self.last_result = None     # type: Optional[RunResult]
        self.proc = None
        self.cancelled = False
        self.started_at = 0.0

    def snapshot(self) -> Dict[str, Any]:
        row = {
            "identifier": self.identifier,
            "status": self.status,
            "attempts": self.attempts,
            "session_id": self.session_id,
        }
        if self.status == RETRY_WAIT:
            row["retry_in_sec"] = max(0, round(self.next_attempt_at - time.time()))
        if self.status in (RUNNING, CANCELLING) and self.started_at:
            row["running_for_sec"] = round(time.time() - self.started_at)
        if self.last_result is not None:
            row["last"] = self.last_result.summary()
        return row


class Orchestrator(object):
    def __init__(self, workflow: Workflow, run_root: str = "~/.local/state/nezha/runs",
                 logger: Optional[StructuredLogger] = None,
                 tracker: Optional[Tracker] = None):
        self.wf = workflow
        self.log = logger or StructuredLogger()
        self.tracker = tracker or build_tracker(workflow.tracker)
        self.sandbox = build_sandbox(workflow.sandbox)
        self.workspaces = WorkspaceManager(workflow.workspace, logger=self.log)
        self.workspaces.hooks = workflow.hooks
        self.runner = CopilotRunner(
            workflow.copilot, self.sandbox,
            timeout_sec=float(workflow.agent.get("timeout_sec", 3600)), logger=self.log)
        self.run_root = os.path.abspath(os.path.expanduser(run_root))
        self.max_concurrent = int(workflow.agent["max_concurrent_agents"])
        self.max_attempts = int(workflow.agent["max_attempts"])
        self.runs: Dict[str, RunState] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=self.max_concurrent)
        self._cleaned_up = False

    # -- lifecycle --------------------------------------------------------
    def preflight(self) -> None:
        self.sandbox.preflight()
        self.workspaces.verify_repo()
        self.log.info("preflight.ok",
                      tracker=self.tracker.kind,
                      sandbox=self.sandbox.describe(),
                      repo=self.workspaces.repo,
                      workspace_root=self.workspaces.root,
                      max_concurrent=self.max_concurrent)

    def run_forever(self) -> None:
        self.preflight()
        interval = self.wf.poll_interval_sec
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the daemon
                    self.log.error("tick.failed", error="%s: %s" % (type(exc).__name__, exc))
                self._stop.wait(interval)
        finally:
            self.shutdown()

    def run_once(self, wait: bool = True) -> Dict[str, Any]:
        self.preflight()
        self.tick()
        if wait:
            while self._active_count() > 0:
                time.sleep(1.0)
        return self.status()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        self.log.info("shutdown.begin", active=self._active_count())
        with self._lock:
            states = list(self.runs.values())
        for state in states:
            self._cancel(state, reason="shutdown")
        self._pool.shutdown(wait=True)
        self.log.info("shutdown.done")

    # -- tick -------------------------------------------------------------
    def tick(self) -> None:
        issues = self.tracker.fetch_all()
        by_id = {i.id: i for i in issues}
        eligible = [i for i in issues if self.tracker._eligible(i)]
        terminal_ids = {i.id for i in issues if i.state in self.tracker.terminal_states}

        if not self._cleaned_up:
            self._startup_cleanup(issues)
            self._cleaned_up = True

        # Order matters: release first, then reconcile. A run cancelled during
        # this tick therefore always survives until the next tick, which keeps
        # the observable state machine deterministic regardless of how fast the
        # child process dies.
        self._release_terminal(terminal_ids, by_id)
        self._reconcile(by_id)

        now = time.time()
        for issue in eligible:
            if self._active_count() >= self.max_concurrent:
                self.log.debug("dispatch.saturated", limit=self.max_concurrent)
                break
            with self._lock:
                state = self.runs.get(issue.id)
                if state is None:
                    state = RunState(issue.id, issue.identifier)
                    self.runs[issue.id] = state
                if state.status in (RUNNING, CANCELLING, EXHAUSTED, SUCCEEDED):
                    continue
                if state.status == RETRY_WAIT and now < state.next_attempt_at:
                    continue
                state.status = RUNNING
                state.cancelled = False
                state.attempts += 1
                state.started_at = now
                attempt = state.attempts
            self.log.info("dispatch", identifier=issue.identifier,
                          state=issue.state, attempt=attempt)
            self._pool.submit(self._execute, issue, state)

    def _startup_cleanup(self, issues: List[Issue]) -> None:
        """Drop the worktree *and* the sandbox home of already-terminal issues."""
        for issue in issues:
            if issue.state not in self.tracker.terminal_states:
                continue
            ws = self.workspaces.describe(issue.id, issue.identifier)
            home = self._sandbox_home(issue.id)
            if not os.path.isdir(ws.path) and not (home and os.path.isdir(home)):
                continue
            self.log.info("cleanup.terminal", identifier=issue.identifier,
                          state=issue.state)
            try:
                if os.path.isdir(ws.path):
                    self.workspaces.remove(ws)
                self._cleanup_sandbox_home(issue.id)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("cleanup.failed", identifier=issue.identifier,
                                 error=str(exc))

    def _sandbox_home(self, issue_id: str) -> Optional[str]:
        getter = getattr(self.sandbox, "home_for", None)
        return getter(issue_id) if getter is not None else None

    def _cleanup_sandbox_home(self, issue_id: str) -> None:
        """Remove the per-issue COPILOT_HOME.

        It lives under the state root rather than in the worktree -- that is what
        keeps the agent from editing its own sandbox policy -- so removing the
        worktree does not remove it. Left behind, every ticket Nezha ever ran
        leaks a directory holding a symlink to the real ``data.db``.
        """
        cleanup = getattr(self.sandbox, "cleanup_home", None)
        if cleanup is None:
            return
        cleanup(issue_id)

    def _reconcile(self, by_id: Dict[str, Issue]) -> None:
        """Stop active runs whose issue left the active state set."""
        with self._lock:
            running = [s for s in self.runs.values() if s.status == RUNNING]
        for state in running:
            issue = by_id.get(state.issue_id)
            if issue is None:
                self._cancel(state, reason="issue_disappeared")
            elif issue.state not in self.tracker.active_states:
                self._cancel(state, reason="state_changed:%s" % issue.state)

    def _release_terminal(self, terminal_ids, by_id: Dict[str, Issue]) -> None:
        """Drop settled runs for terminal issues. A cancelling run keeps its slot
        until its child process exits, so it is released on a later tick."""
        with self._lock:
            done = [s for s in self.runs.values()
                    if s.issue_id in terminal_ids and s.status not in ACTIVE_STATUSES]
        for state in done:
            issue = by_id.get(state.issue_id)
            self.log.info("release", identifier=state.identifier,
                          state=issue.state if issue else "?")
            with self._lock:
                self.runs.pop(state.issue_id, None)
            # _startup_cleanup only runs on the first tick, so for a daemon this
            # is the path a ticket actually takes to terminal. Without it the
            # sandbox home leaks for every ticket finished while the daemon ran.
            try:
                self._cleanup_sandbox_home(state.issue_id)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("cleanup.failed", identifier=state.identifier,
                                 error=str(exc))

    def _cancel(self, state: RunState, reason: str) -> None:
        with self._lock:
            proc = state.proc
            if state.status != RUNNING or state.cancelled:
                return
            state.cancelled = True
            state.status = CANCELLING
        self.log.warning("cancel", identifier=state.identifier, reason=reason)
        if proc is not None:
            CopilotRunner._terminate(proc)

    def _active_count(self) -> int:
        with self._lock:
            return sum(1 for s in self.runs.values() if s.status in ACTIVE_STATUSES)

    # -- execution --------------------------------------------------------
    def _execute(self, issue: Issue, state: RunState) -> None:
        identifier = issue.identifier
        try:
            ws = self.workspaces.ensure(issue.id, identifier)
            ws.save_state(attempts=state.attempts, issue_state=issue.state)
            context = {
                "issue": issue.as_context(),
                "attempt": state.attempts if state.attempts > 1 else None,
                "workspace": {"path": ws.path, "branch": ws.branch},
                "base_ref": self.workspaces.base_ref,
            }
            prompt = self.wf.render_prompt(context)
            run_dir = os.path.join(self.run_root, ws.identifier,
                                   "attempt-%d-%d" % (state.attempts, int(time.time())))
            env_extra = {
                "NEZHA_ISSUE_ID": issue.id,
                "NEZHA_ISSUE_IDENTIFIER": identifier,
                "NEZHA_BRANCH": ws.branch,
                "NEZHA_ATTEMPT": str(state.attempts),
            }
            result = self.runner.run(
                prompt, ws.path, run_dir, env_extra=env_extra,
                resume_session=state.session_id,
                on_start=lambda p: self._bind_proc(state, p))
        except Exception as exc:  # noqa: BLE001
            result = RunResult()
            result.exit_code = -1
            result.error = "%s: %s" % (type(exc).__name__, exc)
            self.log.error("run.error", identifier=identifier, error=result.error)

        self._finish(issue, state, result)

    def _bind_proc(self, state: RunState, proc) -> None:
        with self._lock:
            state.proc = proc
            if state.cancelled:
                CopilotRunner._terminate(proc)

    def _finish(self, issue: Issue, state: RunState, result: RunResult) -> None:
        with self._lock:
            state.proc = None
            state.last_result = result
            if result.session_id:
                state.session_id = result.session_id
            cancelled = state.cancelled
            attempts = state.attempts
            if cancelled:
                state.status = IDLE
            elif result.ok:
                state.status = SUCCEEDED
            elif attempts >= self.max_attempts:
                state.status = EXHAUSTED
            else:
                backoff = self.wf.retry_backoff_sec * (2 ** (attempts - 1))
                backoff = min(backoff, 3600.0)
                state.next_attempt_at = time.time() + backoff
                state.status = RETRY_WAIT

        try:
            ws = self.workspaces.describe(issue.id, issue.identifier)
            ws.save_state(last_run=result.summary(), session_id=state.session_id,
                          status=state.status)
        except Exception:  # noqa: BLE001 - state persistence is best-effort
            pass

        fields = dict(result.summary())
        fields["identifier"] = issue.identifier
        fields["status"] = state.status
        if result.error:
            fields["error"] = result.error[:500]
        if cancelled:
            self.log.warning("run.cancelled", **fields)
        elif result.ok:
            self.log.info("run.succeeded", **fields)
        elif state.status == EXHAUSTED:
            self.log.error("run.exhausted", **fields)
        else:
            self.log.warning("run.retry_scheduled",
                             retry_in_sec=round(state.next_attempt_at - time.time()), **fields)

    # -- observability ----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            rows = [s.snapshot() for s in self.runs.values()]
        return {
            "tracker": self.tracker.kind,
            "sandbox": self.sandbox.describe(),
            "max_concurrent": self.max_concurrent,
            "active": sum(1 for r in rows if r["status"] == RUNNING),
            "runs": sorted(rows, key=lambda r: r["identifier"]),
        }
