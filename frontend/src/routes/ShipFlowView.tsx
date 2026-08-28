import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useDaemonState } from "../lib/useDaemonState";
import { primarySessionName } from "../lib/daemonReducer";
import { projectReview } from "../lib/reviewPresentation";
import { cleanupTask, draftShip, recheckGitHub, recheckGpg, shipCommit } from "../lib/api";
import { confirmCleanup } from "../lib/cleanup";
import { formatElapsed } from "../lib/formatElapsed";
import {
  canonicalGitHubTarget,
  currentGitHubTargetStatus,
  githubCredentialRecovery,
  safeGitHubDetail,
} from "../lib/githubPresentation";
import { ReviewSummary } from "../components/ReviewSummary";
import type {
  GitHubStatus,
  GitHubTargetStatus,
  GpgStatus,
  ReviewState,
  SessionInfo,
  ShipState,
  Task,
} from "../types";
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

function GitHubPreflightBanner({
  github,
  upstreamUrl,
  target,
  checking,
  requestError,
  onRecheck,
}: {
  github: GitHubStatus | null;
  upstreamUrl: string | null | undefined;
  target: GitHubTargetStatus | undefined;
  checking: boolean;
  requestError: string | null;
  onRecheck: () => void;
}) {
  const identity = github?.identity;
  const canonicalTarget = canonicalGitHubTarget(upstreamUrl);
  const targetLabel = target?.target
    ? `${target.target.host}/${target.target.owner}/${target.target.repository}`
    : (canonicalTarget ?? "the registered upstream");
  const account = identity?.login ? `@${identity.login}` : "the selected GitHub account";
  const detail = requestError ?? safeGitHubDetail(target?.detail) ?? safeGitHubDetail(identity?.detail);

  let tone = "blocked";
  let title = "GitHub preflight required";
  let message = `Shipping is blocked before Git changes until ${targetLabel} is checked.`;
  let recovery: string | null = null;
  if (checking) {
    tone = "checking";
    title = "Checking GitHub access";
    message = `Checking ${account} for ${targetLabel}.`;
  } else if (requestError) {
    title = "GitHub check could not complete";
    message = `Shipping remains blocked until ${targetLabel} can be re-checked.`;
  } else if (!identity || identity.state === "unknown") {
    title = "GitHub identity has not been checked";
  } else if (identity.state === "missing") {
    title = "GitHub CLI is unavailable";
    message = `Install or correct the configured GitHub CLI, then re-check ${targetLabel}.`;
  } else if (identity.state === "unauthenticated") {
    title = "GitHub authentication is required";
    message = `${targetLabel} cannot be checked until the daemon's GitHub CLI authenticates.`;
    recovery = githubCredentialRecovery(identity);
  } else if (identity.state === "error") {
    title = "GitHub identity check failed";
    message = `Shipping remains blocked until ${targetLabel} can be checked safely.`;
  } else if (!canonicalTarget) {
    title = "Registered upstream is unsupported";
    message = "Shipping is blocked because the registered upstream is not a supported GitHub target.";
  } else if (!target) {
    title = "GitHub target needs a current check";
    message = `${account} has no current eligibility result for ${targetLabel}.`;
  } else if (target.state === "allowed") {
    tone = "ready";
    title = "GitHub preflight ready";
    message = `${account} is eligible to create a pull request for ${targetLabel}.`;
  } else if (target.state === "denied") {
    title = "Repository access is denied";
    message = `${account} cannot create a pull request for ${targetLabel}.`;
    recovery = `Grant ${account} the required repository and pull-request access, or deliberately select another identity. Ompire will not switch accounts automatically.`;
  } else {
    title = "GitHub target check failed";
    message = `${account} could not be verified for ${targetLabel}.`;
    recovery = githubCredentialRecovery(identity);
  }

  return (
    <div
      className={`githubBanner githubBanner${tone[0].toUpperCase()}${tone.slice(1)}`}
      role={tone === "ready" ? "status" : "alert"}
      aria-live="polite"
      data-testid="github-preflight-banner"
      data-state={tone}
    >
      <strong>{title}</strong>
      <p>{message}</p>
      {detail && <p className="githubBannerDetail">{detail}</p>}
      {recovery && <p>{recovery}</p>}
      <p className="githubBoundaryNote">
        This checks GitHub API identity and repository eligibility only. It does not verify SSH or HTTPS
        authentication used by <code>git push</code>.
      </p>
      <button
        type="button"
        disabled={checking}
        onClick={onRecheck}
        data-testid="recheck-github-target-button"
      >
        {checking ? "Checking GitHub…" : "Re-check GitHub"}
      </button>
    </div>
  );
}

