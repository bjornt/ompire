import type { RetainedResultCounts, Task } from "../types";

/** The shared destructive-cleanup confirmation (tasks capability: "Cleanup
 * requires confirmation in the UI"): names the clone path and, when
 * recorded, the workshop container, plus any delivery-specific data-loss
 * warning. Used by both the task card and the Ship Flow Cleanup step so there
 * is exactly one wording. */
export function confirmCleanup(
  task: Task,
  warning?: string | null,
  retained?: RetainedResultCounts | null,
): boolean {
  const workshopLine = task.workshop_id
    ? `\n…and removes the workshop container:\n${task.workshop_id}`
    : "";
  // A delivery that never pushed has its only Ompire-managed copy in this
  // clone, so the confirmation has to say what is about to be lost (ADR-0032).
  const warningLine = warning ? `\n\n${warning}` : "";
  // Two different facts, stated separately because they point opposite ways
  // (ADR-0034): everything in the workspace that was never captured is about
  // to be lost, and everything that *was* captured is not. Cleanup never
  // purges a result, and an operator who has not captured yet should find that
  // out here rather than afterwards.
  const uncapturedLine =
    "\n\nAny workspace edits you have not captured as a result will be lost.";
  const retainedLine =
    retained && retained.retained > 0
      ? `\n${retained.retained} captured result revision(s)` +
        (retained.accepted > 0 ? `, ${retained.accepted} accepted,` : "") +
        " are retained and stay readable after cleanup."
      : "";
  return window.confirm(
    `Clean up ${task.project_name}/${task.slug}?\n\nThis deletes the clone directory:\n${task.clone_path}${workshopLine}${uncapturedLine}${retainedLine}${warningLine}`,
  );
}

/** `<repo>#<number>` from a GitHub PR URL for compact display; null when the
 * URL doesn't match the expected shape (callers fall back to the raw URL). */
export function prLinkLabel(prUrl: string): string | null {
  const match = /github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/.exec(prUrl);
  if (!match) return null;
  return `${match[2]}#${match[3]}`;
}
