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
| `data_dir` | string | `$SNAP_USER_COMMON`, else `$XDG_DATA_HOME/ompire` | Holds the database, the bearer token, and the audit log. Under the snap that is `~/snap/ompire/common`, which every revision shares. Setting this key also suppresses the [upgrade carry-forward](../how-to/troubleshoot.md#the-ui-is-empty-after-a-snap-upgrade). |
| `task_dir_root` | string | `~/tasks` | Task clones live under `<root>/<project>/<slug>`. Cleanup refuses paths outside this root. |
| `checkout_root` | string | `~/proj` | Parent directory a project's `checkout_path` is derived from. A registry override takes precedence — see [Daemon settings](daemon-settings.md#the-recognized-settings). |

Paths are expanded, so `~` works.

## Spawning

| Key | Type | Default | Notes |
|---|---|---|---|
| `default_branch_pattern` | string | `"ompire/<slug>"` | Used when a template does not override it. |
| `spawn_step_timeout` | integer (s) | `120` | Applies to the Git steps. |
| `project_clone_timeout` | integer (s) | `900` | Bounds "clone it for me" [project setup](projects.md#checkout-modes), which pulls a whole repository over the network. |
| `workshop_step_timeout` | integer (s) | `600` | Much larger than the Git timeout because container launch includes SDK installation. |
| `my_workshop_command` | list of strings | `["my-workshop"]` | Must be non-empty. |

## Agents

| Key | Type | Default | Notes |
|---|---|---|---|
| `agent_ready_timeout` | integer (s) | `30` | Positive. Covers container-side agent startup. |
| `agent_ring_buffer_size` | integer | `1000` | Positive. Retained raw events per session. |
| `judge_model` | string or unset | unset | Model for the workflow engine's LLM-judge step. Unset means the agent's configured default. |

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
| `gpg_signing_key` | string or unset | unset | The signing key, as a fingerprint, key ID, or user-ID substring. Also a daemon-writable setting: a selection made in Templates & settings takes precedence over this file. Unset auto-detects when the host holds exactly one usable signing key. See [daemon settings](daemon-settings.md). |
| `gh_command` | list of strings | `["gh"]` | Non-empty GitHub CLI prefix. The daemon uses it non-interactively for version detection, explicit-host identity/repository reads, PR creation, and PR polling. |
| `pr_poll_interval` | number (s) | `60` | Positive. Spacing between pull-request state polls. |

GitHub identity checks target `github.com` explicitly. A non-empty `GH_TOKEN`
takes precedence over `GITHUB_TOKEN`; either takes precedence over credentials
stored by GitHub CLI. The daemon reports only the source label, never a value.
Correct an environment token in the daemon's launch environment and restart;
use `gh auth login` or `gh auth switch` only when GitHub CLI configuration is
the selected source.

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

Everything else is TOML-only. Infrastructure settings — ports, paths, and
commands — are not editable from a browser.

## Example

```toml
# Optional: only needed when the host holds more than one signing key and you
# would rather seed the choice here than pick it in Templates & settings.
gpg_signing_key = "3AA5C34371567BD2"

checkout_root = "~/src"
task_dir_root = "~/tasks"

default_branch_pattern = "ompire/<slug>"

# Long builds; don't call them stalled too eagerly.
stall_threshold = 900
```
