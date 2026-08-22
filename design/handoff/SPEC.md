# SPEC: ompire

An agent task manager for oh-my-pi (omp + umpire: watches the agents play,
doesn't play itself, makes the calls).

Status: draft — decisions are captured as they are made; open questions at the bottom.
Last updated: 2026-07-16

## Problem

I usually run multiple coding agents (oh-my-pi) in parallel and today the
workflow has three pain points:

1. **Tracking** — no overview of which agents are running, done, stuck, or
   waiting for input/approval.
2. **Setup** — starting a new agent involves repetitive manual setup
   (directory/worktree, initial prompt, configuration).
3. **Teardown** — when an agent finishes I manually review, commit, and create
   the PR.

The system to build is an interface to my coding agents: either a locally run
web service or an Ubuntu desktop application. oh-my-pi is the only agent that
must be supported initially.

## Goals (draft)

- Model work as **tasks** ("fix bug #123"), not sessions: a task may
  orchestrate several omp sessions through a mostly deterministic workflow
  (e.g. reproduce → fix → validate) — see Decision 8.
- Dashboard of all running/finished tasks with meaningful status
  (streaming, waiting for approval, waiting for input, done, failed).
- Spawn new agents with pre-configured setup (cwd/worktree, prompt, model).
- Interact with a running agent: prompt, steer, interrupt, answer approval
  requests.
- Post-run automation: review changes, commit, create PR.

## Non-goals (draft)

- Supporting agents other than oh-my-pi in v1 (but don't design it away).
- Remote/multi-user operation in v1 — single user, local machine.

---

## Decision 1: Integration protocol — oh-my-pi native RPC

**Decision:** Each managed agent is an `omp --mode rpc-ui` child process,
driven over its stdio NDJSON protocol. One process per agent.

Note: `rpc-ui` (not plain `rpc`) is required so approval prompts are delivered
as `extension_ui_request` frames instead of failing when a tool needs
approval (`hasUI` is only set for interactive and `rpc-ui` modes,
`packages/coding-agent/src/main.ts`).

### Rationale

The RPC surface (documented in `docs/rpc.md`, types in
`packages/coding-agent/src/modes/rpc/rpc-types.ts`) covers exactly what the
task manager needs and the alternatives don't:

- **Task status:** `get_state` returns `isStreaming`, `queuedMessageCount`,
  todos, context usage, model; `get_session_stats` returns token/message
  counts. Session events (`agent_start`/`agent_end`, tool execution,
  message deltas) stream on stdout.
- **Interaction:** `prompt`, `steer`, `follow_up`, `abort`,
  `abort_and_prompt`, plus queue-mode control.
- **Subagents:** `set_subagent_subscription` provides lifecycle/progress/event
  frames; `get_subagent_messages` reads full subagent transcripts.
- **Automation hooks:** the `bash` command executes in the agent's
  environment (useful for setup/commit/PR steps); host tools + host URI
  schemes let the manager register its own tools the agent can call back
  (e.g. `create_pr`, `notify_operator`).
- **Approvals:** approval prompts arrive as `extension_ui_request`
  select/confirm frames, answered over stdin.
- **Escape hatch:** RPC mode writes the same session files as the TUI, so any
  session can be picked up hands-on in a real terminal with
  `omp --resume <session-file>`.
- **Client:** `packages/coding-agent/src/modes/rpc/rpc-client.ts` is an
  existing TypeScript wrapper to start from (or crib from).

Accepted trade-offs:

- The protocol is bespoke to oh-my-pi and tracks its internals
  (`AgentSessionEvent`); version churn is possible. Mitigation: keep the
  protocol behind a small internal interface (see Decision 2 candidate:
  `AgentHandle`).
- Approval frames are pre-formatted text dialogs, less structured than ACP's
  permission requests (no raw tool input / file locations). Rendering rich
  diff/approve cards may require parsing or upstream changes.
- Process-per-agent means the manager owns process supervision
  (spawn/restart/stdio plumbing) — acceptable, a task manager wants that
  layer anyway, and it buys crash isolation and per-agent cwd/env.

### Considered alternatives

#### ACP (Agent Client Protocol, as used by Zed)

oh-my-pi ships a mature ACP server (`omp acp`,
`packages/coding-agent/src/modes/acp/`) built on the official
`@agentclientprotocol/sdk`: multi-session per process, session
new/load/resume/list/fork (fork and listing are `unstable_` extensions),
plan/default modes, model + thinking config, and a first-class structured
permission flow (raw input, file locations, allow/reject once/always).

- **Pros:** standardized protocol with typed SDKs; UI code would work with
  any ACP agent (Claude Code, Gemini CLI, …); best-structured approval
  payloads of all options; battle-tested against Zed.
- **Cons:** lowest-common-denominator surface — no `get_state` snapshot, no
  subagent progress feeds, no steering/follow-up queue control, no bash
  passthrough, no compaction/handoff control, no host tools. Several needed
  features are unstable extensions anyway. Multi-session-per-process means
  one crash kills all agents.
- **Verdict:** rejected for v1; the orchestration-specific features drive
  this product and they only exist in native RPC. Revisit if non-omp agents
  become a goal — hence the `AgentHandle` abstraction.

#### Terminal/PTY emulation

Run the real TUI in a PTY per agent, render via xterm.js or an embedded
terminal widget.

- **Pros:** zero protocol work; 100% feature fidelity (setup wizard,
  autocomplete, interactive login); immune to protocol churn.
- **Cons:** does not solve the core tracking problem — status would have to
  be scraped from ANSI output or terminal titles; automation would mean
  synthesizing keystrokes into a TUI (extremely fragile); no
  machine-readable events for notifications, cost, or history.
- **Verdict:** rejected as the integration mechanism. A read-only terminal
  pane could still appear later as a UI feature, but `omp --resume` in a
  real terminal already covers the hands-on case.

#### In-process SDK (`@oh-my-pi/pi-coding-agent`)

`createAgentSession()` in a Bun process, subscribing to events directly
(`docs/sdk.md`).

- **Pros:** richest possible access; no serialization boundary.
- **Cons:** no crash isolation (one wedged agent or native crash takes down
  the whole manager); some state is process-global (URI schemes, settings);
  locks the manager to Bun and to an exact omp version. The SDK docs
  themselves recommend RPC when process isolation is wanted.
- **Verdict:** rejected for hosting agents. May still be useful for
  utilities (e.g. reading session files/listings) inside the manager.

#### Collab web client (`packages/collab-web`)

omp's `/collab` shares a live session over an E2E-encrypted relay; a browser
guest client renders it natively (streaming text, tool cards, prompting,
interrupting), and a local relay can be self-hosted.

- **Pros:** the "render an omp session in a browser" problem is already
  solved here — valuable prior art or embeddable per-session view.
- **Cons:** it is a guest view onto a TUI-hosted session, not an
  orchestrator: no spawning, no aggregate task state, host process still required per
  session.
- **Verdict:** not the integration mechanism, but study its session
  rendering before building the per-agent view from scratch.

---

## Decision 2: Delivery form — local daemon + web UI
<!-- Durable decision: [ADR-0002](../../docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md) -->

**Decision:** A long-running supervisor daemon (systemd user service) owns
the `omp` child processes and serves a browser UI. No desktop app in v1; if
a tray icon/badge is ever missed, wrap the same web UI in a thin shell or a
small tray helper that talks to the daemon.

### Rationale

- Agents are stdio-attached child processes; pipes cannot be re-attached
  after the parent dies, so whatever spawns agents must outlive any casually
  closed UI window. That forces the daemon regardless of UI form — a
  desktop-app monolith would kill running agents on window close.
- Web UI: no packaging, fast iteration, and LAN access later means
  answering an approval from a phone. The classic weakness (browser
  notifications need an open tab) doesn't apply: the daemon is a native
  Linux process and fires desktop notifications itself via
  `notify-send`/D-Bus.
- Crash recovery is cheap because RPC mode writes the same session files as
  the TUI: on daemon restart, respawn each agent with
  `omp --resume <session-file>`. The in-flight turn is lost, the session is
  not.

### Architecture sketch

```
┌─────────────┐  WebSocket + REST   ┌──────────────────────────────┐
│ Browser UI  │◄───────────────────►│  Daemon (systemd user svc)   │
│ (React/TS)  │                     │  - agent registry (SQLite)   │
└─────────────┘                     │  - event ring buffer         │
      ▲                             │  - RPC client per agent      │
      │ notify-send / D-Bus         │  - notifier, audit log       │
      └─────────────────────────────│                              │
                                    └──────┬───────┬───────┬───────┘
                                     stdio │       │       │
                                        ┌──▼──┐ ┌──▼──┐ ┌──▼──┐
                                        │ omp │ │ omp │ │ omp │  (rpc-ui,
                                        └─────┘ └─────┘ └─────┘   one per
                                                                  worktree)
```

- UI is stateless; the daemon is the source of truth. On connect the browser
  gets a task snapshot plus recent events from a per-agent ring buffer,
  then live deltas over WebSocket. REST for commands (spawn, kill, prompt,
  answer approval), WebSocket for events.
- Session files are the archive; the daemon persists only registry +
  outcomes, not full event history.
- The supervisor core stays thin (spawn, multiplex, persist, notify);
  features like templates and PR automation are modules the core does not
  depend on.
- Security posture: bind localhost by default, token auth from day one
  (before any LAN exposure), and an audit log of approval decisions — all
  approvals already flow through the daemon.

## Decision 3: Daemon language — Python

**Decision:** The daemon is Python 3.12+ (`asyncio`; FastAPI or Starlette
for REST + WebSockets; Pydantic for validated frames; SQLite via
SQLAlchemy Core — no ORM — with Alembic for schema migrations).
The browser frontend remains TypeScript/React — unavoidable, but it is the
untrusted presentation side; all sensitive logic (process spawning,
approval relay, auth, audit) lives in the daemon.

### Rationale

- **Security review comes first.** The daemon is the trust boundary: it
  holds the auth token, spawns processes with chosen cwd/env, relays
  approval decisions, and can run bash in repos via agents. Its security
  depends more on the operator being able to audit every line with
  confidence than on marginal language properties; Python is the language
  the operator reviews best.
- The workload is I/O-bound plumbing at trivial scale (a handful of child
  processes, NDJSON lines, WebSocket fan-out) — well within `asyncio`.
- The lost Bun/TS advantage (importing `RpcClient` and protocol types) is
  small: the protocol is documented NDJSON (`docs/rpc.md`) with
  id-correlated responses, and the daemon deliberately treats agent events
  as **opaque JSON**, validating only the subset it interprets
  (`agent_start`/`agent_end`, `response`, `extension_ui_request`, subagent
  lifecycle). Everything else passes through untouched to the
  focused-session view. This keeps the Pydantic surface to roughly a dozen
  models and reduces coupling to omp's internal event-type churn.
- Keep the dependency footprint small enough to actually review.

### Considered alternatives

- **Bun + TypeScript:** direct reuse of `RpcClient`/protocol types and
  potentially collab-web components; rejected because the operator is not
  comfortable security-reviewing TypeScript, which outweighs code reuse.
- **Go:** static binary, smaller runtime surface, stricter typing — real
  but marginal advantages for a local single-user daemon that is not
  shipped; no decisive win over the reviewer-familiarity argument. Revisit
  only if deployment requirements change (e.g. distributing the daemon).

---

## Decision 4: Attention model

**Decision:** The daemon derives a per-agent status from a small set of
interpreted RPC frames. No manager-injected "fleet tools": mid-turn
questions use omp's builtin `ask` tool, and there is no explicit `done`
state — a finished turn is `idle` and the operator reviews from there.

Note (Decision 8): these states describe a single omp **session**. The
task-level status shown on the dashboard is derived from the workflow —
current step + this state for the step's session ("fix: working",
"validate: waiting-input"); `command` and `gate` steps contribute their own
status. Attention tiers apply unchanged. Step completion is signalled by
the outcome-file convention (Decision 8), not by a `done` state — the
"no manager tools, no done state" stance here is about *attention*, and it
stands.

### States

| State | Entered when | Attention tier |
|---|---|---|
| `starting` | process spawned, until `ready` frame | silent |
| `working` | `agent_start`; turn in flight | silent |
| `waiting-approval` | pending `extension_ui_request` select/confirm from an approval gate | **interrupt** |
| `waiting-input` | pending `extension_ui_request` originating from an in-flight `ask` tool call | **notify** |
| `idle` | turn ended (`agent_end`), nothing pending, no queued messages | badge |
| `reviewing` | llmvet review session open for this task (see Decision 7) | notify |
| `retrying` | `auto_retry_start` active | badge |
| `stalled` | `working` but no frames for N minutes (watchdog) | notify |
| `failed` | process exit ≠ 0, retries exhausted, fatal stderr | **interrupt** |

Attention tiers: **interrupt** = desktop notification + sound + badge (the
agent is blocked or dead); **notify** = desktop notification + badge;
**badge** = tab title/favicon count only; **silent** = dashboard only.
`waiting-*` states unanswered for X minutes re-notify (attention ages, it
doesn't fire once and scroll away).

### Why `ask` instead of a manager-injected `needs_input` host tool

- `ask` is a builtin (`src/tools/ask.ts`) available in `rpc-ui` mode; the
  model already knows it — no prompt preamble teaching a custom tool.
- Richer payload: the `tool_execution_start` args carry the full structured
  questions (options, descriptions, multi, recommended), so the dashboard
  renders a real question card. RPC has no rich ask dialog surface, so the
  answers flow back as plain `select`/`editor` `extension_ui_request`
  frames — render from the tool args, answer via the UI frames.
- Consistency with the escape hatch: `ask` works identically when a session
  is picked up via `omp --resume` in a terminal; a manager host tool would
  silently not exist there.
- A `done`/`task_complete` state was considered and dropped: end-of-turn is
  `idle`, review is manual by design.

### Detection details

- **Distinguishing input vs. approval selects:** `ask` declares
  `concurrency: "exclusive"` (runs alone in its tool batch), so a `select`
  frame arriving during an in-flight `ask` execution is a question; a
  `select` during any other tool's execution is an approval gate
  (cross-check: approval selects have exactly `["Approve", "Deny"]`).
- **Force `ask.timeout=0` for managed sessions.** Ask questions can
  auto-select the recommended option after `ask.timeout` seconds. The
  default is 0 (disabled), but RPC mode does not reset `ask.*` settings, so
  a user-level config value would make unattended agents silently
  self-answer. The daemon must pin it to 0.
- **Debounce turn boundaries (~2s) and check `queuedMessageCount`:** with
  queued follow-ups/steering, `agent_end` is often followed immediately by
  the next `agent_start`; don't flicker through `idle` between chained
  turns.
- **Idle decoration, not a state:** if the last assistant message looks like
  a question (`get_last_assistant_text` heuristic), decorate the `idle`
  card ("may be waiting for a reply") rather than inventing a state. Every
  status change carries a `reason` field naming the evidence, for
  debuggability and heuristic tuning.
- **Stall watchdog:** silence while nominally `working` (hung tool, stuck
  network call) is itself a signal — this is the "forgot an agent for 40
  minutes" case.
- **Subagent activity** feeds the focused view only; top-level state is
  driven solely by the root agent lifecycle.

### Advisory signals (dashboard decorations, not states)

- Context usage crossing a threshold (~80%): suggest compact/handoff.
- Cost/token accumulation from throttled stats.

### Task event vocabulary (daemon → UI)

- `status_changed {agent, from, to, reason}`
- `attention {agent, kind, payload}` / `attention_cleared {agent}`
- `advisory {agent, kind, value}`
- `stats {agent, context_pct, tokens, cost}` (throttled)
- Raw omp event passthrough on a per-agent channel for the focused-session
  view (opaque JSON; the daemon validates only the frames it interprets:
  `ready`, `agent_start`/`agent_end`, `response` correlation,
  `extension_ui_request`, `tool_execution_*` for `ask`, `auto_retry_*`,
  process exit).
- **Notification actions:** a single "Open" action focusing the agent's
  view. No approve-from-notification — approval happens in the UI where the
  full prompt renders.

---

## Decision 5: Workspace strategy — clone-per-task in a workshop container

**Decision:** Each task gets a **local hardlink clone** of the project's main
checkout (not a git worktree), launched as its own
[workshop](https://workshop.dev) (LXD) container via
[my-workshop](https://github.com/bjornt/my-workshop). The agent runs
unrestricted (yolo) inside the container; push/PR credentials never enter it.

### Spawn pipeline

1. `git -C ~/proj/<project> fetch origin`, then
   `git clone ~/proj/<project> ~/tasks/<project>/<slug>` (local hardlink
   clone — near-instant, disk cost ≈ one checkout) and branch off
   `origin/<base>`.
2. `my-workshop` in the clone: creates/augments `workshop.yaml`, hides it
   from git, launches the workshop. Each task dir gets its own
   `.workshop.lock` id, so containers are naturally per-task. The additions
   config (`workshop.my.yaml` / `~/.config/my-workshop/my.yaml`) injects the
   omp SDK, the `omp-home` mount, and the `pi-auth-gateway` tunnel.
3. The task's named sessions (Decision 8) spawn lazily — the first step
   that uses one triggers `workshop exec -- omp --mode rpc-ui
   -s ask.timeout=0 …` as a daemon stdio child inside the task's container.
   Yolo is implied by the container; `waiting-approval` goes mostly dormant
   (kept in the state machine — per-tool `tools.approval: prompt` overrides
   can still fire).
4. On the `ready` frame, the workflow sends the current step's prompt
   (template preamble + step prompt).

Teardown: operator review → daemon commits/pushes/creates PR from the host
side of the clone → `workshop remove` → delete the clone dir (optionally
deferred until PR merge so the escape hatch stays available).

### Why clone-per-task, not worktrees

Workshop mounts the project at `/project` inside the container — not at its
host path. A worktree is not self-contained: its `.git` file points at the
main repo's `.git/worktrees/<name>` by absolute host path, and its
HEAD/index live inside the main repo's `.git`. Mounted at `/project`, git is
broken; mounting the main repo in (writable, as worktree commits require)
would hand an unrestricted agent shared write access to the primary repo's
refs and objects. The clone instead:

- is fully self-contained (a real `.git` directory) — works at `/project`;
- limits the agent's blast radius to its own disposable copy; cleanup is
  `rm -rf`, with zero bookkeeping residue in the main repo;
- creates a physical credential boundary: the clone's `origin` is the local
  main repo and no SSH keys/gh tokens are mounted, so the agent **can
  commit but cannot push**. Push/PR are host-side daemon operations with
  the operator's credentials, after review.

### Environment facts (verified against the running oh-my-pi workshop)

- `omp-home` mount: `~/.omp` in the container is backed by a host path
  (`~/.local/share/workshop/id/<id>/<name>/mount/omp/omp-home`) — session
  files persist on the host, so the daemon can archive/inspect them and
  crash recovery works.
- `pi-auth-gateway` tunnel (host `127.0.0.1:4000` → container): model API
  credentials stay on the host.
- Escape hatch inside the container world: `workshop shell` +
  `omp --resume` (host omp does not share the container's `~/.omp`).

### To verify / tune

- `workshop exec` must stream stdin cleanly for a long-lived NDJSON child
  (stdout confirmed; test with `workshop exec -- cat`). Fallback: a small
  FIFO/socat shim.
- Workshop launch + omp SDK install latency per task: start without
  pre-warmed pooling, measure, revisit if painful.
- Task dir layout `~/tasks/<project>/<slug>` — placeholder, adjust to taste.

## Decision 6: Setup templates

**Decision:** A per-project template in the daemon's config defines
everything "spawn" needs:

- the project it runs against (checkout path and push/PR remotes come
  from the project — Decision 9),
- base branch and branch naming pattern (e.g. `bjornt/<slug>`),
- workshop additions source (project `workshop.my.yaml` or the global
  `~/.config/my-workshop/my.yaml`),
- omp flags and settings pins (`--mode rpc-ui`, `-s ask.timeout=0`, model,
  thinking level),
- the workflow to run (Decision 8), e.g. `bugfix`; default is the
  single-agent-step workflow,
- optional prompt preamble (project conventions) prepended to the task
  description.

Spawning from the UI = pick template, name the task slug, write the prompt.

---

## Decision 7: Review, commit, and PR automation — daemon-run llmvet

**Decision:** The daemon (not the agent) runs
[llmvet](https://github.com/bjornt/llmvet) on the host side of the task
clone, loops review comments back to the agent over RPC, and on approval
commits, pushes, and creates the PR with host credentials.

Note (Decision 8): this loop is itself expressible as workflow steps
(`gate` review → `command` llmvet → `decision` approved/comments/aborted,
looping back to an agent step). The mechanics below are unchanged either
way.

### Why the daemon runs llmvet

- The review gates a daemon-side privileged action (push + PR). If the
  agent ran llmvet and relayed the result, the reviewed party would mediate
  its own review; run by the daemon, the exit code and stdout are ground
  truth the agent never touches.
- llmvet binds `127.0.0.1` on the host and needs a human browser; the
  daemon runs it with `-no-open -port <n>` and surfaces the URL on the task
  card (a systemd daemon cannot open browser tabs).
- The host clone's working tree is the same data the container sees at
  `/project`, so the host-side diff is exactly the agent's work.
- Consequence: the llmvet SDK inside the workshop is unnecessary for
  managed tasks.

### llmvet contract (verified)

Shows `git diff` (or `--cached`) in a browser, collects inline comments,
prints a `> `-blockquoted review prompt to stdout. Exit 0 with empty stdout
= approved; exit 0 with output = comments submitted; exit 130 = aborted.

### Full-delta visibility (the reset dance)

llmvet reviews working-tree diffs, not ref ranges, so agent checkpoint
commits would hide part of the task delta. With the agent idle, the daemon
runs in the host clone:

```
ORIG=$(git rev-parse HEAD)
git reset --mixed $(git merge-base origin/<base> HEAD)  # full delta unstaged
llmvet -no-open -port <n>
git reset --mixed $ORIG                                 # exact restore; working tree untouched
```

Works whether or not the agent committed. Possible future llmvet
enhancement (owned tool): a `llmvet <base>..HEAD` range mode to retire the
dance.

### Review loop

1. Operator triggers "Review" on an `idle` task → daemon runs the reset
   dance + llmvet → task enters `reviewing` (notify tier; added to the
   Decision 4 state table), dashboard links the review URL.
2. Exit 0, empty stdout → approved → ship (below).
3. Exit 0 with prompt → daemon restores git state and sends the review
   prompt to omp via `prompt`; agent addresses comments; back to `idle`;
   repeat.
4. Exit 130 → aborted; back to `idle`, no action.

### Shipping on approval

1. **Commit:** per-ship operator choice between two modes, both producing
   operator-authored, GPG-signed commits (agent checkpoints are made
   in-container as the container user, unsigned — they never ship as-is):
   - **Squash** (default): one final commit for the task.
   - **Retain:** keep the agent's commit structure but rewrite the range
     (`git rebase` from the merge-base with
     `--exec 'git commit --amend --no-edit --reset-author -S'` or
     equivalent) — operator authorship + signature on every commit; N
     signing operations, silent while the gpg-agent cache is warm.

   Commit message and PR title/body are drafted by asking the agent over
   RPC (it has full task context) and are operator-editable in the
   dashboard before anything is pushed.
2. **Push + PR:** daemon pushes the branch to the project's fork (or upstream when no
   fork is configured) and runs
   `gh pr create` — host credentials only, per Decision 5's boundary. PR
   URL is stored in the registry and shown on the task card.
3. **Cleanup:** daemon polls PR state; on merge, `workshop remove` + delete
   the clone (grace period / confirmation per Decision 5).

### Commit signing (GPG) without the daemon holding the passphrase

The daemon signs via the operator's existing gpg-agent; the passphrase
lives only in the agent's memory, never in the daemon.

- **Plumbing:** as a systemd *user* service the daemon shares
  `$XDG_RUNTIME_DIR`, so `git commit -S` in the host clone reaches the
  regular gpg-agent socket (`$XDG_RUNTIME_DIR/gnupg/S.gpg-agent`) exactly
  like terminal commits. The failure mode is only the cold cache: a
  headless daemon has no pinentry surface, so signing fails when the key
  is locked.
- **Warm-cache model:** raise `gpg-agent.conf` TTLs to the operator's
  retype tolerance (e.g. `default-cache-ttl 14400`,
  `max-cache-ttl 86400`). The daemon probes lock state without triggering
  a prompt (`gpg-connect-agent 'KEYINFO <keygrip>' /bye` reports the
  cached flag), shows a lock indicator on the dashboard, and checks it
  before starting a ship flow rather than failing mid-flight.
- **Unlock, v1 (daemon never involved):** on lock, raise an attention item
  ("signing key locked"); the operator runs a terminal helper that makes a
  throwaway signature (`echo | gpg --clearsign -u <key> >/dev/null`) —
  normal pinentry, keyboard → pinentry → gpg-agent, cache warmed.
- **Unlock, optional later (off by default):** dashboard-typed passphrase
  piped to `gpg-preset-passphrase` (requires `allow-preset-passphrase`);
  transits daemon memory transiently, never persisted, but is strictly
  weaker than the terminal path. To verify: preset entries follow
  different expiry rules (generally held until agent restart, not the
  idle TTL).
- **Rejected:** `--pinentry-mode loopback` with a daemon-supplied
  passphrase — that is precisely the "daemon holds the password" design
  this project avoids.

### Open knobs

- Commit-message/PR-body drafting via the agent session (default) vs. a
  daemon-side LLM call (works even when the session is dead).

---

## Decision 8: Task model — tasks are workflows of steps

**Decision:** The operator-facing unit is the **task** ("fix bug #123"),
not the omp session. A task owns the workspace (clone + workshop container
+ branch, Decision 5), the dashboard card, and the ship flow, and executes
a **workflow**: a sequence of steps — strictly sequential in v1 — written
in Python in the daemon and selected by the task's template (Decision 6).
Sessions are **named resources declared up front by the workflow** (e.g.
`bugfix` declares `reproducer` and `coder`); each agent step explicitly
names the session it runs on, and sharing a session between steps is just
two steps naming the same one.

### Step kinds

| Kind | Runs | Outcome |
|---|---|---|
| `agent` | one turn (prompt) on its assigned named session | `.ompire/outcome.json` written by the agent |
| `command` | a shell command in the container | exit code + captured output |
| `decision` | routing logic in the daemon | next step (deterministic rule; LLM judge fallback) |
| `gate` | waits for the operator | operator action |

### Sessions as named resources

- Declared up front in the workflow; every `agent` step names its session.
  Reuse is deliberate: reproduce and validate both run on `reproducer`
  (the reproduction context lives there), the fix runs on `coder` (clean
  context, no reproduction rambling).
- At most one session per step; a session serves one step at a time —
  moot in v1 since steps are sequential.
- Spawned lazily on first use (Decision 5 pipeline, step 3) and kept alive
  until the task ends; crash recovery per Decision 2 (`omp --resume`).
- All of a task's sessions share the container and clone, so the **working
  tree is the primary handoff channel** between steps (repro scripts, code
  edits), complemented by the outcome files below.

### Completion signal: outcome file, LLM judge as fallback

- **Deterministic first.** An `agent` step's prompt instructs the agent to
  finish by writing `.ompire/outcome.json` in the clone (status, summary,
  artifacts such as the repro command — schema TBD). The daemon reads it
  on `agent_end`; it is both the step's result and the handoff document
  for later steps. `.ompire/` is kept out of the diff via the clone's
  `.git/info/exclude` (no repo pollution, invisible to llmvet and the PR).
- No manager-injected host tool, consistent with Decision 4: a file
  convention survives `omp --resume` in a terminal and needs no protocol
  additions.
- **LLM judge is the fallback, never the router:** only when the outcome
  file is missing/malformed — or a `decision` step's deterministic rule
  cannot resolve — does the daemon make a cheap LLM call over the
  transcript tail to classify the outcome. If the judge is also uncertain,
  escalate to a `gate` rather than guess.

### Example: the `bugfix` workflow

Sessions: `reproducer`, `coder`.

1. `agent` on `reproducer` — read the issue, tailor a reproducer to it,
   confirm it fails; outcome carries the repro command.
2. `agent` on `coder` — fix the bug (input: issue + repro outcome).
3. `agent` on `reproducer` — validate the fix in the session that holds
   the reproduction context. When step 1 produced a plain script, a
   `command` step running it can replace this.
4. `decision` — validated → review; not validated → back to 2 with the
   validation report; bounded iterations, then escalate to a `gate`.
5. Ship flow (Decision 7).

A code-review-only task is the other end of the spectrum: a workflow of
mostly `command` steps with no agent session at all.

### Consequences

- Decision 4's state machine becomes per-session; the task card derives
  its status from current step + session state (note added there).
- The v1 default workflow is the degenerate single-`agent`-step workflow —
  exactly the pre-Decision-8 behavior, so nothing is lost.
- **No workflow DSL in v1.** Workflows are Python in the daemon behind a
  small step/outcome interface; a declarative format is future work, only
  worth doing once several concrete workflows exist and the shape is
  obvious.

---

## Decision 9: Projects are first-class

**Decision:** A **project** is its own registry entity, managed in the UI:
a short `name` (the id shown on task cards), a human-readable `title`, an
**upstream git URL** (where PRs land), and an optional **fork URL** (the
operator's personal repo). Branches push to the fork when one is set and
PRs are opened against upstream from it; when the operator owns upstream,
the fork is omitted and branches push straight to upstream. Templates
(Decision 6) reference a project by name instead of embedding
checkout/remote details; the main checkout path is derived per project.
Removing a project is guarded while tasks reference it.

---

## Web app views

The screen inventory for the browser UI. Global elements first, then one
entry per view: purpose, key content, key actions.

### Global (visible on every view)

- **Attention badge** in the tab title/favicon: count of tasks needing the
  operator (`waiting-approval`, `waiting-input`, `reviewing`, `stalled`,
  `failed`).
- **Daemon connection indicator** (WebSocket state; the UI is stateless and
  reconnects with a snapshot).
- **GPG lock indicator**: signing key cached/locked, with the unlock
  instruction (terminal helper) when locked.

### 1. Tasks (home)

The "which agents need me" view — the reason the product exists.

- One card per task: project, task slug/branch, **status** (current
  workflow step + the Decision 4 session state, e.g. "validate:
  waiting-input" — visually tiered, interrupt/notify states must
  dominate), reason
  line ("waiting: 2 questions", "review open", "stalled 12m"), context %
  ring, token/cost figure, elapsed/last-activity time, PR link once
  shipped.
- Idle cards show the "may be waiting for a reply" decoration when the
  last-message heuristic fired.
- Sort/group: attention first, then by recency. Filter or tab for
  archived/finished tasks (PR link, outcome, cleanup state).
- Actions: open task, spawn task, quick-answer an ask question inline if
  it fits (single-select), review/ship an idle task, kill/cleanup a task.

### 2. Projects

Manage the repo pairs tasks run against (Decision 9).

- One entry per project: name, title, upstream URL, fork URL (or "you own
  upstream — no fork"), active-task count linking to a filtered Tasks
  view.
- Actions: create project (name, title, upstream, optional fork), edit
  (name rename guarded — referenced by tasks), remove with confirmation.

### 3. Spawn task

Template-driven task creation; must make launch latency visible.

- Fields: project template picker, task slug (→ branch name preview),
  prompt editor (multi-line, the main element), optional overrides (model,
  thinking level).
- After submit: pipeline progress (clone → workshop launch → agent ready →
  prompt sent) with per-step status, since workshop launch takes tens of
  seconds. Fail states surface stderr.

### 4. Task detail (focused session)

Live view of one task; rendered from the raw omp event passthrough.

- **Workflow strip**: the task's steps with the current one highlighted;
  finished steps expand to their outcome (`.ompire/outcome.json` /
  command output). One transcript tab per named session; the active
  step's session is focused by default.
- **Transcript**: streaming assistant text, collapsible tool-call cards,
  thinking blocks; subagent activity grouped under its parent tool call.
  (Prior art: `packages/collab-web` renders exactly this from omp events.)
- **Question cards**: pending `ask` rendered from the tool-call args —
  options with descriptions, multi-select, recommended highlighted,
  free-text "other" — answered inline. Approval prompts (rare; dormant
  under yolo) render as approve/deny cards.
- **Composer**: prompt input with mode control (steer / follow-up /
  interrupt-and-prompt), disabled states reflecting `isStreaming`.
- **Status strip**: state + reason, todos, context % (compact/handoff
  action at threshold), tokens/cost, model + thinking level.
- **Task metadata panel**: branch, clone path, workshop status, session
  file, escape-hatch instructions (`workshop shell` + `omp --resume`).
- Actions: review/ship, kill, archive; jump to review view when
  `reviewing`.

### 5. Ship flow (review → commit → PR)

A stepper attached to a task; llmvet itself opens in its own tab (the
daemon runs it and links its URL — this view wraps the loop around it).

- **Review step**: review state (llmvet session open), link to the llmvet
  UI, outcome display (approved / N comments sent back to agent /
  aborted). Comment-loop iterations are visible as history.
- **Commit step**: mode choice (squash default / retain-and-rewrite),
  agent-drafted commit message and PR title/body in editable fields.
  Blocked with the unlock instruction when the GPG key is locked.
- **Push/PR step**: progress (rewrite → push → `gh pr create`), resulting
  PR link.
- **Cleanup step**: post-merge status, cleanup action (workshop remove +
  delete clone) with confirmation.

### 6. Templates & settings

- Template CRUD: checkout path, remote, base branch, branch pattern,
  workshop additions source, omp flags/model defaults, prompt preamble.
- Daemon settings: notification preferences per attention tier, stall
  watchdog threshold, context-usage threshold, auth token management.

### UI design (2026-07-15)

High-fidelity mockups of the views:
<https://claude.ai/code/artifact/bf6a574e-ad7e-410d-8e22-69538f749e8a>

Design language: a terminal-native operations console (dense, monospace
identity, 13px base). Attention is encoded on two axes that never mix —
**tier is structural** (interrupt: colored card spine + tinted surface +
pulsing solid pill + primary action on the card; notify: spine + steady
dot; badge: neutral chip; silent: recessed card) and **hue is semantic**
(red dead, amber blocked/suspect, cyan question, violet reviewing, green
ok/shipped; the teal interactive accent is outside the semantic set).

Design decisions accepted from the mockup pass:

- `stalled` and `waiting-approval` share amber; tier structure
  disambiguates (fewer hues scan faster).
- During a pending `ask` the composer stays enabled (steer active,
  follow-up queues, with a note) — the turn is still in flight.
- Spawn failure is an inline annotation on the pipeline (stderr expands in
  place; task lands as `failed`), not a separate screen.
- The GPG lock is one global condition: the chrome chip and the ship
  flow's blocked commit step are the same state.

Updated 2026-07-16 for Decision 8 (same artifact URL): status pills gain a
step prefix ("fix: waiting-input") and multi-step cards a workflow trail;
a `gate` card shows loop-exhaustion escalation; task detail gains the
workflow strip (expandable step outcomes) and per-session transcript tabs;
spawn shows the template's workflow and lazy session startup; templates
gain the workflow field; `gate` joins the notify tier.

Updated 2026-07-16 again: "Fleet" is renamed "Tasks" throughout the UI,
and projects are first-class (Decision 9) with their own management view —
six views total.

---

## Open questions (brainstorm backlog)

- **`AgentHandle` abstraction:** the internal Python interface that hides
  the RPC protocol (and would admit an ACP adapter later).
- **Persistence:** manager's own state store (agent registry, history,
  templates) — format and location. Must now also cover workflow state
  (current step, step outcomes, iteration counts) so a daemon restart
  resumes the workflow, not just the sessions.
- **`.ompire/outcome.json` schema:** status vocabulary, artifacts/handoff
  payload, versioning.
- **Fallback LLM judge:** which model/provider it calls (host-side, via
  pi-auth-gateway?), its prompt, and the exact escalation rule when it is
  uncertain.
- **Step/outcome Python interface:** the shape workflow authors code
  against (step definition, outcome access, loop bounds).
