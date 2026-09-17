"""Isolation backends -- the layer a git worktree does *not* give you.

Four backends, weakest to strongest:

``none``
    No process isolation. The agent runs with your full user privileges and can
    read ``~/.ssh``, reach any host, and touch any repo. Only acceptable when
    you review every diff before it lands.

``sandbox-exec`` (legacy, macOS)
    Apple Seatbelt, driven by a profile Nezha generates and applies to the whole
    ``copilot`` process tree. Superseded by ``copilot-native`` and kept only for
    hosts whose CLI has no sandbox support.

    Honest limitations:
      * ``sandbox-exec`` is formally deprecated by Apple (still functional).
      * The profile is ``(allow default)`` plus deny rules, i.e. a deny-list.
        A deny-list is weaker than an allow-list: an unlisted secret store is
        readable. Extend ``sandbox.deny_read`` for your machine.
      * No egress control. The whole CLI is inside the sandbox and Copilot needs
        the network to reach its API, so ``allow_network: false`` breaks the
        agent outright.
      * Worktrees share the parent repo's object store, so the profile must
        grant write access to ``<repo>/.git``. A hostile agent can corrupt the
        parent repository. Use a dedicated clone for untrusted work.

``copilot-native`` (default)
    Copilot CLI's own OS-level command sandbox, via ``--sandbox``. An allow-list
    filesystem policy and a working egress switch, on macOS, Linux and Windows.
    See :class:`CopilotNativeSandbox` for what it does and does not cover.

``docker``
    Strongest of the four: separate filesystem, PID and network namespace, and
    the only backend that confines the CLI process itself as well as the commands
    it spawns. Requires Docker/Colima and an image with your toolchain.

``copilot-native`` and ``sandbox-exec`` cannot be combined: Seatbelt refuses to
nest, and a profile applied inside another one fails ``sandbox_init`` with
``Operation not permitted``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DENY_READ = [
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.kube",
    "~/.docker",
    "~/.npmrc",
    "~/.netrc",
    "~/.git-credentials",
    "~/.config/gh",
    "~/.config/gcloud",
    "~/Library/Keychains",
]
# NOTE: ~/.gitconfig is deliberately NOT denied -- git refuses to run at all when
# it cannot stat the global config. It can still name a credential helper, so
# audit yours before trusting this list.


class SandboxError(RuntimeError):
    pass


def _expand(paths: List[str]) -> List[str]:
    return [os.path.realpath(os.path.expanduser(p)) for p in paths]


class Sandbox(object):
    name = "base"

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.allow_network = bool(config.get("allow_network", True))
        self.deny_read = list(config.get("deny_read") or DEFAULT_DENY_READ)
        self.allow_write = list(config.get("allow_write") or [])

    def preflight(self) -> None:
        """Raise :class:`SandboxError` if the backend cannot run on this host."""

    def wrap(self, argv: List[str], workdir: str, env: Dict[str, str],
             dry_run: bool = False) -> Tuple[List[str], Dict[str, str], Optional[str]]:
        """Return ``(argv, env, tempfile_to_clean)`` for the confined command.

        ``dry_run`` asks the backend to skip anything that touches the host, so
        ``nezha plan`` can show the real command without provisioning state.
        """
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class NoSandbox(Sandbox):
    name = "none"

    def wrap(self, argv, workdir, env, dry_run=False):
        return list(argv), dict(env), None

    def describe(self) -> str:
        return "none (NO process isolation -- agent runs with your full privileges)"


SEATBELT_TEMPLATE = """(version 1)
(allow default)

; ---- writes: deny everything, then re-allow the workspace and runtime dirs ----
(deny file-write*)
(allow file-write*
{allow_write}
)

; ---- reads: block known credential stores ----
(deny file-read*
{deny_read}
)

