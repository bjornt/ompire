import { useEffect, useState } from "react";
import { getWorkflowRevision } from "../lib/api";
import type { WorkflowRevisionDetail } from "../types";

/** The exact definition identified by a revision, readable on demand.
 *
 * A revision is a content identity, so this is the only honest way to answer
 * "what procedure is this" once the installed definition may have moved on —
 * before a launch, it is what the task *would* pin; after acceptance, it is
 * what the task runs (ADR-0028).
 *
 * The document is fetched when the reader opens it rather than carried on
 * every payload: it is large, unchanging, and rarely looked at.
 */
export function WorkflowRevision({
  revision,
  legacyThroughSeq,
  interruptedLegacySeq,
}: {
  revision: string;
  legacyThroughSeq?: number;
  interruptedLegacySeq?: number | null;
}) {
  const [definition, setDefinition] = useState<WorkflowRevisionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open || definition !== null) return;
    let live = true;
    getWorkflowRevision(revision)
      .then((loaded) => {
        if (live) setDefinition(loaded);
      })
      .catch((err: unknown) => {
        if (live) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      live = false;
    };
  }, [open, definition, revision]);

  return (
    <div data-testid="workflow-revision">
      <code className="mono revisionId">{revision}</code>
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
        <summary>read the definition</summary>
        {error !== null ? (
          <p className="fieldHint" data-testid="workflow-definition-error">
            {error}
          </p>
        ) : definition === null ? (
          <p className="fieldHint">Loading&hellip;</p>
        ) : (
          <pre className="definitionBlock">
            {JSON.stringify(definition.definition, null, 2)}
          </pre>
        )}
      </details>
    </div>
  );
}
