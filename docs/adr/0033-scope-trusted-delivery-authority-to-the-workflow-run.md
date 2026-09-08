# ADR 0033: Scope trusted delivery authority to the workflow run

- Status: Accepted
- Date: 2026-09-08

Extends [ADR-0028](0028-retain-declarative-workflow-revisions.md) and
[ADR-0029](0029-declare-domain-outcomes-and-evidence-handoffs.md) with workflow
format 3, preserving format-1 and format-2 canonical semantics unchanged.
Supersedes, for format 3 only,
[ADR-0030](0030-commit-human-decisions-before-advancing.md)'s prohibition on a
gate answer conferring authority, and
[ADR-0032](0032-bind-trusted-delivery-to-retained-candidates.md)'s policy that
an operator may extend a completed delivery to a further ending. Both ADRs'
transaction, protected-candidate, and reconciliation rationale is carried
forward intact. Advances the run/review/decision/action linkage of
[ADR-0016](0016-persist-authority-bearing-task-history-and-provenance.md);
its wider commit-lineage and transcript-retention decision stays Proposed.
[ADR-0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)'s
trust boundary and
[ADR-0009](0009-use-structured-git-excluded-outcomes.md)'s untrusted-outcome
boundary are unchanged.

## Context

Review and publishing sat beside the workflow rather than inside it. A run
finished; then, separately, an operator opened a review, asked an agent to
draft some text, chose an ending from a menu, and confirmed it. The definition
the task had accepted said nothing about any of that.

Four consequences followed, and they were not cosmetic.

**Finishing the work was, in practice, permission to publish it.** Nothing in a
task's own procedure distinguished "this workflow is meant to produce a pull
request" from "this workflow is meant to leave a branch alone". The Ship page
offered all three endings for every task, so what got published was decided by
whoever was looking at the page, against a procedure that had no opinion.

**The correction loop was invisible and unbounded.** When llmvet returned
comments, the daemon prompted the task's primary session with them directly.
That turn appeared in no flow, spent no declared visit, and could not be routed
around — a definition could not say "send review comments to the coder, then
re-validate" because the daemon had already done something else. It also meant
review could only work on a task with a live, idle agent, which has nothing to
do with whether the content is ready to read.

**An approval identified content but not a decision.** ADR-0032 bound an
approval to a protected candidate, which closed the "signed something other
than what was reviewed" gap. It did not close a different one: nothing recorded
*which question* a person had answered, so a confirmation prepared against one
review could be replayed against the next one with identical content, and the
run had no way to say "the decision I am waiting for is this one".

**Extending an ending was a second, quieter authorization.** A completed local
commit could be pushed later, and then turned into a pull request, each on its
own confirmation. The operator who authorized "sign this locally" had not
authorized a push, and the record of what they *had* authorized was rewritten by
the extension that followed it.

The epic this change belongs to made publication an authoring concern. That
forces the question this ADR answers: if an author declares that a workflow
publishes, what exactly grants the right to do it, and to whom does that right
belong?

## Decision

**Publication authority belongs to the run, and is established by three
separate things.**

A definition *requests* operations. Format 3 adds two typed steps: `review`,
which runs the trusted host-side reviewer against the protected candidate and
records its verdict as ordinary evidence, and `delivery`, which performs exactly
one named effect — `commit`, `push`, or `pr`. A delivery step has no prompt, no
model, no argv, no target, and no idempotence flag. It names its action, the
approval that can permit it, the result it consumes, and where the run goes once
the effect is on record.

A person *grants* it. A gate may declare a `delivery` binding naming one of its
own evidence aliases, which may only select review steps. An approving choice
carries `authorize: {steps: [...]}` — the exact contiguous chain it permits,
starting at a local signed commit. The grant is a list of actions, never
"publishing" in general, and a delivery-capable gate must always offer an answer
that publishes nothing.

The trusted service *performs* it. `ReviewManager` and `ShipManager` remain the
only owners of capture, signing, push leases, forge writes, and reconciliation.
The runner decides when an operation is eligible; it never learns how one is
carried out.

**One admission resolver answers "may this happen now", for every caller.**
`runauthority.resolve_authority` reads durable state only — the pinned revision,
the current attempt, the persisted question and its committed decision, the
review iteration the question froze, and the delivery journal — and returns
either how authority would be established or why it cannot be. REST requests,
Ship-flow confirmations, direct service calls, and the runner all go through it,
and its refusals are what the UI renders. A caller supplies identities and
expected versions; it never supplies a verdict.

**The decision, the grant, and the run's next step are one transaction.**
`resolve_gate` already committed a gate answer with its successor. It now also
commits the delivery authorization the answer produces, on the same reserved
write. A crash cannot leave a person having approved publication with nothing on
record permitting it, or the reverse.

