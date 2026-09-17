"""WORKFLOW.md loader: YAML front matter + prompt template.

Loader and config layer in one: the runtime contract lives in-repo and is
versioned with the code.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class WorkflowError(ValueError):
    """Raised when WORKFLOW.md is missing, malformed, or fails validation."""


# --------------------------------------------------------------------------
# Template engine (deliberately a tiny Liquid subset -- see README "Known gaps")
# Supported: {{ dotted.path }}, {% if path %} / {% else %} / {% endif %}
# --------------------------------------------------------------------------

TOKEN_RE = re.compile(r"\{\{\s*(?P<var>[\w.]+)\s*\}\}|\{%\s*(?P<tag>.+?)\s*%\}")


def _lookup(context: Dict[str, Any], path: str) -> Any:
    node: Any = context
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            node = getattr(node, part, None)
        if node is None:
            return None
    return node


def _truthy(value: Any) -> bool:
    return value not in (None, False, "", 0, [], {})


def _parse_nodes(tokens: List[Tuple[str, str]], index: int, stop: Tuple[str, ...]):
    nodes: List[Any] = []
    while index < len(tokens):
        kind, value = tokens[index]
        if kind == "tag":
            parts = value.split(None, 1)
            if not parts:
                raise WorkflowError("empty template tag: {%% %s %%}" % value)
            head = parts[0]
            if head in stop:
                return nodes, index
            if head == "if":
                expr = parts[1] if len(parts) > 1 else ""
                if not expr.strip():
                    raise WorkflowError("`{% if %}` requires an expression")
                body, index = _parse_nodes(tokens, index + 1, ("else", "endif"))
                alt: List[Any] = []
                if tokens[index][1].split(None, 1)[0] == "else":
                    alt, index = _parse_nodes(tokens, index + 1, ("endif",))
                nodes.append(("if", expr.strip(), body, alt))
                index += 1  # consume endif
                continue
            raise WorkflowError("unsupported template tag: {%% %s %%}" % value)
        nodes.append((kind, value))
        index += 1
    if stop:
        raise WorkflowError("unclosed template tag; expected one of %s" % (stop,))
    return nodes, index


def _render_nodes(nodes: List[Any], context: Dict[str, Any]) -> str:
    out: List[str] = []
    for node in nodes:
        if node[0] == "text":
            out.append(node[1])
        elif node[0] == "var":
            value = _lookup(context, node[1])
            out.append("" if value is None else str(value))
        elif node[0] == "if":
            _, expr, body, alt = node
            branch = body if _truthy(_lookup(context, expr)) else alt
            out.append(_render_nodes(branch, context))
    return "".join(out)


def render_template(template: str, context: Dict[str, Any]) -> str:
    """Render the prompt body against ``context``."""
    tokens: List[Tuple[str, str]] = []
    pos = 0
    for match in TOKEN_RE.finditer(template):
        if match.start() > pos:
            tokens.append(("text", template[pos:match.start()]))
        if match.group("var") is not None:
            tokens.append(("var", match.group("var")))
        else:
            tokens.append(("tag", match.group("tag")))
        pos = match.end()
    if pos < len(template):
        tokens.append(("text", template[pos:]))
    nodes, _ = _parse_nodes(tokens, 0, ())
    return _render_nodes(nodes, context)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def expand_env(value: Any, environ: Optional[Dict[str, str]] = None) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` in string values."""
    env = os.environ if environ is None else environ
    if isinstance(value, str):
        def sub(match: "re.Match") -> str:
            name, default = match.group(1), match.group(2)
            resolved = env.get(name)
            if resolved is None:
                if default is None:
                    raise WorkflowError(
                        "environment variable %s referenced by WORKFLOW.md is not set" % name
                    )
                return default
            return resolved
        return ENV_REF_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    return value


DEFAULTS: Dict[str, Dict[str, Any]] = {
    "tracker": {"kind": "file", "active_states": ["Todo", "In Progress"],
                "terminal_states": ["Done", "Cancelled", "Closed"], "required_labels": []},
    "polling": {"interval_ms": 15000},
    "workspace": {"root": "~/code/nezha-workspaces", "base_ref": "origin/main",
                  "branch_prefix": "nezha/"},
    "hooks": {},
    "agent": {"max_concurrent_agents": 2, "max_attempts": 3, "retry_backoff_ms": 30000,
              "timeout_sec": 3600},
    "copilot": {"binary": "copilot", "model": None, "reasoning_effort": None,
                "allow_all_tools": True, "allow_all_paths": False, "add_dir": [],
                "deny_tool": [], "available_tools": [], "secret_env_vars": [],
                "additional_mcp_config": None, "disable_builtin_mcps": False,
                "max_autopilot_continues": None, "extra_args": []},
    "sandbox": {"backend": "copilot-native", "allow_network": True, "deny_read": [],
                "allow_write": [], "readonly_paths": [], "image": "nezha-agent:latest",
                "docker_args": [], "allow_local_network": False, "allow_bypass": False,
                "allow_dev_tool_access": True, "sandbox_mcp_servers": True,
                "sandbox_lsp_servers": True, "keychain_access": False,
                "auth_git": True, "auth_gh": False, "copilot_home_links": []},
}

