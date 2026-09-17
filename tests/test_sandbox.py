import json
import os
import subprocess
import sys

import pytest

from nezha.sandbox import (DEFAULT_DENY_READ, SandboxError, build_sandbox,
                           strip_secrets)

def test_build_unknown_backend():
    with pytest.raises(SandboxError, match="unknown sandbox.backend"):
        build_sandbox({"backend": "chroot"})


def test_none_backend_is_transparent_and_says_so():
    sb = build_sandbox({"backend": "none"})
    argv, env, tmp = sb.wrap(["echo", "hi"], "/tmp", {"A": "1"})
    assert argv == ["echo", "hi"] and env == {"A": "1"} and tmp is None
    assert "NO process isolation" in sb.describe()


def test_strip_secrets():
    assert strip_secrets({"A": "1", "B": "2"}, ["B", "MISSING"]) == {"A": "1"}


# ---------------------------------------------------------------------------
# copilot-native backend
#
# These assert the shape of the policy and the per-issue COPILOT_HOME. Kernel
# enforcement belongs to Copilot CLI and is verified by `nezha doctor`, not
# here: proving it would mean spending model credits on every test run.
# ---------------------------------------------------------------------------

def _native(tmp_path, **overrides):
    config = {"backend": "copilot-native", "state_root": str(tmp_path / "state"),
              "copilot_home": str(tmp_path / "home")}
    config.update(overrides)
    sb = build_sandbox(config)
    # Stub the CLI capability probe: it spawns the real binary, and these tests
    # are about Nezha's policy layer, not about Copilot's.
    sb._probed = "Command Sandboxing"
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "data.db").write_text("stub")
    return sb


def test_native_is_the_default_backend():
    assert build_sandbox({}).name == "copilot-native"


def test_native_policy_shape(tmp_path):
    sb = _native(tmp_path, allow_network=False, deny_read=["~/.ssh"],
                 allow_write=["/srv/cache"], readonly_paths=["/opt/toolchain"])
    policy = sb.policy()
    assert policy["enabled"] is True
    assert policy["addCurrentWorkingDirectory"] is True
    fs = policy["userPolicy"]["filesystem"]
    assert fs["readwritePaths"] == [os.path.realpath("/srv/cache")]
    assert fs["readonlyPaths"] == [os.path.realpath("/opt/toolchain")]
    assert fs["deniedPaths"] == [os.path.realpath(os.path.expanduser("~/.ssh"))]
    assert policy["userPolicy"]["network"]["allowOutbound"] is False


def test_native_disables_bypass_by_default(tmp_path):
    """Copilot defaults the per-command escape hatch ON; unattended runs must not."""
    assert _native(tmp_path).policy()["allowBypass"] is False
    assert _native(tmp_path, allow_bypass=True).policy()["allowBypass"] is True


def test_native_keeps_keychain_out_by_default(tmp_path):
    policy = _native(tmp_path).policy()
    assert policy["userPolicy"]["seatbelt"]["keychainAccess"] is False
    assert policy["auth"] == {"git": True, "gh": False}


def test_native_home_is_per_issue_and_outside_the_worktree(tmp_path):
    sb = _native(tmp_path)
    one = sb.home_for("DEMO-1")
    two = sb.home_for("DEMO-2")
    assert one != two
    assert str(tmp_path / "state") in one


def test_native_slugifies_hostile_issue_ids(tmp_path):
    """An id like '../../etc' must not escape the state root."""
    sb = _native(tmp_path)
    home = sb.home_for("../../etc/passwd")
    root = os.path.realpath(str(tmp_path / "state"))
    assert os.path.realpath(home).startswith(root)


def test_native_ensure_home_writes_policy_and_links_auth(tmp_path):
    sb = _native(tmp_path)
    home = sb.ensure_home("DEMO-1")
    settings = os.path.join(home, "settings.json")
    with open(settings, encoding="utf-8") as handle:
        written = json.load(handle)
    assert written["sandbox"]["enabled"] is True
    assert os.path.islink(os.path.join(home, "data.db"))


def test_native_ensure_home_rewrites_drifted_policy(tmp_path):
    """A resumed attempt must not inherit a policy that drifted from WORKFLOW.md."""
    sb = _native(tmp_path)
    home = sb.ensure_home("DEMO-1")
    settings = os.path.join(home, "settings.json")
    with open(settings, "w", encoding="utf-8") as handle:
        json.dump({"sandbox": {"enabled": False}, "keepMe": 1}, handle)
    sb.ensure_home("DEMO-1")
    with open(settings, encoding="utf-8") as handle:
        written = json.load(handle)
    assert written["sandbox"]["enabled"] is True
    assert written["keepMe"] == 1, "unrelated user settings must survive"


def test_native_wrap_injects_flags_and_home(tmp_path):
    sb = _native(tmp_path)
    argv, env, tmp = sb.wrap(
        ["copilot", "-p", "do it", "--output-format", "json"],
        str(tmp_path), {"NEZHA_ISSUE_ID": "DEMO-1"}, dry_run=True)
    assert argv[:4] == ["copilot", "--experimental", "--sandbox", "-p"]
    assert argv[4] == "do it", "the prompt must stay attached to -p"
    assert env["COPILOT_HOME"] == sb.home_for("DEMO-1")
    assert tmp is None


