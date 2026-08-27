import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ReviewSummary } from "../components/ReviewSummary";
import { useAgentChannel } from "../lib/agentChannel";
import { isStreaming, useAgentStatus } from "../lib/agentStatus";
import { cancelReview, getTaskDetail, resumeWorkflow, startReview } from "../lib/api";
import {
  currentStepRecord,
  defaultSessionName,
  primarySessionName,
  taskSessionNames,
  workflowActive,
} from "../lib/daemonReducer";
import { projectReview } from "../lib/reviewPresentation";
import { hasShipFlowHandoff } from "../lib/shipPresentation";
import { useDaemonState } from "../lib/useDaemonState";
import type { ReviewState, SessionInfo, StepRecord, TaskDetail, WorkflowState, WorkshopStatus } from "../types";
import { formatElapsed } from "../lib/formatElapsed";
import { QuestionCard } from "./QuestionCard";
import { TaskComposer } from "./TaskComposer";
import { TaskStatusStrip } from "./TaskStatusStrip";
import { TaskTranscript } from "./TaskTranscript";
import "./TaskDetailView.css";

/* Task detail: metadata panel + escape hatch, plus the cockpit — streaming
 * transcript, composer, and status strip, all following the selected session
 * tab (workflow-engine design D-9). A session has a live agent while its
 * status is tracked and not `failed` (session-states keeps a failed status
 * after the child exits); the cockpit regions degrade to an inactive/empty
 * state when no agent is live rather than disappearing. The workflow strip
 * summarizes the run's executed steps and a gate card offers the resume
 * action while the run is parked. Single-session workflows (single-step)
 * hide the tab bar and render exactly the pre-workflow layout. */

function hasLiveAgent(session: SessionInfo | null): boolean {
  return session !== null && session.status !== "failed";
}

/** The gate message a waiting step record carries in its outcome (persisted
 * by the daemon so it survives restarts and reconnects). */
function gateMessage(record: StepRecord | undefined): string | null {
  const message = record?.outcome?.message;
  return typeof message === "string" ? message : null;
}

/** One-line summary for a finished step's chip title: the outcome's summary
 * (design D-3 outcome schema) or message, else the error. */
function chipTitle(record: StepRecord): string | undefined {
  if (record.error) return record.error;
  const summary = record.outcome?.summary;
  if (typeof summary === "string") return summary;
  return gateMessage(record) ?? undefined;
}

/** Workflow strip (workflow-engine design D-9): one chip per executed step
 * record in order, the in-flight step highlighted, a waiting gate chip
 * pulsing notify-tier. */
