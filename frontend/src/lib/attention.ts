import type { TaskRecord } from "../types";

/** Count of tasks needing the operator (interrupt/notify attention tiers).
 * Returns 0 until real task states exist (ROADMAP chunk 3+, SPEC Decision
 * 4) — this is the single seam later chunks plug real tier logic into, so
 * the header pill and tab-title badge never need to be rewired. */
export function countNeedsAttention(_tasks: TaskRecord[]): number {
  return 0;
}
