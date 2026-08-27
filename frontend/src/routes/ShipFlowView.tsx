import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useDaemonState } from "../lib/useDaemonState";
import { primarySessionName } from "../lib/daemonReducer";
import { projectReview } from "../lib/reviewPresentation";
import { cleanupTask, draftShip, recheckGpg, shipCommit } from "../lib/api";
import { confirmCleanup } from "../lib/cleanup";
import { formatElapsed } from "../lib/formatElapsed";
import { ReviewSummary } from "../components/ReviewSummary";
import type { GpgStatus, ReviewState, SessionInfo, ShipState, Task } from "../types";
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
  const presentation = projectReview(review, session);

  return (
    <div
      className={`shipStep ${presentation.state === "open" ? "stepOpen" : ""}`}
      data-testid="ship-step-review"
    >
      <div className="stepHeader">
        <StepIcon
          index={1}
          active={presentation.state === "open"}
          done={presentation.state === "approved"}
          error={presentation.state === "error"}
        />
        <span className="stepTitle">Review</span>
      </div>
      <ReviewSummary review={review} primarySession={session} />
    </div>
  );
}

function CommitStep({
  taskId,
  ship,
  gpg,
}: {
  taskId: number;
  ship: ShipState | undefined;
  gpg: GpgStatus | null;
}) {
  const [mode, setMode] = useState<"squash" | "retain">("squash");
  const [message, setMessage] = useState("");
  const [prTitle, setPrTitle] = useState("");
  const [prBody, setPrBody] = useState("");
  const [touched, setTouched] = useState({ message: false, prTitle: false, prBody: false });
  const [redrafting, setRedrafting] = useState(false);
  const [rechecking, setRechecking] = useState(false);

  useEffect(() => {
    const draft = ship?.draft;
    if (!draft) return;
    setMessage((prev) => (touched.message ? prev : draft.commit_message));
    setPrTitle((prev) => (touched.prTitle ? prev : draft.pr_title));
    setPrBody((prev) => (touched.prBody ? prev : draft.pr_body));
  }, [ship?.draft]); // eslint-disable-line react-hooks/exhaustive-deps
  // touched resets intentionally omitted: the effect seeds untouched fields
  // when a draft arrives without clobbering fields the user has edited.

  const gpgCached = gpg?.state === "cached";
  const gpgLocked = gpg?.state === "locked";
  const busy = ship?.status === "drafting" || ship?.status === "committing";
  const canCommit =
    gpgCached && !busy && (mode === "retain" || message.trim().length > 0);
  const unlockCommand = gpg?.key ? `echo | gpg --clearsign -u ${gpg.key} >/dev/null` : "";
  const retainSelected = mode === "retain";

  async function onRedraft() {
    setRedrafting(true);
    setTouched({ message: false, prTitle: false, prBody: false });
    try {
      await draftShip(taskId);
    } finally {
      setRedrafting(false);
    }
  }

  async function onCommit() {
    if (!canCommit) return;
    await shipCommit(taskId, {
      message: message.trim(),
      pr_title: prTitle.trim(),
      pr_body: prBody.trim(),
      mode,
    });
  }

  async function onRecheck() {
    setRechecking(true);
    try {
      await recheckGpg();
    } finally {
      setRechecking(false);
    }
  }

  return (
    <div className="shipStep" data-testid="ship-step-commit">
      <div className="stepHeader">
        <StepIcon
          index={2}
          active={ship?.status === "drafting" || ship?.status === "committing" || ship?.status === "drafted"}
          done={ship?.status === "shipped"}
          error={ship?.status === "error"}
        />
        <span className="stepTitle">Commit</span>
        {ship?.status && ship.status !== "drafted" && ship.status !== "error" && (
          <span className={`stepStatusBadge ${ship.status}`}>{ship.status}</span>
        )}
      </div>

      <div className="commitMode" data-testid="commit-mode">
        <label className="modeOption">
          <input
            type="radio"
            name={`commit-mode-${taskId}`}
            value="squash"
            checked={mode === "squash"}
            onChange={() => setMode("squash")}
            disabled={busy}
          />
          Squash
        </label>
        <label className="modeOption">
          <input
            type="radio"
            name={`commit-mode-${taskId}`}
            value="retain"
            checked={mode === "retain"}
            onChange={() => setMode("retain")}
            disabled={busy}
          />
          Retain
        </label>
      </div>

      <div className="commitFields">
        <label>
          Commit message
          <textarea
            rows={4}
            value={message}
            disabled={busy || retainSelected}
            onChange={(e) => {
              setMessage(e.target.value);
              setTouched((t) => ({ ...t, message: true }));
            }}
            data-testid="commit-message"
          />
          {retainSelected && (
            <span className="fieldHint" data-testid="retain-message-hint">
              Per-commit messages are retained in this mode.
            </span>
          )}
        </label>
        <label>
          PR title
          <input
            type="text"
            value={prTitle}
            disabled={busy}
            onChange={(e) => {
              setPrTitle(e.target.value);
              setTouched((t) => ({ ...t, prTitle: true }));
            }}
            data-testid="pr-title"
          />
        </label>
        <label>
          PR body
          <textarea
            rows={5}
            value={prBody}
            disabled={busy}
            onChange={(e) => {
              setPrBody(e.target.value);
              setTouched((t) => ({ ...t, prBody: true }));
            }}
            data-testid="pr-body"
          />
        </label>
      </div>

      <div className="commitActions">
        <button
          type="button"
          disabled={redrafting || busy}
          onClick={() => void onRedraft()}
          data-testid="redraft-button"
        >
          {redrafting ? "Re-drafting…" : "Re-draft via agent"}
        </button>
        <button
          type="button"
          className="signCommitButton"
          disabled={!canCommit}
          onClick={() => void onCommit()}
          data-testid="sign-commit-button"
        >
          Sign & commit
        </button>
      </div>

      {gpgLocked && unlockCommand && (
        <div className="gpgBanner" data-testid="gpg-locked-banner">
          <strong>GPG signing key is locked</strong>
          <p>Warm the passphrase cache in a terminal, then re-check:</p>
          <code data-testid="gpg-unlock-command">{unlockCommand}</code>
          <button
            type="button"
            disabled={rechecking}
            onClick={() => void onRecheck()}
            data-testid="recheck-gpg-button"
          >
            {rechecking ? "Checking…" : "Re-check key"}
          </button>
        </div>
      )}

      {ship?.status === "error" && ship.error && (
        <div className="shipError" data-testid="ship-error">
          {ship.error}
        </div>
      )}
    </div>
  );
}

