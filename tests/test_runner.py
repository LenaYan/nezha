import json
import time
import os
import subprocess

import pytest

from nezha.runner import CopilotRunner, RunResult, parse_events
from nezha.sandbox import NoSandbox

SAMPLE = [
    json.dumps({"type": "session.tools_updated", "ephemeral": True, "data": {}}),
    json.dumps({"type": "assistant.message_delta", "ephemeral": True,
                "data": {"content": "noise"}}),
    json.dumps({"type": "assistant.message", "data": {"content": "first"}}),
    json.dumps({"type": "tool.execution_complete", "data": {"success": True}}),
    json.dumps({"type": "tool.execution_complete", "data": {"success": False}}),
    json.dumps({"type": "assistant.turn_end", "data": {"turnId": "0"}}),
    json.dumps({"type": "assistant.message", "data": {"content": "final"}}),
    json.dumps({"type": "assistant.turn_end", "data": {"turnId": "1"}}),
    "",
    "not json at all",
    json.dumps({"type": "result", "exitCode": 0, "sessionId": "sess-123",
                "usage": {"premiumRequests": 0.33,
                          "codeChanges": {"filesModified": ["a.py"]}}}),
]


def test_parse_events_folds_stream():
    result = parse_events(SAMPLE)
    assert result.ok
    assert result.exit_code == 0
    assert result.session_id == "sess-123"
    assert result.turns == 2
    assert result.tool_calls == 2
    assert result.tool_failures == 1
    assert result.final_message == "final"
    assert result.files_modified == ["a.py"]
    assert result.summary()["premium_requests"] == 0.33


def test_parse_events_ignores_ephemeral_deltas():
    result = parse_events([
        json.dumps({"type": "assistant.turn_end", "ephemeral": True, "data": {}}),
    ])
    assert result.turns == 0


def test_parse_events_handles_content_parts():
    result = parse_events([json.dumps({
        "type": "assistant.message",
        "data": {"content": [{"text": "a"}, {"text": "b"}]}})])
    assert result.final_message == "ab"


def test_nonzero_exit_is_not_ok():
    result = parse_events([json.dumps({"type": "result", "exitCode": 1, "usage": {}})])
    assert not result.ok


def test_timed_out_is_not_ok():
    result = RunResult()
    result.exit_code = 0
    result.timed_out = True
    assert not result.ok


# -- argv construction ----------------------------------------------------

def runner(**cfg):
    base = {"binary": "copilot", "allow_all_tools": True}
    base.update(cfg)
    return CopilotRunner(base, NoSandbox({}))


def test_build_argv_minimal():
    argv = runner().build_argv("hello")
    assert argv[:2] == ["copilot", "-p"]
    assert argv[2] == "hello"
    assert "--output-format" in argv and "json" in argv
    assert "--allow-all-tools" in argv
    assert "--allow-all-paths" not in argv


def test_build_argv_full():
    argv = runner(
        model="gpt-5.5", reasoning_effort="high", allow_all_paths=True,
        add_dir=["/tmp/x"], deny_tool=["shell"], available_tools=["view", "edit"],
        secret_env_vars=["A", "B"], disable_builtin_mcps=True,
        additional_mcp_config="@/tmp/mcp.json", max_ai_credits=25,
        allow_url=["github.com"], deny_url=["evil.test"],
        no_custom_instructions=True, disallow_temp_dir=True,
        extra_args=["--experimental"],
    ).build_argv("p", resume_session="sess-9")
    joined = " ".join(argv)
    assert "--model gpt-5.5" in joined
    assert "--reasoning-effort high" in joined
    assert "--allow-all-paths" in joined
    assert "--add-dir /tmp/x" in joined
    assert "--deny-tool shell" in joined
    assert "--available-tools view edit" in joined
    assert "--secret-env-vars A,B" in joined
    assert "--disable-builtin-mcps" in joined
    assert "--additional-mcp-config @/tmp/mcp.json" in joined
    assert "--max-autopilot-continues" not in joined
    assert "--max-ai-credits 25" in joined
    assert "--allow-url github.com" in joined
    assert "--deny-url evil.test" in joined
    assert "--no-custom-instructions" in joined
    assert "--disallow-temp-dir" in joined
    assert "--resume sess-9" in joined
    assert joined.endswith("--experimental")


