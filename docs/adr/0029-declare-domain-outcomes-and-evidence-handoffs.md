# ADR 0029: Declare domain outcomes and bind evidence to the attempt that used it

- Status: Accepted
- Date: 2026-09-06

Supersedes [ADR-0009](0009-use-structured-git-excluded-outcomes.md)
for workflow format 2. Extends, and does not replace,
[ADR-0028](0028-retain-declarative-workflow-revisions.md)'s format boundary:
this is a new format version, which is exactly what that decision requires of
a change to what a document means.

## Context

Format 1 gave an agent step one shared envelope: `status: "success" | "failed"`
plus a free-form summary and an untyped artifact bag. That is enough to say
whether a turn went well and nothing more, so every domain question had to be
re-encoded on top of it. The `bugfix` workflow read `status == "success"` on
its `reproduce` step and called the alternative "escalate", which is how a bug
nobody could reproduce and a bug that turned out not to exist became the same
recorded fact. Nothing checked that a step reporting a reproduction had left
any evidence of one, because there was nothing to check against: the artifact
names lived only in prose inside the prompt.

The second problem is *when* a definition reads history. Format 1's `latest`
re-scans the task's records every time it is evaluated. A prompt, the decision
that routes on that prompt's result, and a gate message rendered afterwards
each ask the question again, and a restart asks it again days later against a
history that has since grown. Usually they agree. When they do not, the
disagreement is silent and takes the worst possible shape: a validation that
passed against fix attempt one answering for fix attempt three. `after:` was
added to blunt exactly this, and it blunts it by *ordering* rather than by
identity — it can say "newer than the last fix", never "the fix I was given".

Both problems have the same root. Format 1 recorded what a step produced but
never what it was *asked for*, and never what it was *handed*. An operator
reading a finished run can see a sequence of outcomes and has to reconstruct,
from the definition as it exists today, which of them fed which.

A third, smaller gap: a format-1 run ends by falling off the end of the step
list. `workflow_status` becomes `complete`, and that is all the record says. A
validated fix, a fix nobody could validate, and an abandoned investigation are
indistinguishable afterwards.

## Decision

Workflow format 2 makes all three explicit, and removes the format-1
mechanisms that made them implicit.

**Results are declared.** An agent step declares `outcome: null` — no result is
asked for — or an `outcome.results` mapping from result name to the artifact
fields that result must carry, each with a JSON type. The envelope becomes
`{version: 2, result, summary, artifacts}`. A document is a result only if the
step declared that name and the artifacts satisfy the contract: required
strings must be non-blank, and null is never a substitute for a required value.
Anything else — an unknown name, a missing field, a wrong type, malformed JSON,
duplicate keys, invalid UTF-8, a document over 1 MiB or nested deeper than 32 —
is not a result. It reaches the existing uncertainty pause with a field-specific
reason, and a person decides.

A *declared negative* result is not that. `not-reproduced` and `no-root-cause`
finish their attempt `ok` and follow the route their author gave them. This is
the distinction format 1 could not draw, and it is the whole point: the engine
still never guesses, but a workflow can now say what a negative answer means
instead of having every one of them look like a failure.

Validation establishes structure and attribution only. That a step declared
`reproduced` and wrote the fields it promised says nothing about whether the
bug is real, and nothing here may be read as permission to act on the content.
An artifact is untrusted data, exactly as under ADR-0009.

**Evidence is bound at attempt entry.** A step declares `evidence`: named
selectors over prior attempts, using the same latest/after/with_outcome rules
format 1's `latest` used. The difference is when the question is asked. Each
selector is resolved *once*, when the attempt opens, and what it selected is
written on the attempt in `workflow_step_records.evidence_json` as alias →
`{step, seq}`, or explicit null for an optional selector that matched nothing.
`{op: evidence, name: …}` reads that binding. The prompt, the routing decision,
the gate message, and recovery after a restart all read the same records.

