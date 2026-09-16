import json
import os

import pytest

from nezha.cli import build_parser, main

WORKFLOW = """---
tracker:
  kind: file
  provider:
    path: {board}
  active_states: [Todo]
  terminal_states: [Done]
workspace:
  repo: {repo}
  root: {root}
  state_root: {state}
  base_ref: HEAD
copilot:
  binary: {binary}
sandbox:
  backend: none
---
Work on {{{{ issue.identifier }}}}.
"""


@pytest.fixture
def wf_file(tmp_path, git_repo, board):
    stub = tmp_path / "stub"
    stub.write_text("#!/bin/bash\necho '{\"type\":\"result\",\"exitCode\":0}'\n",
                    encoding="utf-8")
    stub.chmod(0o755)
    path = tmp_path / "WORKFLOW.md"
    path.write_text(WORKFLOW.format(
        board=board, repo=git_repo, root=tmp_path / "ws",
        state=tmp_path / "state", binary=stub), encoding="utf-8")
    return path


# -- argument parsing ------------------------------------------------------

def test_global_flags_accepted_before_subcommand():
    args = build_parser().parse_args(["--json-logs", "--log-level", "debug", "once"])
    assert args.command == "once" and args.json_logs and args.log_level == "debug"


def test_global_flags_accepted_after_subcommand():
    """Regression: `nezha once --json-logs` must not be rejected by argparse."""
    args = build_parser().parse_args(["once", "--json-logs", "--log-level", "debug"])
    assert args.command == "once" and args.json_logs and args.log_level == "debug"


def test_subcommand_does_not_clobber_earlier_global_value():
    args = build_parser().parse_args(["--log-level", "error", "issues"])
    assert args.log_level == "error"


def test_workflow_flag_after_subcommand():
    args = build_parser().parse_args(["doctor", "-w", "/tmp/x/WORKFLOW.md"])
    assert args.workflow == "/tmp/x/WORKFLOW.md"


def test_no_command_prints_help():
    assert main([]) == 2


# -- commands --------------------------------------------------------------

def test_doctor_reports_none_sandbox_as_a_problem(wf_file, capsys):
    code = main(["doctor", "-w", str(wf_file)])
    out = capsys.readouterr().out
    assert "tracker          ok" in out
    assert "PROBLEM" in out and "full user" in out
    assert code == 1


def test_doctor_fails_on_missing_workflow(capsys):
    assert main(["doctor", "-w", "/nonexistent/WORKFLOW.md"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_issues_marks_eligible(wf_file, capsys):
    assert main(["issues", "-w", str(wf_file)]) == 0
    out = capsys.readouterr().out
    assert "* T-1" in out
    assert "  T-2" in out            # Done -> not eligible
    assert "eligible for dispatch" in out


def test_issues_json(wf_file, capsys):
    assert main(["issues", "-w", str(wf_file), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["identifier"] for r in rows} == {"T-1", "T-2", "T-3", "T-4"}


def test_prompt_renders(wf_file, capsys):
    assert main(["prompt", "T-1", "-w", str(wf_file)]) == 0
    assert capsys.readouterr().out.strip() == "Work on T-1."


def test_prompt_unknown_issue(wf_file, capsys):
    assert main(["prompt", "NOPE", "-w", str(wf_file)]) == 1
    assert "not found" in capsys.readouterr().err


def test_plan_prints_argv_without_running(wf_file, capsys, tmp_path):
    assert main(["plan", "T-1", "-w", str(wf_file)]) == 0
    out = capsys.readouterr().out
    assert "--- argv ---" in out
    assert "--output-format" in out
    assert not (tmp_path / "ws" / "T-1").exists()


def test_board_flips_state(wf_file, board, capsys):
    assert main(["board", "T-1", "Done", "-w", str(wf_file)]) == 0
    assert json.loads(board.read_text())["issues"][0]["state"] == "Done"


def test_workspaces_empty(wf_file, capsys):
    assert main(["workspaces", "-w", str(wf_file)]) == 0
    assert "(no workspaces)" in capsys.readouterr().out


def test_once_then_workspaces_and_rm(wf_file, tmp_path, capsys):
    assert main(["once", "-w", str(wf_file)]) == 0
    capsys.readouterr()
    assert main(["workspaces", "-w", str(wf_file)]) == 0
    listing = capsys.readouterr().out
    assert "T-1" in listing and "T-4" in listing
    for name in ("T-1", "T-4"):
        assert main(["rm", name, "-w", str(wf_file)]) == 0
    capsys.readouterr()
    main(["workspaces", "-w", str(wf_file)])
    assert "(no workspaces)" in capsys.readouterr().out


def test_config_error_exits_two(capsys, tmp_path):
    bad = tmp_path / "WORKFLOW.md"
    bad.write_text("---\ntracker: []\n---\nbody", encoding="utf-8")
    assert main(["issues", "-w", str(bad)]) == 2
    assert "config error" in capsys.readouterr().err


def test_runtime_error_exits_one(capsys, tmp_path):
    bad = tmp_path / "WORKFLOW.md"
    bad.write_text("---\ntracker:\n  kind: file\n  active_states: [Todo]\n"
                   "  terminal_states: [Done]\n---\nbody", encoding="utf-8")
    assert main(["issues", "-w", str(bad)]) == 1
    assert "provider.path is required" in capsys.readouterr().err
