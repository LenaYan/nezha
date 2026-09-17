import json
import os
import time

import pytest

from nezha.log import StructuredLogger
from nezha.orchestrator import (CANCELLING, EXHAUSTED, IDLE, RETRY_WAIT,
                                SUCCEEDED, Orchestrator)
from nezha.tracker import build_tracker
from nezha.workflow import Workflow

WORKFLOW_TMPL = """---
tracker:
  kind: file
  provider:
    path: {board}
  active_states: [Todo, In Progress]
  terminal_states: [Done]
polling:
  interval_ms: 200
workspace:
  repo: {repo}
  root: {root}
  state_root: {state}
  base_ref: HEAD
agent:
  max_concurrent_agents: {concurrency}
  max_attempts: {attempts}
  retry_backoff_ms: {backoff}
  timeout_sec: 30
copilot:
  binary: {binary}
  allow_all_tools: false
sandbox:
  backend: none
---
Work on {{{{ issue.identifier }}}}{{% if attempt %}} (attempt {{{{ attempt }}}}){{% endif %}}.
"""

OK_STUB = """#!/bin/bash
# record the prompt we were handed so tests can assert on templating
printf '%s' "$2" > "$NEZHA_PROMPT_SINK/$NEZHA_ISSUE_IDENTIFIER.prompt"
echo '{"type":"assistant.turn_end","data":{"turnId":"0"}}'
echo '{"type":"result","exitCode":0,"sessionId":"sess-'"$NEZHA_ISSUE_IDENTIFIER"'","usage":{}}'
"""

FAIL_STUB = """#!/bin/bash
echo '{"type":"result","exitCode":1,"sessionId":"sess-fail","usage":{}}'
exit 1
"""

SLOW_STUB = """#!/bin/bash
sleep 30
echo '{"type":"result","exitCode":0,"sessionId":"sess-slow","usage":{}}'
"""


