# ADR 0028: Retain declarative workflow revisions and pin them to tasks

- Status: Accepted
- Date: 2026-09-06

Supersedes [ADR-0018](0018-keep-built-in-workflows-in-python-until-portable-versioning-is-required.md).
Extended by [ADR-0031](0031-let-operators-own-a-workflow-library-above-retained-revisions.md),
which lifts the packaged-only catalog boundary below while keeping every
revision rule here intact.

## Context

ADR-0018 accepted Python workflow definitions for a built-in-only phase and
listed the conditions that would end it. Two of them have arrived. Operators
must be able to author workflows without a daemon release, which means a
definition can no longer be reviewed control-plane code. And a task must keep
the semantics it was accepted under across daemon upgrades, which a stable name
cannot provide: a name resolves to whatever is deployed, so a release that
edits a prompt or a route silently changes what an in-flight run does.

The gap is not cosmetic. A task today persists its run position and its step
history but not the procedure that produced them. After an upgrade, a step
record says `triage` finished `ok` with an outcome — and nothing anywhere says
what `triage` asked, what routes it could take, or which of them it took.
Recovery re-drives that run against the *current* definition, which may declare
different steps in a different order.

A second, independent problem sits inside the engine. When an outcome file is
missing or a deterministic route cannot resolve, the current engine asks a
reserved LLM session to classify the result, and continues on what it says.
That judge is not a declared step, has no step record of its own, and is not
visible in the workflow the operator reviewed. ADR-0009 named this as the
conflict blocking its own acceptance. Whatever replaces the Python
representation must not carry the hidden judge across with it, because a
declarative definition that still routes on an undeclared model call is not
actually a description of what runs.

The migration also has to be honest about what it cannot know. Every existing
task recorded a workflow *name*. The document that name referred to at the time
was never stored and cannot be reconstructed, so there is no correct value to
fill a retained-revision field with for those tasks.

## Decision

A workflow definition is a document, not code. It is authored as a bounded YAML
subset, normalized into one canonical JSON form, and identified by the SHA-256
of those canonical bytes. That digest is the revision. A task pins one at
acceptance and executes it for the rest of its life; every runtime consumer —
the runner, restart recovery, session admission, the primary session behind
review and shipping, and the REST and WebSocket projections — resolves through
the task's own revision. Looking a workflow name up in today's catalog is
reserved for exactly two prospective questions: what a *new* launch would pin,
and what an operator is offered as a continuation candidate.

Retained revisions are append-only. The whole executable document is stored,
not just its identifier, because an identifier alone names a definition nobody
could still read. A stored document is decoded, re-validated, and re-hashed
back to the key it is filed under before it is executed, and revisions are
cached by content identity, never by workflow name.

The format carries its own semantics version. Format 1 fixes the grammar *and*
its interpretation: the outcome envelope, the outcome instruction, the resume
nudge, the missing-result rule, and the completion and recovery rules. Changing
what a retained document *means* requires format 2; a retained format-1
document is always read under format-1 rules, and an unsupported version is a
visible refusal rather than a reinterpretation.

The document is non-executable by construction, and this is a property of the
grammar rather than of a sandbox. Prompts are ordered part lists with literal
text, tagged value references, and conditionals — no expression source, no
attribute traversal, no second interpolation pass, no template engine. Value
references are a closed set of tagged data nodes over the task's pinned inputs
and its own step history. Commands are literal argument vectors. Interpolated
task or agent text is data and is never parsed again as definition. The loader
inspects parser events before building objects and refuses anchors, aliases,
merge keys, tags, duplicate keys, non-string keys, extra documents, and YAML
1.1's `yes`/`no`/timestamp scalars; scalars resolve by JSON's rules.

Predicates are three-valued. A missing operand or a type error is *unresolved*,
never false. An unresolved decision, an unresolved step condition, an
unrenderable prompt, and a required outcome that never arrived all stop the run
at that attempt, with the reason and the attempt's own absent result preserved.
The engine does not ask a model to classify a result it could not read, and it
does not fall through as if the missing evidence had been accepted. **The
implicit judge is removed, not relocated.** A declared negative result — a
`failed` outcome, a nonzero exit code — is not missing evidence: it is data,
and it follows the definition's own routes.

An operator retry opens another attempt of the *blocked* step. It never skips
it, never edits the recorded evidence, and pauses again if the same evidence is
still unreadable. It is a human decision, not an exemption: the retry counts
against the step's declared visit bound like any other attempt, and once that
bound is spent it routes to the declared exhaustion gate. Both retry and
declared-gate resume name the waiting attempt's sequence number, so a stale tab
or a double submit is refused rather than applied to a different attempt.

Every loop is cut by a declared visit bound whose exhaustion target is a gate
outside the loop. Validation rejects any cycle that survives removing the
bounded steps, and the engine counts attempts before opening a new one — so a
definition whose routing is wrong still cannot run forever, and a daemon
restart costs no visit because it re-drives the attempt already open.

