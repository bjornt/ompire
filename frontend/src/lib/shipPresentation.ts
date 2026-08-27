import type { ReviewState, ShipState, Task } from "../types";

export type ShipFlowStage =
  | "review"
  | "draft"
  | "sign"
  | "push-pr"
  | "wait-merge"
  | "cleanup"
  | "cleanup-complete";

export type ShipFlowActivity = "ready" | "in-progress" | "error" | "complete";
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
  draft: "Draft",
  sign: "Sign",
  "push-pr": "Push / PR",
  "wait-merge": "Wait for merge",
  cleanup: "Cleanup",
  "cleanup-complete": "Cleanup complete",
};

/** A task belongs in publishing navigation once the daemon has observed an
 * approval, a ship attempt, or a pull request. This is a presentation-only
 * predicate: it does not assert that any command is currently permissible. */
export function hasShipFlowHandoff(
  task: Task,
  review: ReviewState | undefined,
  ship: ShipState | undefined,
): boolean {
  return task.pr_url !== null || review?.status === "approved" || ship !== undefined;
}

function stageFor(task: Task, review: ReviewState | undefined, ship: ShipState | undefined): ShipFlowStage {
  const prUrl = task.pr_url ?? ship?.pr_url;
  if (prUrl) {
    if (task.state === "archived") return "cleanup-complete";
    if (task.pr_state === "merged" || task.pr_state === "closed") return "cleanup";
    return "wait-merge";
  }

  if (ship?.commit_sha !== null && ship?.commit_sha !== undefined) return "push-pr";
  if (ship?.status === "pushing" || ship?.status === "shipped") return "push-pr";
  if (
    ship?.status === "committing" ||
    ship?.status === "drafted" ||
    (ship?.draft !== null && ship?.draft !== undefined)
  ) {
    return "sign";
  }
  if (ship?.status === "drafting") return "draft";

  if (ship?.status === "error") {
    if (ship.lastStep?.step === "push" || ship.lastStep?.step === "pr") return "push-pr";
    if (ship.lastStep?.step === "commit") return "sign";
    return review?.status === "approved" ? "draft" : "review";
  }

  return review?.status === "approved" ? "draft" : "review";
}

function detailFor(stage: ShipFlowStage, ship: ShipState | undefined, error: string | null): string {
  if (error !== null) return `Last ship attempt failed. Retry ${STAGE_LABELS[stage]} in task Ship flow.`;
  if (ship?.status === "drafting") return "Drafting publication metadata…";
  if (ship?.status === "committing") return "Creating the signed commit…";
  if (ship?.status === "pushing") return "Pushing the branch and opening the pull request…";

  switch (stage) {
    case "review":
      return "Review is required before publishing.";
    case "draft":
      return "Prepare commit and pull-request text.";
    case "sign":
      return "Create the signed publication commit.";
    case "push-pr":
      return "Publish the signed commit and open the pull request.";
    case "wait-merge":
      return "Pull request is awaiting resolution.";
    case "cleanup":
      return "Pull request is resolved; workspace cleanup is available.";
    case "cleanup-complete":
      return "Workspace cleanup is complete.";
  }
}

/** Converts the daemon's task, review, ship, and PR projections into one
 * stable row presentation. PR state outranks ship progress because it is the
 * furthest observed publishing milestone. */
export function presentShipFlow(
  task: Task,
  review: ReviewState | undefined,
  ship: ShipState | undefined,
): ShipFlowPresentation {
  const stage = stageFor(task, review, ship);
  const prUrl = task.pr_url ?? ship?.pr_url;
  const error =
    ship?.status === "error" && (prUrl === null || prUrl === undefined) ? ship.error : null;
  const activity: ShipFlowActivity =
    stage === "cleanup-complete"
      ? "complete"
      : error !== null
        ? "error"
        : ship?.status === "drafting" || ship?.status === "committing" || ship?.status === "pushing"
          ? "in-progress"
          : "ready";

  return {
    stage,
    label: STAGE_LABELS[stage],
    detail: detailFor(stage, ship, error),
    activity,
    error,
  };
}

/** Builds the `/ship` chooser groups without changing the daemon-owned ship
 * workflow. Non-archived handoffs are actionable/resumable; archived PR
 * records remain as completed history. */
export function buildShipIndex(
  tasks: Task[],
  reviews: Record<number, ReviewState>,
  ships: Record<number, ShipState>,
): { active: ShipIndexEntry[]; recent: ShipIndexEntry[] } {
  const active: ShipIndexEntry[] = [];
  const recent: ShipIndexEntry[] = [];

  for (const task of tasks) {
    const review = reviews[task.id];
    const ship = ships[task.id];
    const presentation = presentShipFlow(task, review, ship);

    if (task.state !== "archived" && hasShipFlowHandoff(task, review, ship)) {
      active.push({ task, section: "active", ...presentation });
    } else if (task.pr_url !== null || (ship?.pr_url !== null && ship?.pr_url !== undefined)) {
      recent.push({ task, section: "recent", ...presentation });
    }
  }

  const byUpdatedAt = (a: ShipIndexEntry, b: ShipIndexEntry) =>
    b.task.updated_at.localeCompare(a.task.updated_at) || b.task.id - a.task.id;
  active.sort(byUpdatedAt);
  recent.sort(byUpdatedAt);

  return { active, recent };
}
