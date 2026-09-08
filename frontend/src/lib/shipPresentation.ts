import type { ApprovalBinding } from "./reviewPresentation";
import type {
  ReviewState,
  ShipAction,
  ShipActionKind,
  ShipEnding,
  ShipProjection,
  Task,
} from "../types";

export type ShipFlowStage =
  /** The run is at its approval: the work is reviewed, and a person decides
   * what happens to it. Distinct from `deliver`, which is an authorization
   * already given and now being carried out. */
  | "approval"
  /** A named ending with no publication in it. Complete work, not a stalled
   * delivery — an author-chosen result, and the Ship index says so. */
  | "no-publication"
  | "review"
  | "draft"
  | "deliver"
  | "unresolved"
  | "delivered-commit"
  | "delivered-push"
  | "wait-merge"
  | "cleanup"
  | "cleanup-complete";

export type ShipFlowActivity =
  | "ready"
  | "in-progress"
  | "error"
  | "unresolved"
  | "complete";
export type ShipIndexSection = "active" | "recent";

export interface ShipFlowPresentation {
  stage: ShipFlowStage;
  label: string;
  detail: string;
  activity: ShipFlowActivity;
  error: string | null;
}

export interface ShipIndexEntry extends ShipFlowPresentation {
  task: Task;
  section: ShipIndexSection;
}

const STAGE_LABELS: Record<ShipFlowStage, string> = {
  review: "Review",
  approval: "Waiting for your decision",
  "no-publication": "Finished without publishing",
  draft: "Draft",
  deliver: "Deliver",
  unresolved: "Needs a decision",
  "delivered-commit": "Signed locally",
  "delivered-push": "Pushed",
  "wait-merge": "Wait for merge",
  cleanup: "Cleanup",
  "cleanup-complete": "Cleanup complete",
};

export const ENDING_LABELS: Record<ShipEnding, string> = {
  commit: "Local signed commit",
  push: "Pushed branch",
  pr: "Pull request",
};

/** What each ending actually does, named in full.
 *
 * The confirmation used to say "Sign & commit" and perform three effects; an
 * ending has to name every one it permits (ADR-0032). */
export const ENDING_EFFECTS: Record<ShipEnding, string> = {
  commit: "Sign locally and stop. Nothing is pushed and no pull request is opened.",
  push: "Sign, then push to the task's accepted destination. No pull request is opened.",
  pr: "Sign, push, and open a pull request.",
};

const ACTION_VERBS: Record<ShipActionKind, string> = {
  commit: "sign",
  push: "push",
  pr: "open a pull request",
};

/** What *this* confirmation permits, which is the remaining actions rather than
 * the ending in the abstract.
 *
 * A continuation that says "Sign, then push" when the signature already exists
 * describes an effect it will not perform — the same overstatement the single
 * "Sign & commit" button made, pointed the other way. */
export function confirmationEffects(remaining: ShipActionKind[]): string {
  const verbs = remaining.map((kind) => ACTION_VERBS[kind]);
  if (verbs.length === 0) return "nothing — this ending is already complete";
  if (verbs.length === 1) return verbs[0];
  return `${verbs.slice(0, -1).join(", ")} and then ${verbs[verbs.length - 1]}`;
}

/** The action attempts whose outcome Ompire could not establish. Nothing
 * dependent may run and cleanup is refused while any of these stand. */
export function unresolvedActions(ship: ShipProjection | undefined): ShipAction[] {
  return (ship?.actions ?? []).filter(
    (action) => action.phase === "needs_reconciliation" || action.phase === "executing",
  );
}

export function isDelivering(ship: ShipProjection | undefined): boolean {
  if (ship === undefined) return false;
  if (ship.draft?.state === "drafting") return true;
  return ship.actions.some((action) => action.phase === "executing");
}

/** A task belongs in publishing navigation once the daemon has observed an
 * approval, a delivery record, or a pull request. Presentation-only: it does
 * not assert that any command is currently permissible. */
export function hasShipFlowHandoff(
  task: Task,
  review: ReviewState | undefined,
  ship: ShipProjection | undefined,
): boolean {
  return (
    task.pr_url !== null ||
    review?.status === "approved" ||
    (ship !== undefined && (ship.delivery_id !== null || ship.legacy_publication))
  );
}