_REQUIRED_STATE_KEYS = ("active_states", "terminal_states")


class Workflow(object):
    """Parsed WORKFLOW.md: typed config getters plus the prompt template."""

    def __init__(self, config: Dict[str, Any], prompt_template: str, source: str = "<memory>"):
        self.config = config
        self.prompt_template = prompt_template
        self.source = source

    # -- section accessors ------------------------------------------------
    @property
    def tracker(self) -> Dict[str, Any]:
        return self.config["tracker"]

    @property
    def polling(self) -> Dict[str, Any]:
        return self.config["polling"]

    @property
    def workspace(self) -> Dict[str, Any]:
        return self.config["workspace"]

    @property
    def hooks(self) -> Dict[str, Any]:
        return self.config["hooks"]

    @property
    def agent(self) -> Dict[str, Any]:
        return self.config["agent"]

    @property
    def copilot(self) -> Dict[str, Any]:
        return self.config["copilot"]

    @property
    def sandbox(self) -> Dict[str, Any]:
        """Sandbox config, with the two values the backends need from elsewhere.

        ``copilot-native`` stores its per-issue ``COPILOT_HOME`` under the same
        state root as run state, and probes the same binary the runner invokes.
        Injecting them here keeps WORKFLOW.md from having to repeat either.
        """
        merged = dict(self.config["sandbox"])
        merged.setdefault("state_root", self.workspace.get("state_root"))
        merged.setdefault("copilot_binary", self.copilot.get("binary"))
        return merged

    @property
    def poll_interval_sec(self) -> float:
        return float(self.polling["interval_ms"]) / 1000.0

    @property
    def retry_backoff_sec(self) -> float:
        return float(self.agent["retry_backoff_ms"]) / 1000.0

    def render_prompt(self, context: Dict[str, Any]) -> str:
        return render_template(self.prompt_template, context)

    # -- construction -----------------------------------------------------
    @classmethod
    def parse(cls, text: str, source: str = "<memory>",
              environ: Optional[Dict[str, str]] = None) -> "Workflow":
        match = FRONT_MATTER_RE.match(text)
        if not match:
            raise WorkflowError(
                "%s: expected YAML front matter delimited by '---' lines" % source
            )
        raw_yaml, body = match.group(1), match.group(2)
        try:
            parsed = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError as exc:
            raise WorkflowError("%s: invalid YAML front matter: %s" % (source, exc))
        if not isinstance(parsed, dict):
            raise WorkflowError("%s: front matter must be a mapping" % source)

        config: Dict[str, Any] = {}
        for section, defaults in DEFAULTS.items():
            merged = dict(defaults)
            supplied = parsed.get(section)
            if supplied is None:
                supplied = {}
            if not isinstance(supplied, dict):
                raise WorkflowError("%s: section '%s' must be a mapping" % (source, section))
            merged.update(supplied)
            config[section] = merged
        for unknown in set(parsed) - set(DEFAULTS):
            config[unknown] = parsed[unknown]

        config = expand_env(config, environ)
        wf = cls(config, body, source)
        wf.validate()
        return wf

    @classmethod
    def load(cls, path: str, environ: Optional[Dict[str, str]] = None) -> "Workflow":
        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            raise WorkflowError("WORKFLOW.md not found at %s" % expanded)
        with open(expanded, "r", encoding="utf-8") as handle:
            return cls.parse(handle.read(), expanded, environ)

    # -- validation -------------------------------------------------------
    def validate(self) -> None:
        src = self.source
        for key in _REQUIRED_STATE_KEYS:
            value = self.tracker.get(key)
            if not isinstance(value, list) or not value:
                raise WorkflowError("%s: tracker.%s must be a non-empty list" % (src, key))
        overlap = set(self.tracker["active_states"]) & set(self.tracker["terminal_states"])
        if overlap:
            raise WorkflowError(
                "%s: states cannot be both active and terminal: %s" % (src, sorted(overlap))
            )
        if int(self.agent["max_concurrent_agents"]) < 1:
            raise WorkflowError("%s: agent.max_concurrent_agents must be >= 1" % src)
        if int(self.agent["max_attempts"]) < 1:
            raise WorkflowError("%s: agent.max_attempts must be >= 1" % src)
        if float(self.polling["interval_ms"]) <= 0:
            raise WorkflowError("%s: polling.interval_ms must be > 0" % src)
        backend = self.sandbox["backend"]
        if backend not in ("copilot-native", "sandbox-exec", "docker", "none"):
            raise WorkflowError(
                "%s: sandbox.backend must be one of "
                "copilot-native|sandbox-exec|docker|none, got %r" % (src, backend)
            )
        if backend != "copilot-native" and not self.sandbox.get("allow_network", True):
            raise WorkflowError(
                "%s: sandbox.allow_network: false is only supported by the "
                "copilot-native backend. Under %r the whole CLI sits inside the "
                "sandbox, so denying egress also cuts Copilot off from its own "
                "API and every run fails." % (src, backend)
            )
        if not self.prompt_template.strip():
            raise WorkflowError("%s: prompt body is empty" % src)
        # Fail fast on template syntax rather than at dispatch time.
        render_template(self.prompt_template, {})
