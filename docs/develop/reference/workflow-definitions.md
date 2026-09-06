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
| `format` | yes | `1` or `2`. The grammar **and** its interpretation. |
| `name` | yes | Slug: lowercase alphanumerics and single hyphens |
| `sessions` | yes | Nonempty, unique, slug-format. `judge` is reserved. |
| `primary` | yes | Must be a declared session. Explicit — never defaulted. |
| `steps` | yes | Nonempty, ordered, uniquely named. `judge` is reserved. |

Unknown fields are refused, not ignored, everywhere in the document. So are
duplicate step names, undeclared session references, and routes to steps that
do not exist. Every refusal carries a location like
`steps[2].prompt.parts[0].value` and a reason.

### The two formats

Both are implemented and both execute. Format 1 is **frozen**: a retained
format-1 document is always read under format-1 rules, and its canonical bytes
— and so its revision — are unchanged by anything format 2 added. A field
belonging to one format is refused in the other, so the two vocabularies cannot
be mixed by accident.

| | Format 1 | Format 2 |
|---|---|---|
| Agent result | `expects_outcome: true\|false` | [`outcome`](#outcome-contracts-format-2), required, `null` or declared results |
| Reading prior attempts | [`latest`](#value-expressions), evaluated on each read | [`evidence`](#evidence-selectors-format-2) selectors, resolved once at attempt entry |
| Gate | `message` only | `message` plus [`choices`](#gate) |
| Completion | `{complete: true}` | `{complete: true, result: <slug>}` |
| Last step | may fall off the end | rejected at load time |

Format 2 has no `latest` and format 1 has no `evidence`: leaving both available
would leave the unfrozen read path available, and the point of binding evidence
is that there is only one way to read history
([ADR-0029](../../adr/0029-declare-domain-outcomes-and-evidence-handoffs.md)).

## Steps

Every step has `name` and `kind`, and may declare a visit bound.

### `agent`

| Field | Default | Meaning |
|---|---|---|
| `session` | required | A declared session |
| `role` | `default` | Abstract model role: `default`, `smol`, `slow`, `plan` |
| `prompt` | required | A [text document](#text-documents) |
| `expects_outcome` | `false` (format 1 only) | Whether a valid `.ompire/outcome.json` is required |
| `outcome` | required in format 2 | `null`, or the [results this step may declare](#outcome-contracts-format-2) |
| `evidence` | `{}` (format 2 only) | [Evidence selectors](#evidence-selectors-format-2) |
| `when` | `true` | A [predicate](#predicates), or a literal boolean |

`outcome` is required rather than defaulted in format 2. `null` is a real
declaration — "this step is not asked for a result" — and a step that silently
produced no contract would be indistinguishable from one whose author forgot,
when only one of those should be allowed to finish on nothing.

`when: false` makes the step *deliberately unprompted*. Its session is still
put on the accepted policy through the normal boundary, no prompt is sent, no
outcome file is read, and the attempt records that it was skipped on purpose.
It does not pause, because nothing was asked.

A prompt that **renders empty** means the same thing in format 1. In format 2
it pauses when the step owes a result: a definition that cannot ask for what it
requires is not a step that produced nothing.

An unresolved `when` does pause: "should this step run" is a question, and the
engine will not answer it by guessing.

### Outcome contracts (format 2)

```yaml
outcome:
  results:
    reproduced:
      required: {attempts: string, script_available: boolean}
    not-reproduced:
      required: {attempts: string}
```

`results` is a nonempty mapping from a slug result name to that result's
contract. `required` maps an artifact field to one of `boolean`, `integer`,
`number`, `string`, `array`, `object`. `null` is deliberately absent: a
required field whose accepted type is "nothing" would let an agent satisfy the
contract by writing nothing.

A document satisfies the contract when `version` is `2`, `result` is one of the
declared names, `summary` is a non-blank string, and every required field is
present with its declared type — a required `string` must be non-blank, and an
`integer` satisfies a declared `number`. Extra artifact fields are allowed as
bounded JSON data but cannot satisfy an undeclared requirement.

Anything else is not a result: the attempt pauses with a reason naming the
field. There is no recursive schema, no regex validator, no expression, and no
inferred result name. Bounds: at most 16 results, 32 required fields each, a
1 MiB document, and 32 levels of nesting; duplicate JSON keys and invalid UTF-8
are refused.

The step's prompt is suffixed with exactly these names and fields, so the agent
is told what it may declare rather than being asked to guess.

### Evidence selectors (format 2)

```yaml
evidence:
  reproduction: {steps: [reproduce, reproduce-informed]}
  rejection: {steps: [verify], after: fix, required: false}
```

| Field | Default | Meaning |
|---|---|---|
| `steps` | required | Nonempty list of declared step names |
| `after` | `null` | Only attempts newer than this step's own newest attempt |
| `with_outcome` | `true` | Exclude attempts that recorded no outcome |
| `required` | `true` | Whether a miss pauses the attempt |

`steps`, `after`, and `with_outcome` select exactly as format 1's `latest`
does. The difference is *when*: each selector is resolved once, when the
attempt opens, and the `{step, seq}` it selected is written on that attempt.
Every later read — the prompt, the routing decision, a gate message, recovery
after a restart — uses that recorded binding.

An alias is local to the step that declares it; reading another step's evidence
is refused at load time. Aliases cannot reference each other. Available on
every step kind, including `decision` and `gate`, so a route decides on the
same frozen records its neighbours were prompted with.

A **required** selector that matches nothing pauses the attempt before it
prompts or routes. An **optional** one binds to explicit absence, and that
absence reads as *missing* rather than as a null someone wrote.

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
| `choices` | format 2 only, yes | The answers this gate offers |

```yaml
choices:
  - id: retry-diagnosis
    label: Supply information and diagnose again
    feedback_required: true
    next: {step: diagnose}
  - id: stop
    label: Stop without a fix
    next: {complete: true, result: stopped-without-fix}
```

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Slug, unique within the gate |
| `label` | required | Non-blank text shown to the operator |
| `feedback_required` | `false` | Whether an answer must carry a reason |
| `next` | required | A destination naming a step or a named completion |

At most 8 choices, ordered. A choice's destination cannot be a pause and
cannot be computed: answering a gate is picking a declared edge, which is what
makes the decision replayable from the record and refusable when stale
([ADR-0030](../../adr/0030-commit-human-decisions-before-advancing.md)).

Choice destinations are **successors for graph validation**, so a loop built
out of human answers needs a visit bound like any other. A format-2 gate has no
fall-through: its choices are its only edges.

## Destinations

Exactly one of:

| Form | Meaning |
|---|---|
| `{step: <name>}` | Continue at that declared step |
| `{complete: true}` | Finish the run `complete` (format 1) |
| `{complete: true, result: <slug>}` | Finish the run with that declared ending (format 2, `result` required) |
| `{pause: true}` | Stop and wait for a person |

All destinations are static. There is no way to interpolate a step name from
agent output.

In format 2 a run may only end at a named completion. A step that would fall
off the end of the step list is rejected at load time, so the last declared
step must be a `decision` or a `gate` with choices. "The run ended" is not a
work result, and a reader months later cannot tell a validated fix from an
abandoned investigation if both simply stopped.

## Visit bounds

```yaml
max_visits: 3
on_exhausted: {step: investigation-exhausted}
```

Declared together or not at all. `on_exhausted` must name a declared **gate**
step, so an exhausted bound always reaches a human rather than completing or
pausing implicitly.

The bound is a budget for the whole run, not per entry into a loop, and nothing
refills it — including a human answer. A gate choice routing back to a step
that has spent its visits reaches that step's exhaustion gate instead of
opening another attempt, which is why an exhaustion gate typically offers only
stopping: it must sit outside the loop it ends.

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
| `latest` | `steps`, `after`, `with_outcome` | **Format 1 only.** A record view, or missing |
| `evidence` | `name` | **Format 2 only.** The record view this attempt bound under that alias, or missing |
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

`evidence` reads one of *this step's* [declared
selectors](#evidence-selectors-format-2) by alias. It yields the same
`{step, seq, status, outcome}` view, plus an `evidence` map of that record's
own bindings — so a route can ask not just what a verifier concluded but which
attempt it was looking at:

```yaml
# Did this verification check the fix that is actually current?
op: ne
left: {op: get, value: {op: evidence, name: verification}, keys: [evidence, fix, seq]}
right: {op: get, value: {op: evidence, name: current-fix}, keys: [seq]}
```

The difference from `latest` is when the selection happens, not how. `latest`
re-scans on every evaluation, so a prompt, the decision routing on it, and a
restart days later can each get a different answer. An alias was resolved once,
when the attempt opened, and every read returns that same record.

`get` traverses literal string keys and integer indices into JSON data only. A
missing key, a wrong container type, or an absent parent yields missing. It
never reaches a Python attribute.

History references are task-local and evaluate only records *preceding the
current attempt's sequence*, captured at entry. That is what lets a retried
step's prompt carry the previous iteration's report without reading its own
empty record — and it means a record produced *during* a step's own turn is not
visible to it.

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

Canonical output is **format-specific**, and format-1 bytes are frozen. A
format-1 step document carries `expects_outcome` and nothing format 2 added; a
format-2 one carries `outcome`, `evidence`, a gate's `choices`, and a named
`result` on every completion. Adding format 2 changed no format-1 byte, so
every retained format-1 revision still hashes to the identity it was filed
under — a property `test_workflow_definitions.py` checks directly, because
silent drift there would invalidate every pinned task at once.

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
   writing a new document, you need a new format version instead. Retained
   documents must keep being read under the rules they were written for.
4. Moving a packaged definition to a later format is a behavior change for
   *new* launches only. Existing tasks keep the revision they pinned, and a
   legacy task whose history was recorded under the older format can no longer
   be continued onto it — the compatibility check refuses, naming both the
   result-envelope mismatch and any step the new definition does not declare.

`daemon/tests/test_workflow_definitions.py` covers the loader's refusals, the
identity rules, and three-valued evaluation. `test_workflows.py` covers the
built-ins executing through the real engine.
