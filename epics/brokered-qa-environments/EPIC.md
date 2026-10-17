# Epic: Brokered QA environments with human-granted access

## Outcome

An operator can declare what a sophisticated QA environment provides — forge
access, model access, a browser, container tooling — and what it needs to run.
A task whose workflow requires such an environment can launch only after a
human explicitly grants it access, and the agent then exercises real
capabilities through narrow brokers: no credential, signing key, or forge
token ever enters the task sandbox.

The motivating case is Ompire itself: dogfooding needs real GitHub, real
Workshop/LXD, and real model access (docs/develop/how-to/run-the-qa-loop.md),
which today means a human manually wiring `.qa-agent/` credentials, an
auth-gateway tunnel, and setup scripts onto a disposable VM. Agents working on
Ompire cannot self-serve that loop, and handing them the bot PAT is not
acceptable. The same shape recurs for any project whose QA environment is more
than "run the tests."

The credential-rotation worry disappears by construction: because every
agent-facing path is brokered, the agent never touches reusable secret
material, so there is nothing to mark dirty or rotate on task end. Per the
operator's decision, this epic builds **no** dirty-marking or rotation
automation; management-plane rotation (`setup-qa-agent.sh rotate`) stays as
it is.

## Vision alignment

This extends the vision's Part 1 (agent isolation) along its stated direction:
"the gateway becoming the only credential path." The pi-auth-gateway proves
the pattern for model access; this epic generalizes it to the forge and names
the pattern — capabilities satisfied by providers behind brokers — as
declarable infrastructure. It also serves "Human attention is the scarce
resource": environment access becomes one explicit, recorded human decision at
spawn time instead of ambient manual wiring. The secure-by-construction
principle is the boundary condition: a task that leaks everything it can see
must still leak nothing that matters.

## Boundaries

- **No credential dirty-marking or rotation automation.** Brokered access
  exposes no reusable secret; nothing needs rotating because a task ran.
- **The management plane stays manual.** Bot accounts, PAT minting, and VM
  creation (`scripts/setup-qa-agent.sh`) remain operator-run. The daemon
  consumes and probes identities; it never creates them.
- **No remote workshop hosts.** Environments are brought up by the operator
  running declared provisioning scripts (the (b) shape from design
  discussion: a whole Ompire instance runs inside the QA host). The daemon
  verifies and consumes environments; it does not orchestrate remote
  machines.
- **Forge broker is read-only**: REST reads and git clone/fetch for
  allowlisted sandbox repositories only. GraphQL, agent-side push, and PR
  creation are denied; publishing authority remains daemon-side per
  ADR-0011/0032. In the QA loop the *daemon under test* holds credentials
  and ships; the agent observes.
- **The pi-auth-gateway is not rebuilt.** It becomes one capability provider
  among several, unchanged.
- **No change to ship-flow authority, signing, or review boundaries.**

Module placement must respect the modular-monolith epic's map: broker and
workshop slot wiring in `isolation`, spawn preflight and grants in
`application`/`work`, status surfaces in the API layers. Code lives in the
monolith; brokers run as separate trusted-side processes like the auth
gateway.

## Changes

### [x] 1. forge-broker

A trusted-side broker process that gives a task sandbox scoped GitHub access
without handing it the credential — the forge analogue of the pi-auth-gateway
(ADR-0015). Holds the bot PAT from the `.qa-agent/` identity directory,
listens on a trusted-host localhost port, and terminates two protocols:

- **REST reverse proxy**: injects the real `Authorization` header,
  allows `GET`/`HEAD` under `/api/repos/<owner>/<sandbox>`, denies writes and
  account-level paths, and scrubs credential material from responses and logs.
  Commit verification comes from the repository API's `commit.verification`
  object. Agents use plain-HTTP `curl`; GraphQL and `gh` compatibility are
  out of scope.
- **Git smart-HTTP proxy**: clone/fetch of the allowlisted repository only,
  since GitHub accepts git-over-HTTPS with a PAT. Read-only for v1.

Every request is logged with a task-correlation label so grants are auditable
and policy violations are detectable. Ships with the `workshop.yaml`
plug/slot stanza (`github`, `interface: tunnel`) mirroring how
`pi-auth-gateway` is already declared — the tunnel mechanism is
workshop-owned, so this change needs **no daemon modifications** and can be
verified offline with a real `git http-backend`. The self-contained,
stdlib-only module lives at `daemon/src/ompire_daemon/forge_broker.py` so it
also runs directly on a trusted machine without a daemon environment; it
owns no daemon state and imports no product code. Workshop wiring remains
isolation-owned.

