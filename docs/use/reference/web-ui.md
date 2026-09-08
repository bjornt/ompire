# Web UI

## Overview

The frontend is presentation only. It holds no authoritative state, makes no
decisions, and can be closed and reopened at any point without affecting
running work.

Everything it renders comes from the daemon's snapshot and delta stream.
Everything it changes goes through REST.

## States and behavior

### Global chrome

Every route renders a sticky header: the logo, nav links for Tasks, Projects,
Workflows, Spawn task, Ship flow, and Settings, and a right-side chip group.

Task detail is deliberately absent from the nav — it is reached from a task,
not from a menu.

| Chip | Shows |
|---|---|
| "N need you" | Attention count, derived from the daemon's tier model |
| Daemon | WebSocket connection state |
| GPG | Real signing-key lock state |
| GitHub | Current daemon GitHub CLI identity state |

The GPG chip renders one label per signing state — `gpg ready` (with a
remaining cache lifetime only when the agent reports one), `gpg locked`,
`gpg unselected`, `gpg no key`, `gpg missing`, `gpg agent`, `gpg error`, or
`gpg —` — sourced from the snapshot's `gpg` entry and `gpg_status` events,
never a static placeholder. Its accessible description names the condition.

**Settings → Daemon → Commit signing** shows the same state with
the selected key's fingerprint and user ID, how it was chosen, the last-check
time, the recovery action and terminal helper for the current state, and a
**Re-check key** control disabled while a request is in flight. When the host
holds more than one usable signing key it also offers a selector; choosing one
persists it and re-probes. No secret key material or passphrase appears there.

The GitHub chip renders `gh @login`, `gh missing`, `gh auth`, `gh error`, or
`gh —` from snapshot `gh` state and `gh_status` events. Its accessible
description is safe status text only. **Settings** shows the same
state with the login, host, credential-source label, executable path, version,
last-check time, and sanitized failure detail. Its **Re-check GitHub** action
is disabled while a request is in flight.

### The WebSocket client

Connects, receives an authoritative snapshot, then applies deltas. On
disconnect it reconnects and receives a fresh snapshot.

A reconnect loses nothing, because the client never held anything the daemon
did not also hold. The daemon chip reflects connection state so the operator
can tell "nothing is happening" from "I am not being told what is happening".

### Delivery preflight

A task's Ship flow resolves the requested ending against the daemon rather than
against browser state. The resolution lists **every** reason the delivery is
currently refused, so the operator fixes them together instead of one attempt at
a time, and the confirmation stays disabled until none remain.

GitHub eligibility gates only the endings that reach the forge; a local signed
commit is offered regardless. When it does apply, the banner compares the daemon
result to that specific upstream and current identity and never reuses an
allowed result for another target or account, offering a recheck for
missing-credential, authentication, denied, and error states.

The banner says explicitly that GitHub API eligibility does not prove SSH or
HTTPS `git push` authentication. The daemon repeats every preflight; browser
state only controls presentation.

The confirmation names every effect it permits — signing, pushing, and opening a
pull request are three different authorizations, not one button — and it carries
back a token over the exact resolution the operator saw. Any change to the
ending, mode, text, or content invalidates it.
### Model profiles and project defaults

**Settings** carries a **Model profiles** section beside the daemon panels. It
lists each saved profile with all four role bindings, model and thinking level
together, and opens an editor with four fixed rows and no implicit defaults.
There is no template panel: launch configuration is chosen per task, and the
project owns the workspace defaults it inherits.

Both surfaces state what a profile governs: launches that select it from now
on, never a task already accepted.

The panel is snapshot-gated in the same sense as the routes below: before the
current connection's first full snapshot it renders a loading state rather
than an empty saved list, because "you have no profiles" is a claim about
saved state it cannot yet make.

An open editor is keyed by the profile's name and holds its own draft. A
profile edited or deleted in another browser updates the saved list but leaves
that draft untouched; for a deletion the editor stays on screen with a note
that the original is gone. Save and remove lock their controls while a request
is pending, and a refusal — including a deletion refused with the referencing
project names — stays visible with the draft intact.

Project registration and the project Edit panel offer an optional **Default
model profile** selector that always includes **No default** and never
auto-selects. A selection whose profile has since been deleted is kept and
marked unavailable rather than silently changed. Each project card shows the
chosen profile or that none is configured, noting that a launch inherits it
unless the task selects another. See [Model profiles](model-profiles.md).

