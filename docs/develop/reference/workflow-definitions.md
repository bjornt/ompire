# Workflow definitions

The exact format-1 document contract: what a definition may say, what changes
its revision, and how the interpreter reads it.

For what revisions *mean* operationally — pinning, coexistence, uncertainty
pauses, legacy continuation — see the operator reference,
[Workflow engine](../../use/reference/workflow-engine.md), and
[ADR-0028](../../adr/0028-retain-declarative-workflow-revisions.md).

## Where definitions live

Packaged definitions are YAML resources under
`daemon/src/ompire_daemon/builtin_workflows/`, read through
`importlib.resources` so an installed daemon finds them inside its own
distribution. They are declared as a package (`__init__.py`) so packaging tools
carry the `.yaml` files into the wheel; the snap installs that wheel, so no
separate packaging step is needed.

`workflow_definitions.py` owns the data model, the loader, canonicalization,
and the evaluator. It imports nothing from the registry, the task model, or the
supervisor: it answers "what does this document mean", and `workflows.py`
answers "how is that carried out".

## The document

```yaml
format: 1
name: single-step
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    role: default
    expects_outcome: false
    prompt:
      separator: ""
      parts:
        - text: "do the thing"
```

| Field | Required | Meaning |
|---|---|---|
| `format` | yes | `1`. The grammar **and** its interpretation. |
| `name` | yes | Slug: lowercase alphanumerics and single hyphens |
| `sessions` | yes | Nonempty, unique, slug-format. `judge` is reserved. |
| `primary` | yes | Must be a declared session. Explicit — never defaulted. |
| `steps` | yes | Nonempty, ordered, uniquely named. `judge` is reserved. |

Unknown fields are refused, not ignored, everywhere in the document. So are
duplicate step names, undeclared session references, and routes to steps that
do not exist. Every refusal carries a location like
`steps[2].prompt.parts[0].value` and a reason.

## Steps

Every step has `name` and `kind`, and may declare a visit bound.

### `agent`

