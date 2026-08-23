# The bugfix workflow

## Overview

`bugfix` is the worked example of a rigorous workflow: reproduce the bug as an
executable script, fix it, validate the fix in a bounded loop, and escalate to
the operator when the loop is exhausted or the bug was never reproducible.

It exists to demonstrate what the [workflow engine](workflow-engine.md) is
for. Nothing in it is hidden or heuristic — every route is explicit, the loop
bound is deterministic, and every dead end is a human gate.

## Definition

Sessions `("reproducer", "coder")`, primary `coder` — so review, ship, and
task-scoped agent operations target the coder.

| # | Step | Kind | Session |
|---|---|---|---|
| 1 | `reproduce` | agent, outcome-bearing | `reproducer` |
| 2 | `triage` | decision | — |
| 3 | `fix` | agent, outcome-bearing | `coder` |
| 4 | `route-validate` | decision | — |
| 5 | `validate-script` | command | — |
| 6 | `validate-agent` | agent, outcome-bearing | `reproducer` |
| 7 | `check` | decision | — |
| 8 | `escalate` | gate | — |

Splitting reproduction and fixing across two sessions is deliberate: the
session that decides whether the bug still reproduces is not the session that
wrote the fix.

## States and behavior

### 1. Reproduce

The prompt is the template preamble joined to the task's stored prompt — the
issue — and instructs the agent to investigate, write an executable reproducer
at `.ompire/repro.sh` that exits non-zero while the bug is present and zero
once fixed, confirm the script currently fails, and finish through the outcome
file.

The outcome carries a summary and, when a runnable script was produced, a
`repro_command` artifact, plus `expected_behavior` and `observed_behavior`
when determinable.

### 2. Triage

| Latest `reproduce` outcome | Route |
|---|---|
| `status: "success"` | `fix` |
| `status: "failed"` | `escalate` |
| missing | Unresolvable — judge, then gate |

A bug that cannot be reproduced is operator triage, not a coding task. Sending
an agent to fix something nobody has demonstrated is how a plausible,
unverifiable change gets written.

### 3. Fix

The prompt carries the issue, the reproduction handoff — the latest
`reproduce` outcome's summary and artifacts, or an explicit note that no
structured handoff exists and the agent should inspect `.ompire/` and the
working tree — and, on loop revisits, the latest validation report.

It forbids editing `.ompire/` and weakening the reproducer, and instructs the
coder to **commit the fix on the task branch** and never push.

Committing is not optional. The review flow's reset dance unstages everything
for the reviewer, and the ship flow's squash commit is built from the branch
tip's tree — a worktree-only fix would ship as an empty commit.

### 4. Route validation

| Condition | Route |
|---|---|
| Latest `reproduce` outcome carries `repro_command` | `validate-script` |
| Otherwise | `validate-agent` |

Not every bug is scriptable — a visual defect, for instance — so the workflow
falls back to an agent verdict rather than pretending a script exists.

### 5–6. Validate

`validate-script` runs `bash .ompire/repro.sh` in the task's clone via
`workshop exec`, recording the exit code and output tail.

`validate-agent` sends **no prompt** and completes immediately when a
`validate-script` record newer than the latest `fix` record exists — the
script already answered, so an agent turn would be wasted. Otherwise it
prompts the `reproducer` to re-run the reproduction and judge the fix, with
outcome `status: "success"` meaning validated and `"failed"` meaning rejected,
its summary being the report.

### 7. Check

Reads the validation signal for the current fix iteration — the latest
`validate-script` or `validate-agent` record newer than the latest `fix`
record.

| Signal | Result |
|---|---|
| Script exit `0`, or agent `status: "success"` | Validated — run completes |
| Anything else, with fewer than 3 `fix` records | Route back to `fix` |
| Anything else, with 3 `fix` records | Route to `escalate` |
| Missing | Unresolvable — judge, then gate |

**The bound is three fix attempts.** It is deterministic and enforced by the
control plane, not by the agent deciding it has tried enough.

### 8. Escalate

The gate message names the cause — bug not reproducible, iteration bound
exhausted, or an unresolvable decision — and the current state of play. A
notify-tier attention entry is raised.

As the last declared step, resuming completes the run. The operator then
reviews and ships the coder's work, or steers the sessions manually.

## Failures and recovery

Every unresolvable route falls through the engine's judge-then-gate fallback:
the [LLM judge](workflow-engine.md#the-llm-judge) is consulted, and if it
cannot classify confidently the run parks at a synthesized gate rather than
guessing.

A completed or escalated run leaves the workspace and sessions alive until
cleanup.

## Using it

Set a template's `workflow` field to `bugfix`. Tasks spawned from that
template run this workflow instead of `single-step`.

The task prompt should be the issue — what is wrong, and how to observe it.
The workflow supplies the procedure.
