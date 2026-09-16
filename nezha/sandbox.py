"""Isolation backends -- the layer a git worktree does *not* give you.

Three backends, weakest to strongest:

``none``
    No process isolation. The agent runs with your full user privileges and can
    read ``~/.ssh``, reach any host, and touch any repo. Only acceptable when
    you review every diff before it lands.

``sandbox-exec`` (default on macOS)
    Apple Seatbelt. Confines *writes* to the worktree and a small allowlist, and
    denies *reads* of known credential stores. This is the OS mechanism behind
    workspace-confinement sandboxes on macOS.

    Honest limitations:
      * ``sandbox-exec`` is formally deprecated by Apple (still functional).
      * The profile is ``(allow default)`` plus deny rules, i.e. a deny-list.
        A deny-list is weaker than an allow-list: an unlisted secret store is
        readable. Extend ``sandbox.deny_read`` for your machine.
      * No per-domain egress control. Copilot needs network to reach the API,
        so ``allow_network: false`` breaks the agent outright.
      * Worktrees share the parent repo's object store, so the profile must
        grant write access to ``<repo>/.git``. A hostile agent can corrupt the
        parent repository. Use a dedicated clone for untrusted work.

``docker``
    Strongest of the three: separate filesystem, PID and network namespace.
    Requires Docker/Colima installed and an image with your toolchain.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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

    def wrap(self, argv: List[str], workdir: str,
             env: Dict[str, str]) -> Tuple[List[str], Dict[str, str], Optional[str]]:
        """Return ``(argv, env, tempfile_to_clean)`` for the confined command."""
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class NoSandbox(Sandbox):
    name = "none"

    def wrap(self, argv, workdir, env):
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

    def wrap(self, argv, workdir, env):
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

    def wrap(self, argv, workdir, env):
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


_BACKENDS = {
    "none": NoSandbox,
    "sandbox-exec": SeatbeltSandbox,
    "docker": DockerSandbox,
}


def build_sandbox(config: Dict[str, Any]) -> Sandbox:
    backend = (config.get("backend") or "sandbox-exec").lower()
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
