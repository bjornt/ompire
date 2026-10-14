# Environments

An environment names operator-owned QA infrastructure. Its `host` requirement
is the shape it needs (`container`, `vm`, or `host`); its `capabilities` map
names each capability and the provider behind it. These are declarations,
not per-capability verification or remote host orchestration. One read-only
check command determines the environment's readiness.

Environments are observation only. Declaring one changes no task launch,
workflow, or ship decision and grants no sandbox access. The daemon never
runs the provision command. Bring up the infrastructure yourself.

## Declaration

Declare environments in `~/.config/ompire/config.toml`, then restart the
daemon. The file is read once at startup and remains operator-owned;
definitions cannot be edited through the UI or REST API.

```toml
[environments.dogfood]
host = "host"
provision = ["/home/qa/ompire/scripts/setup-qa-env.sh"]
check = ["/home/qa/ompire/scripts/setup-qa-env.sh", "--probe"]

[environments.dogfood.capabilities]
model = "auth-gateway"
forge = "forge-broker"
browser = "chrome"
```

This is a declaration example, not an automatically installed environment.
The operator must supply commands that satisfy the contract below.

| Field | Contract |
|---|---|
| Environment name | Matches `^[a-z0-9][a-z0-9-]*$`; used in recheck URLs |
| `host` | Required: `container`, `vm`, or `host` |
| `provision` | Required non-empty argv list of non-empty strings; operator-run only |
| `check` | Required non-empty argv list of non-empty strings; daemon-run |
| `capabilities` | Optional map of non-empty capability names to non-empty provider strings; defaults to an empty map |

Unknown environment fields, wrong value types, an invalid name or host, and
empty commands fail startup with the offending configuration key. With no
`environments` table, there are no environments and Settings hides the panel.
See [Configuration](configuration.md).

## Command contract and trust

Commands are literal argument vectors, not shell expressions. Only the
program path expands `~`. Plain program names resolve on the daemon's `PATH`;
use absolute script paths because the daemon's working directory is not a
task checkout. Put interpreter and script arguments in separate argv entries
when needed; the program the daemon can inspect is the first entry.

Check commands run locally with daemon privileges, in the same trust class
as `gh_command` and `my_workshop_command`. Declare trusted operator-owned
commands, never repository-controlled task scripts. A check must be
read-only and fast, must not prompt, and must exit 0 only after demonstrating
readiness. Any nonzero exit means readiness was not demonstrated.

Declarations and the first diagnostic line are operator-visible in API and
WebSocket payloads. **Never put credentials in argv, capability/provider
strings, or check output.** Read secrets from trusted-side stores only when
needed; report availability, not their contents. The registry does not read
credential stores or attach credential material to status, and does not log
captured check output. It is not a general secret sanitizer for arbitrary
operator scripts.

Each check has a fixed 60-second deadline. Output is drained with bounded
retention; a failing check reports the first non-empty diagnostic line capped
at 1000 characters, preferring stderr and falling back to stdout or the exit
code. A timeout terminates the check's process group and reports a blocker
rather than hanging startup.
Startup probes environments sequentially, so N declarations can delay
startup by up to N check deadlines. A failed environment is reported; it
does not prevent the daemon from serving unrelated work after those checks.

## Status and recovery

Each definition carries a nested `status` with `state`, `blockers` (a list of
`{code, message}`), and `checked_at` (UTC timestamp, or null before probing).
Definitions are durable in TOML; status is process-local. Restarting the
daemon re-probes every definition and retains no probe history. Checks run
at startup and on demand, not on a periodic polling loop.

| State | Meaning |
|---|---|
| `unknown` | No probe has completed |
| `ready` | Check exited 0 and no required program/script is missing |
| `degraded` | Check failed or exceeded its deadline |
| `missing` | Declared provision or check program/script is absent |
| `error` | Check could not execute or another probe error occurred |

| Blocker code | Meaning and recovery |
|---|---|
| `provision-script-missing` | Provision program/script absent; restore it. This takes precedence over any check outcome |
| `check-program-missing` | Check program/script absent; restore it or fix its path |
| `check-failed` | Nonzero exit; the check's diagnostic names what to fix |
| `check-timed-out` | Deadline exceeded; fix the hung or excessively slow check |
| `check-error` | Execution or probe error; fix permissions, executable format, or the reported OS condition |

In **Settings → Environments**, each environment shows its state, blockers,
host requirement, capability/provider pairs, provision command, and last
check time. **Re-check** probes that environment only. Fix its host or run
the displayed provision command yourself, then re-check; recovery replaces
old blockers with `ready`. Open clients update from the completed probe's
WebSocket event without a refresh.

## API

Both routes use the daemon's bearer-token authentication:

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/environments` | All definitions with cached status; no subprocess runs |
| `POST` | `/api/environments/{name}/recheck` | Fresh full definition and status; unknown name is `404` |

A full entry contains `name`, `host`, `capabilities`, `provision`, `check`,
and nested `status`. The WebSocket snapshot's `environments` list contains
these same entries. Every completed probe publishes `environment_status`
with `{"environment": <full entry>}`, including failed probes. A reconnect
replaces the client's environment list from the new snapshot.
See [HTTP and WebSocket API](api.md).
