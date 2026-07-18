import { Link } from "react-router-dom";
import { useDaemonState } from "../lib/daemonSocket";
import "./TasksView.css";

export function TasksView() {
  const { tasks, projects } = useDaemonState();

  return (
    <>
      <div className="headerRow">
        <h1>Tasks</h1>
        <span className="subline">
          {tasks.length} tasks · {projects.length} projects · attention first, then recency
        </span>
        <span className="spacer" />
        <Link className="spawnButton" to="/spawn">
          Spawn task
        </Link>
      </div>

      {tasks.length === 0 ? (
        <div className="empty" data-testid="tasks-empty-state">
          <strong>No tasks yet</strong>
          <span>Spawn one to get an agent working on something.</span>
        </div>
      ) : (
        <div data-testid="tasks-list">
          {tasks.map((task) => (
            <div key={task.id}>{JSON.stringify(task)}</div>
          ))}
        </div>
      )}
    </>
  );
}
