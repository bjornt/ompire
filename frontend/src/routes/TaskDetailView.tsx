import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ReviewSummary } from "../components/ReviewSummary";
import { WorkflowRevision } from "../components/WorkflowRevision";
import { StepAttempts } from "../components/workflow/StepAttempts";
import { attemptsFor } from "../lib/workflowAttempts";
import { asObject } from "../lib/workflowDocument";
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
import { type ApprovalBinding, projectReview } from "../lib/reviewPresentation";
import { approvalBindingFor, hasShipFlowHandoff } from "../lib/shipPresentation";
import { useDaemonState } from "../lib/useDaemonState";
import type {
  GateChoice,
  GateSnapshot,
  ReviewState,
  RunAuthority,
  SessionInfo,
  StepRecord,
  Task,
  TaskDetail,
  WorkflowState,
  WorkshopStatus,
} from "../types";
import { formatElapsed } from "../lib/formatElapsed";
import { QuestionCard } from "./QuestionCard";
import { TaskConfigurationPanel } from "./TaskConfigurationPanel";
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

/** A format-2 gate's persisted question, if this record is one.
 *
 * Read off the record rather than looked up in the catalog: the question a
 * person is answering is the one that was asked, even if the definition has
 * been edited since. */
function gateSnapshot(record: StepRecord | undefined): GateSnapshot | null {
  const outcome = record?.outcome;
  if (!outcome || typeof outcome !== "object") return null;
  const snapshot = outcome as unknown as GateSnapshot;
  if (typeof snapshot.message !== "string" || !Array.isArray(snapshot.choices)) {
    return null;
  }
  return snapshot;
}

/** The named ending a decided choice leads to, for the card's own summary. */
function choiceDestination(choice: GateChoice): string | null {
  const next = choice.next as { step?: unknown; result?: unknown } | undefined;
  if (next && typeof next.step === "string") return next.step;
  if (next && typeof next.result === "string") return next.result;
  return null;
}

/** One-line summary for a finished step's chip title: the outcome's summary
 * (design D-3 outcome schema) or message, else the error. */
function chipTitle(record: StepRecord): string | undefined {
  if (record.pause) return record.pause.message;
  if (record.error) return record.error;
  const summary = record.outcome?.summary;
  if (typeof summary === "string") return summary;
  return gateMessage(record) ?? undefined;
}

/** Workflow strip (workflow-engine design D-9): one chip per executed step
 * record in order, the in-flight step highlighted, a waiting gate chip
 * pulsing notify-tier. */
