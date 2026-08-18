import type { AttentionEntry, DaemonSettings, Task } from "../types";

/** Whether the given attention entry counts toward the "N need you" total
 * under the effective daemon settings. Badge-tier entries count when that
 * tier's `badge` preference is enabled. Notify/interrupt tiers always count
 * regardless of the matrix (their `badge` preference controls only the tab
 * count, and those tiers are tracked separately). */
function entryCounts(entry: AttentionEntry, settings: DaemonSettings): boolean {
  if (entry.tier === "notify" || entry.tier === "interrupt") return true;
  return Boolean(settings[`tier.${entry.tier}.badge`]);
}

/** Count of tasks needing the operator: the number of tasks with an active
 * daemon attention entry whose tier contributes to the count under the
 * received settings, plus registry-level `failed` tasks. The daemon owns tier
 * classification (design D-1); the client filters by per-tier `badge` prefs
 * delivered in the snapshot and `settings_changed` events.
 *
 * A registry-level `failed` task (spawn pipeline failure, no session ever
 * tracked) still counts even without a daemon attention entry, since the
 * daemon's tier model only covers tasks with a tracked session. */
export function countNeedsAttention(
  tasks: Task[],
  attention: Record<number, AttentionEntry> = {},
  settings: DaemonSettings = {},
): number {
  const countedIds = new Set<number>();
  for (const [taskIdStr, entry] of Object.entries(attention)) {
    if (entryCounts(entry, settings)) {
      countedIds.add(Number(taskIdStr));
    }
  }
  return tasks.filter((task) => task.state === "failed" || countedIds.has(task.id)).length;
}
