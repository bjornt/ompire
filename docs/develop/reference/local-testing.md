# Local testing harness

## Overview

The harness in `local-test/` runs the real daemon, the real frontend, real
Git, real GPG, and the real reviewer, while substituting the four dependencies
that are slow, networked, or nondeterministic: the forge, the container
tooling, the agent, and — for signing state — a throwaway key.

The substitutes are **executable fakes**, not mocks. They honor the same argv,
exit codes, streams, and filesystem effects as the tools they replace.
Production code never learns it is under test: nothing in `daemon/` branches
on a test flag.
The process-boundary and fidelity constraints are recorded in
[ADR-0014](../../adr/0014-test-end-to-end-behavior-at-external-process-boundaries.md).

The operator-facing procedure is in [Run the local end-to-end
harness](../how-to/run-local-e2e.md).

## Components

### Forge

Real bare Git repositories addressed by GitHub-shaped URLs, seeded with a base
branch and a matching `HEAD`, carrying a test identity in their gitconfig.

The production push path works against them unchanged. A one-shot push
rejection can be armed per repository to exercise the failure path.

### `gh`

A fake implementing the daemon's exact `--version`, explicit-host `api user`,
repository read, pull-request-list read, pull-request creation, and
pull-request viewing contracts against local state. Its JSON state controls the
selected identity, repository policy/access evidence, PR lifecycle, and
credential-shaped output injection without production code knowing it is fake.

Hook-up is the `gh_command` config key. There are **zero daemon changes**: the
daemon runs the configured command, and the configured command happens to be
the fake.

Every unsupported argv shape fails loudly rather than returning plausible
success. A fake that silently absorbs an unknown command is how a test passes
against behavior nobody implemented.

### Workshop

`exec` runs commands as local host processes. `launch` writes the project lock
and registers the workshop. `info` answers `present`, `absent`, and `unknown`.
`remove` is idempotent on an already-gone workshop.

Launch failure injection covers both spawn error paths — non-zero exit, and
zero exit with a missing lock file.

### Agent

The fake serves the daemon's config preflight and version banner, then speaks
the recorded RPC protocol over stdio.

Scenario selection happens **at prompt time**, so a single running fake can be
steered mid-run. Behavior scenarios author workflow-visible state in the clone
— outcome files, repro scripts, commits — so the workflow engine sees real
artifacts rather than a canned reply.

Sessions persist transcripts, and resume restores context, which is what makes
crash-recovery testable.

### Reviewer

The **real** llmvet serves review unchanged. A driver steers its UI over HTTP,
and the real exit contract remains abortable exactly as in production.

Review is where the trust boundary lives; faking it would test nothing worth
testing.

### GPG

Signing stays real, against a throwaway passphrase-protected key. Both
ship-gate states are reachable on demand — `ready` and `locked` — status
reports the daemon's own probe verdict, and commits produced while warm carry
verifiable signatures.

The key is passphrase-protected because only a protected key can reach
`locked`, which is the state the ship-gate refusal scenario needs. A
passphrase-less key never reports cached, since it has nothing to cache; the
daemon reads the agent's protection field alongside the cache flag and
classifies it `ready`. The selfcheck pins both halves of that — never cached,
still ready — against a real control key, so the distinction cannot regress
back into "locked forever".

## States and behavior

### Environment

One command brings up a driving-ready offline environment. The daemon runs
sandboxed inside a state root, with a state `bin` directory placing every fake
on its `PATH` — which is the whole substitution mechanism.

The forge-backed project is registered through the REST API, not by writing
rows, so registration itself is exercised — including the adoption validation
that now runs there
([ADR-0022](../../adr/0022-create-or-adopt-base-checkouts-without-mutating-them.md)).
The harness registers in explicit adopt mode against the checkout it just
created; the `project-onboarding` scenario covers clone mode, its failure and
retry paths, and restart reconciliation.

Smoke checks gate the bring-up: a failed check stops the environment rather
than handing over one that is subtly broken.

Teardown stops the daemon without losing state by default, and status reports
daemon health alongside fake state.

### Real tools

Two tools in the daemon's `PATH` are real, not faked: `my-workshop` and
`llmvet`. They are large build artifacts, so they are never committed —
`local-test/.tools/` is gitignored, and `local-test/tools` provisions it:

| Tool | Source | Pinned by |
|---|---|---|
| `llmvet` | Release asset from `bjornt/llmvet` | Tag + sha256 |
| `my-workshop` | `go build` of `bjornt/my-workshop` (no releases published) | Commit + Go version |

Bring-up resolves a real tool from an operator flag, the state root, a source
dir, or `PATH`, and falls back to this cache — fetching the pinned build when
it is empty. A plain checkout therefore needs nothing preinstalled: when `go`
is missing, a pinned Go toolchain is downloaded, sha256-verified, and unpacked
into the cache for the build alone. Nothing outside the cache is written.