def test_native_dry_run_creates_nothing(tmp_path):
    """`nezha plan` must not provision state."""
    sb = _native(tmp_path)
    sb.wrap(["copilot", "-p", "x"], str(tmp_path),
            {"NEZHA_ISSUE_ID": "DEMO-1"}, dry_run=True)
    assert not os.path.exists(sb.home_for("DEMO-1"))


def test_native_cleanup_home(tmp_path):
    sb = _native(tmp_path)
    home = sb.ensure_home("DEMO-1")
    assert os.path.isdir(home)
    sb.cleanup_home("DEMO-1")
    assert not os.path.exists(home)


def test_native_preflight_rejects_missing_auth(tmp_path):
    sb = build_sandbox({"backend": "copilot-native",
                        "state_root": str(tmp_path / "state"),
                        "copilot_home": str(tmp_path / "empty-home")})
    sb._probed = "Command Sandboxing"
    (tmp_path / "empty-home").mkdir()
    with pytest.raises(SandboxError, match="data.db"):
        sb.preflight()


# -- host prerequisite checking ---------------------------------------------
#
# Copilot's own probe checks bwrap and nothing else, and its docs say so. A
# Linux host can pass that probe and still fail every command. These tests pin
# the fuller check; they run on any host because host_backend is faked.

def test_native_host_problems_reports_all_missing_at_once(tmp_path, monkeypatch):
    sb = _native(tmp_path)
    monkeypatch.setattr(sb, "host_backend", lambda: "bubblewrap")
    monkeypatch.setattr("nezha.sandbox.shutil.which", lambda name: None)
    monkeypatch.setattr("nezha.sandbox.os.access", lambda path, mode: False)
    problems = sb.host_problems()
    joined = " ".join(problems)
    for needed in ("bwrap", "slirp4netns", "unshare", "nsenter",
                   "iptables", "ip6tables", "/dev/net/tun"):
        assert needed in joined, "%s not reported" % needed
    assert len(problems) > 5, "an operator wants one list, not one item per run"


def test_native_host_problems_quiet_when_linux_host_is_complete(tmp_path, monkeypatch):
    sb = _native(tmp_path)
    monkeypatch.setattr(sb, "host_backend", lambda: "bubblewrap")
    monkeypatch.setattr("nezha.sandbox.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("nezha.sandbox.os.access", lambda path, mode: True)
    monkeypatch.setattr(sb, "_version_at_least",
                        lambda label, argv, minimum: True)
    assert sb.host_problems() == []


def test_native_preflight_explains_the_silent_failure_mode(tmp_path, monkeypatch):
    sb = _native(tmp_path)
    monkeypatch.setattr(sb, "host_backend", lambda: "bubblewrap")
    monkeypatch.setattr("nezha.sandbox.shutil.which", lambda name: None)
    monkeypatch.setattr("nezha.sandbox.os.access", lambda path, mode: False)
    with pytest.raises(SandboxError, match="slirp4netns"):
        sb.preflight()


def test_native_host_prereq_check_can_be_skipped(tmp_path, monkeypatch):
    """The list is transcribed from docs and untested on Linux; leave an exit."""
    sb = _native(tmp_path, skip_host_prereq_check=True)
    monkeypatch.setattr(sb, "host_backend", lambda: "bubblewrap")
    monkeypatch.setattr("nezha.sandbox.shutil.which", lambda name: None)
    assert sb.host_problems() == []


def test_native_unparseable_version_is_not_treated_as_too_old(tmp_path):
    sb = _native(tmp_path)
    assert sb._version_at_least("x", ["true"], (99, 0)) is True
    assert sb._version_at_least("x", ["no-such-binary-xyz"], (0, 1)) is True


# -- dev-tool access vs deny_read ------------------------------------------

def test_dev_tool_conflict_is_reported(tmp_path):
    sb = _native(tmp_path, allow_dev_tool_access=True,
                 deny_read=["~/.ssh", "~/.npmrc"])
    assert sb.dev_tool_conflicts() == [os.path.realpath(os.path.expanduser("~/.npmrc"))]


def test_dev_tool_conflict_catches_a_parent_directory(tmp_path):
    """Denying ~/.m2 and granting ~/.m2/settings.xml is the same contradiction."""
    sb = _native(tmp_path, allow_dev_tool_access=True, deny_read=["~/.m2"])
    assert sb.dev_tool_conflicts()


def test_no_conflict_when_dev_tool_access_is_off(tmp_path):
    sb = _native(tmp_path, allow_dev_tool_access=False, deny_read=["~/.npmrc"])
    assert sb.dev_tool_conflicts() == []


def test_shipped_default_deny_read_does_not_contradict_dev_tool_access(tmp_path):
    sb = _native(tmp_path, allow_dev_tool_access=True, deny_read=DEFAULT_DENY_READ)
    assert sb.dev_tool_conflicts() == []


def test_policy_clears_filesystem_state_on_exit(tmp_path):
    policy = _native(tmp_path).policy()
    assert policy["userPolicy"]["filesystem"]["clearPolicyOnExit"] is True
    assert _native(tmp_path, clear_policy_on_exit=False) \
        .policy()["userPolicy"]["filesystem"]["clearPolicyOnExit"] is False


def test_removed_backends_are_rejected_by_name():
    for backend in ("sandbox-exec", "docker"):
        with pytest.raises(SandboxError, match="unknown sandbox.backend"):
            build_sandbox({"backend": backend})
