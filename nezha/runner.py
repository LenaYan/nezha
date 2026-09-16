"""Copilot CLI runner: invoke ``copilot -p --output-format json`` and parse JSONL.

Event contract observed from Copilot CLI 1.0.85 (see README "Event contract"):
  * every line is one JSON object with ``type``;
  * lines carrying ``"ephemeral": true`` are streaming deltas -- safe to drop;
  * ``assistant.turn_end`` marks a completed turn;
  * ``tool.execution_complete`` carries ``data.success``;
  * the final line is ``{"type": "result", "exitCode", "sessionId", "usage"}``.

``sessionId`` from the result event is what makes retries resumable.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from .sandbox import Sandbox, strip_secrets


class RunResult(object):
    __slots__ = ("exit_code", "session_id", "turns", "tool_calls", "tool_failures",
                 "final_message", "usage", "duration_sec", "timed_out", "log_path",
                 "files_modified", "error")

    def __init__(self):
        self.exit_code = None            # type: Optional[int]
        self.session_id = None           # type: Optional[str]
        self.turns = 0
        self.tool_calls = 0
        self.tool_failures = 0
        self.final_message = ""
        self.usage = {}                  # type: Dict[str, Any]
        self.duration_sec = 0.0
        self.timed_out = False
        self.log_path = None             # type: Optional[str]
        self.files_modified = []         # type: List[str]
        self.error = None                # type: Optional[str]

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> Dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "session_id": self.session_id,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "timed_out": self.timed_out,
            "duration_sec": round(self.duration_sec, 1),
            "premium_requests": (self.usage or {}).get("premiumRequests"),
            "files_modified": self.files_modified,
        }


def parse_events(lines, result: Optional[RunResult] = None) -> RunResult:
    """Fold a JSONL event stream into a :class:`RunResult`."""
    result = result or RunResult()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "result":
            result.exit_code = event.get("exitCode")
            result.session_id = event.get("sessionId") or result.session_id
            result.usage = event.get("usage") or {}
            changes = result.usage.get("codeChanges") or {}
            result.files_modified = list(changes.get("filesModified") or [])
            continue
        if event.get("ephemeral"):
            continue
        data = event.get("data") or {}
        if etype == "assistant.turn_end":
            result.turns += 1
        elif etype == "tool.execution_complete":
            result.tool_calls += 1
            if data.get("success") is False:
                result.tool_failures += 1
        elif etype == "assistant.message":
            text = data.get("content") or data.get("text") or ""
            if isinstance(text, list):
                text = "".join(
                    part.get("text", "") for part in text if isinstance(part, dict))
            if text:
                result.final_message = text
    return result


class CopilotRunner(object):
    def __init__(self, copilot_config: Dict[str, Any], sandbox: Sandbox,
                 timeout_sec: float = 3600, logger=None):
        self.cfg = copilot_config
        self.sandbox = sandbox
        self.timeout_sec = float(timeout_sec)
        self.log = logger

    # -- argv construction ------------------------------------------------
    def build_argv(self, prompt: str, resume_session: Optional[str] = None) -> List[str]:
        cfg = self.cfg
        argv = [cfg.get("binary") or "copilot", "-p", prompt,
                "--output-format", "json"]
        if cfg.get("allow_all_tools", True):
            argv.append("--allow-all-tools")
        if cfg.get("allow_all_paths"):
            argv.append("--allow-all-paths")
        if cfg.get("model"):
            argv += ["--model", str(cfg["model"])]
        if cfg.get("reasoning_effort"):
            argv += ["--reasoning-effort", str(cfg["reasoning_effort"])]
        if cfg.get("max_autopilot_continues") is not None:
            argv += ["--max-autopilot-continues", str(cfg["max_autopilot_continues"])]
        for directory in cfg.get("add_dir") or []:
            argv += ["--add-dir", os.path.expanduser(str(directory))]
        for tool in cfg.get("deny_tool") or []:
            argv += ["--deny-tool", str(tool)]
        available = cfg.get("available_tools") or []
        if available:
            argv += ["--available-tools"] + [str(t) for t in available]
        secrets = cfg.get("secret_env_vars") or []
        if secrets:
            argv += ["--secret-env-vars", ",".join(str(s) for s in secrets)]
        if cfg.get("disable_builtin_mcps"):
            argv.append("--disable-builtin-mcps")
        if cfg.get("additional_mcp_config"):
            argv += ["--additional-mcp-config", str(cfg["additional_mcp_config"])]
        if resume_session:
            argv += ["--resume", resume_session]
        argv += [str(a) for a in (cfg.get("extra_args") or [])]
        return argv

    # -- execution --------------------------------------------------------
    def run(self, prompt: str, workdir: str, run_dir: str,
            env_extra: Optional[Dict[str, str]] = None,
            resume_session: Optional[str] = None,
            on_start=None) -> RunResult:
        os.makedirs(run_dir, exist_ok=True)
        argv = self.build_argv(prompt, resume_session)

        env = strip_secrets(dict(os.environ), self.cfg.get("secret_env_vars") or [])
        env.update(env_extra or {})
        env.setdefault("COPILOT_ALLOW_ALL", "true" if self.cfg.get("allow_all_tools", True)
                       else "false")

        wrapped, env, tmp_profile = self.sandbox.wrap(argv, workdir, env)
        log_path = os.path.join(run_dir, "events.jsonl")
        meta_path = os.path.join(run_dir, "command.json")
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump({"argv": wrapped, "workdir": workdir,
                       "sandbox": self.sandbox.describe()}, handle, indent=2)

        result = RunResult()
        result.log_path = log_path
        stderr_path = os.path.join(run_dir, "stderr.log")
        started = time.time()
        proc = None
        timer = None
        timed_out = threading.Event()
        try:
            # stderr goes straight to a file, never a pipe: draining stdout to
            # EOF before reading a stderr pipe deadlocks as soon as the child
            # writes more than one pipe buffer (~64 KB) of stderr, which any
            # verbose build or test run does.
            with open(log_path, "w", encoding="utf-8") as sink, \
                    open(stderr_path, "wb") as err_sink:
                proc = subprocess.Popen(
                    wrapped, cwd=workdir, env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=err_sink,
                    start_new_session=True)
                if on_start is not None:
                    on_start(proc)

                # The deadline must be enforced out of band. Checking it inside
                # the read loop is inert against a child that simply stops
                # writing (stalled API call, blocked tool), which would leak the
                # orchestrator's concurrency slot forever.
                def _on_deadline() -> None:
                    timed_out.set()
                    self._terminate(proc)

                timer = threading.Timer(self.timeout_sec, _on_deadline)
                timer.daemon = True
                timer.start()

                lines: List[str] = []
                assert proc.stdout is not None
                for raw in proc.stdout:
                    text = raw.decode("utf-8", "replace")
                    sink.write(text)
                    sink.flush()
                    lines.append(text)
                proc.wait(timeout=30)
            result.timed_out = timed_out.is_set()
            parse_events(lines, result)
            if result.exit_code is None:
                result.exit_code = proc.returncode
            stderr = ""
            if os.path.isfile(stderr_path):
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as fh:
                    stderr = fh.read()
                if not stderr.strip():
                    os.unlink(stderr_path)
            # Copilot writes benign warnings to stderr on successful runs;
            # only surface stderr as an error when the run actually failed.
            if stderr.strip() and (result.exit_code not in (0, None) or result.timed_out):
                result.error = stderr.strip()[-4000:]
        except Exception as exc:  # noqa: BLE001 - surfaced to the orchestrator
            if proc is not None:
                self._terminate(proc)
            result.error = "%s: %s" % (type(exc).__name__, exc)
            if result.exit_code is None:
                result.exit_code = -1
        finally:
            if timer is not None:
                timer.cancel()
            result.duration_sec = time.time() - started
            if tmp_profile and os.path.exists(tmp_profile):
                os.unlink(tmp_profile)
        if result.timed_out:
            result.exit_code = result.exit_code if result.exit_code not in (0, None) else 124
        return result

    @staticmethod
    def _terminate(proc: "subprocess.Popen") -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
