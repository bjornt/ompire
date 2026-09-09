import type { TaskResult } from "../types";

/** Presentation rules for durable task results (ADR-0034).
 *
 * Kept out of the panel for the same reason `reviewPresentation` is: what a
 * revision's state *means* to an operator is a rule worth testing on its own,
 * separately from how it is laid out. */

/** The one word a revision is described by.
 *
 * `unavailable` is derived rather than stored, because a revision can be
 * `ready` and still unreadable. Collapsing the two would either hide the
 * damage or lose the fact that the capture itself succeeded — and the
 * acceptance that may have followed it. */
export type RevisionStatus =
  | "capturing"
  | "failed"
  | "unavailable"
  | "accepted"
  | "complete"
  | "purged";

export function revisionStatus(result: TaskResult): RevisionStatus {
  if (result.state === "capturing") return "capturing";
  if (result.state === "failed") return "failed";
  if (result.state === "purged") return "purged";
  if (!result.available) return "unavailable";
  return result.accepted_at !== null ? "accepted" : "complete";
}

export const REVISION_STATUS_LABEL: Record<RevisionStatus, string> = {
  capturing: "Capturing…",
  failed: "Failed",
  unavailable: "Unavailable",
  // "Not accepted" rather than "Pending": a complete revision nobody accepted
  // is a finished, downloadable result, not one still waiting on the daemon.
  complete: "Not accepted",
  accepted: "Accepted",
  purged: "Purged",
};

export function formatResultBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
}

/** A short, stable handle for a capture id, for labels and confirmations.
 * The full id stays available wherever it is acted on. */
export function shortResultId(id: string): string {
  return id.startsWith("res_") ? id.slice(4, 12) : id.slice(0, 8);
}