- Depends on: None
- Acceptance: an operator can clone an allowlisted sandbox repository and
  read its repository API through the broker using `git` and `curl`;
  `/api/user`, writes, and non-allowlisted repositories are denied with no
  credential material in responses or logs. The documented Workshop tunnel
  contract exposes no PAT in sandbox environment, argv, or files; broker logs
  record allowed and denied requests.
- Verification: policy and scrubbing tests plus offline clone/ls-remote/push
  checks against real `git http-backend` and local CLI smoke. The reference
  supplies a real QA-host recipe, executed by `dogfood-environment` against
  the sandbox service.

### [x] 2. environment-registry

Declarative environment definitions consumed by the daemon: a named
environment records its host requirement (container/vm/host), the
capabilities it offers with their providers (e.g. `model: auth-gateway`,
`forge: forge-broker`, `browser: chrome`), and its provision/check commands.
The daemon gains a registry, a **probe** that runs the check command and
reports ready/degraded/missing with named blockers — the same reporting
shape as the GPG probe (`locked`/`no_key`/`ambiguous`) and ship-preflight
blockers — a REST surface, and a minimal UI status view. No launch
enforcement yet; this makes environments observable and their definitions
durable. Useful immediately: today's QA host (auth gateway + browser) can be
declared and probed before the forge broker exists.

- Depends on: None
- Acceptance: an environment defined in daemon configuration appears in the
  API and UI with live probe status; a failing check names its blocker; an
  environment whose provisioning script is absent reports it rather than
  failing silently.
- Verification: daemon tests over a fixture environment with real
  provision/check scripts; UI status verified in a browser against the
  local-test harness.

### [x] 3. environment-grants

The human decision, as a first-class mechanism. A workflow may declare
`environment: <name>`; spawn preview (`POST /api/tasks/preview` → reviewed
confirmation token, the existing contract in `daemon/src/ompire_daemon/api/
tasks.py` and `application/spawn.py`) surfaces the requirement and its probe
status and refuses confirmation with named blockers when the environment is
not ready. Granting access is an explicit human act recorded durably with
the task; launch wires only the granted capability slots into the task's
workshop declaration, keeping tunnels task-scoped and revoked at cleanup
(the lifecycle ADR-0015 already requires). Workflows without an
`environment:` key behave exactly as today.

- Depends on: environment-registry
- Acceptance: spawning an environment-requiring workflow without a grant is
  refused with the environment's named blockers; a granted task reaches the
  declared capabilities from inside its sandbox; an ungranted capability is
  absent from the sandbox; the grant survives daemon restart as recorded
  state; cleanup revokes the tunnels.
- Verification: new local-test scenario(s) covering grant, refusal, sandbox
  capability presence/absence, crash recovery, and cleanup; browser check of
  the spawn surface.

### [~] 4. dogfood-environment

Ompire's own QA environment as the first real definition, consuming all of
the above: `scripts/setup-qa-env.sh` becomes the environment's declared
provision/check commands, the forge broker and auth gateway its capability
providers, and this repository's `workshop.yaml` gains the `github` plug.
The QA-loop how-to is rewritten around *define → probe → grant* instead of
manual wiring, so an agent working on Ompire can be granted a real loop by
an operator decision rather than by hand-copying credentials.

- Depends on: forge-broker, environment-registry, environment-grants
- Acceptance: an operator grants a dogfooding task access to the `dogfood`
  environment at spawn; the agent drives the QA loop — including observing a
  Verified commit through the brokered API — with the broker as its only
  forge route and no GitHub credential anywhere in its sandbox.
- Verification: the real QA loop (docs/develop/how-to/run-the-qa-loop.md)
  executed by a granted agent end to end, with sandbox inspection proving no
  credential material is present.

## Completion

The epic is complete when all of the following are observable, not merely when
the four changes are checked off:

1. An agent granted access to the `dogfood` environment drives the full QA
   loop — spawn through a Verified pull request — where every forge and model
   call transits a broker and sandbox inspection finds no credential material
   (environment, argv, workspace, transcripts).
2. Spawning a workflow that requires an unavailable or ungranted environment
   refuses with named blockers, in the API and in the browser.
3. The broker policy log shows allowed and denied requests correlated to
   tasks; at least one real denial of an out-of-scope operation is recorded
   during the dogfood run.
4. Documentation is reconciled: `run-the-qa-loop.md` describes the
   grant-based flow, the configuration reference documents environment
   definitions, and an ADR records the broker-policy and grant model as the
   generalization of ADR-0015 (dirty-marking explicitly rejected as
   unnecessary while all agent access is brokered).