{network}
"""


class SeatbeltSandbox(Sandbox):
    name = "sandbox-exec"

    def preflight(self) -> None:
        if not os.path.exists("/usr/bin/sandbox-exec"):
            raise SandboxError(
                "sandbox-exec not found; this backend is macOS-only. "
                "Use sandbox.backend: docker or none."
            )

    def _git_common_dir(self, workdir: str) -> Optional[str]:
        """A worktree's real git dir lives under the parent repo and must be writable."""
        try:
            proc = subprocess.run(
                ["git", "-C", workdir, "rev-parse", "--git-common-dir"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        raw = proc.stdout.decode("utf-8", "replace").strip()
        if not raw:
            return None
        if not os.path.isabs(raw):
            raw = os.path.join(workdir, raw)
        return os.path.realpath(raw)

    def _profile(self, workdir: str) -> str:
        home = os.path.expanduser("~")
        writable = [
            os.path.realpath(workdir),
            os.path.realpath(tempfile.gettempdir()),
            "/private/var/folders",
            "/private/tmp",
            "/dev",
            os.path.join(home, ".copilot"),
            os.path.join(home, ".cache"),
            os.path.join(home, ".local/state/nezha"),
        ] + _expand(self.allow_write)
        git_dir = self._git_common_dir(workdir)
        if git_dir:
            writable.append(git_dir)
        blocked = _expand(self.deny_read)
        # Never deny-read a path we must write to; seatbelt file-read* denial
        # also blocks the stat/open that precedes a write.
        blocked = [p for p in blocked
                   if not any(p == w or w.startswith(p + os.sep) for w in writable)]

        allow_lines = "\n".join('  (subpath "%s")' % p for p in sorted(set(writable)))
        deny_lines = "\n".join('  (subpath "%s")' % p for p in sorted(set(blocked)))
        if not deny_lines:
            deny_lines = '  (subpath "/nonexistent-nezha-placeholder")'
        network = "" if self.allow_network else "(deny network*)"
        return SEATBELT_TEMPLATE.format(
            allow_write=allow_lines, deny_read=deny_lines, network=network)

    def wrap(self, argv, workdir, env, dry_run=False):
        self.preflight()
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".sb", prefix="nezha-", delete=False, encoding="utf-8")
        try:
            handle.write(self._profile(workdir))
        finally:
            handle.close()
        wrapped = ["/usr/bin/sandbox-exec", "-f", handle.name] + list(argv)
        return wrapped, dict(env), handle.name

    def describe(self) -> str:
        return "sandbox-exec (writes confined to workspace; %d read denials; network %s)" % (
            len(self.deny_read), "allowed" if self.allow_network else "DENIED")


class DockerSandbox(Sandbox):
    """STATUS: UNVERIFIED -- Docker was not installed on the development host."""

    name = "docker"

    def __init__(self, config: Dict[str, Any]):
        super(DockerSandbox, self).__init__(config)
        self.image = config.get("image") or "nezha-agent:latest"
        self.extra_args = list(config.get("docker_args") or [])
        self.binary = config.get("docker_binary") or "docker"

    def preflight(self) -> None:
        if shutil.which(self.binary) is None:
            raise SandboxError(
                "%s not found on PATH. Install Docker or Colima, or set "
                "sandbox.backend to sandbox-exec." % self.binary
            )

    def wrap(self, argv, workdir, env, dry_run=False):
        self.preflight()
        home = os.path.expanduser("~")
        args = [
            self.binary, "run", "--rm", "-i",
            "--workdir", "/workspace",
            "-v", "%s:/workspace" % os.path.realpath(workdir),
            # Copilot session/auth state; mount read-write so sessions persist.
            "-v", "%s:/root/.copilot" % os.path.join(home, ".copilot"),
        ]
        if not self.allow_network:
            args += ["--network", "none"]
        for key in ("GITHUB_TOKEN", "COPILOT_ALLOW_ALL", "NEZHA_ISSUE_ID",
                    "NEZHA_ISSUE_IDENTIFIER", "NEZHA_BRANCH"):
            if key in env:
                args += ["-e", key]
        args += self.extra_args
        args += [self.image] + list(argv)
        return args, dict(env), None

    def describe(self) -> str:
        return "docker (image=%s, network %s)" % (
            self.image, "allowed" if self.allow_network else "none")


#: Files and directories linked from the real ``COPILOT_HOME`` into the per-issue
#: one. ``data.db`` carries authentication, so without it every run would need a
#: fresh login. Verified by probing an actual run; the layout is NOT documented by
#: GitHub and can change between CLI releases -- ``doctor`` re-checks it.
DEFAULT_COPILOT_HOME_LINKS = [
    "data.db",
    "config.json",
    "mcp-config.json",
    "mcp-oauth-config",
]

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value).strip("-") or "unknown"


