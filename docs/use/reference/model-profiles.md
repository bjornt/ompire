# Model profiles

## Overview

A model profile is a named, reusable set of model choices. It maps each of
omp's four model roles to a concrete model **and** a thinking level, so the
choices can be made once and selected as the default for several projects.

Profiles are global. They belong to no project and carry no repository,
workflow, prompt, or credential settings — only model identifiers and
reasoning levels.

**Profiles do not run tasks yet.** Saving a profile, and assigning one to a
project, is stored configuration only. Tasks are still created from
[templates](templates.md) and still run with the template's own `model` and
`thinking` values, or a per-spawn override. Nothing on this page changes a
running task, a newly spawned task, or the reviewer. The assignment is
recorded now so that workflow-first task launching can use it; until that
lands, a project's default profile has no effect on execution.

The rationale is in
[ADR-0025](../../adr/0025-store-global-model-profiles-separately-from-launch-policy.md).

## Fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Unique identifier, slug format. Cannot be changed after creation. |
| `roles` | yes | Exactly the four roles below, each with `model` and `thinking`. |
| `created_at` | read-only | When the profile was created. |
| `updated_at` | read-only | When its bindings were last replaced. |

A slug is lowercase alphanumerics separated by single hyphens — `balanced` and
`cheap-and-fast` are valid, `Balanced` and `-fast` are not.

### The four roles

Every profile binds all four, in this order. There are no other roles, no
custom aliases, and no way to leave one out.

| Role | Intended use |
|---|---|
| `default` | The ordinary active agent |
| `smol` | Lightweight work |
| `slow` | Thorough reasoning |
| `plan` | Planning |

The same model and level may be used for several roles — the roles are
distinct bindings, not distinct models.

### Model identifiers

`model` is a provider-qualified identifier, split at its **first** slash:
everything before it is the provider, everything after is the model id. Later
slashes belong to the model id, so `openrouter/Qwen/qwen3-coder` names the
provider `openrouter`.

The provider segment is letters, digits, dots, underscores, and hyphens, and
starts with a letter or digit. Both segments must be non-empty.

Leading and trailing whitespace is trimmed. Case and the model id's own
punctuation are kept exactly as entered, so a suffix that is part of the
model's name — `openrouter/qwen3-coder:free`, or a dated id like
`anthropic/claude-opus-4-1-20250805` — survives unchanged.

Rejected: whitespace inside the identifier, control characters, backslashes,
URLs, and the characters `*`, `?`, and `#`. A bare fuzzy name such as `sonnet`
is also rejected — that is what a template accepts, but a profile binding is
concrete.

A trailing `:<thinking level>`, such as `openai/o3:high`, is rejected with a
message pointing at the role's thinking field. That form is how the level is
passed to omp on the command line; accepting it in a profile would hide a
second level inside the model field where the two could disagree.

### Thinking levels

