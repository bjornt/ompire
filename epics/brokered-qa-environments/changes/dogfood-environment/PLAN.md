# Plan

## Approach

Consume, don't build: the registry, grant mechanics, spawn surfaces, and
forge broker all shipped in the sibling changes, so this change writes no
daemon or frontend code. It extends `scripts/setup-qa-env.sh` — already
idempotent, already restarting the daemon on re-run, already resolving the
sandbox repository and driving the daemon API — to also own the broker
lifecycle, the environment declaration, the `ompire` project registration,
and the `dogfood` workflow installation. Then it rewrites the QA-loop guide
around the new flow and executes the real loop as acceptance evidence.

Design choices and why:

- **`--probe` as the check command.** The environments contract requires a
  read-only, fast, daemon-run argv. A dedicated mode of the same script
  keeps provision and check in one place the operator already trusts.
  Probe checks are local-only (identity files, broker `/healthz`, gateway
  TCP reachability, `setup-browser.sh --status`, container tooling on
  PATH): proving local readiness, not upstream authentication — the broker
  doc is explicit that local health does not prove GitHub auth.
- **Broker lifecycle mirrors the daemon's.** A generated
  `~/.config/ompire/qa-forge-broker.sh` wrapper (invoking
  `daemon/src/ompire_daemon/forge_broker.py` directly — it is stdlib-only
  and needs no venv), pid and log under `~/.local/share/ompire/`, restart
  on every script re-run. Restart is safe: the broker holds no durable
  state and re-reads the token file, which is exactly the documented
  rotation path.
- **The `github` plug arrives via the wiring fragment, not
  `workshop.yaml`.** ADR-0042 (accepted after the epic text was written)
  keeps environment-backed broker endpoints out of static repository
  declarations so grants remain the only path to the capability. The
  fragment mirrors the stanza the broker reference documents:
  `github` tunnel plug/slot at `localhost:4100` plus its `connect` entry.
- **The dogfooding task targets the `ompire` project** — the agent works on
  Ompire (epic motivation). Spawn fetches the project's fetch remote in the
  base checkout before cloning; the QA host's checkout cannot fetch the
  real upstream (the bot identity has no access and the host must not hold
  credentials that do). Registration therefore adds a local-path remote
  `qa-self` pointing at the checkout itself and registers with
  `fetch_remote: qa-self`: the spawn fetch is a local no-op, no network,
  no auth. `upstream_url` is read from the checkout's `origin` for a
  truthful project identity; it is inert because the `dogfood` workflow
  declares no publish steps.
- **The `dogfood` workflow is a tracked file installed via the library
  API.** A minimal format-5 definition: one session, one agent step with a
  small structured outcome and a required terminal decision, `environment:
  dogfood`, no review/publish steps. Installation validates the tracked YAML,
  creates an inert draft, then explicitly saves an executable revision with
  its returned edit version. Existing entries remain operator-owned
  (ADR-0031), including archived or draft-only entries; smoke reports them
  unavailable instead of overwriting them.
- **Custom identity paths remain probe-only metadata.** The daemon wrapper
  sources the selected identity's `env.sh`, which exports `QA_AGENT_DIR`.
  `--probe` uses that path without sourcing or reading the file. Provision
  argv retains the selected `--dir` and resolved `--repo`. A new config gets
  signing/notification defaults; an existing config's other settings remain
  byte-for-byte untouched.
- **The agent drives, the operator holds authority.** The guide assigns
  every loop step to exactly one of them. The agent runs brokered `git`/
  `curl`, sandbox self-inspection, and evidence collection; the operator
  performs spawn, gates, ship confirmation, and merge in the UI. No daemon
  token enters any sandbox.

## Affected areas

- `scripts/setup-qa-env.sh` — `--probe` mode; broker wrapper/pid/log;
  `[environments.dogfood]` config block (idempotent replace, rest of file
  untouched); `qa-self` remote + `ompire` project registration; `dogfood`
  workflow install; smoke additions (broker health, environment state,
  workflow and project presence); final report prints broker log/pid.
- `scripts/qa-dogfood-workflow.yaml` — new tracked workflow definition.
- `docs/develop/how-to/run-the-qa-loop.md` — rewrite (see SPEC).
- `docs/develop/reference/forge-broker.md` — replace the stale
  "does not modify / guide describes the existing loop" paragraph with the
  installed integration.
- `docs/use/reference/environments.md`,
  `docs/use/reference/configuration.md` — align example wiring with the
  installed `github` plug stanza.
