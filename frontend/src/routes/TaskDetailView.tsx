import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getTaskDetail } from "../lib/api";
import { useDaemonState } from "../lib/daemonSocket";
import type { TaskDetail, WorkshopStatus } from "../types";
import { formatElapsed } from "./TasksView";
import "./TaskDetailView.css";

/* Task detail v0 (design D-6): metadata panel + escape hatch only. The
 * transcript, composer, and status strip areas from the mockup belong to
 * later chunks and are omitted entirely, not stubbed. */

function workshopLabel(detail: TaskDetail): { text: string; status: WorkshopStatus | "none" } {
  if (!detail.workshop_id) return { text: "not launched", status: "none" };
  const status = detail.workshop_status ?? "unknown";
  return { text: `${status} · ${detail.workshop_id}`, status };
}

export function TaskDetailView() {
  const { id } = useParams();
  const taskId = Number(id);
  const { tasks } = useDaemonState();
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Live card data from the socket snapshot; derived workshop status needs
  // the detail fetch. Refetch when the socket's copy of the task changes.
  const liveTask = tasks.find((t) => t.id === taskId) ?? null;

  useEffect(() => {
    if (!Number.isInteger(taskId)) return;
    let cancelled = false;
    getTaskDetail(taskId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, liveTask?.updated_at]);

  if (error !== null) {
    return (
      <div className="empty" data-testid="task-detail-error">
        <strong>Task unavailable</strong>
        <span>{error}</span>
        <Link to="/tasks">Back to Tasks</Link>
      </div>
    );
  }
  if (detail === null) {
    return <div className="empty">Loading…</div>;
  }

  const workshop = workshopLabel(detail);
  const escapeHatch = [
    `cd ${detail.clone_path}`,
    "workshop shell",
    "omp --resume",
  ];

  return (
    <>
      <div className="headerRow">
        <h1>
          {detail.project_name}/{detail.slug}
        </h1>
        <span className={`statePill ${detail.state === "failed" ? "failed" : "neutral"}`}>
          {detail.state}
        </span>
        <span className="spacer" />
        <Link className="backLink" to="/tasks">
          ← Tasks
        </Link>
      </div>

      <div className="detailGrid">
        <div className="panel" data-testid="task-metadata">
          <h2 className="panelTitle">Task</h2>
          <dl className="metaList">
            <dt>project</dt>
            <dd>{detail.project_name}</dd>
            <dt>branch</dt>
            <dd>{detail.branch}</dd>
            <dt>clone</dt>
            <dd>{detail.clone_path}</dd>
            <dt>workshop</dt>
            <dd data-testid="workshop-status">
              <span className={`wsStatus ${workshop.status}`}>{workshop.text}</span>
            </dd>
            <dt>spawned</dt>
            <dd>
              {new Date(detail.created_at).toLocaleString()} · {formatElapsed(detail.created_at)} ago
            </dd>
            {detail.error && (
              <>
                <dt>error</dt>
                <dd>
                  <pre className="detailError">{detail.error}</pre>
                </dd>
              </>
            )}
          </dl>
        </div>

        <div className="panel" data-testid="escape-hatch">
          <h2 className="panelTitle">Escape hatch</h2>
          <pre className="escapeBlock">
            {escapeHatch.map((cmd) => (
              <div key={cmd}>
                <span className="promptChar">$ </span>
                {cmd}
              </div>
            ))}
          </pre>
          <button
            type="button"
            className="copyButton"
            onClick={() => navigator.clipboard.writeText(escapeHatch.join("\n"))}
          >
            Copy commands
          </button>
          <div className="hint">
            Drive the container by hand: shell in, then resume the omp session. Same session files
            the daemon will supervise from later chunks.
          </div>
        </div>
      </div>
    </>
  );
}
