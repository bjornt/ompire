import { ContextRing } from "../components/ContextRing";
import type { AgentStatus } from "../lib/agentStatus";
import { contextPercent, modelName, todoSummary } from "../lib/agentStatus";
import { CONTEXT_ADVISORY_THRESHOLD_DEFAULT } from "../lib/advisories";
import type { SessionInfo } from "../types";

/* Status strip: live session state + reason (from the session-states stream)
 * plus the heavier agent metrics (todos, context %, tokens/cost, model) read
 * from the state/stats endpoints. Metric fields degrade to "—" when the agent
 * did not report them (see the open SPEC question on field names). */

function Metric({ label, value, testid }: { label: string; value: string; testid: string }) {
  return (
    <div className="metric" data-testid={testid}>
      <span className="metricLabel">{label}</span>
      <span className="metricValue">{value}</span>
    </div>
  );
}

function formatTokens(input?: number, output?: number): string {
  if (typeof input !== "number" && typeof output !== "number") return "—";
  return `${input ?? 0} in / ${output ?? 0} out`;
}

export function TaskStatusStrip({
  session,
  status,
}: {
  session: SessionInfo | null;
  status: AgentStatus;
}) {
  const { state, stats } = status;
  const todos = todoSummary(state);
  const context = contextPercent(state);
  const contextHigh = context !== null && context >= CONTEXT_ADVISORY_THRESHOLD_DEFAULT;
  const model = modelName(state);
  const cost = typeof stats?.totalCostUsd === "number" ? `$${stats.totalCostUsd.toFixed(4)}` : "—";

  return (
    <div className="panel statusStrip" data-testid="status-strip">
      <div className="statusHeadline">
        <span className={`sessionPill ${session?.status ?? "none"}`} data-testid="session-state">
          {session?.status ?? "no session"}
        </span>
        {session?.reason && (
          <span className="sessionReason" data-testid="session-reason">
            {session.reason}
          </span>
        )}
      </div>
      <div className="metrics">
        <Metric
          label="todos"
          testid="metric-todos"
          value={todos ? `${todos.done}/${todos.total}` : "—"}
        />
        {context !== null && contextHigh ? (
          <div className="metric" data-testid="metric-context">
            <span className="metricLabel">context</span>
            <ContextRing pct={context} title={`context ${context}% — consider compacting or handing off`} />
          </div>
        ) : (
          <Metric
            label="context"
            testid="metric-context"
            value={context === null ? "—" : `${context}%`}
          />
        )}
        <Metric
          label="tokens"
          testid="metric-tokens"
          value={formatTokens(stats?.inputTokens, stats?.outputTokens)}
        />
        <Metric label="cost" testid="metric-cost" value={cost} />
        <Metric label="model" testid="metric-model" value={model ?? "—"} />
      </div>
    </div>
  );
}