- No changes to: daemon, frontend, `workshop.yaml`, `setup-qa-agent.sh`,
  local-test harness (no daemon behavior changes to fake).

## Architecture decisions

No new ADR. ADR-0041 (broker policy) and ADR-0042 (grant model) already
record the ADR-0015 generalization and the explicit rejection of
dirty-marking — the epic completion condition for an ADR is satisfied by
those two. This change's choices (script-owned broker lifecycle, wiring
fragment content, `qa-self` fetch remote, library-installed workflow) are
operational, not architectural. The one deviation from the epic seed —
wiring fragment instead of a static `workshop.yaml` plug — is mandated by
ADR-0042 and recorded in the spec's non-goals.

## Risks

- **Broker down blocks every grant** (acceptance probe degrades). Mitigation:
  script-owned wrapper and smoke check; probe diagnostic names the wrapper;
  recovery is restarting the wrapper on a fixed loopback port, which
  existing tunnels re-serve without re-granting.
- **Probe leaks or hangs.** A daemon-run check must never print secrets or
  exceed its deadline. Mitigation: existence/mode checks only, `curl
  --max-time` on every network probe, no `env.sh` sourcing in `--probe`;
  verified by the local negative-path runs in task 7.
- **Script clobbers operator config.** The `[environments.dogfood]` block is
  script-owned; the rewrite must touch only that block. Verified by re-running
  the script against a config containing unrelated keys and diffing.
- **`qa-self` fetch looks exotic.** A fetch that never touches the network
  is exactly what a host without upstream credentials needs; verified by
  spawning on the `ompire` project during the real loop.
- **Real-loop evidence depends on operator-managed infrastructure** (the
  `ompire-test` bot, a disposable host). This is the epic's named
  acceptance path; the change cannot be marked done from unit-level checks
  alone. Every other task lands first so the loop run is the final gate.
- **Agent overreach.** The prompt and guide must keep the agent off the
  daemon token and off real repositories; the rewritten guide states both
  rules at the top, as it does today.

## Tasks

- [x] 1. Add `--probe` mode to `scripts/setup-qa-env.sh`: mutually exclusive
  with other options; checks identity-file presence and modes (no content),
  broker `GET /healthz`, auth-gateway reachability on `localhost:4000`,
  `scripts/setup-browser.sh --status`, and container tooling on PATH; one
  stderr diagnostic per failure naming the fix; exit 0 only when all pass;
  never prints credential material. (R1)
- [x] 2. Add forge-broker lifecycle to the script: require Python ≥ 3.12,
  write `~/.config/ompire/qa-forge-broker.sh` (token-file `.qa-agent/token`,
  `--repo` the resolved sandbox repo, port 4100), restart it via pid file
  under `~/.local/share/ompire/`, and verify the readiness line. Runs after
  sandbox-repo resolution, before the daemon restart, so the startup probe
  can pass. (R2, R6)
- [x] 3. Write the `[environments.dogfood]` block into `config.toml`
  idempotently (replace only that block): `host = "host"`, provision/check
  argv pointing at the script and its `--probe` mode, capabilities
  `model`/`forge`/`browser`, and the `github`-plug wiring fragment at
  `localhost:4100`. The existing daemon restart picks it up. (R2, R6)
- [x] 4. Add the `qa-self` local-path remote to the QA checkout and register
  the `ompire` project through the daemon API (`checkout_path` = repo root,
  `fetch_remote = "qa-self"`, `upstream_url` from the checkout's `origin`,
  default model profile as for the sandbox project); skip when already
  registered. (R3, R7)
- [x] 5. Add `scripts/qa-dogfood-workflow.yaml` (format 5: one session, one
  agent step, minimal structured outcome and terminal decision,
  `environment: dogfood`, no review/publish steps) and install it into the
  workflow library via draft creation followed by executable revision save
  when absent. Validate it through the library validate route every run. (R3, R7)
- [x] 6. Extend the smoke block: broker `/healthz`, `GET /api/environments`
  reports `dogfood` (state printed, blockers named), workflow and `ompire`
  project present. No credential material in output. (R2)
  Evidence for tasks 4–6: actual setup registration/smoke sections ran twice
  against an isolated real daemon and a disposable local checkout. Both
  projects registered, local `git fetch qa-self` passed, the tracked workflow
  validated and became executable (format 5, zero actions, no reviews), and
  the second install preserved its version. Broker/environment/workflow/project
  smoke all passed. A browser showed the installed requirement/capabilities,
  refused missing grants, refused a stopped broker with its wrapper-naming
  blocker, recovered to ready, launched with a pinned operator grant, and
  cleaned up the fixture task. These are offline fixtures, not real QA-loop
  acceptance evidence.
