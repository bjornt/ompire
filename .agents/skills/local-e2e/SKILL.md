---
name: local-e2e
description: Run an isolated manual local end-to-end check of Ompire with local-test and the browser. Use proactively after changes affecting the frontend, task or session lifecycle, review, shipping, WebSocket state, subprocess integration, or an end-to-end user journey—even when the user does not explicitly request E2E.
---

# Local browser end-to-end verification

Use the offline harness in `local-test/` to verify Ompire's real daemon and
frontend through their production process boundaries. **Manual** means
interaction-driven, not human-required: drive the browser and the harness
yourself. Do not ask the user to launch a daemon, create a sandbox task,
operate Review, or inspect the UI when this harness can do it.

The harness runs the real daemon, frontend, Git, GPG, `my-workshop`, and
`llmvet`. It substitutes the networked or nondeterministic forge, Workshop,
and agent dependencies with executable fakes. It is the routine E2E default;
never use a real repository, credentials, or the QA stack merely to obtain
this evidence.

## When to use it

Use this skill proactively when a change can affect any observable path below:

- frontend rendering, interaction, routing, browser-stored auth, or
  WebSocket-driven state;
- task spawning, agent/session interaction, workflow progression, recovery,
  cleanup, or state surfaced in the UI;
- review, review-comment loopback, ship drafting/signing/pushing, pull-request
  polling, or their errors and recovery;
- daemon subprocess construction or a fake's external contract.

Do not start the harness for documentation-only changes or a narrowly isolated
implementation refactor with no changed observable behavior. Prefer a focused
unit or contract test for those. For a changed browser-visible or cross-process
behavior, a unit test alone is not sufficient evidence when the local harness
can exercise the path.

## Choose the smallest useful E2E check

1. Read `docs/develop/how-to/run-local-e2e.md` and inspect
   `local-test/scenarios/run --list`.
2. Treat an existing scenario as the executable specification for its path;
   run the narrow matching scenario rather than duplicating it.
3. Use a browser-driven manual flow when the changed journey has no scenario,
   when presentation or interaction itself is in scope, or when an existing
   scenario proves the backend path but not the frontend behavior.
4. Run `local-test/scenarios/run --all` only for a broad change that could
   affect the matrix. It creates and tears down its own throwaway environment.
   Do not use it as a replacement for a focused browser check.

Useful current mappings:

| Changed behavior | Focused runbook |
| --- | --- |
| Spawn through review, signed ship, and PR creation | `happy-path` |
| Agent questions or approval gates | `ask-approval` |
| Real reviewer comments returned to the session | `review-comments` |
| Retained commits and re-signing | `ship-retain` |
| Ship refusals and recovery | `ship-failures` |
| PR terminal-state polling | `merge-poll` |
| Session advisories or stalls | `advisories-stalls` |
| Daemon/session recovery | `crash-recovery` |
| Task/workshop cleanup | `cleanup` |
| WebSocket protocol behavior | `ws-watch` (explicit; not in `--all`) |

## Bring up an isolated browser-ready environment

Never disturb a user's persistent `local-test/.state` environment. Allocate a
unique state root and free port for the check. Keep both values for every
subsequent harness command and teardown.

```sh
STATE_ROOT=$(mktemp -d /tmp/ompire-local-e2e-XXXXXX)
PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')

LOCAL_TEST_STATE="$STATE_ROOT" LOCAL_TEST_PORT="$PORT" local-test/env up
LOCAL_TEST_STATE="$STATE_ROOT" LOCAL_TEST_PORT="$PORT" local-test/env status
```

`local-test/env up` provisions the disposable forge and signing identity,
starts the real daemon, registers the `sandbox` project and template, and
builds the frontend when needed. Do not set `LOCAL_TEST_SKIP_FRONTEND` for a
browser check. Do not use `--fresh` against a shared/default state root; the
unique root above is already clean.

For a focused runbook, reuse the same isolated state and port:

```sh
LOCAL_TEST_STATE="$STATE_ROOT" LOCAL_TEST_PORT="$PORT" \
  local-test/scenarios/run happy-path
```

Replace `happy-path` with the selected runbook. A focused runbook and a
browser check can share one environment when both prove distinct properties of
the same change.

Read the temporary token only from the isolated root to build the frontend URL:

