---
# ---------------------------------------------------------------------------
# Nezha runtime contract. Lives in-repo so the agent prompt and the runtime
# settings are versioned with the code.
# String values support ${ENV_VAR} and ${ENV_VAR:-default} indirection.
# ---------------------------------------------------------------------------

tracker:
  kind: jira
  provider:
    # Browse links and, under auth=basic, the API host too.
    site_url: ${JIRA_SITE_URL:-https://your-org.atlassian.net}
    # auth=bearer sends an OAuth 3LO token to the api.atlassian.com gateway.
    # The same token is rejected by site_url, so cloud_id is mandatory here.
    auth: bearer
    cloud_id: ${JIRA_CLOUD_ID}
    token_command: ./examples/atlassian-mcp-token.sh
    # Keep this narrow: nezha pages the whole result set every tick.
    jql: ${JIRA_JQL:-assignee = currentUser() AND statusCategory != Done}
  # Status names as they appear in the Jira workflow, not status categories.
  active_states:
    - Open
    - Coding
    - In Progress
    - Rework
  terminal_states:
    - Code Review
    - Done
    - Closed
    - Cancelled
  # Strongly recommended: without an opt-in label the daemon will pick up
  # every ticket the JQL returns.
  required_labels:
    - nezha

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
  # sandbox-exec (macOS Seatbelt) | docker | none
  backend: ${NEZHA_SANDBOX:-sandbox-exec}
  # Copilot needs network to reach the API; false will break the agent.
  allow_network: true
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
