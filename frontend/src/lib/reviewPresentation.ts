import type { ReviewIteration, ReviewState, SessionInfo } from "../types";

export type ReviewDisplayState = "no-review" | "open" | "comments" | "approved" | "aborted" | "error";

/** Whether an approval can still authorize a delivery (ADR-0032).
 *
 * `unbound` is an approval recorded before reviews named the content they
 * graded; `stale` is one whose content has since changed. Both remain visible
 * as history — they simply are not authorization for what the task holds now. */
export type ApprovalBinding = "usable" | "stale" | "unbound";

export interface ReviewPresentation {
  state: ReviewDisplayState;
  label: string;
  hint: string;
  canStart: boolean;
  canCancel: boolean;
  url: string | null;
  /** Set only when the review is approved. */
  approvalBinding: ApprovalBinding | null;
  iterations: ReviewIteration[];
}

export function latestReviewIteration(review: ReviewState | undefined): ReviewIteration | undefined {
  return review?.iterations.at(-1);
}

/** The shared task-scoped review display contract. `comments` is a completed
 * reviewer process whose feedback remains in the one review history; it is
 * therefore eligible to start again once the primary agent returns to idle. */
export function projectReview(
  review: ReviewState | undefined,
  primarySession: SessionInfo | undefined,
  approvalBinding: ApprovalBinding = "usable",
): ReviewPresentation {
  const latest = latestReviewIteration(review);
  const canStart =
    primarySession?.status === "idle" &&
    (review === undefined || review.status !== "open" || latest?.outcome === "comments");

  if (!review) {
    const hint =
      primarySession === undefined
        ? "Review is unavailable until the primary session has started."
        : primarySession.status === "failed"
          ? "Review is unavailable because the primary agent is no longer live."
          : primarySession.status === "idle"
            ? "The primary agent is idle and ready for independent review."
            : "Review starts when the primary session is idle.";
    return {
      state: "no-review",
      label: "No review started",
      hint,
      canStart,
      canCancel: false,
      url: null,
      approvalBinding: null,
      iterations: [],
    };
  }

  if (review.status === "approved") {
    const hint =
      approvalBinding === "stale"
        ? "The task content changed after this approval. It stays on record; delivering the current content needs a fresh review."
        : approvalBinding === "unbound"
          ? "This approval predates content-bound review, so it does not identify what was approved. It stays on record; delivering needs a fresh review."
          : "Independent review approved this task's current content.";
    return {
      state: "approved",
      label: approvalBinding === "usable" ? "Approved" : "Approved (superseded)",
      hint,
      canStart,
      canCancel: false,
      url: review.url,
      approvalBinding,
      iterations: review.iterations,
    };
  }
  if (review.status === "aborted") {
    return {
      state: "aborted",
      label: "Aborted",
      hint:
        latest?.outcome === "interrupted"
          ? "A daemon restart interrupted the reviewer. Start another review when the primary session is idle."
          : "The last review was aborted. Start another review when the primary session is idle.",
      canStart,
      canCancel: false,
      url: review.url,
      approvalBinding: null,
      iterations: review.iterations,
    };
  }
  if (review.status === "error") {
    return {
      state: "error",
      label: "Error",
      hint: "The last review failed. Inspect its error details, then retry when the primary session is idle.",
      canStart,
      canCancel: false,
      url: review.url,
      approvalBinding: null,
      iterations: review.iterations,
    };
  }

  if (latest?.outcome === "comments" && primarySession?.status !== "reviewing") {
    const hint =
      primarySession?.status === "idle"
        ? "Review comments were returned and the primary agent is ready for another review."
        : "The primary agent is addressing review comments.";
    return {
      state: "comments",
      label: "Comments submitted",
      hint,
      canStart,
      canCancel: false,
      url: review.url,
      approvalBinding: null,
      iterations: review.iterations,
    };
  }

  return {
    state: "open",
    label: "Review open",
    hint: "Independent review is running. Open llmvet to inspect it or cancel the review.",
    canStart: false,
    canCancel: true,
    url: review.url,
    approvalBinding: null,
    iterations: review.iterations,
  };
}

export function formatReviewOutcome(outcome: ReviewIteration["outcome"]): string {
  if (outcome === "comments") return "Comments submitted";
  if (outcome === "interrupted") return "Interrupted by daemon restart";
  return outcome[0].toUpperCase() + outcome.slice(1);
}

export function formatReviewCommentCount(iteration: ReviewIteration): string | null {
  if (iteration.outcome !== "comments") return null;
  if (iteration.comment_count === null) return "Comments submitted";
  return `${iteration.comment_count} ${iteration.comment_count === 1 ? "comment" : "comments"}`;
}

export function formatReviewRecordedAt(recordedAt: string): string {
  const timestamp = Date.parse(recordedAt);
  return Number.isNaN(timestamp) ? recordedAt : new Date(timestamp).toLocaleString();
}
