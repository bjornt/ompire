import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { createWorkflowEntry } from "../lib/api";
import { useDaemonReconcile, useDaemonState } from "../lib/useDaemonState";
import type { WorkflowLibraryEntry } from "../types";
import { readWorkflowFile, workflowStateLabel } from "../lib/workflowLibrary";
import "./WorkflowsView.css";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** What a new entry is created from. All three produce a draft — none of them
 * validates anything or makes the entry launchable. */
type CreateSource =
  | { kind: "starter" }
  | { kind: "imported"; yaml: string; filename: string | null }
  | { kind: "duplicate"; revision: string; of: string };

function EntryCard({ entry }: { entry: WorkflowLibraryEntry }) {
  const state = workflowStateLabel(entry);
  return (
    <li className="workflowCard" data-testid={`workflow-card-${entry.name}`}>
      <div className="workflowCardHead">
        <Link className="workflowName" to={`/workflows/${encodeURIComponent(entry.name)}`}>
          {entry.name}
        </Link>
        <span className={`originChip origin-${entry.origin}`}>{entry.origin}</span>
        <span className={`stateChip state-${state.tone}`} data-testid={`workflow-state-${entry.name}`}>
          {state.label}
        </span>
      </div>
      <p className="workflowMeta">
        {entry.current_revision === null ? (
          <span className="workflowMuted">no executable revision saved</span>
        ) : (
          <>
            format {entry.current_format} ·{" "}
            <code className="mono workflowRevision">{entry.current_revision}</code>
          </>
        )}
      </p>
      {entry.unavailable_detail !== null && (
        <p className="workflowUnavailable">{entry.unavailable_detail}</p>
      )}
    </li>
  );
}

/** Name a new workflow before anything is created.
 *
 * The name is the entry's identity and cannot be changed afterwards, so it is
 * asked for up front rather than derived from a file name or a source
 * workflow — renaming later means creating a separate entry.
 */
function CreateForm({
  source,
  onCancel,
}: {
  source: CreateSource;
  onCancel: () => void;
}) {
  const reconcile = useDaemonReconcile();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const detail = await createWorkflowEntry({
        name: name.trim(),
        ...(source.kind === "imported" ? { yaml: source.yaml } : {}),
        ...(source.kind === "duplicate" ? { source_revision: source.revision } : {}),
      });
      reconcile("workflow_library_updated", detail.entry);
      navigate(`/workflows/${encodeURIComponent(detail.entry.name)}`);
    } catch (err) {
      // A taken name, an archived name still reserving itself, or an invalid
      // one. The typed name stays exactly as it is.
      setError(errorText(err));
      setSubmitting(false);
    }
  }

  return (
    <form className="newWorkflow" onSubmit={onSubmit} data-testid="workflow-create-form">
      <div className="newWorkflowTitle">
        {source.kind === "starter" && "New workflow"}
        {source.kind === "imported" &&
          `Import${source.filename !== null ? ` — ${source.filename}` : ""}`}
        {source.kind === "duplicate" && `Duplicate of ${source.of}`}
      </div>
      <div className="formField">
        <label className="fieldLabel" htmlFor="workflow-name">
          Name{" "}
          <span className="fieldHint">
            lowercase letters, digits, and hyphens — permanent
          </span>
        </label>
        <input
          id="workflow-name"
          className="mono"
          value={name}
          autoFocus
          onChange={(e) => setName(e.target.value)}
          placeholder="my-workflow"
          data-testid="workflow-name-input"
        />
      </div>
      <p className="hint">
        This creates a draft. Nothing is validated and nothing becomes launchable until
        you save an executable revision.
      </p>
      {error !== null && (
        <p className="submitError" data-testid="workflow-create-error">
          {error}
        </p>
      )}
      <div className="formActions">
        <button
          type="submit"
          className="primaryButton"
          disabled={submitting || name.trim() === ""}
        >
          {submitting ? "Creating…" : "Create draft"}
        </button>
        <button type="button" className="ghostButton" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function WorkflowsView() {
  const { snapshotReady, workflowLibrary } = useDaemonState();
  const [source, setSource] = useState<CreateSource | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const visible = useMemo(
    () => workflowLibrary.filter((entry) => showArchived || !entry.archived),
    [workflowLibrary, showArchived],
  );
  const archivedCount = workflowLibrary.filter((entry) => entry.archived).length;
  const customCount = workflowLibrary.filter((entry) => entry.origin === "custom").length;

  const onImport = useCallback(async (file: File) => {
    setImportError(null);
    try {
      setSource({ kind: "imported", yaml: await readWorkflowFile(file), filename: file.name });
    } catch (err) {
      setImportError(errorText(err));
    }
  }, []);

  useEffect(() => {
    document.title = "Workflows · ompire";
  }, []);

  return (
    <div className="workflowsMain">
      <div className="headerRow">
        <h1>Workflows</h1>
        <span className="subline">
          the procedures a task can be launched under — yours and the packaged examples
        </span>
        <span className="spacer" />
        <button
          type="button"
          className="ghostButton"
          onClick={() => fileRef.current?.click()}
          data-testid="workflow-import"
        >
          Import YAML…
        </button>
        <button
          type="button"
          className="primaryButton"
          onClick={() => setSource({ kind: "starter" })}
          data-testid="workflow-create"
        >
          New workflow
        </button>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept=".yaml,.yml,text/yaml,text/plain"
        className="visuallyHidden"
        aria-label="Import a workflow YAML file"
        onChange={(e) => {
          const file = e.target.files?.[0];
          // Cleared so choosing the same file twice still fires.
          e.target.value = "";
          if (file) void onImport(file);
        }}
      />

      {importError !== null && (
        <p className="submitError" data-testid="workflow-import-error">
          {importError}
        </p>
      )}

      {source !== null && <CreateForm source={source} onCancel={() => setSource(null)} />}

      {!snapshotReady ? (
        // Before the authoritative snapshot there is no library to be empty.
        <p className="empty" data-testid="workflows-loading">
          <span>Loading the workflow library…</span>
        </p>
      ) : (
        <>
          {customCount === 0 && source === null && (
            <p className="empty" data-testid="workflows-empty">
              <strong>No workflows of your own yet</strong>
              <span>
                Create one from the starter, import a YAML file, or open a packaged
                example below and duplicate it.
              </span>
            </p>
          )}
          <ul className="workflowList">
            {visible.map((entry) => (
              <EntryCard key={entry.name} entry={entry} />
            ))}
          </ul>
          {archivedCount > 0 && (
            <label className="archivedToggle">
              <input
                type="checkbox"
                checked={showArchived}
                onChange={(e) => setShowArchived(e.target.checked)}
                data-testid="workflows-show-archived"
              />
              Show {archivedCount} archived workflow{archivedCount === 1 ? "" : "s"}
            </label>
          )}
        </>
      )}
    </div>
  );
}