A required selector that matches nothing does not prompt and does not route: it
pauses the attempt — which exists, and says why — because an attempt that
vanished would take the explanation with it.

Format 2 therefore has no `latest`, and format 1 has no `evidence`. That is
deliberate: leaving `latest` available would leave the unfrozen path available,
and the whole value here is that there is only one way to read history.

A record view carries its own bindings, so a route can ask not just what a
verifier concluded but *which* attempt it was looking at. The packaged bugfix
uses this to compare a verification's bound fix against the current one, which
is a stronger check than sequence ordering alone.

**Endings are named.** Completion is `{complete: true, result: <slug>}`, and
the name is persisted on `tasks.workflow_result`. Falling off the end of the
step list is rejected at load time. A run that stopped can now say whether it
was `validated`, `validated-without-reproduction`, `stopped-without-fix`, or
`stopped-unvalidated`.

**No judge returns.** ADR-0028 removed the implicit judge and this preserves
that. A semantic assessment is an ordinary declared agent step with visible
inputs, an accepted model policy, declared results, and an explicit route for
its own uncertainty. There is no judge engine, no reserved consumer, and no
built-in judge turn.

Format 1's grammar, canonical bytes, protocol, gate semantics, routes, and
recovery are unchanged, and existing tasks keep executing under them. Because
the two formats read results differently, a format-2 definition is refused as a
continuation candidate for a task whose history was recorded under format 1:
those outcomes cannot be reinterpreted under a contract that reads results by
declared name, and no automatic upgrade is offered in their place.

## Consequences

An operator reading a finished run can see which attempt fed which, because
every consuming attempt names its sources by sequence. A stale approval is
detectable rather than merely unlikely. A workflow author can express a domain
answer directly instead of encoding it in a success flag and a prose summary,
and the engine enforces that the answer arrives with the evidence its author
said it must have.

The cost is a second interpreter to keep correct. Two formats coexist
indefinitely: format 1 is frozen and still executes, and every parse,
canonicalization, and protocol boundary is format-aware. Format-1 canonical
bytes must remain byte-identical forever, since any drift would change the
revision of every retained document — a property worth testing directly rather
than assuming.

Contracts are structural, not semantic. `required: {findings: string}` gets a
non-blank string; it cannot get a true one. Authors who read a satisfied
contract as a verified claim will be wrong, which is why the run's terminal
results distinguish `validated` from `validated-without-reproduction` rather
than leaving the limitation to prose.

Binding at entry means a step is handed what existed when it opened. A record
produced *during* its turn is not visible to it. That is the intended reading
of "what this attempt was given", but it is a real constraint on authors, who
must sequence a producer before its consumer rather than relying on both
running concurrently.

Existing rows keep NULL for both new columns. NULL means *not recorded*: no
pre-upgrade attempt froze a binding, and no pre-upgrade run declared an ending.
Backfilling either would manufacture history.

## Alternatives considered

### Keep one envelope and add a JSON Schema validator

A general schema runtime would express far more than required-field checks, and
that is the problem: the set of things a definition can demand stops being
enumerable, and a definition becomes something closer to code again — against
ADR-0028's non-executable property. Bounded field contracts cover the actual
need, and the loader can state its limits exactly.

### Keep `latest` in format 2 and add a separate frozen selector

Authors would then have two ways to read history with subtly different
guarantees, and the unsafe one would be shorter to write and would keep working
in every case where it happens to agree. The failure mode is a definition that
is correct until the day a loop runs twice.

### Record only the sequence number a consumer must be newer than

This is what format 1's `after:` does. It orders attempts without identifying
them, so it can prevent an *older* validation from answering for a newer fix
but cannot say which fix a validation actually checked. That is the question
that matters when the history has branched through a gate.

### Infer the terminal result from the last step

Cheap, and wrong in exactly the cases that matter: the last step of an
abandoned run and of a validated one may be the same gate, and the difference
lives in a human's choice rather than in the step's identity. A run's ending is
declared by the destination that ended it.
