import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ContextRing } from "../components/ContextRing";
import { answerAgent, cleanupTask, startReview } from "../lib/api";
import { confirmCleanup, prLinkLabel } from "../lib/cleanup";
import { formatTokensCost } from "../lib/advisories";
import { isSpawning } from "../lib/daemonReducer";
import { useDaemonState } from "../lib/daemonSocket";
import type {
  AdvisoryPayload,
  PendingQuestion,
  ReviewState,
  SessionInfo,
  StatsPayload,
  Task,
} from "../types";
import "./TasksView.css";

/** A pending `ask` fits a one-tap inline answer when it's a single,
 * non-multi question with options (tasks spec: multi-select, multi-question,
 * free-text-only asks, and approval gates all defer to task detail). */
function fitsInlineQuickAnswer(question: PendingQuestion): boolean {
  if (question.kind !== "ask" || question.questions.length !== 1) return false;
  const [q] = question.questions;
  return !q.multi && q.options.length > 0;
}

function QuickAnswer({ taskId, question }: { taskId: number; question: PendingQuestion }) {
  const [busy, setBusy] = useState(false);
  const q = question.questions[0];

  async function answer(value: string) {
    setBusy(true);
    try {
      await answerAgent(taskId, { question_id: question.id, selections: [value] });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="quickAnswer" data-testid={`quick-answer-${taskId}`}>
      <div className="quickAnswerPrompt">{q.prompt}</div>
      <div className="quickAnswerOptions">
        {q.options.map((opt) => (
          <button
            key={opt.value}
            type="button"
            className="quickAnswerOption"
            disabled={busy}
            title={opt.description ?? undefined}
            onClick={() => void answer(opt.value)}
          >
            {opt.label}
            {q.recommended === opt.value && <span className="recommendedTag">·rec</span>}
          </button>
        ))}
      </div>
    </div>
  );
}

export function formatElapsed(fromIso: string, now: Date = new Date()): string {
  const ms = now.getTime() - new Date(fromIso).getTime();
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d`;
}

/** The session pill with SPEC D4 tier styling (per the Tasks.dc.html mockup):
 * working/starting breathe quietly, idle/retrying are bordered quiet badges,
 * stalled is the notify tier (amber), failed is the interrupt tier — with
 * the transition reason accessible on the pill. */
function SessionPill({ session, review }: { session: SessionInfo; review?: ReviewState }) {
  if (session.status === "failed") {
    return (
      <span className="statePill failed" title={session.reason}>
        {session.status}
      </span>
    );
  }
  if (session.status === "stalled") {
    return (
      <span className="statePill notify" title={session.reason}>
        <span className="notifyDot" />
        {session.status}
      </span>
    );
  }
  if (session.status === "reviewing") {
    return (
      <span className="statePill review" title={session.reason}>
        <span className="notifyDot" />
        {session.status}
        {review && (
          <a
            className="reviewPillLink"
            href={review.url}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            reopen
          </a>
        )}
      </span>
    );
  }
  if (session.status === "idle" || session.status === "retrying") {
    return (
      <span className="statePill neutral" title={session.reason}>
        <span className="ringDot" />
        {session.status}
      </span>
    );
  }
  return (
    <span className="statePill live" title={session.reason}>
      <span className="breathingDot" />
      {session.status}
    </span>
  );
}

function TaskCard({
  task,
  session,
  stats,
  advisories,
  review,
}: {
  task: Task;
  session: SessionInfo | undefined;
  stats: StatsPayload | undefined;
  advisories: Partial<Record<"context-high" | "maybe-waiting", AdvisoryPayload>> | undefined;
  review: ReviewState | undefined;
}) {
  const [showError, setShowError] = useState(false);
  const spawning = isSpawning(task);
  const sessionFailed = session?.status === "failed";
  const failed = task.state === "failed" || sessionFailed;
  const stalled = session?.status === "stalled";
  const animating = session?.status === "working" || session?.status === "starting";
  const contextHigh = advisories?.["context-high"];
  const maybeWaiting = advisories?.["maybe-waiting"];
  const tokensCost = formatTokensCost(stats);
  const [startingReview, setStartingReview] = useState(false);

  async function onReview() {
    if (!session || session.status !== "idle") return;
    setStartingReview(true);
    try {
      await startReview(task.id);
    } finally {
      setStartingReview(false);
    }
  }

  async function onCleanup() {
    if (!confirmCleanup(task)) return;
    await cleanupTask(task.id);
  }

  return (
    <article
      className={`taskCard ${failed ? "failed" : ""} ${stalled ? "stalled" : ""} ${spawning ? "spawning" : ""}`}
      data-testid={`task-card-${task.id}`}
    >
      {failed && <span className="spine" />}
      {stalled && <span className="spine spineAmber" />}
      <div className="cardTop">
        <span className="cardProject">{task.project_name}</span>
        <span className="cardSpacer" />
        {session ? (
          <SessionPill session={session} review={review} />
        ) : (
          <span className={`statePill ${failed ? "failed" : spawning ? "spawning" : "neutral"}`}>
            {spawning && <span className="pillDot" />}
            {spawning ? "spawning" : task.state}
          </span>
        )}
      </div>
      <Link className="cardBranch" to={`/tasks/${task.id}`} data-testid={`task-link-${task.id}`}>
        {task.branch}
      </Link>
      {task.pr_url && (
        <a
          className="cardPrLink"
          href={task.pr_url}
          target="_blank"
          rel="noreferrer"
          data-testid={`task-pr-link-${task.id}`}
          onClick={(e) => e.stopPropagation()}
        >
          {task.pr_url.replace("https://", "")}
        </a>
      )}
      {sessionFailed && (
        <div className="sessionReason" data-testid={`session-reason-${task.id}`}>
          {session.reason}
        </div>
      )}
      {(contextHigh || tokensCost) && (
        <div className="cardStats" data-testid={`card-stats-${task.id}`}>
          {contextHigh && (
            <ContextRing
              pct={contextHigh.context_pct ?? 0}
              title={`context ${contextHigh.context_pct}% — consider compacting or handing off`}
            />
          )}
          {tokensCost && <span className="tokensCost">{tokensCost}</span>}
          {contextHigh && <span className="compactHint">consider compacting or handing off</span>}
        </div>
      )}
      <div className="cardActions">
        {session?.status === "idle" && (
          <button
            type="button"
            className="reviewButton"
            disabled={startingReview}
            onClick={() => void onReview()}
            data-testid={`review-button-${task.id}`}
          >
            {startingReview ? "Starting review…" : "Review"}
          </button>
        )}
      </div>
      {session?.status === "idle" && maybeWaiting && (
        <div className="maybeWaiting" data-testid={`maybe-waiting-${task.id}`}>
          <span className="maybeWaitingIcon">?</span>
          may be waiting for a reply — last message ends with a question
        </div>
      )}
      {session?.status === "waiting-input" &&
        (session.question && fitsInlineQuickAnswer(session.question) ? (
          <QuickAnswer taskId={task.id} question={session.question} />
        ) : (
          <div className="quickAnswerDefer" data-testid={`quick-answer-defer-${task.id}`}>
            Open task detail to answer.
          </div>
        ))}
      {session?.status === "waiting-approval" && (
        <div className="quickAnswerDefer" data-testid={`quick-answer-defer-${task.id}`}>
          Open task detail to answer.
        </div>
      )}
      {animating && (
        <div className="slideTrack" data-testid={`slide-bar-${task.id}`}>
          <span className="slideBar" />
        </div>
      )}
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
  const { tasks, projects, sessions, stats, advisories, reviews } = useDaemonState();
  const [searchParams] = useSearchParams();
  // Project filter (projects-view capability): the Projects card's
  // active-tasks pill lands here via `?project=<name>`.
  const projectFilter = searchParams.get("project");
  const visible = tasks.filter(
    (t) => t.state !== "archived" && (projectFilter === null || t.project_name === projectFilter),
  );
  // Shipped rows (merge-poll capability, design D-5): every task with a
  // pr_url, live or archived, most-recently-updated first.
  const shipped = tasks
    .filter((t) => t.pr_url && (projectFilter === null || t.project_name === projectFilter))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));

  return (
    <>
      <div className="headerRow">
        <h1>Tasks</h1>
        <span className="subline">
          {visible.length} tasks · {projects.length} projects · attention first, then recency
          {projectFilter !== null && (
            <>
              {" · project: "}
              <strong data-testid="project-filter-label">{projectFilter}</strong>
            </>
          )}
        </span>
        <span className="spacer" />
        <Link className="spawnButton" to="/spawn">
          Spawn task
        </Link>
      </div>

      {visible.length === 0 && shipped.length === 0 ? (
        <div className="empty" data-testid="tasks-empty-state">
          <strong>No tasks yet</strong>
          <span>Spawn one to get an agent working on something.</span>
        </div>
      ) : (
        <div className="cardGrid" data-testid="tasks-list">
          {visible.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              session={sessions[task.id]}
              stats={stats[task.id]}
              advisories={advisories[task.id]}
              review={reviews[task.id]}
            />
          ))}
        </div>
      )}

      {shipped.length > 0 && (
        <section className="shippedSection" data-testid="shipped-section">
          <div className="shippedHeader">
            <span>Shipped</span>
            <span className="shippedCount">{shipped.length} recent</span>
            <span className="shippedRule" />
          </div>
          <div className="shippedRows">
            {shipped.map((task) => (
              <ShippedRow key={task.id} task={task} />
            ))}
          </div>
        </section>
      )}
    </>
  );
}

/** One collapsed shipped row per the Tasks.dc.html mockup: green pill,
 * project/slug, PR link `repo#N · state`, cleanup-state note. Live rows
 * link to the Ship Flow view (where the cleanup action lives); archived
 * rows are inert — the task is gone. */
function ShippedRow({ task }: { task: Task }) {
  const prUrl = task.pr_url as string; // guaranteed by the filter above
  const prState = task.pr_state ?? "open"; // unpollled PRs render as open
  const label = prLinkLabel(prUrl) ?? prUrl.replace("https://", "");
  const archived = task.state === "archived";

  let note: string;
  if (archived) note = `cleaned up ${formatElapsed(task.updated_at)} ago`;
  else if (task.pr_state === "merged") note = "merged · ready for cleanup";
  else if (task.pr_state === "closed") note = "PR closed unmerged";
  else note = "awaiting merge · cleanup deferred";

  return (
    <div className="shippedRow" data-testid={`shipped-row-${task.id}`}>
      <span className="shippedPill">
        <span className="shippedDot" />
        shipped
      </span>
      {archived ? (
        <span className="shippedSlug">
          {task.project_name}/{task.slug}
        </span>
      ) : (
        <Link className="shippedSlug" to={`/ship/${task.id}`} data-testid={`shipped-link-${task.id}`}>
          {task.project_name}/{task.slug}
        </Link>
      )}
      <a className="shippedPr" href={prUrl} target="_blank" rel="noreferrer">
        {label} · {prState}
      </a>
      <span className="shippedNote">{note}</span>
    </div>
  );
}