function CommitStep({
  task,
  session,
  review,
  ship,
  gpg,
  github,
  upstreamUrl,
}: {
  task: Task;
  session: SessionInfo | undefined;
  review: ReviewState | undefined;
  ship: ShipState | undefined;
  gpg: GpgStatus | null;
  github: GitHubStatus | null;
  upstreamUrl: string | null | undefined;
}) {
  const taskId = task.id;
  const [mode, setMode] = useState<"squash" | "retain">("squash");
  const [message, setMessage] = useState("");
  const [prTitle, setPrTitle] = useState("");
  const [prBody, setPrBody] = useState("");
  const [touched, setTouched] = useState({
    message: false,
    prTitle: false,
    prBody: false,
  });
  const [draftRequest, setDraftRequest] = useState<"automatic" | "replacement" | null>(null);
  const [draftCommandError, setDraftCommandError] = useState<string | null>(null);
  const [commitCommandError, setCommitCommandError] = useState<string | null>(null);
  const [recheckingGpg, setRecheckingGpg] = useState(false);
  const [checkingGitHub, setCheckingGitHub] = useState(false);
  const [gitHubCheckError, setGitHubCheckError] = useState<string | null>(null);
  const draftCommandLocked = useRef(false);
  const gitHubRecheckLocked = useRef(false);
  const gitHubCheckedTarget = useRef<string | null>(null);
  const automaticAttempted = useRef(false);

  useEffect(() => {
    const draft = ship?.draft;
    if (!draft) return;
    setMessage((prev) => (touched.message ? prev : draft.commit_message));
    setPrTitle((prev) => (touched.prTitle ? prev : draft.pr_title));
    setPrBody((prev) => (touched.prBody ? prev : draft.pr_body));
  }, [ship?.draft]); // eslint-disable-line react-hooks/exhaustive-deps
  // `touched` is intentionally sampled when a new draft object arrives. Each
  // field changed after the request baseline keeps the operator's value.

  useEffect(() => {
    if (ship?.updated_at) setDraftCommandError(null);
  }, [ship?.updated_at]);
  useEffect(() => {
    if (ship?.updated_at) setCommitCommandError(null);
  }, [ship?.updated_at]);

  const published =
    task.state === "archived" ||
    task.pr_url !== null ||
    ship?.pr_url != null ||
    ship?.status === "shipped";
  const reviewAllowsAutomatic = review === undefined || review.status === "approved";
  const sessionIdle = session?.status === "idle";
  const automaticEligible =
    ship === undefined && !published && reviewAllowsAutomatic && sessionIdle;
  const automaticPending = automaticEligible && !automaticAttempted.current;
  const drafting =
    draftRequest !== null || ship?.status === "drafting" || automaticPending;
  const publishing =
    ship?.status === "committing" || ship?.status === "pushing";
  const draftFailed =
    ship?.status === "error" && ship.last_step?.step === "draft";
  const canRequestDraft =
    !published &&
    sessionIdle &&
    !drafting &&
    !publishing &&
    ship?.status !== "shipped";

  const requestDraft = useCallback(
    async (replace: boolean, kind: "automatic" | "replacement") => {
      if (draftCommandLocked.current) return;
      draftCommandLocked.current = true;
      if (kind === "automatic") automaticAttempted.current = true;
      setDraftRequest(kind);
      setDraftCommandError(null);
      try {
        await draftShip(taskId, replace ? { replace: true } : undefined);
      } catch (caught: unknown) {
        setDraftCommandError(
          caught instanceof Error ? caught.message : String(caught),
        );
      } finally {
        draftCommandLocked.current = false;
        setDraftRequest(null);
      }
    },
    [taskId],
  );

  useEffect(() => {
    if (automaticEligible && !automaticAttempted.current) {
      void requestDraft(false, "automatic");
    }
  }, [automaticEligible, requestDraft]);

  const githubTarget = currentGitHubTargetStatus(github, upstreamUrl);
  const githubReady = !checkingGitHub && githubTarget?.state === "allowed";
  const targetRequestKey = `${taskId}:${upstreamUrl ?? ""}`;
  const requestGitHubCheck = useCallback(async () => {
    if (!upstreamUrl || gitHubRecheckLocked.current) return;
    gitHubRecheckLocked.current = true;
    setCheckingGitHub(true);
    setGitHubCheckError(null);
    try {
      await recheckGitHub(taskId);
    } catch (caught: unknown) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setGitHubCheckError(safeGitHubDetail(message) ?? "GitHub recheck failed");
    } finally {
      gitHubRecheckLocked.current = false;
      setCheckingGitHub(false);
    }
  }, [taskId, upstreamUrl]);

  useEffect(() => {
    if (
      published ||
      !upstreamUrl ||
      gitHubCheckedTarget.current === targetRequestKey ||
      gitHubRecheckLocked.current
    ) {
      return;
    }
    gitHubCheckedTarget.current = targetRequestKey;
    void requestGitHubCheck();
  }, [checkingGitHub, published, requestGitHubCheck, targetRequestKey, upstreamUrl]);

  const gpgCached = gpg?.state === "cached";
  const gpgLocked = gpg?.state === "locked";
  const canCommit =
    gpgCached &&
    githubReady &&
    !drafting &&
    !publishing &&
    (mode === "retain" || message.trim().length > 0);
  const unlockCommand = gpg?.key ? `echo | gpg --clearsign -u ${gpg.key} >/dev/null` : "";
  const retainSelected = mode === "retain";

  async function onRedraft() {
    if (!canRequestDraft) return;
    const hasOperatorEdits = touched.message || touched.prTitle || touched.prBody;
    if (
      hasOperatorEdits &&
      !window.confirm(
        "Re-draft publication metadata via the agent?\n\nThis replaces the commit message, PR title, and PR body you edited. Changes made after drafting starts will still be preserved.",
      )
    ) {
      return;
    }
    setTouched({ message: false, prTitle: false, prBody: false });
    await requestDraft(true, "replacement");
  }

  async function onCommit() {
    if (!canCommit) return;
    setCommitCommandError(null);
    try {
      await shipCommit(taskId, {
        message: message.trim(),
        pr_title: prTitle.trim(),
        pr_body: prBody.trim(),
        mode,
      });
    } catch (caught: unknown) {
      const detail = caught instanceof Error ? caught.message : String(caught);
      setCommitCommandError(safeGitHubDetail(detail) ?? detail);
    }
  }

  async function onRecheckGpg() {
    setRecheckingGpg(true);
    try {
      await recheckGpg();
    } finally {
      setRecheckingGpg(false);
    }
  }

  async function onRecheckGitHub() {
    await requestGitHubCheck();
  }

  let draftStatus: string;
  if (drafting) {
    draftStatus = "Drafting… Keep editing; fields you change will not be overwritten.";
  } else if (published) {
    draftStatus = "This task is already published; agent drafting is unavailable.";
  } else if (session === undefined || session.status === "failed") {
    draftStatus = "No live primary agent is available. Enter publication metadata manually.";
  } else if (session.status !== "idle") {
    draftStatus = `Drafting is waiting for the primary agent to become idle (currently ${session.status}).`;
  } else if (review !== undefined && review.status !== "approved" && ship === undefined) {
    draftStatus = "Automatic drafting is waiting for an approved review. Manual entry and Re-draft remain available.";
  } else if (draftFailed || draftCommandError !== null) {
    draftStatus = "Drafting failed. Correct the metadata manually or retry via the agent.";
  } else if (ship?.draft) {
    draftStatus = "Agent draft ready. Review and edit every field before signing.";
  } else {
    draftStatus = "Agent drafting is available; manual entry remains available.";
  }

  const draftButtonLabel = drafting
    ? draftRequest === "replacement"
      ? "Re-drafting…"
      : "Drafting…"
    : draftFailed || draftCommandError !== null
      ? "Retry drafting"
      : "Re-draft via agent";

  return (
    <div className="shipStep" data-testid="ship-step-commit">
      <div className="stepHeader">
        <StepIcon
          index={2}
          active={drafting || ship?.status === "committing" || ship?.status === "drafted"}
          done={ship?.status === "shipped"}
          error={ship?.status === "error"}
        />
        <span className="stepTitle">Commit</span>
        {ship?.status && ship.status !== "drafted" && ship.status !== "error" && (
          <span className={`stepStatusBadge ${ship.status}`}>{ship.status}</span>
        )}
      </div>

      <p className="draftStatus" data-testid="draft-status">
        {draftStatus}
      </p>

      <div className="commitMode" data-testid="commit-mode">
        <label className="modeOption">
          <input
            type="radio"
            name={`commit-mode-${taskId}`}
            value="squash"
            checked={mode === "squash"}
            onChange={() => setMode("squash")}
            disabled={publishing}
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
            disabled={publishing}
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
            disabled={publishing || retainSelected}
            onChange={(e) => {
              setMessage(e.target.value);
              setTouched((current) => ({ ...current, message: true }));
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
            disabled={publishing}
            onChange={(e) => {
              setPrTitle(e.target.value);
              setTouched((current) => ({ ...current, prTitle: true }));
            }}
            data-testid="pr-title"
          />
        </label>
        <label>
          PR body
          <textarea
            rows={5}
            value={prBody}
            disabled={publishing}
            onChange={(e) => {
              setPrBody(e.target.value);
              setTouched((current) => ({ ...current, prBody: true }));
            }}
            data-testid="pr-body"
          />
        </label>
      </div>

      <div className="commitActions">
        <button
          type="button"
          disabled={!canRequestDraft}
          onClick={() => void onRedraft()}
          data-testid="redraft-button"
        >
          {draftButtonLabel}
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

      {draftCommandError && (
        <div className="shipError" data-testid="draft-command-error">
          Draft request failed: {draftCommandError}
        </div>
      )}
      {commitCommandError && (
        <div className="shipError" data-testid="commit-command-error">
          {commitCommandError}
        </div>
      )}

      {gpgLocked && unlockCommand && (
        <div className="gpgBanner" data-testid="gpg-locked-banner">
          <strong>GPG signing key is locked</strong>
          <p>Warm the passphrase cache in a terminal, then re-check:</p>
          <code data-testid="gpg-unlock-command">{unlockCommand}</code>
          <button
            type="button"
            disabled={recheckingGpg}
            onClick={() => void onRecheckGpg()}
            data-testid="recheck-gpg-button"
          >
            {recheckingGpg ? "Checking…" : "Re-check key"}
          </button>
        </div>
      )}

      {!published && (
        <GitHubPreflightBanner
          github={github}
          upstreamUrl={upstreamUrl}
          target={githubTarget}
          checking={checkingGitHub}
          requestError={gitHubCheckError}
          onRecheck={() => void onRecheckGitHub()}
        />
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
  else if (ship?.last_step?.step === "commit" && ship.last_step.status === "failed")
    progress = `Commit failed: ${typeof ship.last_step.detail === "string" ? ship.last_step.detail : ""}`.trim();
  else if (ship?.last_step?.step === "push" && ship.last_step.status === "failed")
    progress = `Push failed: ${typeof ship.last_step.detail === "string" ? ship.last_step.detail : ""}`.trim();
  else if (ship?.last_step?.step === "pr" && ship.last_step.status === "failed")
    progress = `PR failed: ${typeof ship.last_step.detail === "string" ? ship.last_step.detail : ""}`.trim();
  else if (ship?.status === "committing" || ship?.last_step?.step === "commit")
    progress = "Creating signed squash commit…";
  else if (ship?.status === "pushing" || ship?.last_step?.step === "push")
    progress = "Pushing branch…";
  else if (ship?.last_step?.step === "pr") progress = "Opening pull request…";

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
  const { snapshotReady, projects, tasks, sessions, workflows, reviews, ships, gpg, gh } = useDaemonState();

  const task = taskId === null ? undefined : tasks.find((candidate) => candidate.id === taskId);
  const project = task === undefined ? undefined : projects.find((candidate) => candidate.name === task.project_name);
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
        <CommitStep
          key={taskId}
          task={task}
          session={session}
          review={review}
          ship={ship}
          gpg={gpg}
          github={gh}
          upstreamUrl={project?.upstream_url}
        />
        <PushPrStep ship={ship} prUrl={task.pr_url} />
        <CleanupStep task={task} />
      </div>
    </>
  );
}
