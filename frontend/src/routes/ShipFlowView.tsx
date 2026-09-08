import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useDaemonState } from "../lib/useDaemonState";
import { primarySessionName } from "../lib/daemonReducer";
import {
  cleanupTask,
  draftShip,
  previewShip,
  recheckGitHub,
  recheckGpg,
  saveShipDraft,
  shipCommit,
  shipPr,
  shipPush,
  shipReconcile,
} from "../lib/api";
import { canSign, gpgPresentation, keyLabel } from "../lib/gpgPresentation";
import { confirmCleanup } from "../lib/cleanup";
import { formatElapsed } from "../lib/formatElapsed";
import {
  ENDING_EFFECTS,
  ENDING_LABELS,
  approvalBindingFor,
  cleanupWarning,
  confirmationEffects,
  isDelivering,
  unresolvedActions,
} from "../lib/shipPresentation";
import {
  githubCredentialRecovery,
  safeGitHubDetail,
} from "../lib/githubPresentation";
import { ReviewSummary } from "../components/ReviewSummary";
import type {
  GpgStatus,
  ReviewState,
  SessionInfo,
  ShipAction,
  ShipEnding,
  ShipPreview,
  ShipProjection,
  Task,
} from "../types";
import "./ShipFlowView.css";

const ENDINGS: ShipEnding[] = ["commit", "push", "pr"];

