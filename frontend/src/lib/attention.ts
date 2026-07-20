import type { SessionInfo, Task } from "../types";

/** Count of tasks needing the operator (interrupt tier, SPEC Decision 4, plus
 * the ask-approvals `waiting-*` badge tier). This is the single seam later
 * chunks plug further tiers into, so the header pill and tab-title badge
 * never need to be rewired. A task counts once when its registry state is
 * `failed`, its session status is `failed`, or its session status is
 * `waiting-input` / `waiting-approval` (a pending question or approval needs
 * the operator); `starting`/`working`/`idle` sessions stay silent (badge-tier
 * treatment for idle arrives with the notifications chunk). */
const ATTENTION_STATUSES = new Set(["failed", "waiting-input", "waiting-approval"]);

export function countNeedsAttention(
  tasks: Task[],
  sessions: Record<number, SessionInfo> = {},
): number {
  return tasks.filter((task) => {
    const status = sessions[task.id]?.status;
    return task.state === "failed" || (status !== undefined && ATTENTION_STATUSES.has(status));
  }).length;
}
