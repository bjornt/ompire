import type { Task } from "../types";

/** The shared destructive-cleanup confirmation (tasks capability: "Cleanup
 * requires confirmation in the UI"): names the clone path and, when
 * recorded, the workshop container. Used by both the task card and the Ship
 * Flow Cleanup step so there is exactly one wording. */
export function confirmCleanup(task: Task): boolean {
  const workshopLine = task.workshop_id
    ? `\n…and removes the workshop container:\n${task.workshop_id}`
    : "";
  return window.confirm(
    `Clean up ${task.project_name}/${task.slug}?\n\nThis deletes the clone directory:\n${task.clone_path}${workshopLine}`,
  );
}

/** `<repo>#<number>` from a GitHub PR URL for compact display; null when the
 * URL doesn't match the expected shape (callers fall back to the raw URL). */
export function prLinkLabel(prUrl: string): string | null {
  const match = /github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/.exec(prUrl);
  if (!match) return null;
  return `${match[2]}#${match[3]}`;
}