def write_stub(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def make(tmp_path, git_repo, board, binary, concurrency=2, attempts=2, backoff=50):
    sink = tmp_path / "prompts"
    sink.mkdir(exist_ok=True)
    os.environ["NEZHA_PROMPT_SINK"] = str(sink)
    text = WORKFLOW_TMPL.format(
        board=board, repo=git_repo, root=tmp_path / "ws", state=tmp_path / "state",
        binary=binary, concurrency=concurrency, attempts=attempts, backoff=backoff)
    wf = Workflow.parse(text, "test")
    logger = StructuredLogger(level="error")
    orch = Orchestrator(wf, run_root=str(tmp_path / "runs"), logger=logger)
    return orch, sink


def test_once_dispatches_eligible_only(tmp_path, git_repo, board):
    orch, sink = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    status = orch.run_once()
    orch.shutdown()
    ids = sorted(r["identifier"] for r in status["runs"])
    assert ids == ["T-1", "T-4"]          # T-2 Done, T-3 Backlog are skipped
    assert all(r["status"] == SUCCEEDED for r in status["runs"])
    assert all(r["last"]["exit_code"] == 0 for r in status["runs"])


def test_worktrees_are_created_per_issue(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    orch.run_once()
    orch.shutdown()
    for name in ("T-1", "T-4"):
        path = tmp_path / "ws" / name
        assert (path / "README.md").exists()
    assert not (tmp_path / "ws" / "T-3").exists()


def test_prompt_is_rendered_per_issue(tmp_path, git_repo, board):
    orch, sink = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    orch.run_once()
    orch.shutdown()
    assert (sink / "T-1.prompt").read_text().strip() == "Work on T-1."


def test_session_id_is_captured_for_resume(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    orch.run_once()
    orch.shutdown()
    assert orch.runs["T-1"].session_id == "sess-T-1"
    state = json.loads((tmp_path / "state" / "T-1.json").read_text())
    assert state["session_id"] == "sess-T-1"
    assert state["last_run"]["exit_code"] == 0


def test_failure_schedules_retry_then_exhausts(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board,
                   write_stub(tmp_path, "fail", FAIL_STUB), attempts=2)
    orch.run_once()
    assert orch.runs["T-1"].status == RETRY_WAIT
    assert orch.runs["T-1"].attempts == 1
    time.sleep(0.2)
    orch.tick()
    while orch._active_count():
        time.sleep(0.05)
    orch.shutdown()
    assert orch.runs["T-1"].status == EXHAUSTED
    assert orch.runs["T-1"].attempts == 2


def test_exhausted_issue_is_not_redispatched(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board,
                   write_stub(tmp_path, "fail", FAIL_STUB), attempts=1)
    orch.run_once()
    assert orch.runs["T-1"].status == EXHAUSTED
    orch.tick()
    while orch._active_count():
        time.sleep(0.05)
    orch.shutdown()
    assert orch.runs["T-1"].attempts == 1


def test_retry_backoff_is_respected(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board,
                   write_stub(tmp_path, "fail", FAIL_STUB), attempts=3, backoff=30000)
    orch.run_once()
    state = orch.runs["T-1"]
    assert state.next_attempt_at > time.time() + 10
    orch.tick()                      # immediate re-tick must be a no-op
    orch.shutdown()
    assert state.attempts == 1


def test_concurrency_limit_is_enforced(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board,
                   write_stub(tmp_path, "slow", SLOW_STUB), concurrency=1)
    orch.preflight()
    orch.tick()
    time.sleep(0.5)
    assert orch._active_count() == 1
    orch.tick()
    assert orch._active_count() == 1
    orch.shutdown()


def test_reconcile_cancels_run_when_issue_leaves_active(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board,
                   write_stub(tmp_path, "slow", SLOW_STUB), concurrency=2)
    orch.preflight()
    orch.tick()
    time.sleep(0.5)
    assert orch._active_count() == 2
    tracker = build_tracker(orch.wf.tracker)
    tracker.set_state("T-1", "Done")
    orch.tick()                      # reconcile sees the state change
    state = orch.runs["T-1"]         # cancelled runs survive the tick they die in
    assert state.cancelled
    deadline = time.time() + 15
    while state.status == CANCELLING and time.time() < deadline:
        time.sleep(0.1)
    assert state.status == IDLE      # never SUCCEEDED: the run was killed
    orch.shutdown()


def test_cancelled_terminal_run_is_released_on_a_later_tick(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board,
                   write_stub(tmp_path, "slow", SLOW_STUB), concurrency=2)
    orch.preflight()
    orch.tick()
    time.sleep(0.5)
    build_tracker(orch.wf.tracker).set_state("T-1", "Done")
    orch.tick()
    deadline = time.time() + 15
    while orch.runs["T-1"].status == CANCELLING and time.time() < deadline:
        time.sleep(0.1)
    orch.tick()
    assert "T-1" not in orch.runs
    orch.shutdown()


def test_startup_cleanup_removes_terminal_workspace(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    orch.preflight()
    ws = orch.workspaces.ensure("T-2", "T-2")   # T-2 is Done
    assert os.path.isdir(ws.path)
    orch.tick()
    while orch._active_count():
        time.sleep(0.05)
    orch.shutdown()
    assert not os.path.isdir(ws.path)


def test_tick_survives_tracker_errors(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    orch.preflight()
    board.unlink()
    with pytest.raises(Exception):
        orch.tick()
    orch._stop.set()
    orch.run_forever()               # must not raise despite the broken tracker
    orch.shutdown()


def test_status_snapshot_shape(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    status = orch.run_once()
    orch.shutdown()
    assert status["tracker"] == "file"
    assert status["sandbox"].startswith("none")
    assert status["max_concurrent"] == 2
    row = status["runs"][0]
    assert {"identifier", "status", "attempts", "session_id", "last"} <= set(row)


def test_run_dir_holds_event_log(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    orch.run_once()
    orch.shutdown()
    runs = tmp_path / "runs" / "T-1"
    attempts = list(runs.iterdir())
    assert attempts
    assert (attempts[0] / "events.jsonl").exists()
    assert (attempts[0] / "command.json").exists()


# -- per-issue sandbox home cleanup ---------------------------------------

class _HomeSpy(object):
    """Stands in for copilot-native, whose COPILOT_HOME lives outside the worktree."""

    name = "spy"

    def __init__(self, root):
        self.root = root
        self.removed = []

    def home_for(self, issue_id):
        return os.path.join(self.root, issue_id)

    def cleanup_home(self, issue_id):
        self.removed.append(issue_id)

    def preflight(self):
        pass

    def describe(self):
        return "spy"

    def wrap(self, argv, workdir, env, dry_run=False):
        return list(argv), dict(env), None


def test_terminal_issue_drops_its_sandbox_home_at_startup(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    spy = _HomeSpy(str(tmp_path / "homes"))
    orch.sandbox = orch.runner.sandbox = spy
    # T-2 is already Done, and a previous run left its home behind.
    os.makedirs(spy.home_for("T-2"))
    orch.run_once()
    orch.shutdown()
    assert "T-2" in spy.removed


def test_home_is_dropped_when_an_issue_reaches_terminal_mid_run(tmp_path, git_repo, board):
    """The daemon path: _startup_cleanup only fires once, _release_terminal is
    what a ticket finished while the daemon was up actually goes through."""
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    spy = _HomeSpy(str(tmp_path / "homes"))
    orch.sandbox = orch.runner.sandbox = spy
    orch.run_once()
    assert "T-1" not in spy.removed        # still Todo, home must survive for --resume
    orch.tracker.set_state("T-1", "Done")
    orch.tick()
    orch.shutdown()
    assert "T-1" in spy.removed


def test_cleanup_is_skipped_for_backends_without_a_home(tmp_path, git_repo, board):
    orch, _ = make(tmp_path, git_repo, board, write_stub(tmp_path, "ok", OK_STUB))
    assert orch.sandbox.name == "none"
    orch.run_once()          # must not raise on a backend with no cleanup_home
    orch.shutdown()