function PushPrStep({
  ship,
  prUrl,
}: {
  ship: ShipState | undefined;
  prUrl: string | null | undefined;
}) {
  const active = ship?.status === "committing" || ship?.status === "pushing";
  const done = ship?.status === "shipped";
  const error = ship?.status === "error";
  const url = ship?.pr_url ?? prUrl ?? null;

  let progress = "Waiting for a signed commit.";
  if (done) progress = "Pull request opened.";
  else if (ship?.lastStep?.step === "commit" && ship.lastStep.status === "failed")
    progress = `Commit failed: ${ship.lastStep.detail ?? ""}`.trim();
  else if (ship?.lastStep?.step === "push" && ship.lastStep.status === "failed")
    progress = `Push failed: ${ship.lastStep.detail ?? ""}`.trim();
  else if (ship?.lastStep?.step === "pr" && ship.lastStep.status === "failed")
    progress = `PR failed: ${ship.lastStep.detail ?? ""}`.trim();
  else if (ship?.status === "committing" || ship?.lastStep?.step === "commit")
    progress = "Creating signed squash commit…";
  else if (ship?.status === "pushing" || ship?.lastStep?.step === "push")
    progress = "Pushing branch…";
  else if (ship?.lastStep?.step === "pr") progress = "Opening pull request…";

  return (
    <div className="shipStep" data-testid="ship-step-push-pr">
      <div className="stepHeader">
        <StepIcon index={3} active={active} done={done} error={error} />
        <span className="stepTitle">Push + PR</span>
        {ship?.status && ["committing", "pushing", "shipped", "error"].includes(ship.status) && (
          <span className={`stepStatusBadge ${ship.status}`}>{ship.status}</span>
        )}
      </div>
      <p className="stepHint" data-testid="push-pr-progress">
        {progress}
      </p>
      {url && (
        <a
          className="reviewReopenLink"
          href={url}
          target="_blank"
          rel="noreferrer"
          data-testid="pr-link"
        >
          {url.replace("https://", "")}
        </a>
      )}
    </div>
  );
}