A project's Edit panel also carries its workspace defaults — base branch,
branch pattern, Workshop additions source, and standing preamble. A project
whose configuration was carried over from templates and still needs a decision
shows a **Launch configuration needs a decision** panel listing every distinct
old value with the template it came from, nothing pre-selected, and requiring
an explicit acknowledgement of any old model choice it supersedes and of any
retired `judge_model` it leaves configuring nothing. See [Projects](projects.md#launch-configuration-state).

### The Spawn view

The form is three selectors — workflow, project, model profile — plus a slug,
a prompt, and an **Advanced** section holding the four workspace overrides.
Each override shows the project's value until it is changed and then offers its
own reset; only changed fields are sent.

Beside it, the daemon's resolution of the current draft: the workflow revision
it would pin — readable as the whole procedure it declares, in the same
presentation the library uses — and every declared step of the chosen workflow
with its kind, session, abstract role, model, and thinking policy. Command, decision, and gate
rows carry no model. A step a route can pass by, or that carries its own
condition, is marked conditional. Every model consumer is one of these rows.

The resolution is re-fetched on every change to an effective choice, and a
slow response for an older draft is discarded rather than shown. Submitting
carries the token identifying what was reviewed; a resolution that changed in
between is refused, and the changed choices are shown to review and submit
again rather than launched automatically.

The draft survives leaving the view — going to Settings to create a profile, or
to Workflows to save one, and coming back restores everything typed. Opening
Spawn from a workflow's **Launch in Spawn** preselects that workflow and keeps
everything else.

Because the library is editable, the selected workflow can change underneath an
open form. A new executable revision of it invalidates the resolution and a new
one is fetched; the task-wide text, project, profile, and workspace choices are
kept, and any per-step model overrides are cleared with a visible notice rather
than reattached to steps that may have moved. Editing only that workflow's
draft changes nothing and refetches nothing. If it is archived or becomes
unreadable, it stays visibly selected with the reason and a link to the library,
submission is disabled, and no other workflow is chosen for you.

### The Workflows views

`/workflows` lists the library: name, origin (`builtin` or `custom`), and one
state phrase — *launchable*, *draft only*, *archived*, or the specific reason
its saved revision cannot be read. Archived entries are behind an explicit
checkbox rather than mixed into the list. Before the first snapshot the view
shows loading, never an empty library. A library with no custom entries offers
Create, Import, and the packaged examples to duplicate.

`/workflows/<name>` is one entry. A built-in shows its procedure read-only,
with its packaged text behind a disclosure and a Duplicate action; a custom
entry opens the editor.

The editor has two views of **one** draft — **Visual**, a list of step cards
with an agents panel, and **YAML**, the text — and switching between them is
neither a save nor a launch. Every field any format supports has a form
control, so a branching, reviewing, publishing workflow can be written without
opening the text view. A workflow's name and format are shown in both and
editable in neither: a name is the entry's identity, and a format is the rules
the document is read under.

The flow reading states what the document could publish, listing its declared
effects or saying plainly that it publishes nothing. An answer that authorizes
publication is drawn as its own kind of edge, naming the chain it grants —
reading it as an ordinary answer is how a diagram would hide what a click
permits.

A **review** card has no instruction and no model: it says what the reviewer
reads and what verdicts the steps after it can route on. A **delivery** card
names its one effect, how a commit composes history, the action whose result it
consumes, the approval that can authorize it, and where the run goes once the
effect is on record. On a gate, a *delivery binding* selects which of that
card's own evidence aliases is the review its grant rests on, and offers the
publication text the workflow suggests. Each answer can then name the exact
chain it authorizes, or none.

Renaming or removing a step moves — or visibly breaks — those references like
any other: a grant, an approval, and a consumed predecessor are references, and
a rename that missed one would leave an answer authorizing a step that no
longer exists.

Opening the visual view and leaving it without changing anything leaves the
text exactly as typed, comments included. Once something is changed visually,
saving rewrites the document — its layout is normalized and comments are
dropped, while what it means is unchanged. Text that cannot be parsed stays in
the YAML view with its line and reason, and the visual view refuses to open
rather than substituting an empty or last-valid document. An unsupported format
or construct is named, kept as written, and never silently converted.

A draft that parses but is not yet a workflow is ordinary work: it can be saved
and reopened, and the daemon's reason appears both in a summary and at the
field it is about, with a link that opens the card even while it is collapsed.
Renaming a step or an agent moves the routes, selectors, and assignments that
name it and leaves instructions and literal values alone; removing a step lists
what names it first and leaves any surviving references visibly broken rather
than repairing them.

The editor's three buttons do three different things — **Save draft**,
**Validate**, **Save executable revision** — and the view says which is which
rather than assuming it is obvious. A successful validation shows the revision
identity and a read-only reading of the definition: each step's kind, session
and role, its instruction, the results it must declare, where its routes go, the
answers a gate offers, what it reads as evidence, and its visit bound. That
reading is a presentation of the saved definition, not a third place to edit
one. Editing marks a validation result out of date rather than hiding it.

The editor's text is **local** until a save succeeds. A change committed
elsewhere updates the library everywhere and never overwrites an open buffer;
an unsaved buffer is marked unsaved and survives a lost connection. Importing a
file, or loading an older revision into the editor, asks before replacing
unsaved text, and so does leaving the page.

A refused save changes nothing and keeps the text exactly as typed. An edit
conflict offers three things and no fourth: copy the local text, discard the
edits and reload the saved version, or keep editing. There is no force and no
automatic merge.

Saved revisions are listed newest first. Any of them can be inspected, exported
as YAML, or loaded into the editor; a draft downloads separately and is labelled
a draft rather than a validated workflow.

Task detail carries the same presentation for the revision that task
*accepted*, with its recorded attempts laid over it. See
[Task detail](task-detail.md#procedure).

### Task detail configuration

Task detail shows the configuration the task was **accepted** with, not a
recomputation from today's settings: its profile and where that profile came
from, the effective workspace values with any task-local overrides marked, and
all four role bindings with which steps consume each. A live session also
shows the model omp reports it is running and the thinking level omp resolved,
beside the policy the profile states.

A task created before Ompire recorded launch inputs shows what is known, names
what is unknown and unrecoverable, and offers a confirmation for what should
happen from here on. The acknowledgement is explicit and unticked; confirming
pins future behavior and changes no recorded history. See
[Tasks](tasks.md#tasks-without-recorded-launch-inputs).

### Snapshot-gated routes

A route that needs to decide whether a task exists waits for the current
connection's first full snapshot. Socket open alone is not enough: it happens
before that message, and a reconnect replaces any previous projection. The
Ship flow index, `/workflows`, and `/ship/<task-id>` therefore render loading
until a snapshot; an unknown or non-numeric ship id after it provides recovery links
to Ship flow and Tasks. Any other unmatched application address renders a
**Page not found** surface inside the normal chrome rather than a blank view.

### Task sections

The Tasks view partitions visible tasks into three sections. A heading is
rendered only when its section is non-empty.

| Section | Contains |
|---|---|
| **Needs you** | `state` is `failed`, or an attention entry in the `interrupt` or `notify` tier |
| **Running** | An attention entry in the `silent` tier, or session status `starting`/`working` with no entry |
| **Idle/other** | All remaining non-archived, non-shipped tasks |

Shipped tasks keep their own separate section below.

### Sort order

Within **Needs you**: attention severity descending — `interrupt`, then
`notify`, then `badge` and `failed` — and `updated_at` descending within equal
severity.

Within **Running** and **Idle/other**: `updated_at` descending.

The severity-first ordering is the point of the section. The task that most
needs the operator is the top row, without them scanning for it.

### The attention chip

The "N need you" count is derived from the daemon's attention entries filtered
by the per-tier `badge` preference — not re-derived from raw session statuses.
The chip navigates to the attention-filtered Tasks view.

When attention clears, every surface updates together: the chip count, the tab
title, the section assignment, and the card styling. They cannot disagree,
because they all read one model.

### Theming

Design tokens with light and dark themes, sourced from the handoff bundle.

## Interfaces

The UI consumes the main WebSocket at `/api/ws` for registry, session,
workflow, review, ship, attention, and settings state, and per-session
channels at `/api/ws/agents/{task_id}/{session}` for transcripts.

It issues commands over REST. It sends nothing over any WebSocket.

Deep links work: any client-side route falls back to the SPA entry point when
no built file matches, so `/tasks/42` survives a reload or a pasted link.
