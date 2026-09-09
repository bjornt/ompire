import type {
  ExportFileOutcome,
  TaskResult,
  TaskResultExport,
  TaskResultExportState,
} from "../types";

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

/** Presentation rules for checkout export (ADR-0036).
 *
 * The labels do the work an export's honesty depends on: "Not fully installed"
 * and "Outcome unknown" are the states an operator has to be able to tell
 * apart, and neither may read as a success. */

export const EXPORT_STATE_LABEL: Record<TaskResultExportState, string> = {
  running: "Exporting…",
  completed: "Exported",
  // Not "Failed": some approved files may well have been installed, and the
  // per-destination outcomes are what say which.
  incomplete: "Not fully installed",
  unresolved: "Outcome unknown",
};

export const EXPORT_OUTCOME_LABEL: Record<ExportFileOutcome, string> = {
  pending: "Not started",
  created: "Created",
  // The file already held exactly this content, so it was left untouched —
  // including its permissions and its modification time.
  "already-identical": "Already identical",
  "not-installed": "Not installed",
  unknown: "Unknown",
};

/** A one-line count of what an export actually did. */
export function exportSummary(record: TaskResultExport): string {
  const parts: string[] = [];
  if (record.created_count > 0) parts.push(`${record.created_count} created`);
  if (record.identical_count > 0) {
    parts.push(`${record.identical_count} already identical`);
  }
  if (record.incomplete_count > 0) {
    parts.push(`${record.incomplete_count} not installed`);
  }
  if (record.unknown_count > 0) parts.push(`${record.unknown_count} unknown`);
  return parts.length > 0 ? parts.join(", ") : "no files";
}

export function shortExportId(id: string): string {
  return id.startsWith("exp_") ? id.slice(4, 12) : id.slice(0, 8);
}
