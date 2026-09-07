import { useEffect, useState, type ReactNode } from "react";
import { getWorkflowRevision } from "../lib/api";
import { WorkflowFlow, type FlowStepExtras } from "./workflow/WorkflowFlow";
import type { DraftObject } from "../lib/workflowDocument";
import type { WorkflowRevisionDetail } from "../types";

/** The exact definition identified by a revision, read the same way
 * everywhere.
 *
 * A revision is a content identity, so this is the only honest way to answer
 * "what procedure is this" once the installed definition may have moved on —
 * before a launch, it is what the task *would* pin; after acceptance, it is
 * what the task runs (ADR-0028). Addressed by revision and never by name, so
 * a library edit cannot relabel an old flow as the current one.
 *
 * The document is fetched when the reader opens it rather than carried on
 * every payload: it is large, unchanging, and rarely looked at.
 */
export function WorkflowRevision({
  revision,
  legacyThroughSeq,
  interruptedLegacySeq,
  extras,
  current,
  defaultOpen = false,
  summaryLabel = "read the definition",
  header,
}: {
  revision: string;
  legacyThroughSeq?: number;
  interruptedLegacySeq?: number | null;
  /** Per-step additions — task detail overlays its recorded attempts here. */
  extras?: (index: number, name: string | null, step: DraftObject) => FlowStepExtras;
  /** The step the run is on, highlighted in the overview. */
  current?: string | null;
  defaultOpen?: boolean;
  summaryLabel?: string;
  header?: ReactNode;
}) {
  const [definition, setDefinition] = useState<DraftObject | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(defaultOpen);

  useEffect(() => {
    if (!open) return;
    let live = true;
    // Every fetch is scoped to the revision that asked for it, so a slow
    // answer for the previous one cannot arrive and be presented as this one.
    setDefinition(null);
    setError(null);
    getWorkflowRevision(revision)
      .then((loaded: WorkflowRevisionDetail) => {
        if (live) setDefinition(loaded.definition);
      })
      .catch((err: unknown) => {
        if (live) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      live = false;
    };
  }, [open, revision]);

  return (
    <div data-testid="workflow-revision">
      <code className="mono revisionId">{revision}</code>
      {header}
      {legacyThroughSeq !== undefined && legacyThroughSeq > 0 && (
        <p className="fieldHint" data-testid="legacy-boundary">
          Steps 1&ndash;{legacyThroughSeq} ran before this definition was pinned, under a
          procedure that was never recorded.
          {interruptedLegacySeq !== null && interruptedLegacySeq !== undefined
            ? ` Step ${interruptedLegacySeq} spans the boundary: it began before the confirmation and finished after it.`
            : ""}
        </p>
      )}
      <details
        open={open}
        onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
        data-testid="workflow-definition"
      >
        <summary>{summaryLabel}</summary>
        {error !== null ? (
          <p className="fieldHint" data-testid="workflow-definition-error">
            {error}
          </p>
        ) : definition === null ? (
          <p className="fieldHint">Loading&hellip;</p>
        ) : (
          <WorkflowFlow
            definition={definition}
            extras={extras}
            current={current}
            testId="workflow-revision-flow"
          />
        )}
      </details>
    </div>
  );
}
