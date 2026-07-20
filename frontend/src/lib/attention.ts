import type { SessionInfo, Task } from "../types";

/** Count of tasks needing the operator (interrupt tier, SPEC Decision 4).
 * This is the single seam later chunks plug further tiers into, so the
 * header pill and tab-title badge never need to be rewired. A task counts
 * once when its registry state is `failed` or its session status is
 * `failed`; `starting`/`working`/`idle` sessions stay silent (badge-tier
 * treatment for idle arrives with the notifications chunk). */
export function countNeedsAttention(
  tasks: Task[],
  sessions: Record<number, SessionInfo> = {},
): number {
  return tasks.filter(
    (task) => task.state === "failed" || sessions[task.id]?.status === "failed",
  ).length;
}