| Field | Default | Meaning |
|---|---|---|
| `session` | required | A declared session |
| `role` | `default` | Abstract model role: `default`, `smol`, `slow`, `plan` |
| `prompt` | required | A [text document](#text-documents) |
| `expects_outcome` | `false` | Whether a valid `.ompire/outcome.json` is required |
| `when` | `true` | A [predicate](#predicates), or a literal boolean |

`when: false` — or a prompt that renders empty — makes the step *deliberately
unprompted*. Its session is still put on the accepted policy through the normal
boundary, no prompt is sent, no outcome file is read, and the attempt records
that it was skipped on purpose. It does not pause, because nothing was asked.

An unresolved `when` does pause: "should this step run" is a question, and the
engine will not answer it by guessing.

### `command`

| Field | Default | Meaning |
|---|---|---|
| `argv` | required | Nonempty list of **literal strings** |
| `timeout` | `600` | Positive, finite seconds |
| `idempotent` | required, must be `true` | Acknowledges that recovery re-runs it |

`argv` entries are literal strings by grammar: there is no way to build a
command from agent output. `idempotent: true` is required rather than assumed —
a restart re-runs an interrupted command, and the author has to say they know.

### `decision`

| Field | Required | Meaning |
|---|---|---|
| `cases` | yes | Ordered list of `{when: <predicate>, next: <destination>}` |
| `otherwise` | yes | The destination when no case is true |

`otherwise` is mandatory. Falling off the end of the cases is never implicit.

### `gate`

| Field | Required | Meaning |
|---|---|---|
| `message` | yes | A [text document](#text-documents) shown to the operator |

## Destinations

Exactly one of:

| Form | Meaning |
|---|---|
| `{step: <name>}` | Continue at that declared step |
| `{complete: true}` | Finish the run `complete` |
| `{pause: true}` | Stop and wait for a person |

All destinations are static. There is no way to interpolate a step name from
agent output.

## Visit bounds

```yaml
max_visits: 3
on_exhausted: {step: escalate}
```

Declared together or not at all. `on_exhausted` must name a declared **gate**
step, so an exhausted bound always reaches a human rather than completing or
pausing implicitly.

Two rules make loops finite, and neither trusts the routing:

- **Validation**: remove every step that declares a bound; whatever cycle
  survives is rejected. An exhaustion target that can route back into the
  bounded step is also rejected.
- **Execution**: the engine counts a step's attempts *before* opening a new
  one, and routes to `on_exhausted` when the bound is reached. A route
  predicate that keeps saying "go back" still cannot loop forever.

Resuming an attempt already open — restart recovery — consumes no visit. The
bound counts work, not daemon restarts. An operator retry after an uncertainty
pause *does* count: it is a work attempt, and once the bound is spent the retry
routes to `on_exhausted` rather than opening another one.

## Text documents

A prompt or gate message is an ordered part list joined by a literal separator:

```yaml
prompt:
  separator: ""
  parts:
    - text: "Reproduction report: "
    - value: {op: get, value: {op: latest, steps: [reproduce]}, keys: [outcome, summary]}
      format: text
```

A part is exactly one of:

| Part | Fields |
|---|---|
| literal | `text` |
| value | `value` (a [value expression](#value-expressions)), `format`: `text` or `json` |
| conditional | `if` (a predicate), `then` (a text document), optional `else` |

`format: text` renders strings, integers, booleans, and finite numbers.
`format: json` renders stable sorted-key JSON. Rendering an object or array as
`text` is an error, not a Python `repr`.

An empty document renders to the empty string, which is a real answer.

Rendering **refuses** rather than degrading. A value that is missing with no
declared fallback, an unresolved conditional, or rendered text over 1 MiB
pauses the run: a prompt that silently dropped a section would send an agent to
work with evidence the author said to include.

There is no template engine, no expression source, no attribute traversal, and
no second interpolation pass. Interpolated task or agent text is data — a
prompt containing something that looks like a value expression renders as that
text.

## Value expressions

Tagged data nodes, discriminated by `op`. The set is closed.

| `op` | Fields | Yields |
|---|---|---|
| `literal` | `value` | That JSON value |
| `input` | `name` | One pinned input (below) |
| `latest` | `steps`, `after`, `with_outcome` | A record view, or missing |
| `get` | `value`, `keys` | Literal key/index traversal into JSON data |
| `count` | `step` | How many attempts that step has |
| `coalesce` | `values` | The first non-missing, non-null value |

`input` names one of exactly four already-pinned facts: `task.prompt`,
`task.slug`, `task.branch`, `workspace.preamble`. There is no lookup into
today's project, a profile, the environment, or the filesystem.

`latest` selects the newest finished `ok` attempt among `steps` and yields
`{step, seq, status, outcome}` — a record *view*, not a bare outcome, because
"which step answered" is itself routing evidence. `after: <step>` excludes
attempts no newer than that anchor's latest attempt, whatever its status; an
anchor that never ran has sequence zero. `with_outcome: true` drops
null-outcome attempts before selecting, which is how a deliberately unprompted
step avoids masking the real evidence beneath it.

`get` traverses literal string keys and integer indices into JSON data only. A
missing key, a wrong container type, or an absent parent yields missing. It
never reaches a Python attribute.

History references are task-local and evaluate only records *preceding the
current attempt's sequence*, captured at entry. That is what lets a retried
step's prompt carry the previous iteration's report without reading its own
empty record.

### Missing is not null

Absence is a distinct internal value. A step that never ran, an outcome key
that was not written, and a record filtered out by `with_outcome` are all
*missing*. `null` is a value the agent wrote. An empty string is a value.
Conflating them is how "no evidence" silently becomes "a negative result".

## Predicates

| `op` | Fields |
|---|---|
| `eq`, `ne`, `lt`, `lte`, `gt`, `gte` | `left`, `right` |
| `exists` | `value` |
| `is_type` | `value`, `type` (a JSON type name) |
| `all`, `any` | `of` (a list of predicates) |
| `not` | `of` |

A bare `true` or `false` is also a predicate.

Evaluation is **three-valued**: true, false, or unresolved. A missing operand
or a type mismatch in an ordered comparison is unresolved, never false.

- `exists` is false for missing and null, true for anything else including an
  empty string.
- `is_type` is false for missing, and never calls a boolean a number.
- Equality compares JSON values with strict types; a boolean is never a number.
- `all` and `any` evaluate **in order and short-circuit**: a decisive operand
  wins even when a later one is unresolved. Otherwise unresolved wins.

That short-circuit is the idiom for guarding an optional value:

```yaml
op: all
of:
  - {op: exists, value: <maybe-missing>}
  - {op: eq, left: <maybe-missing>, right: {op: literal, value: 0}}
```

Without the guard, the comparison is unresolved and the run pauses.

## Loader bounds

Refused before any object is built, by walking parser events:

- anchors, aliases, merge keys, explicit tags, extra documents
- duplicate keys, non-string mapping keys
- more than 1 MiB, deeper than 32 levels, more than 10,000 nodes, more than
  256 steps

Scalars resolve by **JSON's** rules, not YAML 1.1's. `yes`, `off`, `on`, and
`2026-09-06` are strings. Only `null`, `true`, `false`, and JSON numbers are
non-string plain scalars; non-finite numbers are refused.

## Canonical form and revision identity

`canonical_document()` fills in every default explicitly and produces plain
JSON. `canonical_bytes()` serializes it with sorted mapping keys, preserved
sequence order, exact strings, UTF-8, and compact separators. The revision is
`sha256:` plus the full lowercase digest of those bytes.

So: comments and mapping-key order do not change identity. Executable string
contents and sequence order do. The digest is over the *normalized* document,
not the raw YAML, and never includes a timestamp.

The full digest is stored, not a prefix: this is a durable identity kept beside
tasks for as long as they exist, not a short comparison token.

## Retained storage

`registry/workflow_definitions.py` owns the `workflow_revisions` table. It is
append-only — no update, no delete — and reads are cached by *revision*, never
by workflow name.

A stored row is decoded, re-validated, and re-hashed back to the key it is
filed under before it is executed. A row that fails any of those is reported as
unavailable with a classified reason (`missing`, `unsupported_format`,
`integrity`, `invalid`) rather than executed.

## Adding or changing a packaged definition

1. Edit the YAML under `builtin_workflows/`. Startup validates it; a malformed
   built-in stops the daemon.
2. Its revision changes, so new launches pin the new one and existing tasks
   keep theirs. No migration is needed — that is the point.
3. If you changed what an existing *retained* document means rather than
   writing a new document, you need format 2 instead. Retained format-1
   documents must keep being read under format-1 rules.

`daemon/tests/test_workflow_definitions.py` covers the loader's refusals, the
identity rules, and three-valued evaluation. `test_workflows.py` covers the
built-ins executing through the real engine.