/** Whether this review's approval can still authorize a delivery.
 *
 * An approval recorded before content binding names no candidate; it stays
 * visible as history and cannot deliver. An approval whose candidate is not the
 * one the current delivery names is stale for the same reason: what it graded
 * is not what would be published.
 *
 * This is a display hint only. The daemon re-resolves the binding against the
 * live workspace when a delivery is previewed and confirmed, which is the
 * check that actually refuses. */
export function approvalBindingFor(
  review: ReviewState | undefined,
  ship: ShipProjection | undefined,
): ApprovalBinding {
  if (review?.status !== "approved") return "usable";
  const approved = approvedCandidateId(review);
  if (approved === null) return "unbound";
  if (ship?.review_candidate_id != null && ship.review_candidate_id !== approved) {
    return "stale";
  }
  if (ship?.candidate_id != null && ship.candidate_id !== approved) return "stale";
  return "usable";
}

export function approvedCandidateId(review: ReviewState | undefined): string | null {
  if (review?.status !== "approved") return null;
  for (let i = review.iterations.length - 1; i >= 0; i -= 1) {
    const iteration = review.iterations[i];
    if (iteration.outcome === "approved") return iteration.candidate_id ?? null;
  }
  return null;
}

function stageFor(
  task: Task,
  review: ReviewState | undefined,
  ship: ShipProjection | undefined,
): ShipFlowStage {
  if (unresolvedActions(ship).length > 0) return "unresolved";
  const authority = ship?.authority;

  const prUrl = task.pr_url ?? ship?.pr_url ?? null;
  if (prUrl) {
    if (task.state === "archived") return "cleanup-complete";
    if (task.pr_state === "merged" || task.pr_state === "closed") return "cleanup";
    return "wait-merge";
  }

  if (task.state === "archived") return "cleanup-complete";

  // A delivery that reached its selected ending is finished, even though no
  // pull request exists. That is the whole point of a selectable ending.
  if (ship?.disposition === "completed") {
    if (ship.results.push) return "delivered-push";
    if (ship.results.commit) return "delivered-commit";
  }
  if (ship?.results.push) return "delivered-push";
  if (ship?.results.commit) return "delivered-commit";

  if (ship?.disposition === "authorized" || ship?.disposition === "blocked") {
    return "deliver";
  }
  // Nothing has been published, and the run is asking. This is the one stage
  // where the next move is a person's, so it is named for the decision rather
  // than for a drafting step nobody asked for.
  if (authority?.source === "workflow-gate") return "approval";
  // A workflow that declares no publication is finished when its run is, and
  // that is a real ending rather than a delivery nobody got round to.
  if (
    authority !== undefined &&
    authority.declared_actions.length === 0 &&
    authority.format !== null &&
    (task.workflow_status === "complete" || task.workflow_status === "failed")
  ) {
    return "no-publication";
  }
  if (ship?.draft != null) return "draft";
  return review?.status === "approved" ? "draft" : "review";
}

function detailFor(
  stage: ShipFlowStage,
  task: Task,
  ship: ShipProjection | undefined,
  error: string | null,
): string {
  if (stage === "unresolved") {
    const kinds = unresolvedActions(ship)
      .map((action) => action.kind)
      .join(", ");
    return `Ompire could not confirm what happened (${kinds}). Recheck, adopt, or abandon it before anything else runs.`;
  }
  if (isDelivering(ship)) {
    const running = ship?.actions.find((a) => a.phase === "executing");
    if (running) {
      return running.kind === "commit"
        ? "Creating the signed commit…"
        : running.kind === "push"
          ? "Pushing the signed branch…"
          : "Opening the pull request…";
    }
    return "Drafting publication text…";
  }
  if (error !== null) return `Delivery stopped: ${error}`;

  switch (stage) {
    case "approval": {
      const grants = (ship?.authority?.choices ?? []).filter(
        (choice) => choice.authorizes !== null,
      );
      return grants.length === 0
        ? "This run is waiting for your decision. None of its answers publishes anything."
        : `This run is waiting for your decision. Publishing answers: ${grants
            .map((choice) => choice.label)
            .join("; ")}.`;
    }
    case "no-publication":
      return "This workflow declares no publication. Its work is complete and nothing was published — which is the ending it was written to reach.";
    case "review":
      return "An approved review of the current content is required before delivering.";
    case "draft":
      return "Prepare the publication text and choose how far this delivery goes.";
    case "deliver":
      return "Confirm the ending to deliver the reviewed content.";
    case "delivered-commit":
      return "Signed locally. Nothing was pushed; a push or pull request can still be authorized.";
    case "delivered-push":
      return "Pushed to the accepted destination. No pull request was opened.";
    case "wait-merge":
      return "Pull request is awaiting resolution.";
    case "cleanup":
      return "Pull request is resolved; workspace cleanup is available.";
    case "cleanup-complete":
      return task.pr_url
        ? "Workspace cleanup is complete."
        : "Workspace cleanup is complete; the delivery record is retained.";
  }
}