function CleanupStep({ task }: { task: Task }) {
  const [busy, setBusy] = useState(false);
  const archived = task.state === "archived";
  // Cleanup gating (merge-poll capability, design D-4): the grace period is
  // the open-PR window itself — the step offers no action until the PR
  // resolves, then always behind the shared destructive-action confirmation.
  const ready =
    !archived && (task.pr_state === "merged" || task.pr_state === "closed");

  async function onCleanup() {
    if (!confirmCleanup(task)) return;
    setBusy(true);
    try {
      await cleanupTask(task.id);
    } finally {
      setBusy(false);
    }
  }

  let hint: string;
  if (archived) hint = `Cleaned up ${formatElapsed(task.updated_at)} ago — workshop removed, clone deleted.`;
  else if (!task.pr_url) hint = "Cleanup unlocks once this task has shipped a PR.";
  else if (task.pr_state === "merged")
    hint = `Merged ${formatElapsed(task.pr_merged_at ?? task.updated_at)} ago — ready for cleanup.`;
  else if (task.pr_state === "closed") hint = "PR closed without merging — cleanup is your call.";
  else hint = "On merge: workshop remove + delete clone. Awaiting merge · cleanup deferred.";

  return (
    <div
      className={`shipStep ${!task.pr_url && !archived ? "inert" : ""} ${ready ? "stepOpen" : ""}`}
      data-testid="ship-step-cleanup"
    >
      <div className="stepHeader">
        <StepIcon index={4} active={ready} done={archived} error={false} />
        <span className="stepTitle">Cleanup</span>
        {task.pr_state === "closed" && !archived && (
          <span className="stepStatusBadge closed">closed</span>
        )}
        {task.pr_state === "merged" && !archived && (
          <span className="stepStatusBadge merged">merged</span>
        )}
      </div>
      <p className="stepHint" data-testid="cleanup-hint">
        {hint}
      </p>
      {ready && (
        <button
          type="button"
          className="cleanupAction"
          disabled={busy}
          onClick={() => void onCleanup()}
          data-testid="cleanup-ship-button"
        >
          {busy ? "Cleaning up…" : "Clean up"}
        </button>
      )}
    </div>
  );
}

export function ShipFlowView() {
  const { id } = useParams();
  const taskId = id !== undefined && /^\d+$/.test(id) ? Number(id) : null;
  const { snapshotReady, tasks, sessions, workflows, reviews, ships, gpg } = useDaemonState();

  const task = taskId === null ? undefined : tasks.find((candidate) => candidate.id === taskId);
  // Ship is task-scoped: review and publishing always use the workflow's
  // primary session, never an in-flight step's focused session.
  const taskSessions = taskId === null ? undefined : sessions[taskId];
  const session = taskSessions?.[primarySessionName(taskSessions, taskId === null ? undefined : workflows[taskId])];
  const review = taskId === null ? undefined : reviews[taskId];
  const ship = taskId === null ? undefined : ships[taskId];

  if (!snapshotReady) {
    return (
      <div className="empty" data-testid="ship-flow-loading">
        <strong>Loading…</strong>
        <span>Waiting for the daemon snapshot.</span>
      </div>
    );
  }

  if (taskId === null || !task) {
    return (
      <div className="empty" data-testid="ship-flow-not-found">
        <strong>Task not found</strong>
        <span>The task is not available in the current daemon snapshot.</span>
        <span>
          <Link to="/ship">Ship flow</Link> · <Link to="/tasks">Tasks</Link>
        </span>
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
        <CommitStep taskId={taskId} ship={ship} gpg={gpg} />
        <PushPrStep ship={ship} prUrl={task.pr_url} />
        <CleanupStep task={task} />
      </div>
    </>
  );
}