class CopilotNativeSandbox(Sandbox):
    """Delegate isolation to Copilot CLI's own sandbox (``--sandbox``).

    Copilot CLI 1.0.85 ships OS-level command sandboxing powered by Microsoft
    eXecution Containers (MXC): Seatbelt on macOS, bubblewrap on Linux,
    ProcessContainer on Windows. It is stronger than :class:`SeatbeltSandbox` in
    the ways that matter most -- the filesystem policy is an *allow-list* rather
    than a deny-list, and outbound network access is a real, enforceable switch.

    Two properties of the official design drive this implementation:

    1. **Copilot CLI is not itself sandboxed**; it sandboxes each shell command
       it spawns. That is why ``allow_network: false`` works here but breaks
       :class:`SeatbeltSandbox`: denying egress no longer cuts the CLI off from
       its own API. It is also the backend's main weakness -- the CLI's built-in
       file tools bypass the OS sandbox and only honour the policy on a
       best-effort basis, so ``copilot.allow_all_paths`` must stay ``false``.

    2. **The policy lives in ``settings.json``**, not in command-line flags. To
       avoid mutating the operator's own configuration -- and to keep the policy
       out of reach of the agent -- each issue gets a private ``COPILOT_HOME``
       under Nezha's state root, outside every worktree. Authentication and MCP
       configuration are symlinked back to the real home.

    The home is per *issue*, not per attempt: ``--resume`` needs the session
    record that Copilot stores inside ``COPILOT_HOME``, so wiping it between
    attempts would silently turn every retry into a fresh conversation.

    Seatbelt cannot nest: running this backend inside ``sandbox-exec`` makes
    ``sandbox_init`` fail with ``Operation not permitted`` and *every* command
    dies. The two backends are mutually exclusive by construction.
    """

    name = "copilot-native"

    def __init__(self, config: Dict[str, Any]):
        super(CopilotNativeSandbox, self).__init__(config)
        state_root = config.get("state_root") or "~/.local/state/nezha"
        self.state_root = os.path.abspath(os.path.expanduser(state_root))
        self.binary = config.get("copilot_binary") or "copilot"
        self.allow_local_network = bool(config.get("allow_local_network", False))
        # Unattended runs have nobody to approve a bypass prompt, so the
        # per-command escape hatch is off by default -- Copilot's own default is on.
        self.allow_bypass = bool(config.get("allow_bypass", False))
        self.allow_dev_tool_access = bool(config.get("allow_dev_tool_access", True))
        self.sandbox_mcp_servers = bool(config.get("sandbox_mcp_servers", True))
        self.sandbox_lsp_servers = bool(config.get("sandbox_lsp_servers", True))
        self.keychain_access = bool(config.get("keychain_access", False))
        self.auth_git = bool(config.get("auth_git", True))
        self.auth_gh = bool(config.get("auth_gh", False))
        self.readonly_paths = list(config.get("readonly_paths") or [])
        self.home_links = list(config.get("copilot_home_links")
                               or DEFAULT_COPILOT_HOME_LINKS)
        self.source_home = os.path.abspath(os.path.expanduser(
            config.get("copilot_home") or os.environ.get("COPILOT_HOME") or "~/.copilot"))
        # The prerequisite list below is transcribed from the CLI's own docs and
        # could not be tested on a Linux host. Set this to skip it if it is wrong
        # for your machine -- but read host_problems() before you do.
        self.skip_host_prereq_check = bool(config.get("skip_host_prereq_check", False))
        self._probed = None  # type: Optional[str]
        self._host_problems = None  # type: Optional[List[str]]

    # -- host + CLI capability -------------------------------------------
    def host_backend(self) -> str:
        if sys.platform == "darwin":
            return "seatbelt"
        if sys.platform.startswith("linux"):
            return "bubblewrap"
        if os.name == "nt":
            return "process-container"
        return "unsupported"

    def probe(self) -> str:
        """Return the CLI's sandbox help text, proving ``--sandbox`` exists."""
        if self._probed is not None:
            return self._probed
        binary = shutil.which(self.binary)
        if binary is None:
            raise SandboxError("copilot binary %r not found on PATH" % self.binary)
        try:
            proc = subprocess.run(
                [binary, "--experimental", "help", "sandbox"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError("could not probe copilot sandbox support: %s" % exc)
        text = (proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode != 0 or "Command Sandboxing" not in text:
            raise SandboxError(
                "this copilot build does not expose command sandboxing. It is an "
                "experimental feature; upgrade the CLI or set sandbox.backend to "
                "sandbox-exec."
            )
        self._probed = text
        return text

    def preflight(self) -> None:
        host = self.host_backend()
        if host == "unsupported":
            raise SandboxError(
                "Copilot command sandboxing supports macOS, Linux and Windows only; "
                "this host is %r." % sys.platform)
        problems = self.host_problems()
        if problems:
            raise SandboxError(
                "this host cannot run Copilot's command sandbox:\n  - %s\n"
                "Every sandboxed command would fail, and Copilot only reports that "
                "as an ephemeral startup notice that a -p run never surfaces, so "
                "the symptom would be an agent that inexplicably gets nothing done. "
                "Install the missing prerequisites, pick a different sandbox.backend, "
                "or set sandbox.skip_host_prereq_check: true if this check is wrong "
                "for your machine." % "\n  - ".join(problems))
        self.probe()
        missing = [n for n in self.home_links
                   if not os.path.exists(os.path.join(self.source_home, n))]
        if "data.db" in missing:
            raise SandboxError(
                "%s/data.db is missing, so the sandboxed run would have no "
                "authentication. Run `copilot login`, or set "
                "sandbox.copilot_home_links to match this CLI version."
                % self.source_home)

    def host_problems(self) -> List[str]:
        """Every unmet host prerequisite, not just the first.

        Copilot's own probe checks only ``sandbox-exec`` on macOS and ``bwrap`` on
        Linux; its documentation states outright that it does not check the
        namespace prerequisites. A host that passes that probe can still fail
        every single command, so Nezha checks the full documented list here and
        reports all of it at once -- an operator fixing this wants one list, not
        one item per run.
        """
        if self._host_problems is not None:
            return self._host_problems
        problems = []  # type: List[str]
        host = self.host_backend()
        if self.skip_host_prereq_check:
            self._host_problems = problems
            return problems
        if host == "seatbelt" and not os.path.exists("/usr/bin/sandbox-exec"):
            problems.append("sandbox-exec is missing (macOS Seatbelt backend needs it)")
        elif host == "bubblewrap":
            problems.extend(self._linux_problems())
        self._host_problems = problems
        return problems

    def _linux_problems(self) -> List[str]:
        problems = []  # type: List[str]
        if shutil.which("bwrap") is None:
            problems.append("bwrap (bubblewrap 0.5.0+) not found on PATH")
        elif not self._version_at_least("bwrap", ["bwrap", "--version"], (0, 5)):
            problems.append("bwrap is older than 0.5.0")
        # Every Linux sandbox runs in a private network namespace, which needs
        # far more than bwrap alone.
        for binary, why in (
            ("slirp4netns", "user-mode networking for the private namespace"),
            ("unshare", "creating the namespace"),
            ("nsenter", "entering the namespace"),
            ("iptables", "namespace network rules"),
            ("ip6tables", "namespace network rules"),
            ("iptables-restore", "namespace network rules"),
            ("ip6tables-restore", "namespace network rules"),
        ):
            if shutil.which(binary) is None:
                problems.append("%s not found on PATH (needed for %s)" % (binary, why))
        if shutil.which("unshare") and not self._version_at_least(
                "util-linux", ["unshare", "--version"], (2, 35)):
            problems.append(
                "util-linux is older than 2.35, so unshare lacks "
                "--map-current-user/--keep-caps")
        if not os.access("/dev/net/tun", os.R_OK | os.W_OK):
            problems.append("/dev/net/tun is not readable and writable")
        return problems

    @staticmethod
    def _version_at_least(label: str, argv: List[str], minimum) -> bool:
        """True unless the tool reports a version we can parse and it is too old.

        Unparseable output is treated as new enough: a version string we do not
        recognise is a weaker signal than a prerequisite we know is absent, and
        blocking every run over it would be worse than the risk it covers.
        """
        try:
            proc = subprocess.run(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return True
        text = (proc.stdout or b"").decode("utf-8", "replace")
        match = re.search(r"(\d+)\.(\d+)", text)
        if not match:
            return True
        return (int(match.group(1)), int(match.group(2))) >= minimum

    # -- policy -----------------------------------------------------------
    def policy(self) -> Dict[str, Any]:
        """The ``sandbox`` block Nezha writes into the per-issue settings.json."""
        return {
            "enabled": True,
            # The run's cwd is the worktree, so this is what scopes writes to it.
            "addCurrentWorkingDirectory": True,
            "allowBypass": self.allow_bypass,
            "allowDevToolAccess": self.allow_dev_tool_access,
            "sandboxMcpServers": self.sandbox_mcp_servers,
            "sandboxLspServers": self.sandbox_lsp_servers,
            "auth": {"git": self.auth_git, "gh": self.auth_gh},
            "userPolicy": {
                "filesystem": {
                    "readwritePaths": _expand(self.allow_write),
                    "readonlyPaths": _expand(self.readonly_paths),
                    "deniedPaths": _expand(self.deny_read),
                },
                "network": {
                    "allowOutbound": self.allow_network,
                    "allowLocalNetwork": self.allow_local_network,
                },
                "seatbelt": {"keychainAccess": self.keychain_access},
            },
        }

    def home_for(self, issue_id: str) -> str:
        return os.path.join(self.state_root, "copilot-home", _slug(issue_id))

    def ensure_home(self, issue_id: str) -> str:
        """Create the per-issue COPILOT_HOME and (re)write its sandbox policy.

        The policy is rewritten on every attempt. It lives outside every worktree
        so the agent cannot reach it, but rewriting keeps a resumed run from
        inheriting a policy that drifted from WORKFLOW.md.
        """
        home = self.home_for(issue_id)
        os.makedirs(home, exist_ok=True)
        for name in self.home_links:
            src = os.path.join(self.source_home, name)
            dst = os.path.join(home, name)
            if not os.path.exists(src):
                continue
            if os.path.islink(dst):
                if os.path.realpath(dst) == os.path.realpath(src):
                    continue
                os.unlink(dst)
            elif os.path.exists(dst):
                continue
            os.symlink(src, dst)

        settings_path = os.path.join(home, "settings.json")
        settings = {}  # type: Dict[str, Any]
        if os.path.isfile(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    settings = loaded
            except (ValueError, OSError):
                settings = {}
        settings["sandbox"] = self.policy()
        tmp = settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, sort_keys=True)
        os.replace(tmp, settings_path)
        return home

    def cleanup_home(self, issue_id: str) -> None:
        shutil.rmtree(self.home_for(issue_id), ignore_errors=True)

    # -- wrapping ---------------------------------------------------------
    def wrap(self, argv, workdir, env, dry_run=False):
        self.preflight()
        issue_id = env.get("NEZHA_ISSUE_ID") or env.get("NEZHA_ISSUE_IDENTIFIER") \
            or os.path.basename(os.path.realpath(workdir))
        home = self.home_for(issue_id) if dry_run else self.ensure_home(issue_id)
        new_env = dict(env)
        new_env["COPILOT_HOME"] = home
        wrapped = list(argv)
        # Flags go immediately after the binary; `-p <prompt>` must stay intact.
        wrapped[1:1] = ["--experimental", "--sandbox"]
        return wrapped, new_env, None

    def describe(self) -> str:
        return ("copilot-native (MXC/%s; writes confined to the worktree; "
                "%d denied read paths; outbound network %s; bypass %s)" % (
                    self.host_backend(), len(self.deny_read),
                    "allowed" if self.allow_network else "DENIED",
                    "allowed" if self.allow_bypass else "disabled"))


_BACKENDS = {
    "none": NoSandbox,
    "copilot-native": CopilotNativeSandbox,
    "sandbox-exec": SeatbeltSandbox,
    "docker": DockerSandbox,
}

#: Backends that confine the agent by re-entering Seatbelt. Two of them cannot be
#: stacked: ``sandbox_init`` rejects a profile applied inside another one.
NESTING_INCOMPATIBLE = ("copilot-native", "sandbox-exec")


def build_sandbox(config: Dict[str, Any]) -> Sandbox:
    backend = (config.get("backend") or "copilot-native").lower()
    if backend not in _BACKENDS:
        raise SandboxError("unknown sandbox.backend %r (available: %s)"
                           % (backend, ", ".join(sorted(_BACKENDS))))
    return _BACKENDS[backend](config)


def strip_secrets(env: Dict[str, str], names: List[str]) -> Dict[str, str]:
    """Remove secret env vars from the child environment entirely."""
    cleaned = dict(env)
    for name in names or []:
        cleaned.pop(name, None)
    return cleaned