function newRequestId(): string {
  return `ship-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function errorText(caught: unknown): string {
  if (caught instanceof Error) return caught.message;
  return String(caught);
}

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
  if (error) return <span className="stepIcon stepError">!</span>;
  if (done) return <span className="stepIcon stepDone">✓</span>;
  return <span className={`stepIcon ${active ? "stepActive" : "stepPending"}`}>{index}</span>;
}

function ReviewStep({
  session,
  review,
  ship,
}: {
  session: SessionInfo | undefined;
  review: ReviewState | undefined;
  ship: ShipProjection | undefined;
}) {
  const binding = approvalBindingFor(review, ship);
  const approved = review?.status === "approved" && binding === "usable";

  return (
    <div
      className={`shipStep ${review?.status === "open" ? "stepOpen" : ""}`}
      data-testid="ship-step-review"
    >
      <div className="stepHeader">
        <StepIcon
          index={1}
          active={review?.status === "open"}
          done={approved}
          error={review?.status === "error"}
        />
        <span className="stepTitle">Review</span>
      </div>
      <ReviewSummary
        review={review}
        primarySession={session}
        approvalBinding={binding}
      />
      {review?.status === "approved" && binding !== "usable" && (
        <p className="shipError" data-testid="stale-approval-notice">
          {binding === "stale"
            ? "This approval covers content the task has since changed. Start another review of the current content before delivering it."
            : "This approval does not identify the content it graded. Start a review to bind an approval to the current content."}
        </p>
      )}
    </div>
  );
}

function GpgBanner({ gpg }: { gpg: GpgStatus | null }) {
  const [rechecking, setRechecking] = useState(false);
  const signing = gpgPresentation(gpg);

  if (canSign(gpg)) {
    return signing.description ? (
      <p className="gpgSigner" data-testid="gpg-signer">
        Signing as {keyLabel(gpg)}
      </p>
    ) : null;
  }

  return (
    <div className="gpgBanner" data-testid="gpg-blocked-banner">
      <strong data-testid="gpg-blocked-reason">{signing.description}</strong>
      {signing.recovery && <p>{signing.recovery}</p>}
      {signing.command && <code data-testid="gpg-unlock-command">{signing.command}</code>}
      <button
        type="button"
        disabled={rechecking}
        onClick={() => {
          setRechecking(true);
          void recheckGpg().finally(() => setRechecking(false));
        }}
        data-testid="recheck-gpg-button"
      >
        {rechecking ? "Checking…" : "Re-check key"}
      </button>
    </div>
  );
}

function GitHubBanner({ taskId, detail }: { taskId: number; detail: string }) {
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <div
      className="githubBanner githubBannerBlocked"
      role="alert"
      aria-live="polite"
      data-testid="github-preflight-banner"
      data-state="blocked"
    >
      <strong>GitHub access is required for this ending</strong>
      <p>{safeGitHubDetail(detail) ?? detail}</p>
      {error && <p className="githubBannerDetail">{error}</p>}
      <p>{githubCredentialRecovery(undefined)}</p>
      <p className="githubBoundaryNote">
        This checks GitHub API identity and repository eligibility only. It does not verify SSH
        or HTTPS authentication used by <code>git push</code>.
      </p>
      <button
        type="button"
        disabled={checking}
        onClick={() => {
          setChecking(true);
          setError(null);
          void recheckGitHub(taskId)
            .catch((caught: unknown) =>
              setError(safeGitHubDetail(errorText(caught)) ?? "GitHub recheck failed"),
            )
            .finally(() => setChecking(false));
        }}
        data-testid="recheck-github-target-button"
      >
        {checking ? "Checking GitHub…" : "Re-check GitHub"}
      </button>
    </div>
  );
}

/** The authorization step: read what the run is asking, see exactly what an
 * answer would permit, then confirm that resolution and nothing else.
 *
 * How far a delivery goes is not chosen here any more. The workflow's own
 * chain decides it, and each approving answer names the exact actions it
 * grants — so this page shows the decision the run is at rather than offering
 * a menu of endings the procedure may never have declared. */
function DeliverStep({
  task,
  session,
  review,
  ship,
  gpg,
}: {
  task: Task;
  session: SessionInfo | undefined;
  review: ReviewState | undefined;
  ship: ShipProjection | undefined;
  gpg: GpgStatus | null;
}) {
  const taskId = task.id;
  const authority = ship?.authority;
  const atApproval = authority?.source === "workflow-gate";
  const grants = useMemo(
    () => (authority?.choices ?? []).filter((choice) => choice.authorizes !== null),
    [authority?.choices],
  );
  const [choiceId, setChoiceId] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [ending, setEnding] = useState<ShipEnding>("pr");
  const [mode, setMode] = useState<"squash" | "retain">("squash");
  const [message, setMessage] = useState("");
  const [prTitle, setPrTitle] = useState("");
  const [prBody, setPrBody] = useState("");
  const [touched, setTouched] = useState({ message: false, prTitle: false, prBody: false });
  const [preview, setPreview] = useState<ShipPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [drafting, setDrafting] = useState(false);
  const requestId = useRef(newRequestId());
  const commandLock = useRef(false);

  const draft = ship?.draft ?? null;
  const completed = useMemo(
    () => ship?.completed_actions ?? [],
    [ship?.completed_actions],
  );
  const hasSignedResult = completed.includes("commit");
  const delivering = isDelivering(ship);
  const unresolved = unresolvedActions(ship).length > 0;
  const prUrl = task.pr_url ?? ship?.pr_url ?? null;
  const published = task.state === "archived" || prUrl !== null;

  useEffect(() => {
    if (!draft) return;
    setMessage((prev) => (touched.message ? prev : draft.commit_message));
    setPrTitle((prev) => (touched.prTitle ? prev : draft.pr_title));
    setPrBody((prev) => (touched.prBody ? prev : draft.pr_body));
  }, [draft]); // eslint-disable-line react-hooks/exhaustive-deps
  // `touched` is sampled when a new draft arrives: a field the operator changed
  // after the request keeps their value.

  // Any change to what would be delivered invalidates the resolution the
  // operator was shown. There is no confirming a preview that no longer
  // describes the delivery.
  const invalidate = useCallback(() => {
    setPreview(null);
    requestId.current = newRequestId();
  }, []);

  useEffect(() => {
    invalidate();
  }, [ending, mode, message, prTitle, prBody, choiceId, ship?.version, invalidate]);

  // Nothing is preselected. An approval with one publishing answer still has
  // to be chosen: a default would make publication the thing that happens
  // when somebody clicks past a screen.
  useEffect(() => {
    if (!atApproval) setChoiceId(null);
    else if (choiceId !== null && !grants.some((c) => c.id === choiceId)) {
      setChoiceId(null);
    }
  }, [atApproval, grants, choiceId]);

  // The workflow's suggestion is a starting point, not the published text.
  const suggested = authority?.suggested;
  useEffect(() => {
    if (suggested === undefined) return;
    if (suggested.message !== undefined) {
      setMessage((prev) => (touched.message ? prev : suggested.message ?? ""));
    }
    if (suggested.pr_title !== undefined) {
      setPrTitle((prev) => (touched.prTitle ? prev : suggested.pr_title ?? ""));
    }
    if (suggested.pr_body !== undefined) {
      setPrBody((prev) => (touched.prBody ? prev : suggested.pr_body ?? ""));
    }
  }, [suggested]); // eslint-disable-line react-hooks/exhaustive-deps

  // A delivery that already has a signed result fixed its mode when it was
  // authorized; a continuation cannot re-choose it.
  useEffect(() => {
    if (hasSignedResult && ship?.mode) setMode(ship.mode);
  }, [hasSignedResult, ship?.mode]);

  const remaining = useMemo<ShipEnding[]>(
    () => ENDINGS.filter((candidate) => !completed.includes(candidate)),
    [completed],
  );

  useEffect(() => {
    if (remaining.length > 0 && completed.includes(ending)) {
      setEnding(remaining[remaining.length - 1]);
    }
  }, [remaining, completed, ending]);

  async function onDraft() {
    if (commandLock.current) return;
    commandLock.current = true;
    setDrafting(true);
    setCommandError(null);
    try {
      await draftShip(taskId, { replace: true });
    } catch (caught: unknown) {
      setCommandError(errorText(caught));
    } finally {
      commandLock.current = false;
      setDrafting(false);
    }
  }

  async function onSaveDraft() {
    setCommandError(null);
    try {
      await saveShipDraft(taskId, {
        commit_message: message,
        pr_title: prTitle,
        pr_body: prBody,
      });
    } catch (caught: unknown) {
      setCommandError(errorText(caught));
    }
  }

  async function onPreview() {
    setPreviewing(true);
    setCommandError(null);
    try {
      const resolved = await previewShip(taskId, {
        // At an approval the decision identifies itself; everywhere else the
        // run's own position does, and neither needs an ending from here.
        gate_seq: atApproval ? authority?.gate_seq ?? null : null,
        choice_id: atApproval ? choiceId : null,
        ending: authority === undefined ? ending : null,
        mode: authority === undefined ? mode : null,
        message,
        pr_title: prTitle,
        pr_body: prBody,
        request_id: requestId.current,
        delivery_id: ship?.delivery_id ?? null,
      });
      setPreview(resolved);
    } catch (caught: unknown) {
      setCommandError(errorText(caught));
    } finally {
      setPreviewing(false);
    }
  }

  async function onConfirm() {
    if (preview === null || !preview.deliverable) return;
    setConfirming(true);
    setCommandError(null);
    const body = {
      ending: preview.ending,
      pr_title: prTitle,
      pr_body: prBody,
      request_id: preview.request_id,
      preview_token: preview.preview_token,
    };
    try {
      if (preview.source !== "legacy-continuation") {
        // One confirmation operation, whichever page it came from: the
        // decision, the grant it produces, and the run's move to its first
        // action land together.
        await shipCommit(taskId, {
          ...body,
          gate_seq: preview.gate_seq,
          choice_id: preview.choice_id,
          note: note.trim() === "" ? null : note,
          mode: preview.mode,
          message,
          delivery_id: preview.delivery_id,
          expected_version: preview.version,
        });
      } else if (hasSignedResult && ship?.delivery_id != null) {
        const continuation = {
          ...body,
          delivery_id: ship.delivery_id,
          expected_version: preview.version,
        };
        if (completed.includes("push")) await shipPr(taskId, continuation);
        else await shipPush(taskId, continuation);
      } else {
        await shipCommit(taskId, {
          ...body,
          mode: preview.mode,
          message,
          delivery_id: ship?.delivery_id ?? null,
          expected_version: preview.version,
        });
      }
      setPreview(null);
      requestId.current = newRequestId();
    } catch (caught: unknown) {
      setCommandError(errorText(caught));
      setPreview(null);
    } finally {
      setConfirming(false);
    }
  }

  const sessionIdle = session?.status === "idle";
  // A workflow that declares its own publication also declares its own text.
  // Asking an agent for a draft here would be a turn nobody declared, during
  // a wait whose whole point is that the content stops changing.
  const declaresDelivery = (authority?.declared_actions.length ?? 0) > 0;
  const canDraft =
    !declaresDelivery && !published && !delivering && !unresolved && sessionIdle && !drafting;
  const busy = delivering || confirming || previewing;
  const githubBlocker = preview?.blockers.find(
    (blocker) => blocker.code === "github-unavailable",
  );

  let draftStatus: string;
  if (declaresDelivery) {
    draftStatus =
      "This workflow declares its own publication text. Edit the fields below — what you confirm is what gets published.";
  } else if (draft?.state === "drafting" || drafting) {
    draftStatus = "Drafting… Keep editing; fields you change will not be overwritten.";
  } else if (draft?.state === "interrupted") {
    draftStatus =
      "The daemon restarted while the agent was drafting. Nothing was sent again — retry drafting or write the text yourself.";
  } else if (draft?.state === "failed") {
    draftStatus = `Drafting failed: ${draft.error ?? "unknown reason"}. Correct the text by hand or retry.`;
  } else if (published) {
    draftStatus = "This task is already published; agent drafting is unavailable.";
  } else if (session === undefined || session.status === "failed") {
    draftStatus = "No live primary agent is available. Enter publication text manually.";
  } else if (!sessionIdle) {
    draftStatus = `Drafting is waiting for the primary agent to become idle (currently ${session.status}).`;
  } else if (draft?.state === "ready") {
    draftStatus = "Draft ready. Review and edit every field before delivering.";
  } else {
    draftStatus = "Agent drafting is available; manual entry remains available.";
  }

  return (
    <div className="shipStep" data-testid="ship-step-deliver">
      <div className="stepHeader">
        <StepIcon
          index={2}
          active={!published && !hasSignedResult}
          done={hasSignedResult}
          error={ship?.disposition === "blocked"}
        />
        <span className="stepTitle">Deliver</span>
        {ship?.disposition && (
          <span className={`stepStatusBadge ${ship.disposition}`}>{ship.disposition}</span>
        )}
      </div>

      <p className="draftStatus" data-testid="draft-status">
        {draftStatus}
      </p>

      {authority?.refusal != null && (
        <p className="shipRefusal" data-testid="delivery-refusal">
          {authority.refusal}
        </p>
      )}

      {atApproval ? (
        <fieldset className="endingChooser" data-testid="approval-chooser">
          <legend>{authority?.gate_step ?? "This decision"}</legend>
          <p className="fieldHint">
            Each answer authorizes exactly the actions it names, against the
            content the review read — and nothing further. Answers that publish
            nothing are on the task, beside the question itself.
          </p>
          {grants.length === 0 && (
            <p className="fieldHint" data-testid="approval-no-publishing">
              None of this question&apos;s answers publishes anything.
            </p>
          )}
          {grants.map((choice) => (
            <label className="endingOption" key={choice.id}>
              <input
                type="radio"
                name={`ship-choice-${taskId}`}
                value={choice.id}
                checked={choiceId === choice.id}
                disabled={busy}
                onChange={() => setChoiceId(choice.id)}
                data-testid={`approval-choice-${choice.id}`}
              />
              <span className="endingLabel">{choice.label}</span>
              <span className="endingEffects">
                authorizes {(choice.authorizes ?? []).join(" → ")}
              </span>
            </label>
          ))}
          <label>
            Why (recorded with your decision)
            <textarea
              rows={2}
              value={note}
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
              data-testid="approval-note"
            />
          </label>
        </fieldset>
      ) : authority !== undefined ? (
        <p className="fieldHint" data-testid="delivery-derived">
          {authority.source === "workflow-action"
            ? `Continuing the ${authority.action_kind ?? "authorized"} action this run already authorized. Nothing is repeated on your behalf.`
            : authority.source === "legacy-continuation"
              ? "Finishing an authorization made before workflows owned publication. It cannot be extended."
              : `This run publishes ${
                  authority.declared_actions.length === 0
                    ? "nothing"
                    : authority.declared_actions.join(", ")
                }.`}
        </p>
      ) : (
        <fieldset className="endingChooser" data-testid="ending-chooser">
          <legend>Ending</legend>
          {ENDINGS.map((candidate) => {
            const done = completed.includes(candidate);
            return (
              <label className="endingOption" key={candidate}>
                <input
                  type="radio"
                  name={`ship-ending-${taskId}`}
                  value={candidate}
                  checked={ending === candidate}
                  disabled={busy || done}
                  onChange={() => setEnding(candidate)}
                  data-testid={`ending-${candidate}`}
                />
                <span className="endingLabel">{ENDING_LABELS[candidate]}</span>
                <span className="endingEffects">{ENDING_EFFECTS[candidate]}</span>
                {done && <span className="endingDone">already completed</span>}
              </label>
            );
          })}
        </fieldset>
      )}

      {authority !== undefined ? (
        <p className="fieldHint" data-testid="commit-mode-derived">
          Commits are {ship?.mode ?? preview?.mode ?? "squash"} — the workflow
          says how this delivery composes history.
        </p>
      ) : (
      <div className="commitMode" data-testid="commit-mode">
        <label className="modeOption">
          <input
            type="radio"
            name={`commit-mode-${taskId}`}
            value="squash"
            checked={mode === "squash"}
            disabled={busy || hasSignedResult}
            onChange={() => setMode("squash")}
          />
          Squash
        </label>
        <label className="modeOption">
          <input
            type="radio"
            name={`commit-mode-${taskId}`}
            value="retain"
            checked={mode === "retain"}
            disabled={busy || hasSignedResult}
            onChange={() => setMode("retain")}
          />
          Retain
        </label>
      </div>
      )}

      <div className="commitFields">
        {!hasSignedResult && (
          <label>
            Commit message
            <textarea
              rows={4}
              value={message}
              disabled={busy || mode === "retain"}
              onChange={(e) => {
                setMessage(e.target.value);
                setTouched((current) => ({ ...current, message: true }));
              }}
              data-testid="commit-message"
            />
            {mode === "retain" && (
              <span className="fieldHint" data-testid="retain-message-hint">
                Per-commit messages are retained in this mode.
              </span>
            )}
          </label>
        )}
        {(authority === undefined
          ? ending === "pr"
          : (authority.action_kind === "pr" ||
             (atApproval
               ? grants.some((choice) =>
                   (choice.authorizes ?? []).some((step) => step.includes("pr")),
                 )
               : authority.declared_actions.includes("pr")))) && (
          <>
            <label>
              PR title
              <input
                type="text"
                value={prTitle}
                disabled={busy}
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
                disabled={busy}
                onChange={(e) => {
                  setPrBody(e.target.value);
                  setTouched((current) => ({ ...current, prBody: true }));
                }}
                data-testid="pr-body"
              />
            </label>
          </>
        )}
      </div>

      <div className="commitActions">
        <button
          type="button"
          disabled={!canDraft}
          onClick={() => void onDraft()}
          data-testid="redraft-button"
        >
          {drafting ? "Drafting…" : "Draft via agent"}
        </button>
        <button
          type="button"
          disabled={busy || published}
          onClick={() => void onSaveDraft()}
          data-testid="save-draft-button"
        >
          Save text
        </button>
        <button
          type="button"
          disabled={
            busy || published || unresolved || (atApproval && choiceId === null)
          }
          onClick={() => void onPreview()}
          data-testid="preview-delivery-button"
        >
          {previewing ? "Resolving…" : "Review this delivery"}
        </button>
      </div>

      {commandError && (
        <div className="shipError" data-testid="ship-command-error">
          {commandError}
        </div>
      )}

      {preview && <PreviewPanel preview={preview} onConfirm={() => void onConfirm()} busy={confirming} />}

      <GpgBanner gpg={gpg} />
      {githubBlocker && <GitHubBanner taskId={taskId} detail={githubBlocker.message} />}

      {ship?.disposition === "blocked" && ship.blocked_reason && (
        <div className="shipError" data-testid="ship-error">
          {ship.blocked_reason}
        </div>
      )}
      {review?.status !== "approved" && (
        <p className="stepHint">Delivery needs an approved review of the current content.</p>
      )}
    </div>
  );
}

/** Exactly what a confirmation would permit, and every reason it currently
 * cannot. The confirm button names the effects rather than one of them. */
function PreviewPanel({
  preview,
  onConfirm,
  busy,
}: {
  preview: ShipPreview;
  onConfirm: () => void;
  busy: boolean;
}) {
  const signing = preview.identity.signing;
  const github = preview.identity.github;
  return (
    <div className="deliveryPreview" data-testid="delivery-preview">
      <h3>This confirmation permits</h3>
      {preview.gate_seq !== null && (
        <p className="fieldHint" data-testid="preview-decision">
          Answering attempt {preview.gate_seq} with{" "}
          <code className="mono">{preview.choice_id}</code>, against the review
          recorded at attempt {preview.review_seq ?? "—"}
          {preview.review.question_review_outcome == null
            ? ""
            : ` (${preview.review.question_review_outcome})`}
          .
        </p>
      )}
      <ul data-testid="preview-actions">
        {preview.remaining_actions.map((action) => (
          <li key={action} data-testid={`preview-action-${action}`}>
            {action === "commit"
              ? `Sign ${preview.mode === "retain" ? `${preview.candidate?.commit_count ?? 0} commits` : "one commit"} onto ${preview.candidate?.base_branch}`
              : action === "push"
                ? `Push to ${preview.routing.remote_url} ${preview.routing.ref}`
                : `Open a pull request into ${preview.routing.slug ?? preview.routing.upstream_url} (${preview.routing.base_branch})`}
          </li>
        ))}
        {preview.remaining_actions.length === 0 && <li>Nothing — this ending is complete.</li>}
      </ul>

      <dl className="previewFacts">
        <dt>Reviewed content</dt>
        <dd data-testid="preview-candidate">
          {preview.candidate
            ? `${preview.candidate.candidate_id.slice(0, 12)} · tree ${preview.candidate.tree_id.slice(0, 12)} · base ${preview.candidate.base_commit.slice(0, 12)}`
            : "unavailable"}
        </dd>
        <dt>Mode</dt>
        <dd>{preview.mode === "retain" ? "Retain individual commits" : "Squash"}</dd>
        <dt>Signing identity</dt>
        <dd data-testid="preview-signing">
          {signing ? `${signing.uid} (${signing.fingerprint})` : "not required for this ending"}
        </dd>
        <dt>GitHub account</dt>
        <dd data-testid="preview-github">
          {github
            ? `${github.login ?? "unknown"} at ${github.host} via ${github.credential_source ?? "ambient credentials"}`
            : "not required for this ending"}
        </dd>
        <dt>Git transport identity</dt>
        <dd>{preview.identity.git_transport?.detail}</dd>
      </dl>

      {preview.remaining_actions.includes("pr") && (
        <details className="previewBody" data-testid="preview-pr-body">
          <summary>Pull-request body Ompire will write</summary>
          <pre>{preview.pr_body}</pre>
        </details>
      )}

      {preview.blockers.length > 0 && (
        <ul className="previewBlockers" data-testid="preview-blockers">
          {preview.blockers.map((blocker) => (
            <li key={blocker.code} data-testid={`preview-blocker-${blocker.code}`}>
              {blocker.message}
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        className="signCommitButton"
        disabled={!preview.deliverable || busy}
        onClick={onConfirm}
        data-testid="confirm-delivery-button"
      >
        {busy
          ? "Delivering…"
          : `Confirm: ${confirmationEffects(preview.remaining_actions)}`}
      </button>
    </div>
  );
}

function ResultsStep({ task, ship }: { task: Task; ship: ShipProjection | undefined }) {
  const results = ship?.results ?? {};
  const prUrl = ship?.pr_url ?? task.pr_url;
  const anything = results.commit || results.push || prUrl;

  return (
    <div className="shipStep" data-testid="ship-step-results">
      <div className="stepHeader">
        <StepIcon index={3} active={false} done={Boolean(anything)} error={false} />
        <span className="stepTitle">Delivered</span>
      </div>
      {!anything && (
        <p className="stepHint" data-testid="results-empty">
          Nothing has been delivered yet.
        </p>
      )}
      {results.commit && (
        <p className="stepHint" data-testid="result-commit">
          Signed {results.commit.commit_count === 1 ? "commit" : `${results.commit.commit_count} commits`}{" "}
          at {results.commit.signed_tip.slice(0, 12)}
          {results.commit.installed ? "" : " — retained but not installed in the clone"}.
          {results.commit.note ? ` ${results.commit.note}` : ""}
        </p>
      )}
      {results.push && (
        <p className="stepHint" data-testid="result-push">
          Pushed {results.push.branch} at {results.push.head.slice(0, 12)}
          {results.push.adopted ? " (adopted after an interrupted attempt)" : ""}.
        </p>
      )}
      {prUrl ? (
        <a
          className="reviewReopenLink"
          href={prUrl}
          target="_blank"
          rel="noreferrer"
          data-testid="pr-link"
        >
          {prUrl.replace("https://", "")}
        </a>
      ) : (
        results.commit && (
          <p className="stepHint" data-testid="no-pr-notice">
            No pull request was opened. This delivery is complete as it stands.
          </p>
        )
      )}
      {ship?.legacy_publication && (
        <p className="stepHint" data-testid="legacy-publication-notice">
          This pull request predates Ompire's delivery journal, so there is no recorded
          authorization or action history behind it.
        </p>
      )}
    </div>
  );
}

/** An effect whose outcome Ompire could not establish. Nothing here writes:
 * recheck observes, adopt verifies, retry only unlocks a fresh confirmation
 * once non-execution is proven, and abandon records that it is still unknown. */
function RecoveryPanel({
  task,
  ship,
  action,
}: {
  task: Task;
  ship: ShipProjection;
  action: ShipAction;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");

  async function decide(decision: "recheck" | "adopt" | "retry" | "abandon") {
    if (ship.delivery_id === null) return;
    setBusy(decision);
    setError(null);
    try {
      await shipReconcile(task.id, {
        delivery_id: ship.delivery_id,
        action_id: action.id,
        expected_version: ship.version,
        decision,
        note: note.trim() === "" ? null : note.trim(),
      });
      setNote("");
    } catch (caught: unknown) {
      setError(errorText(caught));
    } finally {
      setBusy(null);
    }
  }

  const expected = (action.expected ?? {}) as Record<string, unknown>;
  const evidence = ((action.progress ?? {}) as Record<string, unknown>).evidence as
    | Record<string, unknown>
    | undefined;
  // What Ompire actually saw, in the operator's terms. A raw dump of the
  // attempt's own intent read back at them is not an observation.
  const observed =
    evidence === undefined
      ? null
      : [
          evidence.state ? `state: ${String(evidence.state)}` : null,
          evidence.observed_head
            ? `destination: ${String(evidence.observed_head).slice(0, 12)}`
            : null,
          evidence.detail ? String(evidence.detail) : null,
        ]
          .filter((part): part is string => part !== null)
          .join(" · ") || null;
  const progress = (action.progress ?? {}) as Record<string, unknown>;
  const signed = Array.isArray(progress.signed) ? progress.signed.length : null;
  return (
    <div className="shipStep stepOpen" data-testid={`ship-recovery-${action.kind}`}>
      <div className="stepHeader">
        <StepIcon index={0} active done={false} error />
        <span className="stepTitle">Unresolved {action.kind}</span>
      </div>
      <p className="stepHint" data-testid="recovery-reason">
        {action.error ?? "The outcome of this action could not be established."}
      </p>
      <dl className="previewFacts">
        <dt>Expected</dt>
        <dd data-testid="recovery-expected">
          {action.kind === "commit"
            ? `signed result under ${String(expected.signed_ref ?? "an unknown ref")}`
            : action.kind === "push"
              ? `${String(expected.ref ?? "")} at ${String(expected.signed_tip ?? "").slice(0, 12)}`
              : `a pull request carrying marker ${String(expected.marker ?? "")}`}
        </dd>
        {observed !== null && (
          <>
            <dt>Observed</dt>
            <dd data-testid="recovery-observed">{observed}</dd>
          </>
        )}
        {signed !== null && (
          <>
            <dt>Signatures produced</dt>
            <dd data-testid="recovery-progress">
              {signed} of {String(progress.planned ?? signed)}
            </dd>
          </>
        )}
      </dl>
      <label className="recoveryNote">
        Note
        <input
          type="text"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          data-testid="recovery-note"
        />
      </label>
      <div className="commitActions">
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => void decide("recheck")}
          data-testid="recovery-recheck"
        >
          Recheck
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => void decide("adopt")}
          data-testid="recovery-adopt"
        >
          Adopt the result
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => void decide("retry")}
          data-testid="recovery-retry"
        >
          Allow a retry
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => void decide("abandon")}
          data-testid="recovery-abandon"
        >
          Abandon
        </button>
      </div>
      {error && (
        <div className="shipError" data-testid="recovery-error">
          {error}
        </div>
      )}
    </div>
  );
}

function CleanupStep({ task, ship }: { task: Task; ship: ShipProjection | undefined }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const archived = task.state === "archived";
  const warning = cleanupWarning(task, ship);
  const unresolved = unresolvedActions(ship).length > 0;
  const prUrl = task.pr_url ?? ship?.pr_url ?? null;
  const delivered = Boolean(ship?.results.commit || ship?.results.push);

  // Cleanup gating: a pull request is still the merge-driven case, and a
  // delivery that ends earlier is complete the moment its ending is reached.
  const ready =
    !archived &&
    !unresolved &&
    (prUrl
      ? task.pr_state === "merged" || task.pr_state === "closed"
      : delivered && ship?.disposition === "completed");

  async function onCleanup() {
    if (!confirmCleanup(task, warning)) return;
    setBusy(true);
    setError(null);
    try {
      await cleanupTask(task.id);
    } catch (caught: unknown) {
      setError(errorText(caught));
    } finally {
      setBusy(false);
    }
  }

  let hint: string;
  if (archived) {
    hint = `Cleaned up ${formatElapsed(task.updated_at)} ago — workshop removed, clone deleted. The delivery record is retained.`;
  } else if (unresolved) {
    hint = "Cleanup is refused while a delivery effect's outcome is unknown.";
  } else if (prUrl) {
    if (task.pr_state === "merged") {
      hint = `Merged ${formatElapsed(task.pr_merged_at ?? task.updated_at)} ago — ready for cleanup.`;
    } else if (task.pr_state === "closed") {
      hint = "PR closed without merging — cleanup is your call.";
    } else {
      hint = "On merge: workshop remove + delete clone. Awaiting merge · cleanup deferred.";
    }
  } else if (delivered) {
    hint = "This delivery ended without a pull request, so nothing is being waited for.";
  } else {
    hint = "Cleanup unlocks once this task has delivered or shipped a PR.";
  }

  return (
    <div
      className={`shipStep ${!delivered && !prUrl && !archived ? "inert" : ""} ${ready ? "stepOpen" : ""}`}
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
      {warning && !archived && !unresolved && (
        <p className="shipError" data-testid="cleanup-warning">
          {warning}
        </p>
      )}
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
      {error && (
        <div className="shipError" data-testid="cleanup-error">
          {error}
        </div>
      )}
    </div>
  );
}

export function ShipFlowView() {
  const { id } = useParams();
  const taskId = id !== undefined && /^\d+$/.test(id) ? Number(id) : null;
  const { snapshotReady, tasks, sessions, workflows, reviews, ships, gpg } = useDaemonState();

  const task = taskId === null ? undefined : tasks.find((candidate) => candidate.id === taskId);
  // Delivery is task-scoped: review and publishing always use the workflow's
  // primary session, never an in-flight step's focused session.
  const taskSessions = taskId === null ? undefined : sessions[taskId];
  const session =
    taskSessions?.[
      primarySessionName(taskSessions, taskId === null ? undefined : workflows[taskId], task)
    ];
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

  const unresolved = unresolvedActions(ship);

  return (
    <>
      <div className="headerRow">
        <h1>
          Ship {task.project_name}/{task.slug}
        </h1>
        <span className="subline">Review → Deliver → Delivered → Cleanup</span>
        <span className="spacer" />
        <Link className="backLink" to={`/tasks/${task.id}`}>
          ← Task
        </Link>
      </div>

      <div className="shipFlow" data-testid="ship-flow">
        <ReviewStep session={session} review={review} ship={ship} />
        {ship !== undefined &&
          unresolved.map((action) => (
            <RecoveryPanel key={action.id} task={task} ship={ship} action={action} />
          ))}
        <DeliverStep
          key={taskId}
          task={task}
          session={session}
          review={review}
          ship={ship}
          gpg={gpg}
        />
        <ResultsStep task={task} ship={ship} />
        <CleanupStep task={task} ship={ship} />
      </div>
    </>
  );
}
