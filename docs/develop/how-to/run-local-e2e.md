# Run the local end-to-end harness

The harness in `local-test/` exercises the real daemon, real frontend, real
Git, real GPG, real project launcher, and real reviewer, while substituting
the networked forge, container tooling, and LLM-backed agent.

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

## What `env up` provisions

Bringing the harness up registers a complete model profile named `sandbox`
alongside the project, and sets it as the project's default. A launch needs
one — the daemon has no default of its own and refuses without one — and the
profile's four bindings name the offline fake `omp`, so nothing the harness
runs can reach a provider.

That profile is harness data, not a production fallback. The daemon never
creates one; the harness does, in the same way it creates the sandbox
repository.

Scenarios launch through the real contract: `spawn_task` in
`local-test/scenarios/lib.sh` previews first and submits the reviewed token,
because that is what `POST /api/tasks` requires.

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
| `happy-path` | Spawn through review, GitHub preflight, a previewed and confirmed pull-request delivery |
| `file-mentions` | Prompt `@file` search, the submit refusals, and literal delivery |
| `ask-approval` | Agent questions and approval gates |
| `workflow-decisions` | Declared results, evidence handoffs, and answering a workflow gate |
| `review-comments` | Feeding review comments back into the session |
| `ship-retain` | `retain` mode commit rewriting and signature verification |
| `ship-endings` | Stopping at a local signed commit, pushing it later, opening its pull request later, and cleaning up a local-only result |
| `ship-failures` | Every refusal named before any effect — missing and stale review, GitHub authentication and target denial, redaction, signing, retain preconditions — and recovery from each |
| `ship-interrupted` | Lost replies: an already-landed push adopted rather than repeated, a pull request found by its correlation marker, and an unsearchable forge left explicitly unresolved with cleanup refused |
| `merge-poll` | Pull-request state polling to a terminal state |
| `advisories-stalls` | Stall detection and context advisories |
| `crash-recovery` | Killing the daemon mid-work and recovering, including inside a delivery's push window |
| `durable-results` | Capturing planning files with no commit, reviewing and accepting one revision, downloading it, a successor and its comparison, a refused capture, identical bytes after a daemon restart and after cleanup, and guarded purge |
| `cleanup` | Workshop removal, clone deletion, archival |

`ws-watch` also exists but is **not** in the `--all` matrix. Run it explicitly
if you are changing the WebSocket layer — `--all` will not cover it.

Each scenario is also directly executable — it sources `lib.sh` itself. The
`run` driver adds preflight checks and the clean-machine matrix run.

## Drive the UI in a browser

The harness serves the real frontend, so frontend behavior is verified in a
browser. A REST response or a source reading is not a UI check and must never
be reported as one.

### Find a browser

Ask the script rather than guessing:

```sh
scripts/setup-browser.sh --status
```

```text
browser: available
binary:  /var/lib/workshop/sdk/puppeteer-chrome/chrome/chrome
version: Google Chrome for Testing 150.0.7871.24
source:  PUPPETEER_EXECUTABLE_PATH
```

It installs and downloads nothing, and exits non-zero with a reason when there
is no usable browser. It resolves the same order you should follow yourself:

