"""Nezha CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .log import StructuredLogger
from .orchestrator import Orchestrator
from .sandbox import build_sandbox
from .tracker import build_tracker
from .workflow import Workflow, WorkflowError
from .workspace import WorkspaceManager

DEFAULT_WORKFLOW = "WORKFLOW.md"


def _load(args) -> Workflow:
    return Workflow.load(args.workflow)


def _logger(args) -> StructuredLogger:
    return StructuredLogger(level=args.log_level, human=not args.json_logs,
                            file_path=args.log_file)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    problems: List[str] = []
    notes: List[str] = []

    try:
        wf = _load(args)
        notes.append("WORKFLOW.md      ok (%s)" % wf.source)
    except WorkflowError as exc:
        print("WORKFLOW.md      FAIL: %s" % exc)
        return 1

    binary = wf.copilot.get("binary") or "copilot"
    path = shutil.which(binary)
    if path:
        notes.append("copilot          ok (%s)" % path)
    else:
        problems.append("copilot binary %r not found on PATH" % binary)

    # The policy is inspected before preflight on purpose: a policy that
    # contradicts itself is a config bug, and an operator on a host that cannot
    # sandbox still needs to hear about it.
    sandbox = None
    try:
        sandbox = build_sandbox(wf.sandbox)
    except Exception as exc:  # noqa: BLE001
        problems.append("sandbox: %s" % exc)

    if sandbox is not None:
        if sandbox.name == "none":
            problems.append(
                "sandbox.backend is 'none': the agent runs with your full user "
                "privileges. Acceptable only if you review every diff.")
        if sandbox.name == "copilot-native":
            if sandbox.skip_host_prereq_check:
                notes.append(
                    "sandbox.host     prerequisite check SKIPPED by config. If the "
                    "host cannot sandbox, every command fails silently.")
            if sandbox.allow_bypass:
                problems.append(
                    "sandbox.allow_bypass is true: a sandboxed command can opt out "
                    "of the sandbox, and an unattended run has nobody to refuse it.")
            if sandbox.allow_network:
                notes.append(
                    "sandbox.egress   open (allow_network: true). This backend can "
                    "enforce allow_network: false without breaking Copilot.")
            conflicts = sandbox.dev_tool_conflicts()
            if conflicts:
                problems.append(
                    "sandbox.deny_read and sandbox.allow_dev_tool_access disagree "
                    "about %s. allowDevToolAccess re-grants dev-tool config and "
                    "caches -- including the registry tokens they hold -- and which "
                    "side wins is not documented. Set allow_dev_tool_access: false "
                    "and grant build paths explicitly via readonly_paths, or drop "
                    "those entries from deny_read so the policy states one thing."
                    % ", ".join(sorted(set(conflicts))))
        try:
            sandbox.preflight()
            notes.append("sandbox          ok (%s)" % sandbox.describe())
        except Exception as exc:  # noqa: BLE001
            problems.append("sandbox: %s" % exc)

    if not wf.copilot.get("no_auto_update", True):
        problems.append(
            "copilot.no_auto_update is false: the CLI may download and run a newer "
            "build than the one checked above, and sandbox support plus the "
            "COPILOT_HOME layout are both version-dependent.")
    if wf.copilot.get("max_ai_credits") is None:
        notes.append(
            "copilot.budget   unbounded (max_ai_credits unset). %d attempts x %d "
            "concurrent agents have no cost ceiling."
            % (int(wf.agent["max_attempts"]), int(wf.agent["max_concurrent_agents"])))
    if not wf.copilot.get("no_custom_instructions"):
        notes.append(
            "copilot.instr    AGENTS.md and .github/instructions/** are read from "
            "inside the worktree, so the agent can edit what its own retry obeys. "
            "Set copilot.no_custom_instructions: true if that matters more than "
            "the repo's own guidance.")

    wm = WorkspaceManager(wf.workspace)
    try:
        wm.verify_repo()
        notes.append("repo             ok (%s)" % wm.repo)
    except Exception as exc:  # noqa: BLE001
        problems.append("repo: %s" % exc)

    try:
        tracker = build_tracker(wf.tracker)
        issues = tracker.fetch_all()
        active = tracker.fetch_active()
        notes.append("tracker          ok (%s: %d issues, %d eligible)"
                     % (tracker.kind, len(issues), len(active)))
    except Exception as exc:  # noqa: BLE001
        problems.append("tracker: %s" % exc)

    for line in notes:
        print(line)
    for line in problems:
        print("PROBLEM          %s" % line)
    return 1 if problems else 0


def cmd_issues(args) -> int:
    wf = _load(args)
    tracker = build_tracker(wf.tracker)
    issues = tracker.fetch_all()
    eligible = {i.id for i in tracker.fetch_active()}
    if args.json:
        print(json.dumps([i.as_context() for i in issues], indent=2, ensure_ascii=False))
        return 0
    if not issues:
        print("(no issues)")
        return 0
    width = max(len(i.identifier) for i in issues)
    for issue in issues:
        mark = "*" if issue.id in eligible else " "
        print("%s %-*s  %-14s %s" % (mark, width, issue.identifier, issue.state, issue.title))
    print("\n* = eligible for dispatch")
    return 0


def cmd_prompt(args) -> int:
    """Render the prompt for one issue without running anything."""
    wf = _load(args)
    tracker = build_tracker(wf.tracker)
    matches = [i for i in tracker.fetch_all()
               if args.issue in (i.id, i.identifier)]
    if not matches:
        print("issue %r not found" % args.issue, file=sys.stderr)
        return 1
    issue = matches[0]
    wm = WorkspaceManager(wf.workspace)
    ws = wm.describe(issue.id, issue.identifier)
    print(wf.render_prompt({
        "issue": issue.as_context(),
        "attempt": args.attempt if args.attempt > 1 else None,
        "workspace": {"path": ws.path, "branch": ws.branch},
        "base_ref": wm.base_ref,
    }))
    return 0


def cmd_plan(args) -> int:
    """Show the exact command line Nezha would execute (no side effects)."""
    wf = _load(args)
    sandbox = build_sandbox(wf.sandbox)
    from .runner import CopilotRunner
    runner = CopilotRunner(wf.copilot, sandbox)
    argv = runner.build_argv("<PROMPT>", resume_session=args.resume)
    wm = WorkspaceManager(wf.workspace)
    workdir = wm.path_for(args.issue or "EXAMPLE-1")
    wrapped, run_env, _ = sandbox.wrap(argv, workdir, dict(os.environ), dry_run=True)
    print("workdir: %s" % workdir)
    print("sandbox: %s" % sandbox.describe())
    if hasattr(sandbox, "policy"):
        print("\n--- effective sandbox policy (settings.json) ---")
        print(json.dumps({"sandbox": sandbox.policy()}, indent=2, sort_keys=True))
        print("\nCOPILOT_HOME: %s" % run_env.get("COPILOT_HOME"))
    print("--- argv ---")
    for part in wrapped:
        print("  %s" % part)
    return 0


def cmd_workspaces(args) -> int:
    wf = _load(args)
    wm = WorkspaceManager(wf.workspace)
    rows = wm.list_states()
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return 0
    if not rows:
        print("(no workspaces)")
        return 0
    for row in rows:
        last = row.get("last_run") or {}
        print("%-16s %-10s attempts=%s exit=%s turns=%s  %s"
              % (row.get("identifier"), row.get("status", "?"), row.get("attempts"),
                 last.get("exit_code"), last.get("turns"), row.get("path")))
    return 0


def cmd_rm(args) -> int:
    wf = _load(args)
    wm = WorkspaceManager(wf.workspace, logger=_logger(args))
    wm.hooks = wf.hooks
    ws = wm.describe(args.issue, args.issue)
    wm.remove(ws)
    # The per-issue COPILOT_HOME lives beside run state, not in the worktree,
    # so removing the workspace would otherwise leave it behind.
    sandbox = build_sandbox(wf.sandbox)
    if hasattr(sandbox, "cleanup_home"):
        sandbox.cleanup_home(ws.issue_id)
    return 0


def cmd_board(args) -> int:
    """Flip an issue state in a file-backed board (testing helper)."""
    wf = _load(args)
    tracker = build_tracker(wf.tracker)
    if not hasattr(tracker, "set_state"):
        print("board editing is only supported for tracker.kind=file", file=sys.stderr)
        return 1
    tracker.set_state(args.issue, args.state)
    print("%s -> %s" % (args.issue, args.state))
    return 0


def cmd_once(args) -> int:
    wf = _load(args)
    orch = Orchestrator(wf, run_root=args.run_root, logger=_logger(args))
    status = orch.run_once(wait=True)
    orch.shutdown()
    print(json.dumps(status, indent=2, ensure_ascii=False))
    failed = [r for r in status["runs"]
              if r["status"] in ("exhausted",) or
              (r.get("last") or {}).get("exit_code") not in (0, None)]
    return 1 if failed else 0


def cmd_run(args) -> int:
    wf = _load(args)
    log = _logger(args)
    orch = Orchestrator(wf, run_root=args.run_root, logger=log)

    def handle(signum, frame):  # noqa: ARG001
        log.warning("signal.received", signal=signum)
        orch.stop()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    orch.run_forever()
    return 0


# --------------------------------------------------------------------------

def _common_parser(suppress: bool) -> argparse.ArgumentParser:
    """Options accepted both before and after the subcommand.

    Subparser copies default to ``SUPPRESS`` so that an unspecified option does
    not clobber a value already given at the top level.
    """
    def dflt(value):
        return argparse.SUPPRESS if suppress else value

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-w", "--workflow", default=dflt(DEFAULT_WORKFLOW),
                        help="path to WORKFLOW.md (default: ./WORKFLOW.md)")
    parser.add_argument("--log-level", default=dflt("info"),
                        choices=["debug", "info", "warning", "error"])
    parser.add_argument("--json-logs", action="store_true", default=dflt(False),
                        help="emit machine-readable JSON lines instead of the human view")
    parser.add_argument("--log-file", default=dflt(None),
                        help="append JSON logs to this file")
    parser.add_argument("--run-root", default=dflt("~/.local/state/nezha/runs"),
                        help="directory for per-attempt event logs")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nezha",
        parents=[_common_parser(False)],
        description="Orchestrate Copilot CLI agents over tracker issues "
                    "in isolated, sandboxed git worktrees.")
    parser.add_argument("--version", action="version", version="nezha %s" % __version__)

    common = [_common_parser(True)]
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", parents=common,
                   help="validate config, binaries, sandbox and tracker")

    p_issues = sub.add_parser("issues", parents=common,
                              help="list tracker issues and eligibility")
    p_issues.add_argument("--json", action="store_true")

    p_prompt = sub.add_parser("prompt", parents=common,
                              help="render the prompt for one issue")
    p_prompt.add_argument("issue")
    p_prompt.add_argument("--attempt", type=int, default=1)

    p_plan = sub.add_parser("plan", parents=common,
                            help="print the sandboxed command line, run nothing")
    p_plan.add_argument("issue", nargs="?")
    p_plan.add_argument("--resume", default=None)

    p_ws = sub.add_parser("workspaces", parents=common,
                          help="list known workspaces and their state")
    p_ws.add_argument("--json", action="store_true")

    p_rm = sub.add_parser("rm", parents=common, help="remove a workspace and its state")
    p_rm.add_argument("issue")

    p_board = sub.add_parser("board", parents=common,
                             help="set an issue state (file tracker only)")
    p_board.add_argument("issue")
    p_board.add_argument("state")

    sub.add_parser("once", parents=common,
                   help="run a single poll tick and wait for completion")
    sub.add_parser("run", parents=common,
                   help="run the polling daemon until interrupted")

    return parser


_COMMANDS = {
    "doctor": cmd_doctor, "issues": cmd_issues, "prompt": cmd_prompt, "plan": cmd_plan,
    "workspaces": cmd_workspaces, "rm": cmd_rm, "board": cmd_board,
    "once": cmd_once, "run": cmd_run,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        return _COMMANDS[args.command](args)
    except WorkflowError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
