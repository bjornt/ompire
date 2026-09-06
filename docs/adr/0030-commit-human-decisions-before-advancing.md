# ADR 0030: Commit a human decision before the run advances

- Status: Accepted
- Date: 2026-09-06

Extends [ADR-0008](0008-model-tasks-as-workflows-over-named-sessions.md)'s
human-transition model without changing task or session ownership. Advances the
decision-history slice of
[ADR-0016](0016-persist-authority-bearing-task-history-and-provenance.md); that record stays
Proposed for its outstanding publishing, transcript, retention, and lineage
gaps.

## Context

A declared gate in format 1 asks a person to look and offers one action:
Resume, with an optional free-text note. That is enough for "I have seen this,
carry on" and nothing more. A workflow that needs a person to *choose* — retry
with new information, proceed under an explicit exception, or stop — had no way
to say so. Its author could only phrase the alternatives in the message and
hope the operator's note said which one they meant, leaving the run to continue
down whichever single route the definition had wired.

The mechanism underneath had a worse problem. `resume_gate` validated the
request, then completed an in-memory `asyncio.Future`; the run coroutine woke,
finished the gate record, and opened the next attempt. The acknowledgement went
back to the operator before any of that was durable. A crash in that window
lost a decision a person had already made, and the next startup re-armed the
same gate as though it had never been answered. The reverse ordering is just as
bad in a different way: advancing first and recording afterwards can leave a
successor attempt whose authorization is nowhere on record.

Two submissions of the same question are indistinguishable from a stale browser
tab, and both must be refused rather than applied to whatever the run happens to
be waiting on by then. `expected_seq` already carried that check for the
format-1 resume, but the check and the write were not in the same transaction.

There is also an evidence problem. A gate's message is rendered from the task's
history at the moment it parks. If the definition is later edited, or the
history grows, a recorded answer stops being readable: the record says a person
chose to proceed, and nothing says what they were shown.

## Decision

A format-2 gate declares a nonempty ordered list of `choices`. Each choice has
a unique slug id, a non-blank label, a `feedback_required` flag, and a static
`next` naming a step or a named completion. A choice cannot contain a
predicate, cannot compute a destination, and cannot pause. Its edges are
ordinary graph edges: they participate in cycle and exhaustion validation, so a
loop built out of human answers needs a declared visit bound like any other, and
a bound reached through a human answer routes to its exhaustion gate rather than
opening another attempt. No answer refills a budget.

**The question is persisted before it can be answered.** Parking a gate writes a
snapshot — the rendered message, the offered choices with their destinations,
and the evidence identities it is asking about — into the attempt's outcome.
That snapshot is what a client renders and what a submitted choice is validated
against, not the definition as it stands today. An unanswered gate re-armed
after a restart is the same question, re-broadcast rather than re-rendered.

**The answer commits before the run moves.** One transaction records the
decision on the waiting attempt, completes that attempt, and either opens the
successor with its own frozen evidence bindings or lands the run complete with
its named result. Only after that commit is the parked run notified. The future
is a notification, not the authority.

This makes both crash windows safe. Before the commit, the gate is still
unanswered — which is true, and what recovery re-arms. After it, recovery finds
the successor the answer already opened, and never re-arms the answered gate or
opens a second attempt for one decision.

The recorded decision carries the choice id, the label as shown, the exact
feedback, the resolved destination, a server timestamp, and the actor
`operator`. Actor is deliberately that literal: at a single-user authenticated
boundary the bearer token says "the operator", and recording a guessed name
beside a durable decision would be worse than recording none. The decision is
added *beside* the question snapshot, never over it.

Refusals are separated by what the operator can do about them. A choice this
gate does not offer, or a missing required reason, is `422` with the offending
field named — the person is looking at the right question and gave an answer it
does not accept. A stale `expected_seq`, a run that is no longer waiting, or a
gate that has already been answered is `409` — the daemon has moved on, and the
client re-reads rather than re-submitting. `choice_id` is required at a gate
with choices and refused everywhere else: an uncertainty pause is not a question
with options, and answering one with a choice would be answering something
nobody asked.

Feedback is data. It is stored verbatim, rendered as text, and handed to a later
prompt as content. It never names a route, and no choice can grant authority the
definition did not declare — in particular, no gate answer starts review, signs,
pushes, or creates a pull request.

Format-1 gates keep their exact semantics: Resume, an optional note, and
fall-through to the next declared step. They gain only the durable write path.

## Consequences

A gate becomes a real question with real alternatives, which is what lets a
workflow stop for a person without either guessing or dead-ending. The bugfix
flow can offer "retry diagnosis", "proceed without a reproduction", and "stop"
as three distinct recorded outcomes, and the run's ending says which was taken.

A decision survives every interruption around it, and a repeated or stale
submission advances nothing. This is verifiable rather than argued: the
post-commit, pre-schedule window is exercised directly.

The cost is that a gate's question is now duplicated state. The snapshot is a
copy of what the definition would render, and the two can drift if the
definition changes — which is the point, but it means the record is the
authority and a reader comparing it to today's definition may find them
different. That is correct and should not be "fixed" by re-rendering.

Answering requires knowing the choice ids, so the API is no longer a bare
Resume. Clients must render what the daemon sent rather than what they know
about a workflow. Nothing is pre-selected, so opening a card authorizes
nothing, and a required reason cannot be skipped — both of which make answering
slower on purpose.

The actor is not a person. When Ompire grows multiple identities, `operator`
will be insufficient and this decision will need revisiting; recording it as a
constant now is honest about what the current auth boundary knows.

## Alternatives considered

### Keep Resume and put the choice in the note

The route would then depend on parsing free text, which is the class of
inference this system exists to avoid. It also cannot be validated: a typo
becomes a silently different decision, and the record cannot say which
alternatives were even available.

### Acknowledge the answer, then write it

What the implementation did. It is faster to write and loses decisions on a
crash in the acknowledgement window. No amount of narrowing that window removes
the class of failure, because the window is the design.

### Write the answer, then let the run coroutine open the successor

Splits one decision across two transactions. A crash between them leaves an
answered gate with no successor and no run — a state recovery would have to
guess its way out of, by re-deriving the destination from a definition that may
have changed. Committing the successor with the answer means recovery only ever
finds states the daemon actually intended.

### Let a choice carry a predicate or a computed destination

Tempting for expressing "retry, unless the budget is spent". It would make the
recorded decision insufficient to explain the route taken, since the same choice
could lead somewhere else on a different day. The budget case is handled where
budgets already live: the engine routes a bounded successor to its exhaustion
gate, and the record shows both the answer and where it actually landed.
