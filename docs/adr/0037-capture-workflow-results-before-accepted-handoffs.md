# ADR 0037: Capture workflow results before accepted handoffs

- Status: Accepted
- Date: 2026-09-10

Extends [ADR-0028](0028-retain-declarative-workflow-revisions.md) with format 4,
[ADR-0029](0029-declare-domain-outcomes-and-evidence-handoffs.md) with a
producer-bound file selection, and [ADR-0034](0034-retain-durable-task-results-outside-the-workspace.md)
with workflow-owned captures. It preserves [ADR-0033](0033-scope-trusted-delivery-authority-to-the-workflow-run.md): accepting a result is not a delivery grant.

## Context

A planning task can create a useful proposal without a source commit. Manual
capture retains those bytes, but makes the operator reconstruct which agent
outcome named them, choose paths by hand, and separately decide whether the
workflow may finish. A successful agent summary only claims files exist; it is
not a retained, reviewable revision.

Letting an outcome directly complete a planning run would make an untrusted,
agent-authored string stand in for the bytes the operator needs to inspect.
Letting result acceptance generally advance a workflow would be worse: one
acceptance could accidentally satisfy a later question or acquire publication
meaning.

## Decision

**Format 4 adds a declarative `capture` step.** It names one evidence alias for
the producer outcome, text documents that render the exact repository-relative
paths, literal allowable roots, and an explicit successor. The runner uses the
attempt's already frozen evidence, validates the full selection, and delegates
retention to `ResultManager`. It records the capture result identity and
workflow provenance before taking the declared route. There is no generic
capture command, no path built from a current workspace scan, and no fall-through
on an absent or failed capture.

**A format-4 gate may bind one exact captured result.** Its persisted snapshot
contains the result and manifest identities. Only a choice explicitly marked
`requires_result_acceptance` rechecks, within the answer transaction, that this
same revision remains readable and accepted. A different result, a successor,
a stale answer, or acceptance alone cannot advance the run.

**Acceptance remains a result-service decision.** The Results surface is where
an operator inspects bytes and accepts them. A result-gate choice observes that
separate decision; it does not publish, does not imply a review verdict, and
cannot carry delivery authorization. Format 3's `authorize` remains the only
way a declared run can grant trusted delivery effects.

**The packaged planning workflow uses this protocol.** It routes one declared
proposal outcome to a bounded capture of its EPIC, SPEC, and PLAN files, then
allows feedback, a non-authorizing stop, or completion only after the named
revision is accepted. It exposes no review or delivery path.

## Consequences

- A planning proposal is a durable, exact result before its workflow can finish
  as accepted; cleanup cannot erase the reviewed bytes.
- Capture provenance now identifies its producing workflow attempt and records
  accepted result inputs, so a result panel can navigate both directions.
- Format 4 is forward-only. Older revisions retain their existing canonical
  grammar and execution behavior; format 4 inherits format 3's publication
  rules without adding an undeclared effect.
- A malformed producer binding, missing file, unsafe rendered path, unavailable
  revision, or stale result identity refuses visibly rather than silently
  substituting another result or falling through.

## Alternatives considered

### Require the operator to capture planning files manually

Rejected. It retains bytes but leaves the producer-to-files contract outside
the pinned workflow and makes a completed planning outcome dependent on an
unrecorded manual selection.

### Treat an agent outcome as the accepted proposal

Rejected. The outcome is a small, untrusted summary inside the disposable
workspace, not immutable proposal bytes suitable for inspection or handoff.

### Make accepting any result advance waiting workflows

Rejected. Acceptance is intentionally independent of review and publication.
A global transition would be ambiguous across revisions and could turn a
retention decision into authority.