Every download is sha256-verified against a pin in the script; the source
build is trimmed of local paths and build ids, so one (commit, Go version)
yields one digest on any machine. That digest is not the digest of a
`my-workshop` built elsewhere, which matters where [fidelity](#fidelity)
identifies that unversioned binary by hash.

`local-test/env` also writes to this cache — it re-caches whatever it pinned
so a later `up --fresh` need not re-resolve. `local-test/tools status`
distinguishes the two: `pinned` is the verified fetch, `local` is a copy the
environment supplied.

### Browser

Frontend behavior is verified by driving the real frontend, so a headless
Chrome is part of the environment rather than an optional extra.

`workshop.yaml` declares the `puppeteer-chrome` SDK. A workshop built from it
carries Chrome for Testing, a vendored Puppeteer, and the `pptr-node` wrapper,
which exports `PUPPETEER_EXECUTABLE_PATH`, extends `NODE_PATH` so `puppeteer`
resolves, and then execs the SDK's own Node. The SDK also exports
`PUPPETEER_SKIP_DOWNLOAD=1`, so Puppeteer never fetches a second Chrome over
the network.

Nothing here is pinned in `frontend/` or `daemon/`. Neither project depends on
Puppeteer, and no browser runs in the unit suites or the scenario matrix — the
browser serves interactive verification only.

Read the live values rather than trusting a documented one:

| Value | Command |
|---|---|
| The wrapper | `command -v pptr-node` |
| The Chrome binary and build | `"$PUPPETEER_EXECUTABLE_PATH" --version` |
| The vendored Puppeteer | `pptr-node -e "console.log(require('puppeteer/package.json').version)"` |
| All of the above, resolved in order | `scripts/setup-browser.sh --status` |

Outside a workshop, `scripts/setup-browser.sh` provisions one: it installs
Chrome's runtime shared libraries and `unzip`, seeds a pinned Chrome for
Testing build into the Puppeteer cache under `$PI_CONFIG_DIR/puppeteer`, and
with `--write-env` pins `PUPPETEER_EXECUTABLE_PATH` in that config directory's
`.env`.

`--status` is its read-only mode and the one command worth running first. It
resolves in a fixed order — `PUPPETEER_EXECUTABLE_PATH`, then `pptr-node`, then
the seeded cache, then `chrome`/`chromium` on `PATH` — and reports the binary,
its version, and its source. It installs nothing, downloads nothing, and needs
neither root nor network. It exits 0 only for a binary that runs and identifies
itself as Chrome or Chromium; a missing browser, unresolved shared libraries,
an unrunnable path, and a path pointing at something that is not Chrome each
exit non-zero with that reason on stderr.

The procedure — finding a browser, opening the tokenized frontend URL, and what
to report when there is none — is in [Run the local end-to-end
harness](../how-to/run-local-e2e.md#drive-the-ui-in-a-browser).

### Control CLIs

Steering happens through published surfaces only:

| Tool | Steers |
|---|---|
| `ghctl` | GitHub fake identity/authentication, repository policy/permission, credential-shaped output, pull-request lifecycle, and one-shot create/view failures |
| `wsctl` | Workshop registry and launch injection |
| `ompctl` | One-shot agent scenarios by task, clone, session, or global key; lists sessions and transcripts |
| `gpgctl` | The ship-gate signing state |

All share the state root.

### Runbooks

Scenario runbooks drive the flow stack through published surfaces only — REST,
WebSocket, and the control CLIs. A shared harness gives every runbook the same
driving and assertion contract, and a WebSocket recorder exposes daemon events
to assertions.

| Runbook | Proves |
|---|---|
| `happy-path` | Spawn through shipped |
| `file-mentions` | Prompt `@file` search, refusal, and delivery |
| `ask-approval` | Interactive gates |
| `review-comments` | Comment loopback through the real reviewer UI |
| `ship-retain` | Multi-commit re-signing |
| `ship-failures` | GitHub auth/target denial before clone mutation, redaction, GPG, PR, push, and retain recovery |
| `merge-poll` | The poll observes merging |
| `crash-recovery` | Daemon `kill -9` recovery |
| `cleanup` | Task teardown |
| `advisories-stalls` | Stall and advisory surfaces |

A driver runs a single runbook, or the whole matrix in a throwaway
environment.

`ws-watch` also exists but is **not** in the matrix — run it explicitly when
changing the WebSocket layer.

### Fidelity

The standing risk with executable fakes is drift: the fake keeps passing while
the real tool changes underneath it.

Recording is opt-in, transparent, and secret-safe. The tool never writes
unsanitized process streams to disk — tokens and passphrases are stripped on
the way through and the recorded environment is reduced to an allowlist.

Recordings are schema-versioned, sanitized golden fixtures with provenance.
One tool can retain evidence from multiple real versions; each case remains
bound to the version that produced it rather than rewriting historical
provenance. Conformance replays only cases that can be reconstructed from argv
and controlled fake state; credential-free real GitHub failures remain
observational evidence. Daemon-observable outcome snapshots can be compared
across environments.

Every component ships a self-check proving its contract offline.

## Failures and recovery

A runbook failure is a real failure — the daemon under it is real.

To locate it: if a recording still replays cleanly against the fake, the
daemon changed. If the recording no longer replays, the real tool changed and
the fake needs updating.

## Interfaces

The harness touches the daemon only through configuration and published
surfaces:

| Mechanism | Substitutes |
|---|---|
| `gh_command` | The forge CLI |
| `my_workshop_command` | Container tooling |
| `PATH` in the state root | `workshop`, `omp` |
| `llmvet_command` | Not substituted — the real tool runs |
