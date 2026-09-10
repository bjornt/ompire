# Task detail

## Overview

The task detail view is where an operator watches one task closely: what the
agent is doing, what it is asking, where the workflow has got to, and how to
get into the workspace by hand.

It is reached from a task's card, not from the nav.

## Using task detail

### Metadata panel

Project, branch, clone path, workshop identity with its derived status,
creation time, and elapsed time.

### Accepted configuration panel

The launch decision this task was accepted under: its workflow and the exact
**revision** it executes, the task-wide model profile and where it came from,
the effective base branch, Workshop additions source and preamble with any
task-local override marked, and one row per model consumer.

The revision is the content identity of the definition itself, and the panel
offers the normalized document for reading. That is the only honest answer to
"what procedure did this task run" once the installed definition has moved on.
A task whose continuation was confirmed after the upgrade also states its
boundary: which attempts ran under a definition that was never retained, and
which single attempt spans it.

Each consumer row — every declared agent step, and there are no others — names
its source profile, its role, the concrete model and thinking policy those
resolve to, and, separately for the profile and the role, whether that value
was inherited or set for this step. Expanding a row shows the complete
`smol`/`slow`/`plan` map that consumer's process carries.

These are the stored values, not a recomputation — a project or profile edited
or deleted since acceptance does not change what this panel shows or what the
task runs. A named profile is provenance; the snapshot beside it is what
executes.

While a session is live, a **Running now** table adds what omp reports it is
actually running: the active model, the thinking policy the profile states, and
the level omp resolved it to. Those last two differ legitimately for `auto` and
`max`, which resolve per model.

A session whose process is being replaced to apply a different policy reads as
*starting* while that happens, and reports no model until the new one is
verified. Transitioning is never shown as a successfully applied new policy,
and it is not a failure: the conversation is resumed, not restarted.

### Handoff inputs panel

