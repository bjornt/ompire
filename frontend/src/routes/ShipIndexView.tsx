import { Link } from "react-router-dom";
import { buildShipIndex, type ShipIndexEntry } from "../lib/shipPresentation";
import { useDaemonState } from "../lib/useDaemonState";
import "./ShipIndexView.css";

function ShipIndexRow({ entry }: { entry: ShipIndexEntry }) {
  return (
    <li className={`shipIndexRow ${entry.activity}`}>
      <Link
        className="shipIndexLink"
        to={`/ship/${entry.task.id}`}
        data-testid={`ship-index-row-${entry.task.id}`}
      >
        <span className="shipIndexTopline">
          <span className="shipIndexTask">
            {entry.task.project_name}/{entry.task.slug}
          </span>
          <span className={`shipIndexStage ${entry.activity}`}>Next: {entry.label}</span>
        </span>
        <span className="shipIndexDetail">{entry.detail}</span>
        {entry.error !== null && (
          <span className="shipIndexError" data-testid={`ship-index-error-${entry.task.id}`}>
            {entry.error}
          </span>
        )}
      </Link>
    </li>
  );
}

function ShipIndexSection({ title, entries, testId }: { title: string; entries: ShipIndexEntry[]; testId: string }) {
  if (entries.length === 0) return null;

  return (
    <section className="shipIndexSection" data-testid={testId}>
      <h2 className="sectionHeading">{title}</h2>
      <ul className="shipIndexList">
        {entries.map((entry) => (
          <ShipIndexRow key={entry.task.id} entry={entry} />
        ))}
      </ul>
    </section>
  );
}

/** A chooser for existing task-specific ship workflows. It deliberately emits
 * no ship command: state and authority remain daemon-owned in `/ship/:id`. */
export function ShipIndexView() {
  const { snapshotReady, tasks, reviews, ships } = useDaemonState();

  if (!snapshotReady) {
    return (
      <div className="empty" data-testid="ship-index-loading">
        <strong>Loading Ship flow…</strong>
        <span>Waiting for the daemon snapshot.</span>
      </div>
    );
  }

  const { active, recent } = buildShipIndex(tasks, reviews, ships);
  if (active.length === 0 && recent.length === 0) {
    return (
      <div className="empty" data-testid="ship-index-empty">
        <strong>Nothing to ship right now</strong>
        <span>Finish and approve a task, then return here to publish it.</span>
        <Link to="/tasks">Back to Tasks</Link>
      </div>
    );
  }

  return (
    <>
      <div className="headerRow">
        <h1>Ship flow</h1>
        <span className="subline">Choose a task to review, publish, or clean up.</span>
      </div>

      <div className="shipIndex" data-testid="ship-index">
        <ShipIndexSection title="Ready or in progress" entries={active} testId="ship-index-active" />
        <ShipIndexSection title="Recently shipped" entries={recent} testId="ship-index-recent" />
      </div>
    </>
  );
}