**Each effect is linked to the attempt that asked for it, before it happens.**
A delivery action's write-ahead intent carries its `workflow_seq`, unique among
live actions for that delivery; a review's process marker carries the attempt it
was launched for. A succeeded action lands its journal result and the run's step
transition together. If that write is interrupted, recovery attaches the
recorded result to the same attempt — it never dispatches a second effect to
close the gap.

**A restart never publishes.** An interrupted action is adopted when its effect
is proven to have happened, and otherwise held as a *continuation*: the same
attempt, the same journal context, waiting for a fresh preview and confirmation
within the authority already granted. It is deliberately not a generic retry,
which would open a new attempt and lose that link.

**An ending does not grow.** A run performs the chain its answer named, and
nothing more. Endings are still selectable — an author declares one chain per
approving choice, and separate choices may name separate chains — but the
selection happens when the person answers, not afterwards.

**Older definitions keep their semantics and lose new authority.** A format-1 or
format-2 revision executes exactly as before and can no longer acquire
publication authority: there is no step to grant it, and nothing infers one from
a workflow's name, a `complete` result, or a historical approval. A delivery
authorized before this format existed may still finish the prefix it was
genuinely granted; a one-time recorded boundary distinguishes such a row from a
new one whose links merely happen to be unset. There is no in-place run upgrade
and no automatic conversion — launching a new task from a new-format definition
is the supported path.

## Consequences

Publication becomes readable. A launch preview can say, before anything runs,
exactly which effects a workflow could perform and which answer would permit
each — or that it can perform none. A finished run's result names the ending it
reached, and the delivery journal says what actually happened independently of
what the author called it.

The correction loop becomes a declared edge. The reviewer's whole report is
retained as evidence with an explicit state (`complete`, `empty`, `truncated`,
`unavailable`), so a definition can route comments back to a working step and
require a complete report before doing so. Review no longer needs a live idle
agent, because it is a host-side operation on the workspace rather than
something an agent does.

Hidden turns are gone in format 3. The Ship page no longer asks an agent for
publication text; the gate's own `metadata` renders a suggestion from frozen
evidence, and the operator edits it. During an approval wait, daemon-managed
writers are refused with a pointer to the workflow's own correction choice —
because a turn started there would change the very content the decision is
about.

**The costs are real and deliberate.** Existing tasks pinned to format-1 or
format-2 definitions cannot be published through Ompire any more. That is a
capability people had; it is removed because the alternative is inferring
authority a procedure never declared. Operators who relied on extending a
completed commit into a push must instead author the ending they want, and the
existing ones stay as they were. And the packaged `single-step` is a complete
procedure now rather than a single turn: it reviews, asks, and can publish, so
it costs a review and a decision where it used to cost neither.

Format 3 is a larger grammar to hold in one's head, and a delivery chain is
verbose — three declared steps to open a pull request. That verbosity is the
readability: each action, its predecessor, and its approval are visible in the
document rather than implied by an ending name.

## Alternatives considered

### A capability list on the definition

`capabilities: [commit, push, pr]` at the top of a document, with the daemon
deciding when to run them. It is shorter, and it is the version of this feature
that cannot say *when* an effect happens, what it consumes, or which answer
permits it. Routing would have to move back into the engine, and the launch
preview would describe a set of powers rather than a procedure.

### A generic privileged-command step

One `kind: privileged` step with a command and arguments. It would have made
publishing an instance of something general — and made the grammar's job
impossible: there would be no way to validate that a push follows a commit, no
way to bind a grant to a chain, and no way for a preview to say what a
confirmation permits without executing it. Ompire is not becoming a CI system,
and this is where that boundary is enforced.

### Letting a gate answer grant authority directly, without a preview

The simplest reading of "a person approves publication" is that answering the
gate publishes. It is also how an approval stops being content-specific: the
answer would be given against the question's text rather than against the
current candidate, target, identities, and final text. Keeping the confirmation
bound to a preview is what makes a stale approval visible instead of quietly
honoured.

### Backfilling authority for existing runs

A migration could have marked completed format-2 runs as "authorized to
publish", preserving the capability people had. It would have been an
authorization nobody gave, recorded as though somebody had. Refusing, visibly,
and pointing at the new-format path is the honest version — and the one that
leaves the record readable.

### Retrying an interrupted delivery through the ordinary step retry

`retry_paused_step` already exists and re-enters a paused step. Reusing it would
have been less code and would have opened a *new* attempt for an action whose
old attempt still owns a write-ahead intent and possibly a partially observed
effect. The uniqueness that prevents a second signature is per attempt, so the
continuation resumes the same row instead.
