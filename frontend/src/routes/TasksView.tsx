import { useState } from "react";
import { Link } from "react-router-dom";
import { cleanupTask } from "../lib/api";
import { isSpawning } from "../lib/daemonReducer";
import { useDaemonState } from "../lib/daemonSocket";
import type { Task } from "../types";
import "./TasksView.css";

export function formatElapsed(fromIso: string, now: Date = new Date()): string {
  const ms = now.getTime() - new Date(fromIso).getTime();
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d`;
}

function TaskCard({ task }: { task: Task }) {
  const [showError, setShowError] = useState(false);
  const spawning = isSpawning(task);
  const failed = task.state === "failed";
  const pillLabel = spawning ? "spawning" : task.state;

  async function onCleanup() {
    const workshopLine = task.workshop_id
      ? `\n…and removes the workshop container:\n${task.workshop_id}`
      : "";
    const confirmed = window.confirm(
      `Clean up ${task.project_name}/${task.slug}?\n\nThis deletes the clone directory:\n${task.clone_path}${workshopLine}`,
    );
    if (!confirmed) return;
    await cleanupTask(task.id);
  }

  return (
    <article
      className={`taskCard ${failed ? "failed" : ""} ${spawning ? "spawning" : ""}`}
      data-testid={`task-card-${task.id}`}
    >
      {failed && <span className="spine" />}
      <div className="cardTop">
        <span className="cardProject">{task.project_name}</span>
        <span className="cardSpacer" />
        <span className={`statePill ${failed ? "failed" : spawning ? "spawning" : "neutral"}`}>
          {spawning && <span className="pillDot" />}
          {pillLabel}
        </span>
      </div>
      <Link className="cardBranch" to={`/tasks/${task.id}`} data-testid={`task-link-${task.id}`}>
        {task.branch}
      </Link>
      {failed && task.error && (
        <>
          <button
            type="button"
            className="errorToggle"
            onClick={() => setShowError((v) => !v)}
            data-testid={`task-error-toggle-${task.id}`}
          >
            {showError ? "Hide error" : "Show error"}
          </button>
          {showError && (
            <pre className="cardError" data-testid={`task-error-${task.id}`}>
              {task.error}
            </pre>
          )}
        </>
      )}
      <div className="cardFooter">
        <span className="clonePath" title={task.clone_path}>
          {task.clone_path}
        </span>
        <span className="elapsed">{formatElapsed(task.created_at)}</span>
        <span className="cardSpacer" />
        <button type="button" className="cleanupButton" onClick={onCleanup}>
          Clean up
        </button>
      </div>
    </article>
  );
}

export function TasksView() {
  const { tasks, projects } = useDaemonState();
  const visible = tasks.filter((t) => t.state !== "archived");

  return (
    <>
      <div className="headerRow">
        <h1>Tasks</h1>
        <span className="subline">
          {visible.length} tasks · {projects.length} projects · attention first, then recency
        </span>
        <span className="spacer" />
        <Link className="spawnButton" to="/spawn">
          Spawn task
        </Link>
      </div>

      {visible.length === 0 ? (
        <div className="empty" data-testid="tasks-empty-state">
          <strong>No tasks yet</strong>
          <span>Spawn one to get an agent working on something.</span>
        </div>
      ) : (
        <div className="cardGrid" data-testid="tasks-list">
          {visible.map((task) => (
            <TaskCard key={task.id} task={task} />
          ))}
        </div>
      )}
    </>
  );
}
