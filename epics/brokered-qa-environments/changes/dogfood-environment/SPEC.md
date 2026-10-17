# Dogfood environment

Epic child of [brokered-qa-environments](../../EPIC.md), change 4.

## Outcome

An operator brings up the dogfooding QA host with one script run, and from
that moment the daemon *knows* the environment: `dogfood` appears in Settings
with a live probe, and an agent working on Ompire can be granted brokered
forge access at spawn by one explicit, recorded operator decision. The QA
loop stops being manual credential wiring and becomes a granted,
agent-executable procedure: the agent observes real GitHub through the forge
broker and real models through the auth gateway, and no credential material
exists anywhere in its sandbox.

This is the first real environment definition. It consumes the forge broker
([ADR-0041](../../../../docs/adr/0041-broker-forge-access-behind-read-only-proxy.md)),
the environment registry, and environment grants
([ADR-0042](../../../../docs/adr/0042-pin-environment-grants-at-task-acceptance.md))
without changing any of them.

## Vision alignment

Aligned with Part 1's stated direction — "the gateway becoming the only
credential path" — by extending the broker pattern from model access to forge
access on the real dogfooding path. It serves "human attention is the scarce
resource": environment access becomes one recorded decision at spawn instead
of ambient manual wiring of `.qa-agent/` material. The secure-by-construction
principle is the acceptance bar: a granted task that leaks everything it can
see still leaks no credential.

## User experience

### Provision (operator, QA host)

The operator follows the same setup path as today — create the bot identity
on the management machine, copy it over, run `scripts/setup-qa-env.sh` on the
QA host. The script now additionally:

- starts the forge broker as a supervised trusted-side process (wrapper
  script, pid and log next to the daemon's), allowlisting exactly the
  resolved sandbox repository;
- declares the `dogfood` environment in `config.toml` — host `host`, the
  script itself as provision command, a new `--probe` mode as the check
  command, capabilities `model: auth-gateway`, `forge: forge-broker`,
  `browser: chrome`, and a wiring fragment carrying the `github` tunnel
  plug/slot at `localhost:4100`;
- registers the QA host's checkout of this repository as the `ompire`
  project so the dogfooding task has something to target;
- installs a `dogfood` workflow into the operator's workflow library — a
  single-agent-session driver workflow that declares `environment: dogfood`;
- reports broker and environment health in its closing smoke block.

Re-running the script stays idempotent and doubles as the trusted-side
rotation path: it restarts the broker, which re-reads the token file.

### Probe (operator, UI)

Settings → Environments shows `dogfood` with its capabilities, host
requirement, and a live state. Until the auth-gateway tunnel is up, the
environment reports `degraded` with that blocker named; starting
`qa-auth-tunnel.sh` and pressing **Re-check** turns it `ready`. A broker that
is not running is reported the same way, with the wrapper path as the fix.
The `--probe` check is read-only and fast: it verifies identity-file presence,
broker health, gateway reachability, browser availability, and container
tooling presence. It never reads out or prints credential material, and it
does not prove upstream GitHub authentication — only local readiness.

### Grant (operator, Spawn)

The operator spawns a task on the `ompire` project with the `dogfood`
workflow and a prompt pointing at the QA-loop guide. The preview shows the
environment requirement, its capability/provider pairs, and a freshly probed
status. The operator toggles the grant; acceptance probes again and refuses
with named blockers if the environment went unready. The recorded grant is
pinned to the task and survives daemon restart. Workflows without an
`environment:` key behave exactly as today.

### Drive the loop (agent + operator)

The granted agent executes the QA-loop guide from its workspace as the
driver: it clones and reads the sandbox repository through the broker with
plain `git` and `curl` — no PAT, no `Authorization` header, no `gh` — and
observes a daemon-published commit's `commit.verification` through the
brokered API. Its model access rides the existing ambient auth-gateway
tunnel; its only forge route is the broker.

Authority acts stay with the human, by design: spawning the sandbox task
under observation, answering gates, confirming the ship, and merging are
operator acts in the UI. The agent never holds the daemon token — the
rewritten guide assigns every step to exactly one of the agent or the
operator, and the agent requests operator action at the declared points
instead of acting on the daemon itself.

### Audit and failure

The agent deliberately attempts one out-of-scope operation (for example a
push, or `GET /api/user`); the broker denies it and the operator finds the
denial in the broker's stderr request log alongside the task's allowed
reads, correlated by label. Before closing the loop, the agent inspects its
own sandbox — environment, argv, workspace files — and reports that no
credential material is present; the operator can spot-check from the host
side.

If the broker stops mid-task, brokered commands fail with connection
refused; restarting the wrapper on the QA host restores the route because
the tunnel endpoint is a fixed loopback port. Cleaning up the task removes
its container and therefore the tunnel. Teardown gains one step: stop the
broker via its pid file.

## Requirements

- R1: `scripts/setup-qa-env.sh --probe` is read-only and non-interactive,
  completes well inside the 60-second check deadline, exits 0 only when
  every capability dependency is demonstrably ready, and otherwise exits
  nonzero with a first stderr line naming the concrete blocker and its fix.
  It never prints credential material.
- R2: After a full script run on a provisioned QA host, `dogfood` appears in
  the environments API and in Settings → Environments; with the broker up
  and the auth tunnel connected it probes `ready`, and with either missing
  it reports `degraded` naming that blocker.
- R3: The `dogfood` workflow is present in the workflow library and the
  `ompire` project is registered, so the operator can preview a spawn; the
  preview surfaces the environment requirement and refuses confirmation
  without the grant or while unready, with named blockers (existing spawn
  contract, now exercised by a real definition).
- R4: Inside the granted task's sandbox, `git clone`/`fetch` and repository
  REST reads against the allowlisted sandbox repository succeed through
  `http://localhost:4100`; no GitHub credential is present in the sandbox
  environment, process argv, workspace files, or session transcripts; an
  ungranted task has no `github` tunnel at all.
- R5: The granted agent observes a daemon-published commit's
  `commit.verification` through the brokered API, and at least one real
  out-of-scope denial from that task appears in the broker request log.
- R6: Re-running the setup script restarts the broker against the current
  token file and rewrites only the `[environments.dogfood]` configuration
  block, leaving other operator configuration untouched; task cleanup
  revokes the granted tunnel.
- R7: Spawning a task on the `ompire` project works without network access
  to the real upstream repository: the base checkout's fetch remote is
  satisfied locally, and the dogfood workflow declares no publish steps, so
  nothing the granted task does can target a real repository.
- R8: The QA-loop guide describes the define → probe → grant flow and the
  agent/operator step split, and the broker, environments, and configuration
  references are consistent with what the script actually installs.

## Scope

- `scripts/setup-qa-env.sh`: `--probe` mode, forge-broker lifecycle,
  environment declaration, `ompire` project registration, `dogfood`
  workflow installation, smoke coverage.
- A new tracked workflow definition installed through the library API.
- Documentation: the QA-loop how-to rewrite and reference reconciliation
  named under Documentation impact.
- Real-loop execution on the QA host as this change's acceptance evidence.

## Non-goals

- No daemon or frontend changes. The registry, grants, spawn surfaces, and
  broker shipped in the sibling changes; this change consumes them.
- No change to this repository's `workshop.yaml`. The epic seed named a
  static `github` plug there, but ADR-0042 requires environment-backed
  broker endpoints to stay out of static repository declarations — a static
  plug would hand every task ambient forge access with no grant. The plug
  arrives through the environment's wiring fragment instead.
- No management-plane changes: `setup-qa-agent.sh`, bot accounts, PAT
  minting, and VM creation stay operator-run.
- No new brokers and no agent-held daemon authority: the agent never
  receives the daemon token; UI and ship acts remain operator acts.
- No credential dirty-marking or rotation automation (epic boundary).
- No remote-host orchestration: the daemon consumes and probes the
  environment; the operator brings it up.

## Documentation impact

- `docs/develop/how-to/run-the-qa-loop.md` (contributor how-to) — rewritten
  around define → probe → grant: script-owned definition, Settings probe,
  spawn grant, the agent/operator step split, brokered verification commands
  including `commit.verification`, the deliberate-denial check, sandbox
  inspection, and broker teardown/troubleshooting rows.
- `docs/develop/reference/forge-broker.md` (contributor reference) — the
  "this component does not modify … the QA-loop guide continues to describe
  the existing loop" paragraph becomes stale once this change lands; update
  it to describe the installed integration and point at the rewritten
  how-to.
- `docs/use/reference/environments.md` (operator reference) — align the
  example's tunnel plug naming with what the script installs (`github` on
  `localhost:4100`), so the documented example and the real `dogfood`
  definition agree.
- `docs/use/reference/configuration.md` (operator reference) — same example
  alignment for the `[environments.dogfood]` stanza it shows.

No new ADR: ADR-0041 records the broker policy and ADR-0042 the grant model,
together the ADR-0015 generalization with dirty-marking explicitly rejected —
the epic's documentation completion condition is already satisfied by them;
this change is their first consumer, not a new decision.
