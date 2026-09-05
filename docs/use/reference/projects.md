# Projects

## Overview

A project is a registered source repository and the routing Ompire needs to
publish against it. Every task belongs to exactly one project, so registering
a project is the first thing an operator does.

The project owns the checkout location and the upstream/fork routing. It does
not own spawn configuration — that belongs to [templates](templates.md).

## Fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Unique identifier, slug format. Used in paths and URLs. |
| `title` | yes | Human-readable name. |
| `upstream_url` | yes | Repository pull requests are opened against. |
| `fork_url` | no | Push target. Absent means push straight to upstream. |
| `checkout_mode` | no | `adopt` (default) or `clone`. Stored as `adopted`/`cloned`. |
| `checkout_path` | adopt only | Local checkout. Defaults to `<checkout root>/<name>`. Derived, and refused, in clone mode. |
| `fetch_remote` | no | The remote spawn fetches **in that checkout**. Defaults to `origin`. |
| `setup_state` | read-only | `ready`, `cloning`, or `failed`. |
| `setup_error` | read-only | Why setup failed, with the failing step's git stderr. |
| `default_model_profile` | no | Name of a [model profile](model-profiles.md), or `null` for no default. |

A slug is lowercase alphanumerics separated by single hyphens — `my-project`
is valid, `My_Project` and `-leading` are not.

Only `https://`, `ssh://`, and `git@host:owner/repo` URLs are accepted for
`upstream_url` and `fork_url`. Local paths, `git://`, `http://`, and
transports that make git run a helper command are refused before any git
command is built.

## Checkout modes

A project's base checkout is the repository Ompire clones each task workspace
from. It is registered one of two ways, and the mode is fixed at registration.

### Adopt an existing checkout

The default. You supply an absolute path to a checkout you already have.
Ompire accepts it only if it is the top level of a non-bare git work tree, has
a remote with the name in `fetch_remote`, and has at least one commit. The
answer is immediate: the project is either ready, or refused with the reason.

Ompire only ever **reads** that checkout. Registration adds no remote, runs no
fetch, and leaves the branch, index, working tree, and config untouched —
including when it refuses. Detected remotes are offered as suggested
`upstream_url` and `fork_url` values for you to confirm, never applied on
their own.

### Let Ompire clone it

Ompire derives the destination as `<effective checkout root>/<name>`, refuses
to start if anything already exists there, and clones the upstream in the
background, adding the fork as a second remote named `fork` when one is set.
A cloned checkout's `fetch_remote` is `origin`.

The clone is built beside the destination and moved into place only when it is
complete, so the destination path never holds a half-finished repository. It
runs with your own git configuration and no injected credential, with
interactive prompts disabled — a repository your git cannot reach fails
quickly with git's own message rather than hanging.

The rationale for both modes is in
[ADR-0022](../../adr/0022-create-or-adopt-base-checkouts-without-mutating-them.md).

## Fetch remote

`fetch_remote` names the remote **in the base checkout** that spawn refreshes
before cloning a task workspace. It defaults to `origin`, and a fork layout
that calls its upstream `upstream` sets it accordingly.

This is unrelated to the `origin` inside a task's own clone, which always
points at the base checkout and is what branch creation, review, and shipping
resolve against.

## Default model profile

A project may name one [model profile](model-profiles.md) as its default. The
field is optional and defaults to `null`; several projects may name the same
profile.

**This is stored configuration, not execution.** Tasks are still created from
[templates](templates.md) and still run with the template's `model` and
`thinking` values or a per-spawn override. The assignment is recorded for
workflow-first task launching; today it changes nothing about how a task runs.

The field is three-valued on `PUT /api/projects/{name}`:

| The request body | Effect |
|---|---|
| Omits `default_model_profile` | The stored reference is preserved |
| Sets it to `null` | The default is cleared |
| Sets it to a name | That profile becomes the default |

