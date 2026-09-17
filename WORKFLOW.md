---
# ---------------------------------------------------------------------------
# Nezha runtime contract. Lives in-repo so the agent prompt and the runtime
# settings are versioned with the code.
# String values support ${ENV_VAR} and ${ENV_VAR:-default} indirection.
# ---------------------------------------------------------------------------

tracker:
  kind: file
  provider:
    path: ${NEZHA_BOARD:-./examples/board.json}
  active_states:
    - Todo
    - In Progress
    - Rework
  terminal_states:
    - Done
    - Cancelled
    - Closed
  required_labels: []

polling:
  interval_ms: 15000

workspace:
  repo: ${NEZHA_REPO:-.}
  root: ${NEZHA_WORKSPACE_ROOT:-~/code/nezha-workspaces}
  state_root: ~/.local/state/nezha
  base_ref: ${NEZHA_BASE_REF:-HEAD}
  branch_prefix: nezha/

hooks:
  after_create: |
    echo "workspace ready: $NEZHA_WORKSPACE (branch $NEZHA_BRANCH)"
  before_remove: |
    git -C "$NEZHA_WORKSPACE" status --short || true

agent:
  max_concurrent_agents: 2
  max_attempts: 3
  retry_backoff_ms: 30000
  timeout_sec: 1800

copilot:
  binary: copilot
  model: ${NEZHA_MODEL:-claude-haiku-4.5}
  reasoning_effort: null
  allow_all_tools: true
  # Keep file-path verification ON: the agent's own tools stay inside the worktree.
  allow_all_paths: false
  add_dir: []
  deny_tool: []
  # Values of these variables are stripped from the child env and redacted.
  secret_env_vars:
    - GITHUB_TOKEN
    - JIRA_API_TOKEN
  disable_builtin_mcps: false
  additional_mcp_config: null
  extra_args: []

sandbox:
  # copilot-native (default) | sandbox-exec (legacy, macOS) | docker | none
  #
  # copilot-native delegates to Copilot CLI's own OS-level command sandbox.
  # Its filesystem policy is an allow-list, and unlike sandbox-exec it can
  # actually deny egress: the CLI process stays outside the sandbox, so only the
  # commands it spawns lose the network.
  backend: ${NEZHA_SANDBOX:-copilot-native}
  # Outbound network for sandboxed commands. Safe to set false under
  # copilot-native; it breaks the agent outright under sandbox-exec and docker.
  allow_network: true
  allow_local_network: false
  # Unattended runs have nobody to approve a per-command escape hatch.
  # Copilot's own default is true; Nezha forces it off.
  allow_bypass: false
  # Grant the caches and registry config that builds need (npm, cargo, maven...).
  allow_dev_tool_access: true
  # Run local MCP and language servers inside the sandbox too.
  sandbox_mcp_servers: true
  sandbox_lsp_servers: true
  # macOS: keep the login keychain out of the sandbox.
  keychain_access: false
  # git credentials are needed to commit; gh is not, by default.
  auth_git: true
  auth_gh: false
  deny_read:
    - ~/.ssh
    - ~/.aws
    - ~/.gnupg
    - ~/.kube
    - ~/.netrc
    - ~/.npmrc
    - ~/.git-credentials
    - ~/.config/gh
    - ~/.config/gcloud
    - ~/Library/Keychains
  allow_write: []
  readonly_paths: []
  # Linux needs far more than bwrap (slirp4netns, iptables, /dev/net/tun...).
  # doctor checks the documented list; set this true if the check is wrong
  # for your host -- but a host that cannot sandbox fails every command.
  skip_host_prereq_check: false
  image: nezha-agent:latest
---

You are working on ticket `{{ issue.identifier }}` in an isolated git worktree.

## Ticket

- Identifier: {{ issue.identifier }}
- Title: {{ issue.title }}
- State: {{ issue.state }}
- Link: {{ issue.url }}

{{ issue.description }}

## Workspace

- Working directory: `{{ workspace.path }}` (you are already here)
- Branch: `{{ workspace.branch }}`, cut from `{{ base_ref }}`
- Stay inside this directory. Do not touch other checkouts on this machine.

{% if attempt %}
## Follow-up context

This is attempt #{{ attempt }} — either a continuation or a retry after a failure.
Resume from the current workspace state instead of starting over. Do not repeat
investigation or validation that already succeeded unless new code changes require it.
{% endif %}

## What to do

1. Read the ticket and the surrounding code before changing anything.
2. Make the smallest change that fully addresses the ticket.
3. Run the project's existing build, lint and test commands. Discover them from
   the repo (Makefile, package.json, pyproject.toml, CI config) — do not invent them.
4. Commit on the current branch with a clear message. Do not force-push and do not
   push to any branch other than `{{ workspace.branch }}`.

## Definition of done

Before you end the turn, state explicitly:

- **Changed**: files touched and why.
- **Verified**: which commands you ran and their results.
- **Not verified**: anything you could not check, and why.
- **Risk**: what a reviewer should look at first.

If you are blocked by missing access or an ambiguous requirement, say so plainly
and stop rather than guessing. A clear blocked report is a successful outcome.
