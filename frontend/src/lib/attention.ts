import type { AttentionEntry, Task } from "../types";

/** Count of tasks needing the operator: the number of tasks with an active
 * daemon attention entry (`notify`/`interrupt` tier, SPEC Decision 4), per
 * design D-7. Driven entirely by the daemon's `attention`/`attention_cleared`
 * events (plus the snapshot's attention map on reconnect) — the daemon owns
 * tier classification (design D-1), so the client no longer re-derives it
 * from a hardcoded status set. This is the single seam later chunks plug
 * further tiers into.
 *
 * A registry-level `failed` task (spawn pipeline failure, no session ever
 * tracked) still counts even without a daemon attention entry, since the
 * daemon's tier model only covers tasks with a tracked session. */
export function countNeedsAttention(
  tasks: Task[],
  attention: Record<number, AttentionEntry> = {},
): number {
  return tasks.filter((task) => task.state === "failed" || task.id in attention).length;
}
