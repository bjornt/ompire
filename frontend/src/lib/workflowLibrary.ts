import type { WorkflowLibraryEntry } from "../types";

/** The 1 MiB UTF-8 limit the daemon applies to a workflow document. Checked
 * here too so an oversized import fails at the file picker, where the operator
 * can see which file it was, rather than on a save. */
export const MAX_WORKFLOW_BYTES = 1024 * 1024;

/** Read a local YAML file into the editor.
 *
 * Local, and only local: the daemon never receives a path and never opens one.
 * What crosses the wire is the text, through the same draft and validate
 * operations typing it by hand would use.
 */
export async function readWorkflowFile(file: File): Promise<string> {
  if (file.size > MAX_WORKFLOW_BYTES) {
    throw new Error(
      `${file.name} is ${file.size} bytes; a workflow document is limited to ${MAX_WORKFLOW_BYTES}.`,
    );
  }
  const text = await file.text();
  if (new TextEncoder().encode(text).length > MAX_WORKFLOW_BYTES) {
    throw new Error(`${file.name} is larger than ${MAX_WORKFLOW_BYTES} bytes of UTF-8.`);
  }
  return text;
}

/** What an entry's state is, in one phrase.
 *
 * Deliberately distinguishes "you have not saved a revision yet" from "the
 * revision you saved cannot be read": they are different problems with
 * different fixes, and a single "unavailable" would make the operator guess. */
export function workflowStateLabel(entry: WorkflowLibraryEntry): {
  label: string;
  tone: "ready" | "draft" | "archived" | "broken";
} {
  if (entry.archived) return { label: "archived", tone: "archived" };
  if (entry.available) return { label: "launchable", tone: "ready" };
  if (entry.unavailable_reason === "draft_only") return { label: "draft only", tone: "draft" };
  if (entry.unavailable_reason === "not_packaged") {
    return { label: "not in this package", tone: "broken" };
  }
  return { label: `unavailable · ${entry.unavailable_reason ?? "unknown"}`, tone: "broken" };
}
