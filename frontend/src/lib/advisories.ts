import type { StatsPayload } from "../types";

/** Mirrors the daemon's `context_advisory_threshold` default (config.py):
 * the frontend has no live view of the configured value (no Settings UI
 * yet — ROADMAP #20), so the task-detail status strip's own context-ring
 * styling (over the REST-polled `get_state` percentage, a separate data path
 * from the daemon's `stats`/`advisory` WS events) uses this default as its
 * threshold. Task cards instead key off the daemon's `context-high` advisory
 * directly and never need this constant. */
export const CONTEXT_ADVISORY_THRESHOLD_DEFAULT = 80;

/** The tokens/cost line shown on a task card and derived from the latest
 * `stats` sample, or `null` when neither tokens nor cost were reported. */
export function formatTokensCost(stats: StatsPayload | undefined): string | null {
  if (!stats) return null;
  const tokens = stats.tokens;
  const hasTokens =
    !!tokens && (typeof tokens.input === "number" || typeof tokens.output === "number");
  const hasCost = typeof stats.cost === "number";
  if (!hasTokens && !hasCost) return null;
  const parts: string[] = [];
  if (hasTokens) parts.push(`${tokens?.input ?? 0} in / ${tokens?.output ?? 0} out`);
  if (hasCost) parts.push(`$${stats!.cost!.toFixed(4)}`);
  return parts.join(" · ");
}
