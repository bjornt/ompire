import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ContextRing } from "../components/ContextRing";
import { ReviewSummary } from "../components/ReviewSummary";
import { answerAgent, cleanupTask, startReview } from "../lib/api";
import {
  attentionSection,
  getAttentionSeverity,
  isAttentionTask,
} from "../lib/attention";
import { confirmCleanup, prLinkLabel } from "../lib/cleanup";
import { formatTokensCost } from "../lib/advisories";
import {
  currentStepRecord,
  defaultSessionName,
  isSpawning,
  primarySessionName,
  workflowActive,
} from "../lib/daemonReducer";
import { projectReview } from "../lib/reviewPresentation";
import { hasShipFlowHandoff } from "../lib/shipPresentation";
import { useDaemonState } from "../lib/useDaemonState";
import type {
  AdvisoryKind,
  AdvisoryPayload,
  PendingQuestion,
  ReviewState,
  SessionInfo,
  ShipState,
  StatsPayload,
  Task,
  WorkflowState,
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

function QuickAnswer({
  taskId,
  session,
  question,
}: {
  taskId: number;
  session: string;
  question: PendingQuestion;
}) {
  const [busy, setBusy] = useState(false);
  const q = question.questions[0];

  async function answer(value: string) {
    setBusy(true);
    try {
      await answerAgent(taskId, session, { question_id: question.id, selections: [value] });
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

import { formatElapsed } from "../lib/formatElapsed";

/** The session pill with SPEC D4 tier styling (per the Tasks.dc.html mockup):
 * working/starting breathe quietly, idle/retrying are bordered quiet badges,
 * stalled is the notify tier (amber), failed is the interrupt tier — with
 * the transition reason accessible on the pill. While the task's workflow
 * run is in flight the pill is step-prefixed (`<step>: <status>`, tasks
 * spec); after completion it renders the bare session status as before. */
function SessionPill({
  session,
  prefix,
}: {
  session: SessionInfo;
  prefix?: string | null;
}) {
  const label = prefix ? `${prefix}: ${session.status}` : session.status;
  if (session.status === "failed") {
    return (
      <span className="statePill failed" title={session.reason}>
        {label}
      </span>
    );
  }
  if (session.status === "stalled") {
    return (
      <span className="statePill notify" title={session.reason}>
        <span className="notifyDot" />
        {label}
      </span>
    );
  }
  if (session.status === "reviewing") {
    return (
      <span className="statePill review" title={session.reason}>
        <span className="notifyDot" />
        {label}

      </span>
    );
  }
  if (session.status === "idle" || session.status === "retrying") {
    return (
      <span className="statePill neutral" title={session.reason}>
        <span className="ringDot" />
        {label}
      </span>
    );
  }
  return (
    <span className="statePill live" title={session.reason}>
      <span className="breathingDot" />
      {label}
    </span>
  );
}

/** The pill a card shows during an active workflow run (tasks spec): agent
 * steps prefix the underlying session status; command/decision steps read
 * `<step>: running`; a parked gate reads `<step>: waiting` with notify-tier
 * styling. Returns null when the run isn't active — the bare session pill
 * renders exactly as before workflows. */
function workflowPill(
  workflow: WorkflowState | undefined,
): { label: string; tier: "notify" | "live" } | null {
  if (!workflowActive(workflow) || workflow?.step == null) return null;
  const kind = currentStepRecord(workflow)?.kind;
  if (kind === "gate") return { label: `${workflow.step}: waiting`, tier: "notify" };
  if (kind === "command" || kind === "decision")
    return { label: `${workflow.step}: running`, tier: "live" };
  return null; // agent step (or no record yet): the session pill carries the prefix
}

function TaskCard({
  task,
  sessions,
  workflow,
  stats,
  advisories,
  review,
  ship,
}: {
  task: Task;
  sessions: Record<string, SessionInfo> | undefined;
  workflow: WorkflowState | undefined;
  stats: Record<string, StatsPayload> | undefined;
  advisories: Record<string, Partial<Record<AdvisoryKind, AdvisoryPayload>>> | undefined;
  review: ReviewState | undefined;
  ship: ShipState | undefined;
}) {
  const [showError, setShowError] = useState(false);
  const spawning = isSpawning(task);
  // The card reports on the workflow's relevant session (tasks spec): the
  // current step's session while the run is in flight, else the primary.
  const sessionName = defaultSessionName(sessions, workflow);
  const session = sessions?.[sessionName];
  const primarySession = sessions?.[primarySessionName(sessions, workflow)];
  const reviewPresentation = projectReview(review, primarySession);
  const showShipFlow = hasShipFlowHandoff(task, review, ship);
  const runPill = workflowPill(workflow);
  const stepPrefix = workflowActive(workflow) ? workflow?.step : null;
  const sessionFailed = session?.status === "failed";
  const failedStep =
    workflow?.status === "failed"
      ? [...workflow.steps].reverse().find((record) => record.status === "failed")
      : undefined;
  const failed = task.state === "failed" || sessionFailed || workflow?.status === "failed";
  const stalled = session?.status === "stalled";
  const animating =
    session?.status === "working" ||
    session?.status === "starting" ||
    runPill?.tier === "live";
  const contextHigh = advisories?.[sessionName]?.["context-high"];
  const maybeWaiting = advisories?.[sessionName]?.["maybe-waiting"];
  const tokensCost = formatTokensCost(stats?.[sessionName]);
  const [startingReview, setStartingReview] = useState(false);
  const startLocked = useRef(false);

  useEffect(() => {
    if (startingReview && primarySession?.status === "reviewing") {
      startLocked.current = false;
      setStartingReview(false);
    }
  }, [primarySession?.status, startingReview]);

  async function onReview() {
    if (startLocked.current || !reviewPresentation.canStart) return;
    startLocked.current = true;
    setStartingReview(true);
    try {
      await startReview(task.id);
    } catch {
      startLocked.current = false;
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
        {workflow?.status === "failed" && !sessionFailed ? (
          <span
            className="statePill failed"
            title={failedStep?.error ?? undefined}
            data-testid={`workflow-failed-pill-${task.id}`}
          >
            {failedStep ? `${failedStep.step}: failed` : "workflow failed"}
          </span>
        ) : runPill ? (
          <span className={`statePill ${runPill.tier}`} data-testid={`workflow-pill-${task.id}`}>
            {runPill.tier === "notify" ? <span className="notifyDot" /> : <span className="breathingDot" />}
            {runPill.label}
          </span>
        ) : session ? (
          <SessionPill session={session} prefix={stepPrefix} />
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
      {review && (
        <div className="cardReview">
          <ReviewSummary review={review} primarySession={primarySession} compact />
        </div>
      )}
      <div className="cardActions">
        {reviewPresentation.canStart && (
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
        {showShipFlow && (
          <Link className="shipFlowButton" to={`/ship/${task.id}`} data-testid={`ship-link-${task.id}`}>
            Ship flow
          </Link>
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
          <QuickAnswer taskId={task.id} session={sessionName} question={session.question} />
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
  const { tasks, projects, sessions, workflows, stats, advisories, reviews, ships, attention, settings } =
    useDaemonState();
  const [searchParams] = useSearchParams();
  // Project filter (projects-view capability): the Projects card's
  // active-tasks pill lands here via `?project=<name>`.
  const projectFilter = searchParams.get("project");
  const attentionFilter = searchParams.get("attention") === "1";
  const visible = tasks.filter((t) => {
    if (t.state === "archived") return false;
    if (projectFilter !== null && t.project_name !== projectFilter) return false;
    if (attentionFilter && !isAttentionTask(t, attention, settings)) return false;
    return true;
  });
  // Shipped rows (merge-poll capability, design D-5): every task with a
  // pr_url, live or archived, most-recently-updated first.
  const shipped = tasks
    .filter((t) => t.pr_url && (projectFilter === null || t.project_name === projectFilter))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));

  // Partition into sections (ROADMAP2 chunk 1: make-attention-actionable).
  const severity = (t: Task) => getAttentionSeverity(t, attention) ?? -1;
  const byRecency = (a: Task, b: Task) => b.updated_at.localeCompare(a.updated_at);
  const needsYou: Task[] = [];
  const running: Task[] = [];
  const idle: Task[] = [];
  for (const t of visible) {
    const section = attentionSection(t, sessions[t.id], attention, settings);
    if (section === "needs-you") needsYou.push(t);
    else if (section === "running") running.push(t);
    else idle.push(t);
  }
  needsYou.sort((a, b) => severity(b) - severity(a) || byRecency(a, b));
  running.sort(byRecency);
  idle.sort(byRecency);

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
        attentionFilter ? (
          <div className="empty" data-testid="attention-empty-state">
            <strong>No tasks need your attention right now</strong>
            <span>
              <Link to={projectFilter != null ? `?project=${projectFilter}` : "/tasks"}>
                Show all tasks
              </Link>
            </span>
          </div>
        ) : (
          <div className="empty" data-testid="tasks-empty-state">
            <strong>No tasks yet</strong>
            <span>Spawn one to get an agent working on something.</span>
          </div>
        )
      ) : (
        <>
          {needsYou.length > 0 && (
            <section data-testid="section-needs-you">
              <h3 className="sectionHeading">Needs you</h3>
              <div className="cardGrid">
                {needsYou.map((task) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    sessions={sessions[task.id]}
                    workflow={workflows[task.id]}
                    stats={stats[task.id]}
                    advisories={advisories[task.id]}
                    review={reviews[task.id]}
                    ship={ships[task.id]}
                  />
                ))}
              </div>
            </section>
          )}
          {running.length > 0 && (
            <section data-testid="section-running">
              <h3 className="sectionHeading">Running</h3>
              <div className="cardGrid">
                {running.map((task) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    sessions={sessions[task.id]}
                    workflow={workflows[task.id]}
                    stats={stats[task.id]}
                    advisories={advisories[task.id]}
                    review={reviews[task.id]}
                    ship={ships[task.id]}
                  />
                ))}
              </div>
            </section>
          )}
          {idle.length > 0 && (
            <section data-testid="section-idle">
              <h3 className="sectionHeading">Idle/other</h3>
              <div className="cardGrid">
                {idle.map((task) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    sessions={sessions[task.id]}
                    workflow={workflows[task.id]}
                    stats={stats[task.id]}
                    advisories={advisories[task.id]}
                    review={reviews[task.id]}
                    ship={ships[task.id]}
                  />
                ))}
              </div>
            </section>
          )}
        </>
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
