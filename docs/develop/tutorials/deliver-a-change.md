# Deliver a change end to end

This walks through the workflow used to land work in this repository, using a
small real change as the example. Read [The change
workflow](../explanation/change-workflow.md) first if you want the reasoning;
this is the mechanics.

## 1. Propose

```text
/skill:change-propose add a --version flag to the daemon
```

The skill researches the repository — instructions, `VISION.md`, relevant
feature documentation, relevant ADRs, the affected code and its tests — and
writes two files:

```text
changes/add-version-flag/SPEC.md
changes/add-version-flag/PLAN.md
```

Read them both before continuing. This is the cheapest point to disagree.

`SPEC.md` should describe what an operator experiences, not how it is built.
If it contains class names and database columns, the boundary slipped.

`PLAN.md` should have an ordered task list where every task maps back to
something the spec asked for, and every spec requirement maps to at least one
task. Documentation and ADR work appear as tasks, not afterthoughts.

If something is wrong, run the skill again with your correction. It refines
the existing directory rather than overwriting it.

## 2. Implement

```text
/skill:change-implement add-version-flag
```

The skill treats the plan as a hypothesis rather than truth. It validates each
task against the current code before editing, implements it, verifies it, and
checks the box.

Checkboxes in `PLAN.md` are the durable status. A session task tracker may
mirror them; it does not replace them.

When implementation reveals the plan was wrong:

| Discovery | Where it goes |
|---|---|
| The approach changes, behavior does not | `PLAN.md` |
| Desired behavior or scope changes | `SPEC.md`, **first** |
| A durable architectural decision emerges | A new or superseding ADR |
| Product behavior changes | `docs/features/` |

The rule that matters: **never weaken the spec afterwards to match what got
built.** If the implementation cannot satisfy the agreed spec, change the spec
deliberately and say so — do not quietly lower the bar.

## 3. Verify

Before finishing, everything must pass:

```sh
make test && make lint && make typecheck
```

For a change touching the task lifecycle, review, or shipping, run the
relevant end-to-end scenario as well:

```sh
local-test/scenarios/run happy-path
```

Unit tests do not exercise the real spawn, review, or ship paths. See [Run the
local end-to-end harness](../how-to/run-local-e2e.md).

## 4. Finish

```text
/skill:change-finish add-version-flag
```

The skill audits behavior against the spec rather than trusting the
checkboxes. It confirms the implementation satisfies every requirement, that
no obsolete path remains, that feature documentation stands alone without the
change files, that durable decisions are recorded as ADRs, and that the result
aligns with `VISION.md`.

Then it deletes `changes/add-version-flag/`.

That deletion is the point. Git and the pull request retain the delivery
history; the repository is not left carrying a directory nobody will read
again.

## 5. Commit

Commit the code, the updated feature documentation, and any ADR together. The
change directory is already gone.

## What you should end up with

- Working, verified behavior.
- [Feature documentation](../../features/README.md) describing the new current state —
  revised, not appended to.
- An [ADR](../../adr/README.md) if a durable decision was made.
- No `changes/` directory.

If feature documentation still needs the change files to make sense, the
change is not finished.
