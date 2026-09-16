# Nezha

Nezha turns tracker tickets into isolated, sandboxed Copilot CLI runs. It is a
ticket-driven orchestrator built on **GitHub Copilot CLI**.

It polls an issue tracker, creates one git worktree per ticket, runs
`copilot -p --output-format json` inside an OS-level sandbox, parses the JSONL
event stream, and retries with backoff — persisting enough state to survive a
restart without a database.

> **Engineering preview.** Read [Security posture](#security-posture) before
> pointing this at anything you care about.

---

## Why this exists

A git worktree gives you *collision* isolation. It gives you **zero** privilege
isolation: an agent running `bash` inside a worktree is still your user and can
read `~/.ssh`, reach any host, and delete any repo on the machine. Some agent
runtimes close that gap with a built-in workspace-write sandbox; Copilot CLI
has no equivalent.

Nezha adds the missing layer.

| Layer | Mechanism | Prevents |
|---|---|---|
| Code isolation | `git worktree` | branch/working-tree collisions |
| Tool path scoping | Copilot `--add-dir`, path verification on | agent tools touching unrelated files |
| **Process privilege** | **`sandbox-exec` / Docker** | **credential theft, out-of-tree damage** |

---

## Quick start

```bash
uv venv .venv && uv pip install -e . pytest      # or: python3 -m venv .venv && pip install -e .

export NEZHA_REPO=/path/to/your/repo             # repository to cut worktrees from
.venv/bin/python -m nezha.cli doctor             # validate config, binary, sandbox, tracker
.venv/bin/python -m nezha.cli issues             # what is eligible right now
.venv/bin/python -m nezha.cli plan DEMO-1        # exact argv + seatbelt profile, runs nothing
.venv/bin/python -m nezha.cli once               # one tick, wait for completion
.venv/bin/python -m nezha.cli run                # daemon
```

The shipped `WORKFLOW.md` is wired to the file-backed demo board in
`examples/board.json`, so `once` works with no credentials at all.

### Commands

| Command | Purpose |
|---|---|
| `doctor` | validate WORKFLOW.md, the `copilot` binary, the sandbox and the tracker |
| `issues` | list tracker issues and mark which are eligible |
| `prompt <id>` | render the prompt for one issue, run nothing |
| `plan [id]` | print the full sandboxed command line and seatbelt profile |
| `once` | single poll tick, wait for all runs, exit non-zero on failure |
| `run` | poll forever until SIGINT/SIGTERM |
| `workspaces` | list known workspaces and their last run |
| `rm <id>` | remove a workspace and its state |
| `board <id> <state>` | flip an issue state (file tracker only; testing helper) |

---

## Architecture

```
tracker.fetch_all()            one call per tick, all queries derived from it
      |
      v
Orchestrator.tick()
      |-- _startup_cleanup     remove worktrees for already-terminal issues
      |-- _release_terminal    drop settled runs for terminal issues
      |-- _reconcile           kill runs whose issue left the active states
      `-- dispatch             bounded by agent.max_concurrent_agents
                |
                v
      WorkspaceManager.ensure()          git worktree add -b nezha/<id>
                |
                v
      Workflow.render_prompt()           WORKFLOW.md body + issue context
                |
                v
      Sandbox.wrap()                     sandbox-exec -f <profile> ...
                |
                v
      CopilotRunner.run()                copilot -p --output-format json
                |
                v
      parse_events()                     JSONL -> RunResult -> retry/backoff
```

Module map:

| File | Role |
|---|---|
| `nezha/workflow.py` | `WORKFLOW.md` loader: YAML front matter, env indirection, template |
| `nezha/tracker/` | read-only adapters (`file`, `jira`) over a normalized `Issue` |
| `nezha/workspace.py` | worktree lifecycle, hooks, path safety, state files |
| `nezha/sandbox.py` | `sandbox-exec` / `docker` / `none` backends |
| `nezha/runner.py` | argv construction, process supervision, JSONL folding |
| `nezha/orchestrator.py` | poll, dispatch, retry, reconcile, cancel |
| `nezha/cli.py` | operator commands |

### Boundary

Nezha is a **scheduler, runner and tracker reader**. It never writes to the
tracker. Ticket transitions, comments and PR links are the agent's job via
provider-native MCP tools. Keep it that way — it is what lets you swap trackers
without touching the orchestrator.

---

## Event contract

Verified against Copilot CLI **1.0.85**. Each stdout line from
`--output-format json` is one JSON object:

| Field / type | Meaning |
|---|---|
| `"ephemeral": true` | streaming delta (`*_delta`, reasoning). Dropped. |
| `assistant.turn_end` | one completed turn |
| `tool.execution_complete` | `data.success` drives the tool-failure counter |
| `assistant.message` | durable assistant output; last one wins |
| `result` (final line) | `exitCode`, `sessionId`, `usage.premiumRequests`, `usage.codeChanges.filesModified` |

`sessionId` is captured and replayed as `--resume <id>` on the next attempt, so
retries continue the conversation instead of restarting it.

This schema is **not a documented stability contract**. If Copilot CLI changes
it, `parse_events` is the single place to adapt; `tests/test_runner.py` pins the
current shape.

---

## Security posture

`sandbox.backend` selects one of three levels. **Pick deliberately.**

### `sandbox-exec` (default, macOS)

Apple Seatbelt — the OS mechanism behind workspace-confinement sandboxes on
macOS. The generated profile:

- denies **all** writes, then re-allows the worktree, the parent repo's git
  common dir, the system temp dir, `~/.copilot`, `~/.cache` and Nezha's state dir;
- denies **reads** of `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.netrc`,
  `~/.npmrc`, `~/.git-credentials`, `~/.config/gh`, `~/.config/gcloud` and
  `~/Library/Keychains`;
- leaves network open.

Verified by `tests/test_sandbox.py`, which asserts real kernel enforcement:
writes outside the worktree fail, secret reads fail, and `git commit` inside the
worktree still works.

**Limitations you are accepting:**

1. `sandbox-exec` is formally deprecated by Apple. It still works; it may not forever.
2. The profile is `(allow default)` + deny rules, i.e. a **deny-list**. An
   unlisted secret store is readable. Audit `sandbox.deny_read` for your machine.
3. **No egress control.** Copilot needs the network to reach the API, so
   `allow_network: false` breaks the agent outright. An agent that exfiltrates
   data over HTTPS is not stopped by this backend.
4. `~/.gitconfig` is deliberately readable — git refuses to run without it. If
   yours names a credential helper, that helper is reachable.
5. A worktree shares the parent repo's object store, and the profile must grant
   write access to it. **A hostile agent can corrupt the parent repository.** Use
   a dedicated clone as `workspace.repo` for untrusted work.

### `docker`

Separate filesystem, PID and network namespace — the only backend that can do
egress allow-listing. **UNVERIFIED:** Docker was not installed on the
development host, so this path has argv-shape tests only, no execution tests.
You must build an image containing `copilot` plus your toolchain.

### `none`

No isolation whatsoever. `doctor` reports it as a problem on purpose. Only
acceptable when a human reviews every diff before it lands.

### Also note

- `copilot.allow_all_paths` is `false` by default: Copilot's own path
  verification stays on as a second layer behind the sandbox.
- `copilot.secret_env_vars` values are removed from the child environment *and*
  passed to `--secret-env-vars` for redaction.
- Orchestrator state lives in `~/.local/state/nezha`, outside every worktree, so
  a run cannot forge its own attempt count or status.

---

## WORKFLOW.md

The runtime contract lives in-repo and is versioned with the code. String values
support `${VAR}` and `${VAR:-default}`.

| Section | Notable keys |
|---|---|
| `tracker` | `kind`, `provider`, `active_states`, `terminal_states`, `required_labels` |
| `polling` | `interval_ms` |
| `workspace` | `repo`, `root`, `state_root`, `base_ref`, `branch_prefix` |
| `hooks` | `after_create`, `before_remove` (bash, run in the worktree) |
| `agent` | `max_concurrent_agents`, `max_attempts`, `retry_backoff_ms`, `timeout_sec` |
| `copilot` | `model`, `reasoning_effort`, `allow_all_tools`, `allow_all_paths`, `add_dir`, `deny_tool`, `available_tools`, `secret_env_vars`, `additional_mcp_config`, `extra_args` |
| `sandbox` | `backend`, `allow_network`, `deny_read`, `allow_write`, `image` |

The body is the prompt template. Supported syntax is a **small Liquid subset**:
`{{ dotted.path }}` and `{% if path %}` / `{% else %}` / `{% endif %}` (nesting
allowed). No loops, no filters. Template errors fail at load time, not at dispatch.

Context exposed to the template: `issue.*`, `workspace.path`, `workspace.branch`,
`base_ref`, and `attempt` (unset on the first try, so `{% if attempt %}` gates
retry-only guidance).

Hooks receive `NEZHA_WORKSPACE`, `NEZHA_BRANCH`, `NEZHA_ISSUE_ID`,
`NEZHA_ISSUE_IDENTIFIER`.

---

## Trackers

**`file`** — a JSON board on disk. Fully implemented and tested. Use it to
rehearse the whole loop with zero credentials.

**`jira`** — Jira Cloud REST, JQL-driven, **verified against a live site**:
`fetch_all()` paged 865 real issues and `doctor`/`issues`/`prompt` all render
them. Jira **Server/Data Center is not supported** — it serves
`/rest/api/2/search` with `startAt`/`total` paging and wiki-markup instead of
ADF.

`auth: basic` (default) — an Atlassian API token against the site host:

```yaml
tracker:
  kind: jira
  provider:
    site_url: https://your-org.atlassian.net
    email: ${JIRA_EMAIL}
    token: ${JIRA_API_TOKEN}
    jql: project = ABC AND labels = nezha
```

`auth: bearer` — an OAuth 3LO token with `read:jira-work`. This mode **must**
go through the `api.atlassian.com` gateway (hence `cloud_id`); the same token
returns 401 on the site host, which stays in `site_url` purely for browse links.
`token_command` is re-read every `token_ttl_sec` (default 60), so rotating
tokens survive without a restart and a single paged fetch spawns it once:

```yaml
tracker:
  kind: jira
  provider:
    site_url: https://your-org.atlassian.net
    auth: bearer
    cloud_id: ${JIRA_CLOUD_ID}
    token_command: ./examples/atlassian-mcp-token.sh
    jql: assignee = currentUser() AND labels = nezha
```

A worked config is in `examples/WORKFLOW.jira.md`.
`examples/atlassian-mcp-token.sh` reuses the token Copilot CLI already holds for
the Atlassian MCP server, so you need no second credential — read its header
first, it depends on an undocumented on-disk layout and does not refresh.

> **Keep the JQL narrow.** Every tick pages the entire result set. A broad
> `assignee = currentUser()` cost 9 requests and ~40 s per poll here; pair it
> with `statusCategory != Done` and `required_labels`.

Adding an adapter means subclassing `Tracker` and implementing `fetch_all()`;
eligibility, reconciliation and cleanup are derived for free.

---

## Observability

Human logs by default, `--json-logs` for machine consumption, `--log-file` to
append. Per attempt, under `~/.local/state/nezha/runs/<id>/attempt-N-<ts>/`:

- `events.jsonl` — the complete raw Copilot event stream
- `command.json` — exact argv, workdir and sandbox description
- `stderr.log` — written whenever stderr is non-empty

Per workspace, `~/.local/state/nezha/<id>.json` holds attempts, `session_id`,
status and the last run summary.

---

## Verification status

`133 passed` on Python 3.12 / macOS 26.0 (arm64), run three times for flakiness.

| Area | Coverage |
|---|---|
| Workflow loader + template | parsing, defaults, env indirection, validation, nesting, error paths |
| Trackers | normalization, eligibility, label filters, every error branch |
| Jira adapter | request shape, token paging, repeated-token and page-cap guards, HTTP/URL/non-JSON/non-object failures, sparse rows, **basic vs bearer routing** (gateway host, browse host, `token_command` caching and failures), **ADF nodes sampled from a live site** (media, hardBreak, inlineCard, bullets, link marks) |
| Workspace | create, **injective slug (no collisions)**, idempotent reuse, **cross-issue reuse refused**, hooks, path-escape rejection, removal |
| Sandbox | **real kernel enforcement** — write allowed in-tree, blocked out-of-tree, secret reads blocked, `git commit` works |
| Runner | argv matrix, JSONL folding, **watchdog timeout against a silent child**, **1 MB stderr without deadlock**, process-group kill, missing binary, stderr classification |
| CLI | flag ordering before and after the subcommand, every command, exit codes |
| Orchestrator | dispatch, concurrency cap, retry/backoff/exhaustion, cancellation, startup cleanup, resilience |

End-to-end, with a real Copilot agent on a scratch repo: worktree created →
agent wrote `greet.py` + `test_greet.py` → committed on `nezha/DEMO-1` →
its tests pass → parent repo untouched → no sandbox escape residue.

An independent `code-review` pass found four defects, all fixed with regression
tests: a stderr-pipe deadlock, an inert run timeout, slug collisions between
distinct issues, and a template parser `IndexError`.

The Jira adapter was then run against a live Jira Cloud site. Paging and every
issue field matched, but ADF flattening lost real content — `hardBreak` line
structure, `inlineCard` URLs, bullet markers and media placeholders were all
dropped, silently reducing one real ticket to an empty description. Fixed, with
the observed node types pinned as tests. `doctor` reported
`jira: 865 issues, 0 eligible` over 9 pages; `issues` and `prompt` rendered real
tickets, links included.

**Not verified:** the Docker backend (Docker not installed, marked `UNVERIFIED`
in source), `auth: basic` against a live site (only `auth: bearer` was exercised
live; Basic is covered by stubbed-transport tests), and a long-running `run`
daemon.

---

## Known gaps

| Gap | Impact |
|---|---|
| No egress allow-listing under `sandbox-exec` | an agent can exfiltrate over HTTPS |
| Docker backend untested | may need argv fixes on first real use |
| Jira `auth: basic` unexercised live | may fail on first real call (401/proxy/SSO) |
| Jira Server/DC unsupported | different API version, paging and markup |
| Every tick pages the whole JQL result | a broad query costs seconds and many requests per poll |
| Restart loses in-memory scheduler state | attempt counters reload from disk, but in-flight runs are orphaned |
| No dashboard | logs and `workspaces` only |
| Template subset | no loops or filters; add them if prompts grow |

---

## Prior art

The ticket-to-isolated-run model is not original to Nezha; it follows the
publicly published [Symphony](https://github.com/openai/symphony) SPEC. Nezha
is an independent Python implementation targeting GitHub Copilot CLI and shares
no code with it. Not affiliated with or endorsed by that project.

---

## License

Apache-2.0.