```sh
TOKEN=$(cat "$STATE_ROOT/home/.local/share/ompire/token")
printf 'http://127.0.0.1:%s/?token=%s\n' "$PORT" "$TOKEN"
```

The query token bootstraps the frontend's local storage, so it is needed once
per browser profile.

Then obtain a browser. Do not assume you have none, and do not ask the operator
to open the UI: resolve it in this order, which
`scripts/setup-browser.sh --status` also reports in one command.

1. A browser capability your own tooling provides — Oh My Pi's `browser` tool,
   a Chrome MCP server, an editor integration. Prefer it; it gives observation
   and interaction without a script.
2. `pptr-node` on `PATH`. Inside the workshop this is always present: it runs
   the SDK's Node with Puppeteer vendored and Chrome wired up, so a short
   script navigates, reads rendered state, clicks, and screenshots with no
   install and no network.
3. A Chrome named by `PUPPETEER_EXECUTABLE_PATH` or seeded in the Puppeteer
   cache.
4. `scripts/setup-browser.sh`, on a host with none that may be provisioned.

`docs/develop/how-to/run-local-e2e.md`, section *Drive the UI in a browser*,
carries the working recipe.

Whichever you use, observe rendered state before acting, re-observe after every
navigation or re-render, and drive the real controls. Do not infer a passed UI
check from a successful REST response or source inspection.

If no browser can be obtained at all, say which property you could not verify
in the browser and report the non-browser evidence as exactly that. An
unverified claim is worse than a named gap.

## Drive the real path

The harness exposes deterministic external conditions without bypassing the
system under test:

- Put `[[scenario:<name>]]` in a spawned task prompt, or arm the next fake
  agent behavior with `local-test/ompctl scenario <name> --global`. Run
  `local-test/ompctl scenarios` to discover supported names. For a complete
  happy flow, `[[scenario:commit]]` makes the fake agent create a real commit
  in the disposable clone.
- Use `local-test/wsctl` to inspect workshops or arm a launch failure.
- Use `local-test/gpgctl lock` and `warm` to exercise the ship gate and its
  recovery.
- Use `local-test/ghctl` to inspect PRs, drive merge/close transitions, or
  arm a forge failure.
- Review runs the real `llmvet`. When reviewer presentation is in scope, open
  the review URL in the browser and approve or submit comments through its UI.
  `local-test/review` is a valid HTTP driver for deterministic setup or a
  non-visual reviewer outcome, but it does not prove the reviewer UI.

Drive the changed user journey through the frontend wherever it is available:
spawn a task, wait for the visible state transition, answer a prompt or
approval gate, start Review, operate the real review page, and use the Ship
controls. Use control CLIs only to establish an external condition or inspect
an external result—not to replace the frontend action being verified.

For a browser-visible failure or recovery path, create the condition with the
appropriate control CLI, then observe the resulting error and recovery in the
frontend. Do not edit harness JSON, the daemon database, task clones, or fake
state files directly; the published control CLIs and REST/WebSocket surfaces
are the test boundary.

## Verify and diagnose

Pair browser evidence with the relevant external outcome:

- a task/session/review/ship state visible in the actual frontend;
- the matching focused runbook's machine-checkable assertions when one exists;
- `local-test/gpgctl status` and a real signature verification for a ship path;
- `local-test/ghctl pr` and the UI's observed PR state for a PR path;
- WebSocket evidence through `local-test/scenarios/ws-watch` for event-order
  or live-update behavior.

If the path fails, inspect the harness before weakening the test:

```sh
LOCAL_TEST_STATE="$STATE_ROOT" LOCAL_TEST_PORT="$PORT" local-test/env status
# daemon log: $STATE_ROOT/home/.local/share/ompire/daemon.log
```

A local E2E failure is evidence against the real daemon/frontend integration,
not a reason to patch production code with a test mode or silently bypass the
external boundary. A passing local E2E does not claim real-stack QA fidelity;
state that distinction when it matters.

## Teardown

Always stop the isolated environment after capturing evidence, including on a
failed check. Close the browser session you opened — a `pptr-node` script that
ends closes its own — and wipe only the state root you created:

```sh
LOCAL_TEST_STATE="$STATE_ROOT" LOCAL_TEST_PORT="$PORT" local-test/env down --wipe
```

Report the exact browser journey, control conditions, scenario(s) run, and
observable results. Name any unverified property rather than treating an API
or source-level check as a browser E2E result.