`thinking` is required on every binding and must be exactly one of `off`,
`minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `auto`.

There is no null, empty, or "omp default" choice for a profile binding. A
template may leave thinking unset — a profile may not, because the point of a
profile is to say what a role uses rather than to defer.

### What validation does not check

Validation is structural. Ompire checks the shape of the identifier and the
spelling of the level, and nothing else. Saving a profile **never** contacts a
provider or model endpoint.

It is therefore not a claim that the provider exists, that your credentials
are configured, that the model is available to you, or that the model supports
the level you chose. A profile that saves cleanly can still name a model you
cannot reach. No model is ever substituted for another.

## Using model profiles

Model profiles live in their own section of **Templates & settings**, beside
the existing template and daemon settings, which are unchanged.

The list shows each saved profile's name and all four role bindings, model and
thinking level together, sorted by name. Before the daemon's first state
arrives the panel says it is loading — an empty saved list is a claim it does
not make until it knows. With no profiles saved, it explains what a profile is
and offers **New model profile**.

### Creating and editing

The editor has a name field and four fixed rows, one per role, each with a
model text input and a thinking selector. New fields start empty: no role is
given a vendor, model, level, or another role's value on your behalf, and the
thinking selector opens on a "choose a level…" placeholder that is not itself
a value.

A profile's name is its identifier and cannot be changed. Editing an existing
profile shows the name as fixed text and replaces all four bindings together —
a save is a whole-profile replacement, not a per-field edit.

While a save or removal is in flight the controls are disabled, so the same
form cannot be submitted twice. On success the row updates as soon as the
daemon answers; the matching broadcast arriving afterwards does not add a
second row.

A rejection leaves the editor open with everything you typed intact, and shows
the daemon's own detail — naming the role and field at fault for a bad
binding. The saved profile is unchanged: an invalid replacement never lands
partially, so the three good rows in the same save are not written either.
**Discard changes** closes the draft and changes nothing.

If another browser edits or deletes the profile you have open, the saved list
follows the daemon but your draft is left alone. For a deletion the editor
stays on screen with a note that the original is gone, so you can copy what
you need; saving then reports that it no longer exists rather than recreating
it. Two valid saves of the same profile do not merge — the last one committed
wins.

### Removing a profile

Removal asks for confirmation first.

If any project uses the profile as its default, removal is refused and names
every referencing project, including projects that are still cloning or whose
setup failed. Clear or reassign those defaults and the removal succeeds.
Nothing is cascaded: a refused removal changes no project.

Removing a *project* releases its reference and never removes the profile.

## Project defaults

Projects may select one profile as their default. The selector appears in
project registration — in both adopt and clone modes — and in the project Edit
panel, and always offers **No default**.

No profile is ever auto-selected, and registration never requires one: a
project can be created before any profile exists, and the selector links to
where to make one when the list is empty. Several projects may select the same
profile, and reassigning or clearing one project's default changes only that
project.

Each project card shows the chosen profile name, or that no default is
configured, with a reminder that the assignment is stored for launching and
does not override today's template-driven tasks.

If the profile a draft selected has been deleted since you chose it, the
selection stays visible and is marked unavailable. It does not silently become
another profile or **No default** — pick a replacement before saving.

See [Projects](projects.md#default-model-profile) for how the field behaves on
the project API.

## Durability

Saved profiles and project assignments are stored in the daemon's database.
They survive a browser reload, a reconnect, and a daemon restart. Rejected
operations never publish a successful-looking change.

Existing projects have no default after upgrading. None is inferred from a
template's model, from configured provider credentials, from omp's own
settings, or from the project's name — nothing recorded before profiles
existed says which one you would have picked.

## Failures and recovery

| Condition | Response |
|---|---|
| Name is not a valid slug | `422` |
| Name already exists | `409`, registry unchanged |
| Unknown profile on read, update, or delete | `404` |
| A role is missing, or an unknown role is supplied | `422` naming what is missing or unrecognized |
| A binding is missing `model` or `thinking`, or either is null | `422` naming the role and field |
| A binding carries an unknown field | `422` naming the field |
| Model is not provider-qualified, or contains rejected syntax | `422` naming the role and field |
| Model carries a trailing `:<thinking level>` | `422` pointing at the thinking field |
| Thinking is not one of the eight levels | `422` naming the role and field |
| Delete while projects reference the profile | `409` naming every referencing project |
| Project references a profile that does not exist | `422`; the whole project create or update is unapplied |

An invalid reference on clone-mode registration refuses before any clone job
starts, so nothing is created or cloned.

## Interfaces

| Method | Path | Success |
|---|---|---|
| `GET` | `/api/model-profiles` | `200`, profiles sorted by name |
| `POST` | `/api/model-profiles` | `201`, the created profile |
| `GET` | `/api/model-profiles/{name}` | `200`, the profile |
| `PUT` | `/api/model-profiles/{name}` | `200`, the replaced bindings |
| `DELETE` | `/api/model-profiles/{name}` | `200`, `{"deleted": "<name>"}` |

`POST` takes `{"name": "<slug>", "roles": {...}}`; `PUT` takes only
`{"roles": {...}}`, because the name is immutable. Each role value is
`{"model": "provider/model-id", "thinking": "high"}`. Read, create, and update
responses carry `name`, `roles`, `created_at`, and `updated_at`.

```json
{
  "name": "balanced",
  "roles": {
    "default": { "model": "anthropic/claude-sonnet-4.5", "thinking": "medium" },
    "smol":    { "model": "openai/gpt-4.1-mini",         "thinking": "off"    },
    "slow":    { "model": "openai/o3",                   "thinking": "high"   },
    "plan":    { "model": "google/gemini-2.5-pro",       "thinking": "max"    }
  },
  "created_at": "2026-09-05T10:00:00+00:00",
  "updated_at": "2026-09-05T10:00:00+00:00"
}
```

Unknown fields anywhere in a request body are errors, not ignored
configuration.

The complete sorted list appears in the WebSocket snapshot as
`model_profiles`. Mutations broadcast `model_profile_created` and
`model_profile_updated` with the full profile, and `model_profile_deleted`
with `{"name": "<slug>"}`. Only committed mutations are broadcast — a refusal
publishes nothing.