No pre-upgrade task is assigned a retained revision. Its binding is explicitly
null, and the operator is offered the current definition of that task's own
workflow name as a candidate, with a compatibility check against the steps,
kinds, sessions, and position already on record. Confirmation is allowed only
when that candidate can account for all of them, and it records the revision,
the timestamp, and the boundary: everything through `legacy_through_seq` ran
under a definition nobody kept, and at most one attempt spans the boundary.
Confirmation governs future execution only; it starts nothing.

In this change the catalog is still daemon-packaged definitions only. There is
no project scan, upload, CRUD, or plugin loader, and a packaged definition that
does not validate fails daemon startup. That boundary was explicitly temporary;
[ADR-0031](0031-let-operators-own-a-workflow-library-above-retained-revisions.md)
lifts it by adding an operator-owned library *above* these retained revisions,
leaving them append-only and content-addressed exactly as described here.

## Consequences

A task's procedure is now a durable fact. An old run stays explainable after
the packaged definition changes and after its workflow name leaves the catalog,
because the document it ran is retained and readable by content identity. Two
revisions of the same name execute concurrently without special handling: they
are simply different rows. A prompt or route edit invalidates a reviewed launch
preview — the operator reviews a procedure, not a name — while an unrelated
workflow or profile change leaves it valid.

Uncertainty became visible where it used to be absorbed. Runs that previously
continued on a synthesized outcome now stop and wait, which is more operator
interruptions and better ones: each names the evidence it lacked. The
capability that produced those synthesized outcomes is gone rather than
disabled, so the retired `judge_model` setting and the per-task judge bindings
configure nothing; both are kept as inert upgrade evidence so the old choice
stays inspectable without staying live.

Authoring is now bounded by a grammar. Anything the closed operation set cannot
express cannot be written, and the escape hatch — arbitrary code — is exactly
what this decision removes. Extending the vocabulary is a format decision with
a migration cost, not a patch. That is the intended trade: the same closure is
what lets a definition be accepted from an operator without becoming trusted
daemon code.

The honest migration has a real cost. Every existing task is blocked from
further execution until a person confirms a continuation, and a task whose
history the current definition cannot explain stays blocked — readable,
stoppable, and cleanable, but not resumable. That is the intended outcome:
silently continuing such a run under a definition that does not match it is the
failure this ADR exists to prevent.

Retained documents accumulate and are never deleted. No garbage collection is
introduced, because a revision must outlive the workspace of every task that
references it; the documents are small and the growth is bounded by how often
definitions change.

What this decision does *not* claim: reproducible model output, pinned
binaries, credentials, tool versions, or working-tree contents. It pins
orchestration semantics. It also does not claim exactly-once agent or tool
execution — an idempotent command may re-run on recovery, which is why the
format requires commands to declare it.

Revisit this decision when definitions arrive from outside a daemon release —
which happened in
[ADR-0031](0031-let-operators-own-a-workflow-library-above-retained-revisions.md),
and changed only the packaged-only catalog boundary; when
the operation set genuinely cannot express a needed workflow, which is a
format-2 question; or if retained-document growth stops being negligible.

## Alternatives considered

### Hash the Python definitions instead of replacing them

Recording a source hash on each task would make drift detectable without
designing a format, and it was the cheap option ADR-0018 already anticipated.
It was rejected because an identity does not preserve what it identifies: the
hash of code no longer deployed cannot be executed, and keeping historical
modules importable would build an implicit plugin and migration system with the
same trust properties as the one being replaced. Retained normalized data plus
a versioned interpreter is stronger precisely because the retained thing is not
code.

### Embed a general expression language for prompts and routes

Jinja, or a small expression evaluator over the run context, would express
anything a Python callable could and would make the format feel unconstrained
in a good way. It was rejected because an operator-supplied definition would
then carry an evaluator's authority into the trusted control plane, and because
"what can this expression reach" stops being answerable by reading the
document. Tagged data nodes are less convenient and enumerable, which is the
property that matters when the author is not the daemon.

### Keep the judge as a declared step with an accepted fallback exception

Declaring the judge as a real step, and letting the engine invoke it when
evidence is missing, would preserve today's uninterrupted runs while satisfying
ADR-0009's provenance requirement. It was rejected for this change because a
fallback the engine reaches for on its own is still routing the operator did
not choose, whatever it is called. A declared semantic judge is a legitimate
future *step* — one a definition routes to explicitly, on a path the launch
preview shows — and that is a different thing from an engine reflex.

### Assign every legacy task the current definition of its workflow name

The migration could simply pin whatever `bugfix` means today, unblocking every
existing task with no operator action. It was rejected because it is a false
claim: those runs did not execute this document, and the records would then
attribute prompts and routes to a definition that never produced them. The
per-task confirmation is more work for the operator and is the only version of
this that is true.

### Store only the revision identity and re-derive the document from the package

Keeping just the digest, and reading the document out of the installed daemon,
would avoid duplicating documents in the database. It was rejected because it
reintroduces the failure being fixed one level down: after an upgrade removes
or edits that packaged definition, the pinned identity resolves to nothing, and
the task's own procedure becomes unreadable exactly when it is needed.
