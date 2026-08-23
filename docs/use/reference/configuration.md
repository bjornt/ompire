# Configuration

Ompire reads `~/.config/ompire/config.toml` once at startup. The file is
optional — every key has a default. Unknown keys, malformed TOML, and wrong
value types make the daemon exit with a message naming the offending key.

Changing configuration requires a daemon restart, except for the three keys
that are also editable at runtime (marked below).

## Network

| Key | Type | Default | Notes |
|---|---|---|---|
| `port` | integer | `4173` | |
| `bind` | string | `"127.0.0.1"` | Localhost binding is the single-user security boundary. Changing it exposes the daemon and its bearer token to the network. |

## Paths

| Key | Type | Default | Notes |
|---|---|---|---|
| `data_dir` | string | `$SNAP_USER_DATA`, else `$XDG_DATA_HOME/ompire` | Holds the database, the bearer token, and the audit log. |
| `task_dir_root` | string | `~/tasks` | Task clones live under `<root>/<project>/<slug>`. Cleanup refuses paths outside this root. |
| `checkout_root` | string | `~/proj` | Default parent for a project's `checkout_path`. |

Paths are expanded, so `~` works.

## Spawning

| Key | Type | Default | Notes |
|---|---|---|---|
| `default_branch_pattern` | string | `"ompire/<slug>"` | Used when a template does not override it. |
| `spawn_step_timeout` | integer (s) | `120` | Applies to the Git steps. |
| `workshop_step_timeout` | integer (s) | `600` | Much larger than the Git timeout because container launch includes SDK installation. |
| `my_workshop_command` | list of strings | `["my-workshop"]` | Must be non-empty. |

## Agents

| Key | Type | Default | Notes |
|---|---|---|---|
| `agent_env` | table of strings | `{}` | Forwarded into the agent's command line, unfiltered. Not the intended way to supply model credentials. See the warning below. |
| `agent_ready_timeout` | integer (s) | `30` | Positive. Covers container-side agent startup. |
| `agent_ring_buffer_size` | integer | `1000` | Positive. Retained raw events per session. |
| `judge_model` | string or unset | unset | Model for the workflow engine's LLM-judge step. Unset means the agent's configured default. |

**On `agent_env`:** do not put credentials here.

Agents get model authentication through the `pi-auth-gateway` tunnel declared
in `workshop.yaml`, not through this key. That is the supported path, and the
intended direction is for it to become the only one. `agent_env` exists
because early testing found the agent refuses to start without credential
environment variables in deployments that have no gateway — it is a fallback,
not the design.

Two things to understand before using it anyway:

- **The daemon does not inspect what you put here.** There is no allowlist and
  no name matching. It validates that keys and values are strings and forwards
  them.
- **Values end up in a process command line, not just an environment.** The
  daemon builds `workshop exec ... -- env KEY=VALUE omp ...`, so anything here
  is visible in the host process table (`ps auxww`, `/proc/<pid>/cmdline`) to
  every process running as your user — including the agent itself and anything
  it spawns.

Treat any value placed here as disclosed to the agent and to anything sharing
your user account.

## Sessions and attention

| Key | Type | Default | Notes |
|---|---|---|---|
| `session_idle_debounce` | number (s) | `2.0` | Non-negative. Prevents a chained turn from flickering through `idle`. |
| `stall_threshold` | number (s) | `300` | Positive. Silence past this marks a working session `stalled`. Runtime-editable. |
| `renotify_interval` | number (s) | `300` | Positive. Re-notification interval for an unanswered `notify` or `interrupt` entry. Runtime-editable. |
| `context_advisory_threshold` | integer | `80` | In `(0, 100]`. Context-percent crossing that fires a `context-high` advisory. Runtime-editable. |
| `stats_throttle_interval` | number (s) | `10` | Non-negative. Minimum spacing between `stats` events for one task. |
| `notifications_enabled` | boolean | `true` | Turns desktop notifications off. Badge counts are unaffected. |

## Review and publishing

| Key | Type | Default | Notes |
|---|---|---|---|
| `llmvet_command` | list of strings | `["llmvet"]` | Must be non-empty. |
| `review_port_range` | `[low, high]` | `[7180, 7280]` | Positive integers, `low <= high`. Probed with an ephemeral bind so concurrent reviews do not collide. |
| `gpg_signing_key` | string or unset | unset | Required before any task can ship. |
| `gh_command` | list of strings | `["gh"]` | Must be non-empty. |
| `pr_poll_interval` | number (s) | `60` | Positive. Spacing between pull-request state polls. |

## Lifecycle

| Key | Type | Default | Notes |
|---|---|---|---|
| `shutdown_grace` | number (s) | `10.0` | Positive. SIGTERM-to-SIGKILL grace for agent children, long enough for the agent to flush its session file. |
| `recovery_concurrency` | integer | `4` | Positive. Concurrent session resumes at startup. Deliberately small — each is a real container-side startup. |

## Runtime-editable settings

Fifteen settings can be changed from the UI without a restart. Resolution
order is registry override, then `config.toml`, then the built-in default.

Three are also seedable from `config.toml`:

| Setting | `config.toml` default |
|---|---|
| `renotify_interval` | yes |
| `stall_threshold` | yes |
| `context_advisory_threshold` | yes |

The other twelve are the attention-tier preferences — three booleans for each
of the four tiers. They are **default-only**: they cannot be set in
`config.toml`, only overridden at runtime.

| Setting | Default |
|---|---|
| `tier.interrupt.desktop` / `.sound` / `.badge` | `true` / `true` / `true` |
| `tier.notify.desktop` / `.sound` / `.badge` | `true` / `false` / `true` |
| `tier.badge.desktop` / `.sound` / `.badge` | `false` / `false` / `true` |
| `tier.silent.desktop` / `.sound` / `.badge` | `false` / `false` / `false` |

`desktop` controls whether the tier fires a desktop notification, `sound`
raises it to critical urgency (the freedesktop-standard sound trigger), and
`badge` controls whether it counts toward the "N need you" badge.

Ompire never rewrites your TOML. A runtime change is stored separately, so
your file keeps its comments and cannot be corrupted by the daemon. Clearing
the override with `DELETE /api/settings/{key}` falls back to whatever your
file says, or the default if it says nothing.

Everything else is TOML-only. Infrastructure settings — ports, paths, commands,
credentials — are not editable from a browser.

## Example

```toml
# Signing is required before any task can ship.
gpg_signing_key = "3AA5C34371567BD2"

checkout_root = "~/src"
task_dir_root = "~/tasks"

default_branch_pattern = "ompire/<slug>"

# Long builds; don't call them stalled too eagerly.
stall_threshold = 900

# Non-secret values only — see the warning above. Model credentials come
# from the auth gateway, not from here.
[agent_env]
SOME_TOOL_ENDPOINT = "https://internal.example"
```
