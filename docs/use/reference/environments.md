# Environments

An environment names operator-owned QA infrastructure. Its `host` requirement
is the shape it needs (`container`, `vm`, or `host`); its `capabilities` map
names each capability and the provider behind it. These are declarations,
not per-capability verification or remote host orchestration. One read-only
check command determines the environment's readiness.

Declaring an environment alone grants no sandbox access. A workflow may name
it with `environment: <name>`; launch then requires an explicit operator grant
and a fresh ready check. The daemon never runs the provision command. Bring
up the infrastructure yourself.

## Declaration

Declare environments in `~/.config/ompire/config.toml`, then restart the
daemon. The file is read once at startup and remains operator-owned;
definitions cannot be edited through the UI or REST API.

```toml
[environments.dogfood]
host = "host"
provision = ["/home/qa/ompire/scripts/setup-qa-env.sh", "--dir", "/home/qa/ompire/.qa-agent", "--repo", "ompire-test/sandbox"]
check = ["/home/qa/ompire/scripts/setup-qa-env.sh", "--probe"]
wiring = '''
sdks:
  - name: omp
    plugs:
      github:
        interface: tunnel
        endpoint: localhost:4100
  - name: system
    slots:
      github:
        interface: tunnel
        endpoint: localhost:4100
provision:
  connect:
    - plug: omp:github
      slot: system:github
'''

[environments.dogfood.capabilities]
model = "auth-gateway"
forge = "forge-broker"
browser = "chrome"
```

This is the shape installed by `scripts/setup-qa-env.sh` for the `dogfood`
environment; paths and sandbox repository are resolved on the trusted QA host.
The forge capability's provider is `forge-broker`, while its Workshop tunnel
plug/slot is named `github`. The existing `pi-auth-gateway` model tunnel and
browser SDK stay in Ompire's repository Workshop declaration; only the forge
route arrives through this grant-scoped fragment. Do not add `github` to the
static declaration, which would give tasks ambient access without a grant.

Setup starts `~/.config/ompire/qa-forge-broker.sh` with the one sandbox
allowlisted and keeps pid/log files at
`~/.local/share/ompire/forge-broker.{pid,log}`. It registers the local `ompire`
project with `fetch_remote: qa-self` and installs the non-publishing `dogfood`
driver workflow when absent. The daemon does not do any of that provisioning.
See the [QA-loop guide](../../develop/how-to/run-the-qa-loop.md) for the
operator/agent split and real-service evidence procedure.

The installed check's `--probe` option is mutually exclusive with other CLI
options. It uses `QA_AGENT_DIR` inherited from the trusted daemon wrapper
(which sources the identity's `env.sh`), or the checkout's `.qa-agent/` by
default. For a direct check with a custom identity, run
`QA_AGENT_DIR=/absolute/identity /absolute/ompire/scripts/setup-qa-env.sh --probe`
on the trusted host, not `--probe --dir ...`. Only the non-secret identity
path is supplied; the probe does not source `env.sh` or print secret material.
It checks identity presence/modes, local broker health, gateway reachability,
browser availability and container tooling; a ready probe is not evidence of
upstream GitHub authentication.

| Field | Contract |
|---|---|
| Environment name | Matches `^[a-z0-9][a-z0-9-]*$`; used in recheck URLs |
| `host` | Required: `container`, `vm`, or `host` |
| `provision` | Required non-empty argv list of non-empty strings; operator-run only |
| `check` | Required non-empty argv list of non-empty strings; daemon-run |
| `capabilities` | Optional map of non-empty capability names to non-empty provider strings; defaults to an empty map |
| `wiring` | Optional non-empty string containing a YAML mapping; the exact Workshop additions fragment to apply only to granted tasks |

Unknown environment fields, wrong value types, an invalid name or host, and
empty commands or malformed wiring fail startup with the offending dotted
configuration key. With no `environments` table, there are no environments
and Settings hides the panel.
See [Configuration](configuration.md).

## Task access

A definition with `wiring` is **grantable**: its declared capability/provider
pairs describe the access the grant wires into the task. Without wiring it is
observation-only: a requiring workflow still asks for a recorded readiness
grant, but no tunnel or endpoint is added. There are no per-capability grants.

Put only non-secret Workshop tunnel declarations and their connections in the
fragment. The daemon validates that it is a YAML mapping, not the Workshop
schema or that each named provider is reachable. The launcher owns those
semantics. Credentials stay behind trusted-side brokers, never in the fragment.

In **Spawn**, review the environment, capability/provider pairs, readiness,
and named blockers, then explicitly grant the selected workflow access.
**Re-check** runs the check again; completed probes update open clients live.
An unready preview is readable, but acceptance probes again and refuses unless
the environment is ready. A workflow naming an undeclared environment can be
saved in the library but cannot be previewed or launched on this daemon.
See [Task spawn](task-spawn.md#environment-access) for request and refusal rules.

The grant is pinned to the task with its name, capability map, exact wiring,
UTC grant time, and actor `operator`. Changes to configuration do not alter
accepted tasks. Wiring is staged only around that task's container launch;
the existing restoration and startup recovery remove the staged file. A
surviving container keeps its tunnels across a daemon restart. Cleaning up
the task removes the container and therefore its tunnels; no credential
dirty-marking or rotation is needed merely because a brokered task ran.

**Grant-gating covers daemon-applied wiring only.** A repository's static
`workshop.yaml` can grant ambient access independently. Keep environment-backed
broker endpoints out of repository Workshop declarations; otherwise every task
using that declaration can receive those tunnels without an environment grant.
The daemon deliberately does not parse or police repository Workshop schemas.


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
at startup, on demand, and during preview and acceptance of requiring workflows,
not on a periodic polling loop.

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
nullable `wiring`, `grantable`, and nested `status`. The WebSocket snapshot's
`environments` list contains these same entries. Every completed probe publishes `environment_status`
with `{"environment": <full entry>}`, including failed probes. A reconnect
replaces the client's environment list from the new snapshot.
See [HTTP and WebSocket API](api.md).
