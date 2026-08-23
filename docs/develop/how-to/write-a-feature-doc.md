# Write a feature document

Feature documentation describes the product as it currently works. It is the
single authoritative place for current behavior, and it is useful to
operators, contributors, and agents alike.

It replaced cumulative capability specifications when the repository adopted
the lightweight change workflow.

## Where it goes

[`docs/features/`](../../features/), one file per feature, flat.

The directory stays flat because three change skills name that location. A
feature document is surfaced in the documentation navigation under the
reference section of whichever audience it serves — that placement is a
navigation decision, not a directory one.

## Shape

```markdown
# <Feature>

## Overview

## Using <feature>

## States and behavior

## Failures and recovery

## Configuration

## Interfaces
```

Use only the sections that apply. An internal feature has no "Using" section;
a feature with no configuration has no "Configuration" section.

## What to cover

- Purpose, and when the feature is used.
- The normal operator flow.
- Meaningful states and the actions available in each.
- Failures, unavailable states, and recovery.
- Configuration and externally meaningful interfaces.

## Rules

**Describe the present, not the change.** A feature document explains the
resulting current state permanently. The change's `SPEC.md` explains the
intended delta while the change is active, and then it is deleted.

**Revise, never append.** When behavior changes, update or remove the
superseded statement. Do not append a contradictory delta below it. A document
with two conflicting paragraphs is worse than one that is out of date, because
the reader cannot tell which is current.

**Do not restate it elsewhere.** Reference pages in the documentation sets link
to feature documentation rather than duplicating it. Only feature
documentation is reconciled automatically when behavior changes, so a copy
elsewhere is a copy that will drift.

**Rationale belongs in an ADR.** If you find yourself explaining why a design
was chosen, that is either an [ADR](write-an-adr.md) or an explanation page —
link to it instead.

## When it gets updated

Updating feature documentation is part of delivering a change, not a follow-up
task:

- `change-implement` updates it as behavior changes.
- `change-finish` verifies it stands alone without the change files, then
  deletes the change directory.

A change is not finished while its documentation still depends on artifacts
that are about to be deleted.

See [The change workflow](../explanation/change-workflow.md).
