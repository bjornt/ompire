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
| `checkout_path` | no | Local checkout. Defaults to `checkout_root/<name>`. |

A slug is lowercase alphanumerics separated by single hyphens — `my-project`
is valid, `My_Project` and `-leading` are not.

The checkout at `checkout_path` must already exist with `origin` pointing at
the repository. Ompire fetches from it during spawn and never modifies it.

## Fork routing

A project with no `fork_url` means "the operator owns upstream — push straight
to upstream". The absent fork is represented explicitly as `null` rather than
an empty string, so clients can distinguish "no fork" from "not yet set".

When `fork_url` is present, task branches are pushed there and the pull
request is opened against `upstream_url`.

## Using projects

Create, edit, and remove projects from the Projects view. Each project renders
as a card showing name, title, upstream and fork rows, and a pill counting its
non-archived tasks that links to the Tasks view filtered to that project.

A project with no fork shows the upstream row annotated "you own upstream — no
fork needed" and no fork row.

The view updates over the WebSocket. Creates, updates, deletes, renames, and
task changes are reflected without a reload. An empty list renders an empty
state rather than example cards.

## States and behavior

Projects have no lifecycle state. They exist or they do not.

### Guarded removal

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

## Failures and recovery

| Condition | Response |
|---|---|
| Name is not a valid slug | `422` |
| Name already exists | `409`, registry unchanged |
| Delete or rename while tasks reference it | `409` naming the tasks |
| Delete or rename while templates reference it | `409` naming the templates |
| Rename to a name already in use | `409`, both projects unchanged |

Every rejection leaves the registry unchanged. Rejections surface inline in
the form or Edit panel, which stays open with the daemon's detail rather than
discarding the operator's input.

## Configuration

`checkout_root` in `config.toml` supplies the default parent directory for a
project's `checkout_path`. It defaults to `~/proj`.

## Interfaces

| Method | Path |
|---|---|
| `GET` | `/api/projects` |
| `POST` | `/api/projects` |
| `GET` | `/api/projects/{name}` |
| `PUT` | `/api/projects/{name}` |
| `DELETE` | `/api/projects/{name}` |

Projects appear in the WebSocket snapshot. Mutations broadcast
`project_created` and `project_renamed`.
