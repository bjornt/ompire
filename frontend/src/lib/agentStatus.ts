import { useEffect, useState } from "react";
import type { AgentStateData, AgentStatsData } from "../types";
import { getAgentState, getAgentStats } from "./api";

/* Status-strip data source: pulls `get_state` + `get_session_stats` from the
 * agent-interaction endpoints on view open and again at each turn boundary
 * (design decision — no per-tick polling). The exact field names for todos and
 * context usage are this change's open SPEC question, so the accessors below
 * read a few candidate shapes defensively and return null when absent. */

export interface AgentStatus {
  state: AgentStateData | null;
  stats: AgentStatsData | null;
}

export function useAgentStatus(
  taskId: number,
  enabled: boolean,
  turnEpoch: number,
): AgentStatus {
  const [status, setStatus] = useState<AgentStatus>({ state: null, stats: null });

  useEffect(() => {
    if (!enabled || !Number.isInteger(taskId)) {
      setStatus({ state: null, stats: null });
      return;
    }
    let cancelled = false;
    Promise.allSettled([getAgentState(taskId), getAgentStats(taskId)]).then(([state, stats]) => {
      if (cancelled) return;
      setStatus({
        state: state.status === "fulfilled" ? state.value : null,
        stats: stats.status === "fulfilled" ? stats.value : null,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [taskId, enabled, turnEpoch]);

  return status;
}

export function isStreaming(state: AgentStateData | null): boolean | null {
  return typeof state?.isStreaming === "boolean" ? state.isStreaming : null;
}

export interface TodoSummary {
  done: number;
  total: number;
}

export function todoSummary(state: AgentStateData | null): TodoSummary | null {
  const todos = state?.todos;
  if (!Array.isArray(todos) || todos.length === 0) return null;
  const done = todos.filter(
    (t) => t && typeof t === "object" && (t as { status?: unknown }).status === "completed",
  ).length;
  return { done, total: todos.length };
}

/** Context usage as a 0–100 percentage, or null if the agent didn't report a
 * shape we recognise. Tolerates a fraction (0–1), an explicit percent, or a
 * used/total token pair. */
export function contextPercent(state: AgentStateData | null): number | null {
  if (!state) return null;
  const percent = state.contextPercent;
  if (typeof percent === "number") return Math.round(percent);
  const usage = state.contextUsage;
  if (typeof usage === "number") return Math.round(usage <= 1 ? usage * 100 : usage);
  const used = state.contextTokens;
  const max = state.maxContextTokens ?? state.contextWindow;
  if (typeof used === "number" && typeof max === "number" && max > 0) {
    return Math.round((used / max) * 100);
  }
  return null;
}

export function modelName(state: AgentStateData | null): string | null {
  if (!state) return null;
  if (typeof state.model === "string") return state.model;
  if (typeof state.modelId === "string") return state.modelId;
  return null;
}