def test_allow_all_tools_can_be_disabled():
    assert "--allow-all-tools" not in runner(allow_all_tools=False).build_argv("p")


# -- execution against a stub binary --------------------------------------

STUB = """#!/bin/bash
echo '{"type":"assistant.turn_end","data":{"turnId":"0"}}'
echo '{"type":"result","exitCode":%d,"sessionId":"stub-1","usage":{}}'
exit %d
"""


def make_stub(tmp_path, code=0, body=None):
    path = tmp_path / "stub-copilot"
    path.write_text(body if body else STUB % (code, code), encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_run_captures_result_and_writes_log(tmp_path):
    stub = make_stub(tmp_path)
    run_dir = tmp_path / "run"
    cop = CopilotRunner({"binary": stub, "allow_all_tools": False}, NoSandbox({}))
    result = cop.run("prompt", str(tmp_path), str(run_dir))
    assert result.ok
    assert result.session_id == "stub-1"
    assert result.turns == 1
    assert os.path.isfile(os.path.join(str(run_dir), "events.jsonl"))
    meta = json.load(open(os.path.join(str(run_dir), "command.json")))
    assert meta["argv"][0] == stub


def test_run_reports_nonzero_exit(tmp_path):
    cop = CopilotRunner({"binary": make_stub(tmp_path, code=7), "allow_all_tools": False},
                        NoSandbox({}))
    result = cop.run("p", str(tmp_path), str(tmp_path / "run"))
    assert not result.ok
    assert result.exit_code == 7


def test_run_times_out_and_kills(tmp_path):
    body = "#!/bin/bash\nwhile true; do echo '{\"type\":\"noop\"}'; sleep 0.1; done\n"
    cop = CopilotRunner({"binary": make_stub(tmp_path, body=body), "allow_all_tools": False},
                        NoSandbox({}), timeout_sec=1.0)
    result = cop.run("p", str(tmp_path), str(tmp_path / "run"))
    assert result.timed_out
    assert not result.ok


SILENT_HANG_STUB = """#!/bin/bash
echo '{"type":"assistant.turn_end","data":{"turnId":"0"}}'
sleep 600
"""

STDERR_FLOOD_STUB = """#!/bin/bash
echo '{"type":"assistant.turn_end","data":{"turnId":"0"}}'
# 1 MB of stderr: far more than one pipe buffer
for i in $(seq 1 4000); do
  printf 'noisy build output line %s %s\\n' "$i" "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" >&2
done
echo '{"type":"result","exitCode":0,"sessionId":"flood","usage":{}}'
"""


def test_timeout_fires_when_child_goes_silent(tmp_path):
    """Regression: a deadline checked only inside the stdout read loop is inert
    against a child that simply stops writing, leaking a concurrency slot."""
    cop = CopilotRunner({"binary": make_stub(tmp_path, body=SILENT_HANG_STUB),
                         "allow_all_tools": False}, NoSandbox({}), timeout_sec=2.0)
    started = time.time()
    result = cop.run("p", str(tmp_path), str(tmp_path / "run"))
    assert result.timed_out
    assert not result.ok
    assert time.time() - started < 30, "timeout did not fire"


def test_large_stderr_does_not_deadlock(tmp_path):
    """Regression: draining stdout to EOF before reading a stderr *pipe*
    deadlocks once the child exceeds the ~64 KB stderr buffer."""
    run_dir = tmp_path / "run"
    cop = CopilotRunner({"binary": make_stub(tmp_path, body=STDERR_FLOOD_STUB),
                         "allow_all_tools": False}, NoSandbox({}), timeout_sec=60.0)
    started = time.time()
    result = cop.run("p", str(tmp_path), str(run_dir))
    assert not result.timed_out, "run deadlocked and was killed by the watchdog"
    assert result.ok
    assert result.session_id == "flood"
    assert time.time() - started < 45
    assert (run_dir / "stderr.log").stat().st_size > 500_000


def test_empty_stderr_log_is_removed(tmp_path):
    run_dir = tmp_path / "run"
    cop = CopilotRunner({"binary": make_stub(tmp_path), "allow_all_tools": False},
                        NoSandbox({}))
    cop.run("p", str(tmp_path), str(run_dir))
    assert not (run_dir / "stderr.log").exists()


def test_run_handles_missing_binary(tmp_path):
    cop = CopilotRunner({"binary": str(tmp_path / "nope"), "allow_all_tools": False},
                        NoSandbox({}))
    result = cop.run("p", str(tmp_path), str(tmp_path / "run"))
    assert result.exit_code == -1
    assert "FileNotFoundError" in result.error


WARN_STUB = """#!/bin/bash
echo 'Warning: something cosmetic' >&2
echo '{"type":"result","exitCode":0,"sessionId":"s","usage":{}}'
"""

FAIL_WITH_STDERR_STUB = """#!/bin/bash
echo 'boom: real failure' >&2
echo '{"type":"result","exitCode":2,"sessionId":"s","usage":{}}'
exit 2
"""


def test_stderr_on_success_is_logged_but_not_reported_as_error(tmp_path):
    """Copilot emits benign warnings on stderr; they must not poison a green run."""
    run_dir = tmp_path / "run"
    cop = CopilotRunner({"binary": make_stub(tmp_path, body=WARN_STUB),
                         "allow_all_tools": False}, NoSandbox({}))
    result = cop.run("p", str(tmp_path), str(run_dir))
    assert result.ok
    assert result.error is None
    assert "cosmetic" in (run_dir / "stderr.log").read_text()


def test_stderr_on_failure_is_reported(tmp_path):
    cop = CopilotRunner({"binary": make_stub(tmp_path, body=FAIL_WITH_STDERR_STUB),
                         "allow_all_tools": False}, NoSandbox({}))
    result = cop.run("p", str(tmp_path), str(tmp_path / "run"))
    assert not result.ok
    assert "real failure" in result.error


# -- sandbox notices ---------------------------------------------------------
#
# Copilot reports "this host cannot sandbox" as an ephemeral warning, and then
# every command fails. parse_events drops ephemeral events, which is right for
# render deltas and was wrong for this one.

def test_sandbox_warning_survives_the_ephemeral_filter():
    lines = [
        json.dumps({"type": "log", "ephemeral": True,
                    "data": {"level": "warning", "type": "sandbox",
                             "message": "Sandboxing is enabled but unsupported here"}}),
        json.dumps({"type": "result", "exitCode": 0}),
    ]
    result = parse_events(lines)
    assert result.sandbox_notices == ["Sandboxing is enabled but unsupported here"]
    assert "sandbox_notices" in result.summary()


def test_ordinary_ephemeral_noise_is_still_dropped():
    lines = [
        json.dumps({"type": "assistant.message_delta", "ephemeral": True,
                    "data": {"deltaContent": "hello"}}),
        json.dumps({"type": "log", "ephemeral": True,
                    "data": {"level": "info", "message": "sandbox ready"}}),
        json.dumps({"type": "result", "exitCode": 0}),
    ]
    result = parse_events(lines)
    assert result.sandbox_notices == []
    assert "sandbox_notices" not in result.summary()


def test_sandbox_notices_are_deduped_and_capped():
    lines = [json.dumps({"type": "log", "ephemeral": True,
                         "data": {"level": "warning",
                                  "message": "sandbox broke"}})] * 20
    lines += [json.dumps({"type": "log", "ephemeral": True,
                          "data": {"level": "error",
                                   "message": "sandbox issue %d" % i}})
              for i in range(20)]
    result = parse_events(lines)
    assert result.sandbox_notices.count("sandbox broke") == 1
    assert len(result.sandbox_notices) <= 5


def test_unattended_defaults_are_on():
    """A -p run has nobody to answer ask_user, and must not silently swap the
    binary that doctor probed for a freshly downloaded one."""
    joined = " ".join(runner().build_argv("p"))
    assert "--no-ask-user" in joined
    assert "--no-auto-update" in joined
    # These two change repo behaviour, so they stay opt-in.
    assert "--no-custom-instructions" not in joined
    assert "--disallow-temp-dir" not in joined


def test_unattended_defaults_can_be_turned_off():
    joined = " ".join(runner(no_ask_user=False, no_auto_update=False).build_argv("p"))
    assert "--no-ask-user" not in joined
    assert "--no-auto-update" not in joined