function WorkflowStrip({ workflow, task }: { workflow: WorkflowState; task: Task | null }) {
  const current = workflowActive(workflow) ? currentStepRecord(workflow) : undefined;
  return (
    <div className="panel workflowStrip" data-testid="workflow-strip">
      <span className={`workflowRunStatus ${workflow.status ?? "none"}`} data-testid="workflow-run-status">
        {workflow.name}
        {workflow.status ? ` · ${workflow.status}` : ""}
      </span>
      {/* The declared ending, when the run named one. `complete` says the run
          stopped; only this says what stopping meant (ADR-0029). A format-1
          run has no name for its ending and shows none. */}
      {task?.workflow_result != null && (
        <span className="workflowResult" data-testid="workflow-result">
          {task.workflow_result}
        </span>
      )}
      {/* The revision this task accepted, not what the name means today
          (ADR-0028). A short prefix is enough to tell two apart at a glance;
          the whole thing, and the definition itself, are in Configuration. */}
      {task?.workflow_revision != null && (
        <span
          className="workflowRevision mono"
          title={task.workflow_revision}
          data-testid="workflow-revision-chip"
        >
          {task.workflow_revision.replace(/^sha256:/, "").slice(0, 12)}
        </span>
      )}
      {task?.workflow_ready === false && (
        <span className="workflowRevision warn" data-testid="workflow-not-ready">
          {task.workflow_readiness_reason === "needs_workflow_confirmation"
            ? "definition not recorded"
            : `definition unavailable (${task.workflow_readiness_reason})`}
        </span>
      )}
      <div className="workflowChips">
        {/* An empty chip row is ambiguous — it reads the same as a strip that
            failed to load. Say which it is. */}
        {workflow.steps.length === 0 && (
          <span className="workflowEmpty" data-testid="workflow-not-started">
            no steps have run yet
          </span>
        )}
        {workflow.steps.map((record) => {
          const isCurrent = current !== undefined && record.seq === current.seq;
          return (
            <span
              key={record.seq}
              className={`workflowChip ${record.status}${isCurrent ? " current" : ""}`}
              title={chipTitle(record)}
              data-testid={`workflow-chip-${record.seq}`}
              data-paused={record.pause !== null ? "true" : undefined}
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

/** Gate card (workflow-engine design D-9): shown while the run is `waiting`.
 *
 * Three different things park a run here and the card must not conflate them
 * (ADR-0028, ADR-0030).
 *
 * A *format-2 gate* is a question with named choices: the operator picks one,
 * some require feedback, and each one has a declared destination. There is no
 * generic Resume — answering means choosing a route the definition offered.
 * A *format-1 gate* is the older "look at this and continue", with an
 * optional note. An *uncertainty pause* is the engine refusing to guess; the
 * action retries the step that could not be decided and never continues past
 * it.
 *
 * All three send the waiting attempt's own sequence number, so a stale tab or
 * a double submit is refused rather than applied to a different attempt.
 * Nothing is pre-selected, so opening the card authorizes nothing. Errors
 * surface inline; a 409 means the daemon has moved on, and the card is
 * replaced by whatever it is waiting on now rather than re-submitting. */
function GateCard({ taskId, workflow }: { taskId: number; workflow: WorkflowState }) {
  const [note, setNote] = useState("");
  const [choiceId, setChoiceId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const waiting = [...workflow.steps].reverse().find((r) => r.status === "waiting");
  const pause = waiting?.pause ?? null;
  const snapshot = pause === null ? gateSnapshot(waiting) : null;
  const choices = snapshot?.choices ?? [];
  const message =
    pause?.message ?? snapshot?.message ?? gateMessage(waiting) ?? "Waiting at a workflow gate.";
  // Unsent input belongs to the attempt it was typed against. When the run
  // moves — another tab answered, a restart re-armed something else — the
  // draft is dropped rather than carried onto a different question.
  const identity = `${taskId}:${waiting?.seq ?? "none"}`;
  const lastIdentity = useRef(identity);
  useEffect(() => {
    if (lastIdentity.current !== identity) {
      lastIdentity.current = identity;
      setNote("");
      setChoiceId(null);
      setError(null);
    }
  }, [identity]);

  const selected = choices.find((c) => c.id === choiceId) ?? null;
  const feedbackMissing = selected?.feedback_required === true && note.trim() === "";
  // An answer that authorizes publication is not answerable from here. It
  // needs the content, target, and identities the confirmation is checked
  // against, and those live on one surface rather than two: this card sends
  // the operator to it instead of offering a shortcut that would be refused.
  const authorizes = (selected?.authorize?.steps ?? []).length > 0;
  const blocked =
    busy ||
    waiting === undefined ||
    authorizes ||
    (choices.length > 0 && (selected === null || feedbackMissing));

  async function advance() {
    if (waiting === undefined) return;
    if (choices.length > 0 && selected === null) return;
    setBusy(true);
    setError(null);
    try {
      await resumeWorkflow(
        taskId,
        waiting.seq,
        note.trim() || undefined,
        selected?.id,
      );
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="panel gateCard"
      data-testid="gate-card"
      data-waiting-kind={pause !== null ? "pause" : choices.length > 0 ? "choice" : "gate"}
    >
      <h2 className="panelTitle">
        <span className="gateDot" />{" "}
        {pause === null
          ? `Workflow waiting — ${workflow.step}`
          : `Workflow stopped — ${pause.step}`}
      </h2>
      <div className="gateMessage" data-testid="gate-message">
        {message}
      </div>
      {pause !== null && (
        <p className="fieldHint" data-testid="pause-retry-target">
          Retrying re-enters <code>{pause.retry_step}</code> rather than skipping it, and
          it changes nothing that was recorded — if the same evidence is still missing,
          the run stops here again. A step that has used up the attempts its workflow
          allows goes to that workflow&apos;s gate instead.
        </p>
      )}
      {choices.length > 0 && (
        <fieldset className="gateChoices" data-testid="gate-choices">
          <legend className="fieldHint">
            Choose what happens next. Nothing is selected for you.
          </legend>
          {choices.map((choice) => {
            const destination = choiceDestination(choice);
            return (
              <label
                key={choice.id}
                className="gateChoice"
                data-testid={`gate-choice-${choice.id}`}
              >
                <input
                  type="radio"
                  name={`gate-choice-${waiting?.seq ?? 0}`}
                  value={choice.id}
                  checked={choiceId === choice.id}
                  disabled={busy}
                  onChange={() => setChoiceId(choice.id)}
                />
                <span className="gateChoiceLabel">{choice.label}</span>
                {destination !== null && (
                  <span className="gateChoiceNext mono">→ {destination}</span>
                )}
                {choice.feedback_required && (
                  <span className="gateChoiceRequired">needs a reason</span>
                )}
                {(choice.authorize?.steps ?? []).length > 0 && (
                  <span
                    className="gateChoiceNext"
                    data-testid={`gate-choice-authorizes-${choice.id}`}
                  >
                    authorizes {(choice.authorize?.steps ?? []).join(" → ")}
                  </span>
                )}
              </label>
            );
          })}
        </fieldset>
      )}
      {authorizes && (
        <p className="fieldHint" data-testid="gate-choice-needs-preview">
          This answer authorizes publication. Confirm it in{" "}
          <Link to={`/ship/${taskId}`} data-testid="gate-ship-link">
            Ship flow
          </Link>
          , where the exact content, destination, identities, and final text
          are shown — that preview is what the authorization is checked
          against.
        </p>
      )}
      {pause === null && (
        <textarea
          className="gateNote"
          aria-label={
            choices.length === 0
              ? "Resume note"
              : selected?.feedback_required
                ? "Reason (required)"
                : "Reason"
          }
          placeholder={
            selected?.feedback_required
              ? "Why — this is recorded with your decision and shown to the next step…"
              : "Optional note for the workflow (e.g. reviewed, looks good)…"
          }
          rows={2}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={busy}
        />
      )}
      {feedbackMissing && (
        <p className="fieldHint" data-testid="gate-feedback-required">
          “{selected?.label}” needs a reason before it can be submitted.
        </p>
      )}
      {error && (
        <div className="composerError" data-testid="gate-error">
          {error}
        </div>
      )}
      <div className="questionActions">
        <button
          type="button"
          className="sendButton"
          disabled={blocked}
          onClick={() => void advance()}
          data-testid={
            pause !== null ? "pause-retry" : choices.length > 0 ? "gate-answer" : "gate-resume"
          }
        >
          {pause !== null
            ? busy
              ? "Retrying…"
              : `Retry ${pause.retry_step}`
            : choices.length > 0
              ? busy
                ? "Submitting…"
                : "Submit decision"
              : busy
                ? "Resuming…"
                : "Resume"}
        </button>
      </div>
    </div>
  );
}

function ReviewPanel({
  taskId,
  review,
  primarySession,
  approvalBinding,
  showShipFlow,
  authority,
}: {
  taskId: number;
  review: ReviewState | undefined;
  primarySession: SessionInfo | undefined;
  approvalBinding: ApprovalBinding;
  showShipFlow: boolean;
  /** What this run's own procedure permits. A workflow that declares its own
   * review starts one at the step that declares it; starting another by hand
   * would grade content the run is still changing. */
  authority: RunAuthority | undefined;
}) {
  const presentation = projectReview(review, primarySession, approvalBinding);
  // The daemon decides; this only mirrors that answer so the button is not
  // offered where the service would refuse it.
  const reviewOwnedByRun =
    authority?.declares_review === true && authority.review_step_seq === null;
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
      <ReviewSummary
        review={review}
        primarySession={primarySession}
        approvalBinding={approvalBinding}
      />
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
      {reviewOwnedByRun && (
        <p className="stepHint" data-testid="review-owned-by-run">
          This task&apos;s workflow declares its own review step. The run starts
          the review when it reaches it — reviewing at another moment would
          grade content the run is still changing.
        </p>
      )}
      <div className="reviewActions">
        {presentation.canStart && !reviewOwnedByRun && (
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
            {presentation.state === "approved" && approvalBinding === "usable"
              ? "Continue to Ship flow"
              : "Open Ship flow"}
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

/** The procedure this task accepted, with what happened laid over it.
 *
 * Addressed by the task's *retained* revision, never by its workflow name:
 * the library may have moved on, been edited, or been archived since, and the
 * only definition that explains this run is the one it pinned (ADR-0028).
 *
 * The declaration and the attempts stay separate. An unvisited step is shown
 * as work the procedure allows; a step visited three times shows three
 * records. Answering a gate and retrying a pause remain the existing
 * attempt-scoped controls above — there is no action here on a step the run
 * has merely not reached. */
function PinnedProcedure({
  revision,
  workflow,
  onOpenSession,
}: {
  revision: string;
  workflow: WorkflowState;
  onOpenSession: (session: string) => void;
}) {
  const openAttempt = (seq: number) => {
    const card = document.getElementById(`attempt-${seq}`);
    card?.scrollIntoView?.({ block: "nearest" });
    card?.focus();
  };
  return (
    <div className="panel" data-testid="pinned-procedure">
      <h2 className="panelTitle">Procedure</h2>
      <p className="hint">
        The definition this task accepted, not what its name means today.
        Editing or archiving the workflow in the library changes neither this
        flow nor anything it recorded.
      </p>
      <WorkflowRevision
        revision={revision}
        current={workflow.step}
        summaryLabel="read the pinned procedure and what happened at each step"
        extras={(_index, name, step) => {
          const attempts = attemptsFor(workflow, name);
          const declaresEvidence = Object.keys(asObject(step.evidence) ?? {}).length > 0;
          return {
            className: name === workflow.step ? "flowCard current" : undefined,
            badge:
              attempts.length === 0 ? (
                <span className="flowChip">not visited</span>
              ) : (
                <span className="flowChip">
                  {attempts.length} attempt{attempts.length === 1 ? "" : "s"}
                </span>
              ),
            body: (
              <StepAttempts
                attempts={attempts}
                workflow={workflow}
                declaresEvidence={declaresEvidence}
                onOpenSession={onOpenSession}
                onOpenAttempt={openAttempt}
              />
            ),
          };
        }}
      />
    </div>
  );
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
  const primarySession = taskSessions?.[primarySessionName(taskSessions, workflow ?? undefined, liveTask ?? undefined)];
  const review = reviews[taskId];
  const ship = ships[taskId];
  const sessionNames = taskSessionNames(taskSessions ?? undefined, workflow ?? undefined);
  const activeName =
    selected !== null && sessionNames.includes(selected)
      ? selected
      : defaultSessionName(taskSessions ?? undefined, workflow ?? undefined, liveTask ?? undefined);
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

      {/* Shown as soon as the task has a run, even before its first attempt:
          a strip that appears only once something has happened cannot say
          that nothing has yet. */}
      {workflow !== null && <WorkflowStrip workflow={workflow} task={liveTask} />}

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

        <TaskConfigurationPanel detail={detail} sessions={taskSessions ?? {}} />

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
        approvalBinding={approvalBindingFor(review, ship)}
        showShipFlow={showShipFlow}
        authority={ship?.authority}
      />


      {workflow !== null && workflow.status === "waiting" && (
        <GateCard taskId={taskId} workflow={workflow} />
      )}

      {workflow !== null && liveTask?.workflow_revision != null && (
        <PinnedProcedure
          revision={liveTask.workflow_revision}
          workflow={workflow}
          onOpenSession={setSelected}
        />
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