Omission preserving the reference is what lets an API caller written before
profiles existed update a project's title or URLs without silently clearing
its default. The Projects view's Edit panel always sends the field, because
the operator can see the selector — choosing **No default** there means clear
it.

A reference to a profile that does not exist is `422`, and the whole project
create or update is unapplied: no field lands, and in clone mode no setup job
starts. Deleting a profile a project still names is refused, naming the
projects; see
[Removing a profile](model-profiles.md#removing-a-profile). Removing a project
releases its reference and never removes the profile.

Existing projects have no default after upgrading. None is inferred from a
template, from provider credentials, from omp's own settings, or from the
project's name. Setup completion, setup retry, and a permitted rename all
preserve an assigned reference.

## Fork routing

A project with no `fork_url` means "the operator owns upstream — push straight
to upstream". The absent fork is represented explicitly as `null` rather than
an empty string, so clients can distinguish "no fork" from "not yet set".

When `fork_url` is present, task branches are pushed there and the pull
request is opened against `upstream_url`.

## Using projects

Create, edit, and remove projects from the Projects view. Each project renders
as a card showing name, title, setup state, upstream and fork rows, the
checkout path with whether it was adopted or cloned and which remote it
fetches, its default model profile or that none is configured, and a pill
counting its non-archived tasks that links to the Tasks view filtered to that
project.

A project with no fork shows the upstream row annotated "you own upstream — no
fork needed" and no fork row.

The view updates over the WebSocket. Creates, updates, deletes, renames, and
task changes are reflected without a reload. An empty list renders an empty
state rather than example cards.

### Creating and editing

Registration in both modes, and the Edit panel, offer an optional **Default
model profile** selector listing every saved profile plus **No default**. No
profile is auto-selected, and a project can be registered when none exist —
the selector then links to where to create one. If the profile a draft
selected has since been deleted, the selection stays visible and is marked
unavailable rather than silently becoming another profile or **No default**;
correct it before saving.

While a create or edit request is in flight the form stays on screen with every
field and button disabled, and the submit button reads `Creating…` or `Saving…`.
A second click cannot submit the same form twice.

The daemon's answer is what the view acts on. On success the create form shows a
short `Created <name>` confirmation and closes once the new card is in the list —
it never closes into a list without it. The card is there as soon as the daemon
responds; it does not wait for the matching WebSocket event, and the event
arriving afterwards does not add a second card. Other connected clients see the
new card from the event, also without a reload.

A rejection leaves the form open and re-enabled, with every field's text intact
and the daemon's own detail shown inline. A success whose body is not a usable
project record is reported the same way and adds nothing to the list.

## States and behavior

### Setup state

| State | Meaning |
|---|---|
| `ready` | The checkout is usable. Templates and tasks may reference the project. |
| `cloning` | Ompire is creating the checkout. Progress shows on the card. |
| `failed` | Setup did not finish. The card shows the failing step and git's stderr, and offers **Retry setup** and **Remove project**. |

An adopted project is `ready` the moment it is registered — validation already
happened. Only clone mode passes through `cloning`.

While a project is not ready it cannot be selected in the template editor and
cannot be spawned against; both surfaces say why rather than hiding it. A
project cannot be removed while its clone is running.

Retrying a failed setup starts from a clean slate: no partial tree is ever
left at the destination, so nothing has to be cleaned up first.

### Restart during a clone

A daemon restart kills a running clone. At the next startup, before any client
can see the project list, Ompire resolves every project left `cloning` against
the filesystem: a valid checkout at the destination makes it `ready`, anything
else makes it `failed` with "interrupted by daemon restart" and removes the
leftover partial tree. A clone is never restarted automatically.

### Guarded removal

Removing a project unregisters it and nothing else. **The checkout on disk is
never deleted** — not one you registered, and not one Ompire cloned. The
confirmation says so, and names the path that stays.

Deleting a project fails with `409` while **any** task row references it,
archived ones included, and while any template references it. The response
names the referencing tasks and templates.

Archived tasks block deletion deliberately: they are the historical record of
work done against that project. Purging them, and deleting or repointing the
templates, unblocks removal.

### Guarded rename

Project update accepts an optional new name. A rename requires zero
referencing task rows and zero referencing templates — the same guard as
removal, with no cascade.

A successful rename is atomic. Reads under the new name succeed, reads under
the old name return `404`, and a `project_renamed` event carrying the old name
and the full updated payload is broadcast.

While a project is referenced, the name field in the Edit panel is disabled
and annotated with the referencing count.

### File search

`GET /api/projects/{name}/files` lists the checkout's repository-relative
paths, so the Spawn view can offer them for a prompt's
[`@file` mentions](task-spawn.md#file-mentions).

`q` filters; `limit` bounds the result count and is capped by the daemon, and
the response reports whether the result was truncated. The listing follows the
repository's ignore rules: tracked files and files present but not yet
committed are listed, ignored files are not.

Only names are returned — never file contents, never absolute paths, and
nothing under `.git`.

## Failures and recovery

| Condition | Response |
|---|---|
| Name is not a valid slug | `422` |
| Name already exists | `409`, registry unchanged |
| Upstream or fork URL is not an accepted form | `422`, nothing created |
| `fetch_remote` is not a valid git remote name | `422`, nothing created |
| Adopted path is missing, relative, not a git work tree, or a subdirectory of one | `422` naming the reason |
| Adopted checkout has no remote named `fetch_remote` | `422` naming the remotes it does have |
| Adopted checkout has no commits | `422` |
| Clone mode was given a `checkout_path` | `422` — the destination is derived |
| Clone-mode destination already exists | `409` naming the path, nothing written |
| Clone job fails | Project becomes `failed`; `setup_error` carries the step and git's stderr |
| Editing a cloned project's `checkout_path` | `409` — the path is fixed |
| Edit or delete while setup is running | `409` |
| Template or task references a project that is not `ready` | `409` |
| Delete or rename while tasks reference it | `409` naming the tasks |
| Delete or rename while templates reference it | `409` naming the templates |
| Rename to a name already in use | `409`, both projects unchanged |
| `default_model_profile` names a profile that does not exist | `422`, the entire create or update unapplied |
| File search for an unknown project | `404` |
| File search when `checkout_path` is missing or is not a git repository | `409` naming the path |

Every rejection leaves the registry unchanged. Rejections surface inline in
the form or Edit panel, which stays open with the daemon's detail rather than
discarding the operator's input.

## Configuration

`checkout_root` is the parent directory a project's `checkout_path` is derived
from. It resolves as registry override → `config.toml` → `~/proj` and is
editable in [Settings](daemon-settings.md#the-recognized-settings). A change
applies to the next clone-mode registration; existing checkouts are never
moved and existing `checkout_path` values never change.

## Interfaces

| Method | Path |
|---|---|
| `GET` | `/api/projects` |
| `POST` | `/api/projects` |
| `POST` | `/api/projects/checkout-inspect` |
| `GET` | `/api/projects/{name}` |
| `PUT` | `/api/projects/{name}` |
| `DELETE` | `/api/projects/{name}` |
| `POST` | `/api/projects/{name}/setup/retry` |
| `GET` | `/api/projects/{name}/files` |

`checkout-inspect` looks at a path that is not registered yet and reports
whether it is usable, why not, and which remotes it has. It returns remote
names and URLs only — never file contents — and writes nothing.

`setup/retry` re-arms a failed clone-mode setup. It is `409` for an adopted
project, which has nothing to retry.

Projects appear in the WebSocket snapshot. Mutations broadcast
`project_created`, `project_updated`, `project_renamed`, and `project_deleted`;
a running clone also emits `project_setup_step` progress, which is transient —
`setup_state` and `setup_error` on the project are what a reconnecting client
reads.
