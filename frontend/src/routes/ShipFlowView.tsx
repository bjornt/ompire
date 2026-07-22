import { Link, useParams } from "react-router-dom";
import { useDaemonState } from "../lib/daemonSocket";
import type { ReviewIteration, ReviewState, SessionInfo } from "../types";
import "./ShipFlowView.css";

function StepIcon({
  index,
  active,
  done,
  error,
}: {
  index: number;
  active: boolean;
  done: boolean;
  error: boolean;
}) {
  if (error) {
    return <span className="stepIcon stepError">!</span>;
  }
  if (done) {
    return <span className="stepIcon stepDone">✓</span>;
  }
  return <span className={`stepIcon ${active ? "stepActive" : "stepPending"}`}>{index}</span>;
}

function ReviewStep({
  session,
  review,
}: {
  session: SessionInfo | undefined;
  review: ReviewState | undefined;
}) {
  if (!review) {
    return (
      <div className="shipStep" data-testid="ship-step-review">
        <div className="stepHeader">
          <StepIcon index={1} active={false} done={false} error={false} />
          <span className="stepTitle">Review</span>
        </div>
        <p className="stepHint">No review started yet. Start one from the task card when idle.</p>
      </div>
    );
  }

  const open = review.status === "open";
  const done = review.status === "approved";
  const error = review.status === "error";

  return (
    <div className={`shipStep ${open ? "stepOpen" : ""}`} data-testid="ship-step-review">
      <div className="stepHeader">
        <StepIcon index={1} active={open} done={done} error={error} />
        <span className="stepTitle">Review</span>
        <span className={`stepStatusBadge ${review.status}`}>{review.status}</span>
      </div>
      {open && (
        <a
          className="reviewReopenLink"
          href={review.url}
          target="_blank"
          rel="noreferrer"
          data-testid="review-reopen-link"
        >
          reopen {review.url.replace("http://", "")}
        </a>
      )}
      {review.iterations.length > 0 && (
        <ul className="iterationList" data-testid="review-iterations">
          {review.iterations.map((it: ReviewIteration, idx: number) => (
            <li key={idx} className={`iterationItem ${it.outcome}`}>
              <span className="iterationOutcome">{it.outcome}</span>
              {it.outcome === "comments" &&
                (it.comment_count != null ? (
                  <span className="iterationCount">{it.comment_count} comments</span>
                ) : (
                  <span className="iterationCount">comments submitted</span>
                ))}
              {it.stderr && <span className="iterationError" title={it.stderr}>error details</span>}
            </li>
          ))}
        </ul>
      )}
      {session?.status === "reviewing" && (
        <p className="stepHint">Review is open. Use the link to reopen the llmvet UI.</p>
      )}
    </div>
  );
}

function InertStep({
  index,
  title,
  hint,
  testid,
}: {
  index: number;
  title: string;
  hint: string;
  testid: string;
}) {
  return (
    <div className="shipStep inert" data-testid={testid}>
      <div className="stepHeader">
        <StepIcon index={index} active={false} done={false} error={false} />
        <span className="stepTitle">{title}</span>
      </div>
      <p className="stepHint">{hint}</p>
    </div>
  );
}

export function ShipFlowView() {
  const { id } = useParams();
  const taskId = Number(id);
  const { tasks, sessions, reviews } = useDaemonState();

  const task = tasks.find((t) => t.id === taskId);
  const session = sessions[taskId];
  const review = reviews[taskId];

  if (!task) {
    return (
      <div className="empty" data-testid="ship-flow-not-found">
        <strong>Task not found</strong>
        <Link to="/tasks">Back to Tasks</Link>
      </div>
    );
  }

  return (
    <>
      <div className="headerRow">
        <h1>
          Ship {task.project_name}/{task.slug}
        </h1>
        <span className="subline">Review → Commit → Push + PR → Cleanup</span>
        <span className="spacer" />
        <Link className="backLink" to={`/tasks/${task.id}`}>
          ← Task
        </Link>
      </div>

      <div className="shipFlow" data-testid="ship-flow">
        <ReviewStep session={session} review={review} />
        <InertStep
          index={2}
          title="Commit"
          hint="Commit and sign changes after approval. (Coming in a later chunk.)"
          testid="ship-step-commit"
        />
        <InertStep
          index={3}
          title="Push + PR"
          hint="Push the branch and open a pull request. (Coming in a later chunk.)"
          testid="ship-step-push-pr"
        />
        <InertStep
          index={4}
          title="Cleanup"
          hint="Remove the workshop container and archive the task after merge. (Coming later.)"
          testid="ship-step-cleanup"
        />
      </div>
    </>
  );
}
