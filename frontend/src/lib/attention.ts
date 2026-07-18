import type { Task } from "../types";

/** Count of tasks needing the operator (interrupt/notify attention tiers).
 * This is the single seam later chunks plug real tier logic into, so the
 * header pill and tab-title badge never need to be rewired. With only
 * created/failed/archived states existing (ROADMAP chunk 3), `failed` is
 * the sole attention-tier state (interrupt, SPEC Decision 4). */
export function countNeedsAttention(tasks: Task[]): number {
  return tasks.filter((task) => task.state === "failed").length;
}