| Order | Source | Use it when |
|---|---|---|
| 1 | A browser capability your own tooling provides | Your agent harness has one — Oh My Pi's `browser` tool, a Chrome MCP server, an editor integration. Prefer it; it gives you observation and interaction without writing a script |
| 2 | `pptr-node` on `PATH` | Inside a workshop. It runs the SDK's Node with Puppeteer vendored and Chrome already wired up |
| 3 | `PUPPETEER_EXECUTABLE_PATH`, or a Chrome seeded in the Puppeteer cache | A browser was provisioned here earlier |
| 4 | `scripts/setup-browser.sh` | The host has none and may be provisioned |
| 5 | Nothing | Say so — see [below](#when-there-is-no-browser) |

### Open the frontend

`local-test/env up` prints the tokenized URL. Reconstruct it later from the
state root:

```sh
TOKEN=$(cat "${LOCAL_TEST_STATE:-local-test/.state}/home/.local/share/ompire/token")
printf 'http://127.0.0.1:%s/?token=%s\n' "${LOCAL_TEST_PORT:-4173}" "$TOKEN"
```

The `?token=` stashes itself in the browser's local storage, so it is needed
once per browser profile.

### Drive it with `pptr-node`

When you are writing the script yourself, `pptr-node` needs no flags, no
install, and no network:

```js
// visit.mjs
import puppeteer from 'puppeteer';

const [url, shot] = process.argv.slice(2);
const browser = await puppeteer.launch();
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900 });

await page.goto(url, { waitUntil: 'networkidle2' });
console.log(await page.title());
console.log(await page.evaluate(() => document.body.innerText));
if (shot) await page.screenshot({ path: shot, fullPage: true });

await browser.close();
```

```sh
pptr-node visit.mjs "http://127.0.0.1:$PORT/?token=$TOKEN" tasks.png
```

Observe rendered state before and after every navigation or interaction, and
use the real controls — `page.click`, `page.type`, `page.waitForSelector` —
rather than asserting against the API behind them. Run Chrome with
`--no-sandbox` only where the container forces it; it is not needed in a
workshop.

### Verify visual workflow authoring

The workflow editor is a browser surface, and its interesting failures are all
browser failures: work lost across a mode switch, a stale answer applied to a
newer edit, an invalid draft that a diagram makes look finished. Verify it
against the running harness, not against the conversion endpoint.

A reproducible pass:

1. Bring the harness up and open **Workflows** at 1440×900. Create a workflow
   and switch to **Visual**.
2. Build the flow entirely with the form controls — agents and the primary,
   step cards, instructions with explicit references, ordered decision cases,
   a gate with named answers, and a visit bound with its exhaustion gate. Do
   not type in the YAML editor at any point.
3. Save a draft while it is still incomplete, reload, and confirm the same
   partial work comes back with its located reason.
4. Finish it, save an executable revision, and launch it from Spawn. Check that
   the preview names the revision you just saved and shows the same flow.
5. Drive the run with `local-test/ompctl` and follow the pinned procedure on
   task detail: each visit separately, the evidence links to the producing
   attempts, and the same conversation across the steps that share an agent.
6. Repeat the editing pass at 390×844 and with the keyboard only — every card
   edit, reorder, route selection, error jump, and mode switch is reachable
   with Tab and Enter.

Interruptions worth including, because each one has a way to lose work
silently: a two-tab conflicting save, a reconnect with local edits on screen,
a daemon restart while the run is gated, and a library edit after launch
followed by reopening the old task.

The `workflow-decisions` scenario establishes a run with declared results,
evidence handoffs and a gate, which is the state task-detail inspection is
worth checking against.

### Verify durable results

Retention is a claim about bytes surviving things, so the browser pass is about
watching them survive. The `durable-results` runbook already proves the
filesystem and checksum side; what a browser adds is that the operator can see
and do it.

A reproducible pass, against a task that has written planning files (spawn one
with `[[scenario:plan-files]]`):

1. Open the task at 1440×1200. The **Results** panel says nothing is retained
   until you capture, and states the supported types and limits.
2. Capture a directory using the form. Check the revision's file list, sizes,
   checksums, and provenance — the producing step must read `unknown`, and the
   commit must be labelled as a capture-time observation.
3. Open a file. It must render as escaped source: an agent-authored heading is
   text, not a heading, and there is no `<img>` or `<script>` element inside the
   preview.
4. Accept the revision, and confirm the Review and Ship surfaces are unchanged
   by it. Accepting a result is not an approval.
5. Have the agent revise the files, capture again, and open the comparison.
   Then reselect the earlier revision and confirm its acceptance is untouched.
6. Download the ZIP, then **Clean up workspace** from task detail. Read the
   confirmation: it must say uncaptured edits are lost *and* how many revisions
   are retained.
7. After cleanup, capture is unavailable and the revisions are still readable
   and downloadable. Find the task again from **Tasks → Retained results** — a
   task with no pull request has no other way back — and follow the link.
8. Purge a revision. The confirmation names it, whether it was accepted, and the
   bytes being removed; afterwards the record stays visible with no files.

Interruptions worth including: a daemon restart with the panel open, a second
tab accepting the revision this one is showing, and a capture attempted while a
review holds the workspace.

### When there is no browser

Say which property you could not verify in the browser, report the non-browser
evidence as exactly what it is, and stop. Do not describe an API result as a UI
check, and do not ask the operator to open the UI by hand — the harness is
drivable, and an unverified claim is worse than a named gap.

## Driving the fakes

Control scripts let a scenario steer the substituted tools:

| Tool | Control |
|---|---|
| Agent | `local-test/ompctl` |
| Forge | `local-test/ghctl` |
| GPG | `local-test/gpgctl` |
| WebSocket | `local-test/wsctl` |

`ghctl auth`, `ghctl repository`, and `ghctl token-echo` control only the
fake's selected account, eligibility evidence, and credential-shaped output.
Use them to set an external condition; observe the daemon's safe REST,
WebSocket, and browser behavior rather than editing fake state directly.

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