Present only for a task launched with
[handoff inputs](task-spawn.md#handoff-inputs). It names the exact commit the
task was built from and, per pinned revision, the producing task, the
acceptance timestamp, and every installed file with its length and checksum.
Every entry is labelled **Handoff input — not publishable**. If the operator
acknowledged an unvalidated base, the panel says so, and each attachment states
how its own recorded base compared with this task's.

These are the task's stored inputs, read from its accepted launch document. The
panel keeps saying what the task ran with after the producing task is cleaned
up, after its revision is purged, and after the agent edited or deleted its own
working copies — none of which changes a retained revision or any other task
that pinned it.

The publication restriction is stated here because it is what matters when this
task's code is later shipped: Review and Ship refuse a delivery whose Git result
carries one of these paths. See [Ship flow](ship-flow.md#handoff-inputs).

A task created before launch inputs were recorded shows a **Configuration
needed** form instead: what is known, what is unknown and unrecoverable, and
the fields to confirm for future behavior. Confirming requires an explicit
acknowledgement and changes no recorded history; an interrupted run can then be
continued explicitly. An archived task of that vintage says so and asks for
nothing. See [Tasks](tasks.md#tasks-without-recorded-launch-inputs).

When a task has no live agent, the transcript, composer, and status-strip
regions degrade to an inactive or empty state rather than disappearing. A
region that vanishes reads as a bug; one that says "nothing here" reads as an
answer.

### Review panel

Every task detail shows one Review panel for the task's **primary session**.
It does not follow the selected transcript tab or the session currently used
by a workflow step. This keeps review and the next publishing handoff attached
to the task that owns them.

**Who starts a review depends on the workflow.** When the task's pinned
definition declares its own `review` step, the panel is a read-only view of
what the run is doing: no **Start review** button, and a line saying that the
run starts the review when it reaches that step — reviewing at another moment
would grade content the run is still changing.

When the definition declares none, the panel drives review. It says why review
is unavailable, or offers **Start review** when the primary session is idle and
has a live agent. Starting locks the action as **Starting…** until the daemon
reports the review. If the command is refused or fails, its error remains
inline and the operator can retry when the displayed state permits it.

While independent review is open, the full llmvet URL is a keyboard-accessible
external link and **Cancel review** is available. Cancellation similarly stays
locked until the daemon reports its outcome; a failed cancellation leaves the
observed review visible and restores the valid action.

A review restored after a daemon restart keeps its status and iterations but
has no live reviewer, so the panel offers no llmvet link for it. See
[Review](review.md#retention-and-restart).

The panel updates from the main daemon stream without a reload. It distinguishes
an open review, comments, approval, abort, review error, and a reviewer
interrupted by a daemon restart. Where the workflow declares its own review,
comments go to the correction step the definition names and the run opens the
next review itself; where it does not, comments go to the primary agent and
**Start another review** becomes available once that session returns to idle. It exposes the task's Ship flow link when review is approved, the
daemon has recorded a delivery, or the task has a pull request. The link reads
**Continue to Ship flow** only when the approval still covers the content that
would be published; a superseded approval reads **Open Ship flow**, because the
next step there is another review rather than a handoff. Both open
`/ship/<task-id>`.

An approval names the content it graded. When the task has moved on since, the
panel labels it **Approved (superseded)** and says that delivering the current
content needs a fresh review. An approval recorded before content-bound review
says instead that it does not identify what it approved. Neither is deleted:
both remain visible as history. See [Review](review.md).

Every iteration is ordered from oldest to newest and records its outcome,
recorded time, optional comment count, and any captured reviewer stderr. Error
output is available in an expandable, readable disclosure.

### Results panel

Files this task produced, retained outside its workspace. Capturing a result
needs no commit, push, or pull request, and a captured result survives cleanup.
The panel is independent of Review and Ship flow: keeping a result and approving
code are different decisions, and this panel makes only the first.

An empty panel says so — workspace files are not retained until they are
captured.

#### Capturing

Enter repository-relative file or directory paths, one per line, and choose
**Capture result**. A selected directory contributes its supported files
recursively, with their repository-relative paths preserved.

The panel states the limits before submission, and the daemon enforces them:

| Limit | Value |
|---|---|
| File types | UTF-8 `.md`, `.txt`, `.json`, `.yaml`, `.yml` |
| Files per bundle | 128 |
| Bytes per file | 1 MiB |
| Bytes per bundle | 8 MiB |
| Path components | 16 |
| Path length | 1,024 bytes |
| Capture deadline | 30 seconds |

These are fixed. They are not settings, and there is no way to raise them for
one capture.

Capture refuses, rather than interrupts, while another Ompire operation owns the
task's workspace — a review, a draft, a delivery, or a cleanup. Wait for that to
finish and capture again.

#### What is refused

An ineligible entry fails the whole capture and names the entry. A partially
honoured selection would be a bundle nobody asked for, so there is no such
result. Refused: paths outside the workspace, absolute paths and traversal,
symlinks in any component, multiply linked and special files, cross-device
entries, dot-prefixed names (`.git`, `.ompire`, credential and session
directories), the whole clone or its root, the reserved
`__ompire_result_manifest__.json` name, unsupported file types, invalid UTF-8,
and any bound above.

Files inside a selected directory whose names begin with a dot are skipped
rather than refused — the incidental metadata beside a planning directory is not
what was selected.

Recognizable credential material is refused too: private-key blocks, GitHub
tokens, authorization-header values, credential-bearing URLs, and Ompire's own
bearer token. The failure names the file and the reason and never quotes what it
found. This check is bounded and cannot recognize every secret. Retained files
are still sensitive, untrusted content — review them, and treat a download as
you would any file an agent wrote.

A capture that fails is recorded as a failed revision with its reason. It
retains nothing, never becomes another revision's predecessor, and leaves every
existing revision exactly as it was. Correct the selection or the workspace and
capture again.

#### Reviewing a revision

A completed capture appears as an unaccepted revision with its exact file list,
sizes, media types, and SHA-256 checksums, plus its provenance.

Provenance says what is actually known. A manual capture is attributed to the
operator, and the producing run, step, and session read **unknown**: the run's
most recent step is evidence that something executed, not evidence that it wrote
these files. A workflow-owned capture instead names its producing step and the
workflow attempt that retained the revision. The commit shown is labelled as an
observation made at capture time, distinct from the base branch recorded when
the task was launched. When the producer was launched with accepted result
inputs, the panel links each exact input revision back to its producing task.
Anything unrecorded is listed as a gap rather than guessed at.

Selecting a file shows its retained text as escaped source. Markdown is not
rendered, HTML and SVG are not executed, embedded resources are not fetched, and
agent-authored links are not followed. What is shown is the retained content,
not a fresh read of the workspace — later edits to the workspace do not change
it.

A revision captured after an earlier one offers **Compare with previous
revision**: added, changed, and omitted paths plus a text difference. An omitted
path means it is not in this bundle. It is not an instruction to delete
anything. A very large comparison is truncated, and says so; every file's
complete source and download stay available. When the previous revision was
purged or is unavailable, the panel names that instead of treating it as empty.

#### Accepting

**Accept this revision** records that the operator reviewed and is keeping
exactly the displayed files. The label beside it states the scope: it does not
approve code, answer a workflow question, or allow anything to be published.

A workflow gate can name a retained revision without turning acceptance into
publication authority. Its task card identifies the capture step and attempt
and links to Results. A finish choice marked **requires accepted result** is
refused until that exact readable revision is accepted; accepting a successor,
predecessor, or another result does not satisfy it.

Acceptance names the exact revision. A stale page — one showing a revision that
has since been superseded, purged, or found damaged — is refused, and the
operator reviews what is actually there before deciding again. Repeating a
successful acceptance changes nothing. Capturing or accepting a later revision
leaves every earlier revision and decision untouched; there is no floating
"latest accepted".

#### Starting a task from a revision

**Start task from this result**, on an accepted, readable revision, opens the
[Spawn form](task-spawn.md#handoff-inputs) with exactly that revision attached
and this project selected. It starts nothing: the operator chooses the
workflow, model profile, slug, and prompt, reviews the attachment and its base
comparison, and submits.

The action is offered only on an accepted revision. A complete revision nobody
accepted is a downloadable result, not an input another task may be launched
with.

#### Exporting into the project checkout

**Export to project checkout**, on an accepted, readable revision, copies
selected files from it into this project's registered checkout. It stays
available after the producing task has been cleaned up or archived, because it
reads the retained bytes and never the workspace.

The destination root is the project's own registered checkout and nothing else.
There is no field for an arbitrary directory and no way to export into another
project.

Select the files to export — all of them initially — and optionally enter one
checkout-relative destination prefix. An empty prefix keeps each file's own
repository-relative path, so `epics/design/EPIC.md` lands at that path. A prefix
of `handoffs` puts the whole tree under it, at
`handoffs/epics/design/EPIC.md`. Individual files are never renamed. Selecting a
subset can leave a relative link in an exported file pointing at something that
was not exported; the form says so, and nothing rewrites the content.

**Preview export** reads the retained revision and the actual checkout, and
changes nothing. It shows the revision, the full checkout location, the prefix,
the selected and omitted files, and a classification for every destination:

| Classification | Meaning |
|---|---|
| **Create** | Nothing is there. The file would be created. |
| **Already identical** | An ordinary file already holds exactly these bytes. It is left untouched, including its permissions and modification time. |
| **Conflict** | Different content, a directory where the file belongs, a symlink, a special or multiply-linked file, an unreadable entry, a nested repository boundary, or an unsafe path. |

A conflict shows its reason and, where the existing content is safe to display,
a bounded difference as escaped source. Content that is not valid UTF-8, is
past the size bound, or holds recognizable credential material is named as a
conflict without showing it — a preview never displays your own secrets back to
the browser. A large comparison is truncated and says so.

A difference is not an approval to replace. **Ompire never overwrites, deletes,
truncates, chmods, merges, or renames anything in your checkout, and there is no
force option.** While a conflicting file is selected the export cannot be
submitted. Resolve it by deselecting that file, choosing a different prefix, or
fixing the checkout yourself.

Every change to the selection or the prefix clears the preview: approval names
one exact set of destinations, and a new selection has to be reviewed as one.

**Confirm export** creates only the files listed as *Create*, plus the
directories they need, as ordinary non-executable, owner-private copies. It
commits nothing, pushes nothing, and opens no pull request. A target that
changed since the preview refuses before writing. If a destination is taken
after confirmation — an editor saved a file in between — that file wins and is
left exactly as it is.

Exported copies are ordinary files in your checkout. Unlike a task's handoff
inputs, nothing stops you committing them yourself later; Ompire adds no ignore
rules and takes no Git action either way.

A selection where every file is already identical is a valid export. It writes
nothing and is recorded as such.

#### Export history and recovery

Every export is recorded with who approved it, when, the exact revision, the
destinations, and a per-file outcome: **Created**, **Already identical**, **Not
installed**, or **Unknown**. The record survives a reconnect, a daemon restart,
and a purge of the revision it delivered.

Files are installed one at a time and each is installed whole. A bundle is not
atomic. An interrupted export can therefore leave some approved files created
and others not, plus directories it made along the way. Nothing is removed to
make that look tidy — deleting from your checkout is exactly what export will
not do — and the record names every created directory.

- **Not fully installed** means the known outcomes do not add up to the whole
  approved set. Nothing was overwritten. **Preview remaining export** restores
  that export's own selection and prefix into the form and reviews it against
  the checkout as it now stands: delivered files classify as already identical
  and receive no second write, and anything that differs is a conflict. It is a
  fresh review and a fresh confirmation, not a resumption of the old approval.
- **Outcome unknown** means at least one effect could not be established. This
  is a real answer, not a pending one. **Re-check the checkout** re-observes it
  read-only; it never retries a write. **Close as unresolved** records that you
  read the uncertainty and releases the revision and the checkout — it does not
  claim the unknown files were delivered, and the per-file outcomes still read
  Unknown afterwards.

Ompire never automatically re-runs or undoes an export after a restart. It
re-observes and classifies, and resuming is a fresh preview and confirmation.

While an export is running or unresolved, exports to that same checkout are
refused, this revision's files cannot be purged, and the project's checkout path
cannot be repointed. All three holds are released once the export settles or is
closed — unlike a launch input's hold, which is permanent.

#### Downloading

Any complete revision can be downloaded before or after acceptance, as a single
file or as one ZIP containing the original relative paths plus an Ompire
manifest at `__ompire_result_manifest__.json`. Downloads carry the retained
bytes, never a fresh read of the workspace, and work after the workspace is
gone. Nothing downloaded is executable. If any file in a revision is
unavailable, the whole download fails rather than producing a partial archive.

#### Unavailable and purged revisions

A retained revision whose bytes no longer match its manifest reads
**Unavailable** with the reason. It keeps its history, including its acceptance
if it had one, cannot be newly accepted or downloaded, and is never rebuilt from
current workspace files. Other revisions and the task stay usable.

**Purge this revision** permanently deletes one revision's retained files. It
confirms first, naming the revision, whether it was accepted, and the files and
bytes being removed. Purge is never part of cleanup. A purged revision stays
visible as a record — its identity, manifest, provenance, acceptance and purge
decision — with no readable or downloadable files.

A revision that a task was launched with cannot be purged. The panel names the
tasks holding it and the control is unavailable, because those tasks' records
say they ran with these files and an operator reading such a record has to be
able to read them. Cleaning up, failing, or archiving a consumer releases
nothing; purging that task's record is the only release, and it is refused by
the ordinary task-purge rules until the task is archived.

A revision with an unfinished checkout export cannot be purged either, and the
panel names it. That hold is temporary: it exists because the export is still
reading those bytes, or because nobody knows what it did, and it is released
when the export settles or is closed as unresolved. Purging afterwards deletes
the retained bytes and leaves the exported copies in your checkout exactly where
they are — they are ordinary files now, and the export record survives the
revision's tombstone.

Purge is logical removal. It makes no promise about database free space,
backups, or copies already downloaded, and the database file need not shrink.

An older task that captured nothing shows **No captured results**, rather than a
result reconstructed from its outcome text.

### Cleanup

**Clean up workspace** removes this task's clone and container. It is available
here as well as on the task card, because a task that produced a durable result
and no pull request never enters Ship flow at all.

The confirmation names the clone directory and the container, warns that any
workspace edits not captured as a result will be lost, and reports how many
result revisions are retained. Cleanup never purges a result: retained
revisions stay readable and downloadable afterwards, and the Results panel then
offers every action except capture, because there is no longer a workspace to
capture from.

Cleanup is refused while another host-side operation owns the workspace —
including an in-flight capture — and while a delivery is unresolved. See
[Tasks](tasks.md#cleanup-and-purge).

### Escape-hatch instructions

Copyable instructions for entering the task's container by hand: change to the
clone directory, open a workshop shell, and resume the agent inside the
container.

This is the deliberate escape hatch. Ompire orchestrates the work; it does not
imprison it. Anything the operator can do through the UI they can also do
directly, and the instructions name the actual paths for this task.

### Session tabs

When a task's workflow declares more than one session, a tab bar renders above
the transcript with one tab per declared session, showing its name and a live
status dot.

Selecting a tab switches the transcript, composer, status strip, and question
card to that session.

The default selection is the session of the workflow's current step when a run
is in flight, otherwise the workflow's primary session — so opening a running
task shows the session actually doing something.

Sessions not yet spawned render inactive as "not started" and **do not open an
event channel**. The tab bar is hidden entirely for single-session workflows,
leaving the `single-step` layout unchanged.

### Transcript

Streamed from the selected session's event channel. Tool executions render as
collapsible tool cards showing the tool identity and expandable to detail,
agent thinking renders as distinct thinking blocks, and subagent activity is
grouped under the parent tool call that spawned it.

The panel occupies a bounded, viewport-relative region: its heading stays put
and the stream below it is what scrolls, rather than the page growing. Wide
tool input and output still scroll horizontally inside their own cards. A conversation of any length therefore
leaves the rest of the cockpit — metadata, review panel, status strip, workflow
strip — where the operator left it, instead of pushing it further off-screen
with every tool call. On the narrow one-column layout the panel is bounded more
tightly so the composer below it stays reachable.

The stream follows the live output. It starts at the newest output and stays
there as text, tool cards, and tool output arriving on an existing tool card
extend it. Scrolling away from the end suspends that: output keeps arriving and
the stream keeps growing, but the view stays exactly where the operator put it.
Scrolling back to the end resumes following. "At the end" is deliberately
forgiving, so a stray wheel tick does not silently strand the operator behind a
live stream.

A stream that starts over starts at the newest output again — selecting a
different session tab, opening a different task, or the event channel
reconnecting and replaying the session buffer. Expanding a tool card is a
reader action, not new output, and never moves the view.

The stream is a keyboard-focusable scroll region with its own accessible name,
so it can be reached and scrolled from the keyboard and shows a visible focus
ring. Following is instant and never animated: no motion is introduced for an
operator who has reduced motion enabled. Streamed output is not announced
continuously to assistive technology.

### Question and approval cards

The selected session's pending question renders from the normalized `question`
payload, updating live.

An `ask` question renders each prompt with its options and descriptions,
single- or multi-select per the payload, the recommended option highlighted,
and a free-text "other" input when allowed. An approval gate renders as an
approve/deny card.

Submitting calls the session's answer endpoint with the question id and the
selection. The card disappears when the question resolves.

A pending question on a session **other** than the selected one surfaces as a
marker on that session's tab, so a question raised in a background session is
not silently missed.

### Composer

Steer, follow-up, and interrupt-and-prompt modes, each sending to the matching
endpoint of the **selected** session.

Enabled state derives from that session's live agent streaming flag and
status. Invalid modes for the current state are disabled, and the whole
composer is disabled when the selected session has no live agent.

The composer stays enabled while the session is `waiting-input`, with a note
that a question is pending — the turn is still in flight, so steering is
active and follow-ups queue.

### Status strip

The selected session's state and reason, plus the agent's todos, context usage
percentage, token and cost figures, and model.

State and reason update live from `status_changed`. The metrics come from the
session's state and stats endpoints and refresh at turn boundaries.

### Workflow strip

For a task with a workflow run: one chip per executed step record in order,
carrying the step name, kind, and status coloring, with the step's outcome
summary or error available on the chip. The run's overall status —
`running`, `waiting`, `complete`, `failed` — is visible alongside.

Updates live from `workflow_step` events and task payloads.

### Gate card

While a run is `waiting`, a card renders what it is waiting on. Three different
waits land here and the card keeps them apart.

A **gate with declared choices** shows the question, the evidence it is asking
about, and every answer the workflow offers with the destination each one
leads to. Nothing is selected for you, so opening the card authorizes nothing,
and the submit action stays disabled until you choose. A choice that declares a
required reason cannot be submitted without one, and the card says which choice
is asking. There is no generic Resume to bypass the choices with.

An answer that **authorizes publication** says so: the card names the exact
actions it would permit, and answering it here is not offered. It needs the
content, destination, identities, and final text the authorization is checked
against, so the card links to [Ship flow](ship-flow.md), where that preview and
the confirmation live. Every other answer at the same question — including
finishing without publishing — is answerable right here.

A **gate without them** (a format-1 workflow) shows its message, an optional
note, and a Resume action.

An **uncertainty pause** shows why the engine stopped and offers a retry of the
step that could not be decided, naming that step — retrying re-enters it rather
than skipping past it.

What the card renders is the question **as it was asked**, read from the
attempt's own record rather than from the workflow's current definition. A
decision answered last month still shows the message, the options, and the
records it was about, even if the definition has since changed.

The card disappears when the run leaves `waiting`. Anything typed into it
belongs to the attempt it was typed against: if the run moves on — another tab
answered, or a restart re-armed something else — the draft is dropped rather
than carried onto a different question, and a stale submission is refused
rather than applied. The pending state is also in the snapshot-driven task
payload, so the card survives reloads and reconnects; a gate that vanished on
refresh would strand the run.

### Procedure

The whole definition the task accepted, with what happened laid over it. It is
addressed by the task's **retained revision**, so editing or archiving the
workflow in the library afterwards changes neither this flow nor anything the
run recorded.

Every step of the definition is listed, whether or not it ran, with its kind,
assigned agent, abstract role, instruction, required results, the evidence it
reads, its visit bound, and every route declared out of it. Those routes are
declarations: nothing here evaluates a condition, so no branch is shown as the
one a run took or will take.

Selecting a step shows its attempts, each one separately:

| Shown | Meaning |
|---|---|
| No attempts | Work the procedure allows. Not skipped, not done — never reached |
| Several attempts | Every visit, in order. A rejected verification and the corrected one are both there |
| A declared result | The name the step wrote, with its summary |
| A stop without a decision | The engine refusing to guess, with the reason it gave |
| A recorded answer | Who chose it, which answer, any reason they wrote, and where it went |
| An evidence alias | A link to the exact producing attempt, by step and sequence number |

Where the record is silent, it says so. A step that declares no evidence was
handed none; an attempt from a format-1 run kept no bindings, so what it was
handed is not on the record. Neither is filled in from today's definition. A
visit count is the number of visits **on the record**, which for a run
predating step records is fewer than it actually had.

Answering a gate and retrying a stopped step remain the [gate
card](#gate-card)'s controls on the waiting attempt. A step the run has not
reached offers no action.

### Step history

Each attempt records which prior attempts it was handed. A finished run can
therefore say not just what each step concluded but which reproduction a fix
was given, and which fix a verification checked — by attempt, not by recency.

A run that ended under a format-2 workflow also carries its declared ending —
`validated`, `stopped-without-fix`, and so on — beside its `complete` status.
A format-1 run has no name for its ending and shows none.

## Interfaces

The view reads from the main WebSocket snapshot and deltas, and opens a
per-session channel at `/api/ws/agents/{task_id}/{session}` for the selected
session's transcript only.

Actions post to the session-addressed endpoints described in [agent
interaction](agent-interaction.md), to `/api/tasks/{id}/workflow/resume` for
gates and pauses — carrying the waiting attempt's sequence number and, at a
gate with declared choices, the chosen `choice_id` — and to
`POST /api/tasks/{id}/review` or
`POST /api/tasks/{id}/review/cancel` for the Review panel.

The Results panel reads and commands `/api/tasks/{id}/results` and its
revision-scoped routes, and receives committed changes as `task_results_updated`
on the main socket. Downloads use ordinary bearer authentication — there are no
token-bearing download links. Cleanup posts to `/api/tasks/{id}/cleanup`. See
the [API reference](api.md#task-results).
