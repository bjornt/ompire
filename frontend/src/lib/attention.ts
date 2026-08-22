// Central daemon-owned attention classification: ADR-0012
// (docs/adr/0012-derive-attention-centrally-from-session-state.md)

import type { AttentionEntry, AttentionTier, DaemonSettings, SessionInfo, Task } from "../types";

/** Whether the given attention entry counts toward the "N need you" total
 * under the effective daemon settings. Badge-tier entries count when that
 * tier's `badge` preference is enabled. Notify/interrupt tiers always count
 * regardless of the matrix (their `badge` preference controls only the tab
 * count, and those tiers are tracked separately). */
function entryCounts(entry: AttentionEntry, settings: DaemonSettings): boolean {
  if (entry.tier === "notify" || entry.tier === "interrupt") return true;
  return Boolean(settings[`tier.${entry.tier}.badge`]);
}

/** Whether a single task needs the operator's attention. A task needs
 * attention when its `state` is `failed` (registry-level, no daemon attention
 * entry needed) or when it has an attention entry whose tier contributes to
 * the count under the given settings.
 *
 * This is the single predicate driving the chrome chip count, the Tasks view
 * attention filter, and section assignment — they cannot disagree. */
export function isAttentionTask(
  task: Task,
  attention: Record<number, AttentionEntry> = {},
  settings: DaemonSettings = {},
): boolean {
  if (task.state === "failed") return true;
  const entry = attention[task.id];
  return entry != null && entryCounts(entry, settings);
}

/** Count of tasks needing the operator: delegates to `isAttentionTask` so
 * the count, chip, page filter, and sectioning always agree. */
export function countNeedsAttention(
  tasks: Task[],
  attention: Record<number, AttentionEntry> = {},
  settings: DaemonSettings = {},
): number {
  return tasks.filter((t) => isAttentionTask(t, attention, settings)).length;
}

/** Numeric severity rank for sorting attention tasks within the Needs-you
 * section. Higher rank = more urgent. `null` for tasks counted only via
 * `state === "failed"` (no attention entry). */
const SEVERITY: Record<AttentionTier, number> = {
  interrupt: 3,
  notify: 2,
  badge: 1,
  silent: 0,
};

/** Return the severity rank of a task's attention entry, or `null` when the
 * task is counted only because `state === "failed"`. */
export function getAttentionSeverity(
  task: Task,
  attention: Record<number, AttentionEntry> = {},
): number | null {
  if (task.state === "failed" && attention[task.id] == null) return null;
  const entry = attention[task.id];
  if (!entry) return null;
  return SEVERITY[entry.tier];
}

/** Section assignment for the Tasks view: "needs-you" for tasks needing
 * attention (interrupt/notify/failed), "running" for tasks with a silent-tier
 * entry or an actively working/starting session, "idle" for everything else. */
export type AttentionSection = "needs-you" | "running" | "idle";

export function attentionSection(
  task: Task,
  sessions: Record<string, SessionInfo> | undefined,
  attention: Record<number, AttentionEntry> = {},
  settings: DaemonSettings = {},
): AttentionSection {
  // Registry-level failed or an attention entry above badge tier → Needs you
  if (isAttentionTask(task, attention, settings)) return "needs-you";

  // Silent-tier attention entry (working/starting) → Running
  const entry = attention[task.id];
  if (entry?.tier === "silent") return "running";

  // No attention entry — check session statuses directly
  if (sessions) {
    for (const session of Object.values(sessions)) {
      if (session.status === "working" || session.status === "starting") return "running";
    }
  }

  return "idle";
}