function WorkflowStrip({ workflow }: { workflow: WorkflowState }) {
  const current = workflowActive(workflow) ? currentStepRecord(workflow) : undefined;
  return (
    <div className="panel workflowStrip" data-testid="workflow-strip">
      <span className={`workflowRunStatus ${workflow.status ?? "none"}`} data-testid="workflow-run-status">
        {workflow.name}
        {workflow.status ? ` · ${workflow.status}` : ""}
      </span>
      <div className="workflowChips">
        {workflow.steps.map((record) => {
          const isCurrent = current !== undefined && record.seq === current.seq;
          return (
            <span
              key={record.seq}
              className={`workflowChip ${record.status}${isCurrent ? " current" : ""}`}
              title={chipTitle(record)}
              data-testid={`workflow-chip-${record.seq}`}
            >
              <span className={`chipDot ${record.kind}`} />
              {record.step}
              <span className="chipKind">{record.kind}</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

/** Gate card (workflow-engine design D-9): shown while the run is `waiting`
 * at a gate; the operator message comes from the waiting step record's
 * outcome, and Resume posts to the workflow resume endpoint with an optional
 * note. A 409 (the run already moved on) surfaces inline; the card also
 * disappears by itself once the run leaves `waiting`. */
function GateCard({ taskId, workflow }: { taskId: number; workflow: WorkflowState }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const waiting = [...workflow.steps].reverse().find((r) => r.status === "waiting");
  const message = gateMessage(waiting) ?? "Waiting at a workflow gate.";

  async function resume() {
    setBusy(true);
    setError(null);
    try {
      await resumeWorkflow(taskId, note.trim() || undefined);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel gateCard" data-testid="gate-card">
      <h2 className="panelTitle">
        <span className="gateDot" /> Workflow waiting — {workflow.step}
      </h2>
      <div className="gateMessage" data-testid="gate-message">
        {message}
      </div>
      <textarea
        className="gateNote"
        aria-label="Resume note"
        placeholder="Optional note for the workflow (e.g. reviewed, looks good)…"
        rows={2}
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      {error && (
        <div className="composerError" data-testid="gate-error">
          {error}
        </div>
      )}
      <div className="questionActions">
        <button
          type="button"
          className="sendButton"
          disabled={busy}
          onClick={() => void resume()}
          data-testid="gate-resume"
        >
          {busy ? "Resuming…" : "Resume"}
        </button>
      </div>
    </div>
  );
}

function ReviewPanel({
  taskId,
  review,
  primarySession,
  showShipFlow,
}: {
  taskId: number;
  review: ReviewState | undefined;
  primarySession: SessionInfo | undefined;
  showShipFlow: boolean;
}) {
  const presentation = projectReview(review, primarySession);
  const [pending, setPending] = useState<"starting" | "cancelling" | null>(null);
  const [error, setError] = useState<{ action: "start" | "cancel"; message: string } | null>(null);
  const commandLocked = useRef(false);

  useEffect(() => {
    const started = pending === "starting" && primarySession?.status === "reviewing";
    const cancelled = pending === "cancelling" && presentation.state !== "open";
    if (started || cancelled) {
      commandLocked.current = false;
      setPending(null);
      setError(null);
    }
  }, [pending, presentation.state, primarySession?.status]);

  useEffect(() => {
    if (
      (error?.action === "start" && primarySession?.status === "reviewing") ||
      (error?.action === "cancel" && presentation.state !== "open")
    ) {
      setError(null);
    }
  }, [error?.action, presentation.state, primarySession?.status]);

  async function command(action: "start" | "cancel") {
    if (commandLocked.current || (action === "start" ? !presentation.canStart : !presentation.canCancel)) {
      return;
    }
    commandLocked.current = true;
    setPending(action === "start" ? "starting" : "cancelling");
    setError(null);
    try {
      if (action === "start") await startReview(taskId);
      else await cancelReview(taskId);
    } catch (caught: unknown) {
      commandLocked.current = false;
      setPending(null);
      setError({
        action,
        message: caught instanceof Error ? caught.message : String(caught),
      });
    }
  }

  return (
    <section className="panel reviewPanel" data-testid="task-detail-review">
      <h2 className="panelTitle">Review</h2>
      <ReviewSummary review={review} primarySession={primarySession} />
      {pending && (
        <p className="reviewCommandState" data-testid="review-command-state">
          {pending === "starting"
            ? "Starting review… Waiting for daemon status."
            : "Cancelling review… Waiting for daemon status."}
        </p>
      )}
      {error && (
        <div className="composerError" data-testid="review-command-error">
          {error.message}
        </div>
      )}
      <div className="reviewActions">
        {presentation.canStart && (
          <button
            type="button"
            className="reviewAction"
            disabled={pending !== null}
            onClick={() => void command("start")}
            data-testid="task-detail-start-review"
          >
            {pending === "starting"
              ? "Starting…"
              : review
                ? "Start another review"
                : "Start review"}
          </button>
        )}
        {presentation.canCancel && (
          <button
            type="button"
            className="reviewAction cancel"
            disabled={pending !== null}
            onClick={() => void command("cancel")}
            data-testid="task-detail-cancel-review"
          >
            {pending === "cancelling" ? "Cancelling…" : "Cancel review"}
          </button>
        )}
        {showShipFlow && (
          <Link className="reviewAction ship" to={`/ship/${taskId}`} data-testid="task-detail-ship-link">
            {presentation.state === "approved" ? "Continue to Ship flow" : "Open Ship flow"}
          </Link>
        )}
      </div>
    </section>
  );
}

/** Session tab bar (workflow-engine design D-9): one tab per known session,
 * hidden for single-session workflows. A tab is disabled while its session
 * is unspawned (no tracker entry), and shows a question dot when another
 * session has a pending question. */
function SessionTabs({
  names,
  taskSessions,
  active,
  onSelect,
}: {
  names: string[];
  taskSessions: Record<string, SessionInfo> | null;
  active: string;
  onSelect: (name: string) => void;
}) {
  return (
    <div className="sessionTabs" role="tablist" data-testid="session-tabs">
      {names.map((name) => {
        const info = taskSessions?.[name];
        const selected = name === active;
        const pendingElsewhere = !selected && info?.question !== undefined;
        return (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={selected}
            className={`sessionTab${selected ? " active" : ""}`}
            disabled={!info}
            title={info ? `${name}: ${info.status}` : `${name}: not started`}
            data-testid={`session-tab-${name}`}
            onClick={() => onSelect(name)}
          >
            <span className={`tabDot ${info?.status ?? "none"}`} />
            {name}
            {pendingElsewhere && (
              <span className="tabQuestionDot" data-testid={`tab-question-${name}`} />
            )}
          </button>
        );
      })}
    </div>
  );
}

function workshopLabel(detail: TaskDetail): { text: string; status: WorkshopStatus | "none" } {
  if (!detail.workshop_id) return { text: "not launched", status: "none" };
  const status = detail.workshop_status ?? "unknown";
  return { text: `${status} · ${detail.workshop_id}`, status };
}

export function TaskDetailView() {
  const { id } = useParams();
  const taskId = Number(id);
  const { tasks, sessions, workflows, reviews, ships } = useDaemonState();
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Tab selection is local UI state (design D-9); null means "follow the
  // default" — the in-flight step's session, else the primary.
  const [selected, setSelected] = useState<string | null>(null);

  // Live card data from the socket snapshot; derived workshop status needs
  // the detail fetch. Refetch when the socket's copy of the task changes.
  const liveTask = tasks.find((t) => t.id === taskId) ?? null;
  const taskSessions = sessions[taskId] ?? null;
  const workflow = workflows[taskId] ?? null;
  const primarySession = taskSessions?.[primarySessionName(taskSessions, workflow ?? undefined)];
  const review = reviews[taskId];
  const ship = ships[taskId];
  const sessionNames = taskSessionNames(taskSessions ?? undefined, workflow ?? undefined);
  const activeName =
    selected !== null && sessionNames.includes(selected)
      ? selected
      : defaultSessionName(taskSessions ?? undefined, workflow ?? undefined);
  const session = taskSessions?.[activeName] ?? null;
  const live = hasLiveAgent(session);

  // The cockpit: transcript from the raw event channel, metrics polled at turn
  // boundaries, both gated on there being a live agent for the selected tab.
  const { transcript, turnEpoch } = useAgentChannel(taskId, activeName, live);
  const status = useAgentStatus(taskId, activeName, live, turnEpoch);

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

  const taskForShipFlow = liveTask ?? detail;
  const showShipFlow = hasShipFlowHandoff(taskForShipFlow, review, ship);

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

      {workflow !== null && workflow.steps.length > 0 && <WorkflowStrip workflow={workflow} />}

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
              {/* Tasks that predate templates have a null template_name — no annotation. */}
              {detail.template_name !== null && ` · template ${detail.template_name}`}
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

      <TaskStatusStrip session={session} status={status} />
      <ReviewPanel
        taskId={taskId}
        review={review}
        primarySession={primarySession}
        showShipFlow={showShipFlow}
      />


      {workflow !== null && workflow.status === "waiting" && (
        <GateCard taskId={taskId} workflow={workflow} />
      )}

      {session?.question && (
        <QuestionCard taskId={taskId} session={activeName} question={session.question} />
      )}

      <div className="cockpitGrid">
        <div className="transcriptColumn">
          {sessionNames.length > 1 && (
            <SessionTabs
              names={sessionNames}
              taskSessions={taskSessions}
              active={activeName}
              onSelect={setSelected}
            />
          )}
          <TaskTranscript transcript={transcript} />
        </div>
        <TaskComposer
          taskId={taskId}
          session={activeName}
          hasLiveAgent={live}
          isStreaming={isStreaming(status.state)}
          sessionStatus={session?.status ?? null}
        />
      </div>
    </>
  );
}
