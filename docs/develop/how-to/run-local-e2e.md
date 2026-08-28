# Run the local end-to-end harness

The harness in `local-test/` exercises the real daemon, the real frontend,
real Git, and real GPG, while substituting the dependencies that are slow,
networked, or nondeterministic: the forge, the container tooling, the agent,
and the reviewer.

The substitutes are executable fakes, not mocks. They honor the same argv,
exit codes, streams, and filesystem effects as the real tools, so production
code has no idea it is under test. Nothing in `daemon/` branches on a test
flag.

## Real tools on a clean machine

`my-workshop` and `llmvet` are *not* faked, and they are not committed either.
The first bring-up fetches the pinned, sha256-verified builds into the
gitignored `local-test/.tools/` cache — no preinstall, not even Go, which is
vendored into the cache when it is missing.

```sh
local-test/tools status         # what is cached, and where it came from
local-test/tools fetch --force  # re-fetch after a pin bump
```

Supplying your own build still wins: `local-test/env up --my-workshop PATH`,
`--llmvet PATH`, `--my-workshop-src DIR`, or the matching `LOCAL_TEST_*`
variables. See [Local testing harness](../reference/local-testing.md).

## Run the scenarios

```sh
local-test/scenarios/run --list      # show the matrix
local-test/scenarios/run happy-path  # one scenario
local-test/scenarios/run --all       # the whole matrix, clean machine
```

`--all` provisions a throwaway state root on a free port, runs every scenario
in matrix order, and tears it down. Your persistent state root is left
untouched.

Scenario order is not arbitrary: `happy-path` runs first because the others
assume the shape it establishes, `crash-recovery` runs late because it kills
the daemon, and `cleanup` runs last.

## The scenarios

| Scenario | Covers |
|---|---|
| `happy-path` | Spawn through review, ship, and pull request |
| `file-mentions` | Prompt `@file` search, the submit refusals, and literal delivery |
| `ask-approval` | Agent questions and approval gates |
| `review-comments` | Feeding review comments back into the session |
| `ship-retain` | `retain` mode commit rewriting and signature verification |
| `ship-failures` | Refusal paths — locked key, in-flight ship, bad mode |
| `merge-poll` | Pull-request state polling to a terminal state |
| `advisories-stalls` | Stall detection and context advisories |
| `crash-recovery` | Killing the daemon mid-work and recovering |
| `cleanup` | Workshop removal, clone deletion, archival |

`ws-watch` also exists but is **not** in the `--all` matrix. Run it explicitly
if you are changing the WebSocket layer — `--all` will not cover it.

Each scenario is also directly executable — it sources `lib.sh` itself. The
`run` driver adds preflight checks and the clean-machine matrix run.

## Driving the fakes

Control scripts let a scenario steer the substituted tools:

| Tool | Control |
|---|---|
| Agent | `local-test/ompctl` |
| Forge | `local-test/ghctl` |
| GPG | `local-test/gpgctl` |
| WebSocket | `local-test/wsctl` |

## Fidelity

The risk with executable fakes is drift: the fake keeps passing while the real
tool changes underneath it.

`local-test/fidelity` addresses this by recording real tool invocations,
sanitizing them, normalizing them, and replaying them against the fakes.
Recordings live in `local-test/recordings/`.

Sanitization is not optional and not a post-processing step — the tool never
writes unsanitized process streams to disk. Tokens and passphrases are
stripped on the way through, and the recorded environment is reduced to an
allowlist.

Re-record against the real QA stack periodically. A fake that has drifted from
its recording is a test that passes for the wrong reason.

## When a scenario fails

The scenarios run the real daemon, so failures are real failures. Check
whether the fake or the production code changed: if a recording still replays
cleanly against the fake, the daemon changed; if the recording no longer
replays, the real tool changed and the fake needs updating.