/** Converts the daemon's task, review, and delivery projections into one stable
 * row presentation. A completed local or pushed delivery is a success, not an
 * incomplete pull request. */
export function presentShipFlow(
  task: Task,
  review: ReviewState | undefined,
  ship: ShipProjection | undefined,
): ShipFlowPresentation {
  const stage = stageFor(task, review, ship);
  const error =
    stage === "unresolved"
      ? (ship?.blocked_reason ?? "an effect's outcome is unknown")
      : ship?.disposition === "blocked"
        ? (ship.blocked_reason ?? "delivery is blocked")
        : null;
  const activity: ShipFlowActivity =
    stage === "unresolved"
      ? "unresolved"
      : stage === "cleanup-complete"
        ? "complete"
        : isDelivering(ship)
          ? "in-progress"
          : error !== null
            ? "error"
            : stage === "delivered-commit" || stage === "delivered-push"
              ? "complete"
              : "ready";

  return {
    stage,
    label: STAGE_LABELS[stage],
    detail: detailFor(stage, task, ship, error),
    activity,
    error,
  };
}

/** Builds the `/ship` chooser groups. Non-archived handoffs are actionable or
 * resumable; archived records remain as completed history. */
export function buildShipIndex(
  tasks: Task[],
  reviews: Record<number, ReviewState>,
  ships: Record<number, ShipProjection>,
): { active: ShipIndexEntry[]; recent: ShipIndexEntry[] } {
  const active: ShipIndexEntry[] = [];
  const recent: ShipIndexEntry[] = [];

  for (const task of tasks) {
    const review = reviews[task.id];
    const ship = ships[task.id];
    if (!hasShipFlowHandoff(task, review, ship)) continue;
    const presentation = presentShipFlow(task, review, ship);

    if (task.state !== "archived") {
      active.push({ task, section: "active", ...presentation });
    } else {
      recent.push({ task, section: "recent", ...presentation });
    }
  }

  const byUpdatedAt = (a: ShipIndexEntry, b: ShipIndexEntry) =>
    b.task.updated_at.localeCompare(a.task.updated_at) || b.task.id - a.task.id;
  active.sort(byUpdatedAt);
  recent.sort(byUpdatedAt);

  return { active, recent };
}

/** What cleanup is about to remove, in the operator's terms.
 *
 * A local-only result lives in the clone alone: removing it removes the only
 * Ompire-managed copy of the signed commit. The delivery record survives, and
 * a record is not a backup. */
export function cleanupWarning(
  task: Task,
  ship: ShipProjection | undefined,
): string | null {
  if (task.state === "archived") return null;
  if (unresolvedActions(ship).length > 0) {
    return "Cleanup is refused while a delivery effect's outcome is unknown. Resolve it first.";
  }
  const prUrl = task.pr_url ?? ship?.pr_url ?? null;
  if (prUrl) return null;
  if (ship?.results.push) {
    return `This task was pushed to ${ship.results.push.branch} and has no pull request. Cleanup removes the workspace; the remote branch is left alone.`;
  }
  if (ship?.results.commit) {
    return "This task was signed locally and never pushed. Removing the clone removes the only Ompire-managed Git copy of that commit — the delivery record is retained, but it is not a backup of the commit itself.";
  }
  return null;
}