- [x] 7. Local negative verification, no QA host needed: `bash -n`; run
  `--probe` with identity missing, broker stopped, and gateway absent —
  assert named diagnostics, nonzero exit, fast completion, and no secret
  strings in output; run it again with a fixture broker (dummy token,
  `--upstream-base` loopback) and a stub gateway listener to see exit 0.
  Re-run the full script's config-writing path against a fixture config and
  diff to prove only the `dogfood` block changed. (R1, R6)
  Evidence: `bash -n scripts/setup-qa-env.sh` passed. A throwaway driver ran
  the actual CLI with fixture identity/tooling and isolated loopback port
  translation: missing identity/broker/gateway and unsafe modes failed in
  0.13–0.23s; all dependencies ready passed in 0.13s; no dummy credential or
  encoded authorization appeared. The actual config-writing section retained
  unrelated bytes (including header-looking multiline string content), was
  byte-identical on rerun, and initialized valid fresh config. The actual
  broker section started, observed readiness, restarted with a new pid, and
  stopped cleanly against the dummy-token fixture.
- [x] 8. Rewrite `docs/develop/how-to/run-the-qa-loop.md` around
  define → probe → grant per the spec, including the agent/operator step
  table, brokered verification commands (clone, PR/commit reads,
  `commit.verification`), the deliberate-denial capture, sandbox inspection,
  and broker teardown/troubleshooting rows. (R4, R5, R8)
- [x] 9. Reconcile references: update the stale integration paragraph in
  `docs/develop/reference/forge-broker.md`; align the wiring examples in
  `docs/use/reference/environments.md` and
  `docs/use/reference/configuration.md` with the installed `github` stanza.
  (R8)
- [ ] 10. Execute the rewritten QA loop on a real QA host with a granted
  agent, capturing the spec's acceptance evidence: environment `ready` in
  Settings; grant recorded on the task; brokered clone/API reads from inside
  the sandbox including `commit.verification` of a daemon-published commit;
  at least one real denial in the broker log with the task label; sandbox
  inspection (environment, argv, workspace, transcripts) finding no
  credential material; tunnel gone after cleanup. Verify the spawn surface
  in a real browser: environment section, grant toggle, and refusal with
  named blockers while the environment is degraded (e.g. broker stopped).
  (R2–R7)
  Blocked: the available `.qa-agent` identity records `ompire-test`, but a
  trusted-side GitHub `GET /user` returned `Bad credentials`. No alternate
  QA host is configured. A valid bot sandbox-scoped PAT and a real granted
  agent/subject publication are required; local fixtures cannot substitute.
- [x] 11. Re-check the completed behavior against `docs/VISION.md`:
  broker/gateway as the only credential paths, one recorded human decision
  at spawn, and a sandbox that can leak nothing that matters.
  The implementation keeps the forge credential in the trusted-side broker,
  model access behind the existing gateway, daemon authority out of the
  workflow, and `github` out of static repository wiring. The offline browser
  run recorded one explicit operator grant. The real sandbox non-disclosure
  evidence remains the unmet task-10 acceptance gate, not an inferred pass.

## Verification and finish audit

- `make test lint typecheck docs`: passed; daemon 1613 passed / 1 skipped,
  frontend 540 passed; Ruff, Oxlint, mypy, TypeScript, strict MkDocs all passed.
- Both reference TOML examples parse and match the installed environment's
  host, capability map, and exact `github` wiring; the guide's embedded
  non-emitting scanner compiles.
- Isolated `local-test/scenarios/run environment-grants`: 44 checks passed.
  An initial run had 44 passes and one shutdown failure because the smoke
  driver retained a killed daemon child as a zombie; reaping that driver-owned
  process corrected the fixture ownership, and the rerun passed.
- Finish audit: tasks 1–9 and 11 implemented and locally verified; task 10's
  real GitHub publication, granted real-agent inspection, and real container
  revocation evidence are unavailable with the invalid bot credential.
  `SPEC.md` and `PLAN.md` are intentionally retained and the parent remains
  `[~]`. No new ADR is needed; ADR-0041/0042 still own the decisions.
