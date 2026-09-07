import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  DaemonError,
  createWorkflowEntry,
  exportWorkflowRevision,
  getWorkflowEntry,
  getWorkflowRevision,
  saveWorkflowDraft,
  saveWorkflowRevision,
  setWorkflowArchived,
  validateWorkflow,
} from "../lib/api";
import { WorkflowOutline } from "../components/WorkflowOutline";
import { useDaemonReconcile, useDaemonState } from "../lib/useDaemonState";
import { readWorkflowFile, workflowStateLabel } from "../lib/workflowLibrary";
import type {
  WorkflowLibraryDetail,
  WorkflowRevisionSummary,
  WorkflowValidation,
} from "../types";
import "./WorkflowsView.css";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Hand the operator a file.
 *
 * The anchor is put in the document before it is clicked and the object URL is
 * released only after the browser has had a turn to start reading it: a
 * detached `<a download>` is unreliable outside Chromium, and revoking on the
 * next line can beat the read. A download that silently does not happen is
 * worst exactly where it is offered as the way to keep text before discarding
 * it. */
function download(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/yaml" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** A validation result, and whether the text it describes is still the text in
 * the editor. Any edit makes it `stale`: a result that outlived its input is
 * the one thing a validator must never present as current. */
type Checked =
  | { kind: "none" }
  | { kind: "ok"; result: WorkflowValidation; stale: boolean }
  | { kind: "failed"; message: string; location: string | null; line: number | null; stale: boolean };

function ConflictNotice({
  message,
  onReload,
  onDismiss,
  buffer,
}: {
  message: string;
  onReload: () => void;
  onDismiss: () => void;
  buffer: string;
}) {
  return (
    <div className="conflictNotice" data-testid="workflow-conflict">
      <p className="conflictMessage">{message}</p>
      <p className="hint">
        Nothing was saved. Your text is still here. Copy or download it first if you want
        to keep it, then reload the saved version and reapply your changes — there is no
        automatic merge, and no way to overwrite what somebody else saved.
      </p>
      <div className="formActions">
        <button
          type="button"
          className="ghostButton"
          onClick={() => void navigator.clipboard?.writeText(buffer)}
        >
          Copy my text
        </button>
        <button
          type="button"
          className="ghostButton"
          onClick={onReload}
          data-testid="workflow-conflict-reload"
        >
          Discard my edits and reload
        </button>
        <button type="button" className="ghostButton" onClick={onDismiss}>
          Keep editing
        </button>
      </div>
    </div>
  );
}

function RevisionList({
  revisions,
  current,
  onInspect,
}: {
  revisions: WorkflowRevisionSummary[];
  current: string | null;
  onInspect: (revision: string) => void;
}) {
  if (revisions.length === 0) {
    return <p className="hint">No executable revision has been saved for this workflow yet.</p>;
  }
  return (
    <ul className="revisionList" data-testid="workflow-revisions">
      {[...revisions].reverse().map((summary) => (
        <li key={summary.revision}>
          <code className="mono workflowRevision">{summary.revision}</code>
          <span className="revisionMeta">
            format {summary.format} · {summary.created_at.slice(0, 19).replace("T", " ")}
            {summary.revision === current ? " · current" : ""}
          </span>
          <button
            type="button"
            className="ghostButton"
            onClick={() => onInspect(summary.revision)}
          >
            Inspect
          </button>
        </li>
      ))}
    </ul>
  );
}

export function WorkflowDetailView() {
  const { name = "" } = useParams();
  const navigate = useNavigate();
  const reconcile = useDaemonReconcile();
  const { snapshotReady, workflowLibrary } = useDaemonState();

  const entry = workflowLibrary.find((candidate) => candidate.name === name) ?? null;
  const [detail, setDetail] = useState<WorkflowLibraryDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** The editor's own text. Local until a save succeeds: a snapshot, an event,
   * or a slow response must never overwrite what somebody is typing. */
  const [buffer, setBuffer] = useState<string | null>(null);
  const [checked, setChecked] = useState<Checked>({ kind: "none" });
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  /** A refused save the operator has to reconcile. Held as the daemon's own
   * message — it already names both versions — because the only recovery is
   * one they choose: their text is never replaced automatically. */
  const [conflict, setConflict] = useState<string | null>(null);
  const [inspecting, setInspecting] = useState<{
    revision: string;
    definition: Record<string, unknown> | null;
    error: string | null;
  } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  /** The buffer as it stands *now*, so a validation response can tell whether
   * the text it describes is still on screen. Reading state inside an async
   * handler would read the value captured when it started. */
  const bufferRef = useRef<string | null>(buffer);
  /** The entry version the loaded text belongs to. Every save submits it, so a
   * save can never be applied over an edit this editor never saw. */
  const [baseVersion, setBaseVersion] = useState<number | null>(null);

  const load = useCallback(
    async (options: { replaceBuffer: boolean }) => {
      setLoadError(null);
      try {
        const loaded = await getWorkflowEntry(name);
        setDetail(loaded);
        setBaseVersion(loaded.entry.version);
        if (options.replaceBuffer) {
          setBuffer(loaded.draft_yaml ?? "");
          setChecked({ kind: "none" });
          setConflict(null);
        }
      } catch (err) {
        setLoadError(errorText(err));
      }
    },
    [name],
  );

  // Loaded once per entry. A later library update refreshes the *summary*
  // through daemon state; it deliberately never refetches over the buffer.
  useEffect(() => {
    setDetail(null);
    setBuffer(null);
    void load({ replaceBuffer: true });
  }, [load]);

  useEffect(() => {
    bufferRef.current = buffer;
  }, [buffer]);

  const dirty =
    detail !== null && buffer !== null && buffer !== (detail.draft_yaml ?? "");

  // Leaving with unsaved text is guarded the same way anywhere it can happen.
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  /** The entry as saved state knows it.
   *
   * The socket's projection when it has one, and this route's own REST read
   * otherwise — opening the detail by URL resolves the read before the first
   * snapshot arrives, and every decision below has to agree about what the
   * entry is even in that window. */
  const summary = entry ?? detail?.entry ?? null;
  const readOnly = summary?.origin === "builtin" || summary?.archived === true;

  const setText = useCallback((next: string) => {
    setBuffer(next);
    // An edit does not discard the last result — it marks it as describing
    // text that is no longer what is on screen.
    setChecked((current) =>
      current.kind === "none" ? current : { ...current, stale: true },
    );
  }, []);

  const replaceBuffer = useCallback(
    (next: string, confirmMessage: string) => {
      if (dirty && !window.confirm(confirmMessage)) return;
      setText(next);
    },
    [dirty, setText],
  );

  const applyDetail = useCallback(
    (saved: WorkflowLibraryDetail) => {
      setDetail(saved);
      setBaseVersion(saved.entry.version);
      reconcile("workflow_library_updated", saved.entry);
    },
    [reconcile],
  );

  const refuse = useCallback((err: unknown) => {
    if (err instanceof DaemonError && err.reason === "workflow_version_conflict") {
      setConflict(err.message);
      return;
    }
    setActionError(errorText(err));
  }, []);

  async function run<T>(label: string, action: () => Promise<T>): Promise<T | null> {
    if (busy !== null) return null;
    setBusy(label);
    setActionError(null);
    try {
      return await action();
    } catch (err) {
      refuse(err);
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function onSaveDraft() {
    if (buffer === null || baseVersion === null) return;
    const saved = await run("draft", () => saveWorkflowDraft(name, buffer, baseVersion));
    if (saved !== null) applyDetail(saved);
  }

  async function onValidate() {
    if (buffer === null || busy !== null) return;
    // Tied to the exact text submitted, so a result can never describe a
    // different buffer than the one it was asked about.
    const submitted = buffer;
    setBusy("validate");
    setActionError(null);
    try {
      const result = await validateWorkflow(submitted, name);
      setChecked({ kind: "ok", result, stale: submitted !== bufferRef.current });
    } catch (err) {
      const detailBody = err instanceof DaemonError ? err.detail : null;
      setChecked({
        kind: "failed",
        message: errorText(err),
        location: typeof detailBody?.location === "string" ? detailBody.location : null,
        line: typeof detailBody?.line === "number" ? detailBody.line : null,
        stale: submitted !== bufferRef.current,
      });
    } finally {
      setBusy(null);
    }
  }

  async function onSaveRevision() {
    if (buffer === null || baseVersion === null) return;
    const saved = await run("revision", () =>
      saveWorkflowRevision(name, buffer, baseVersion),
    );
    if (saved !== null) {
      applyDetail(saved);
      setChecked({ kind: "none" });
    }
  }

  async function onArchive(archived: boolean) {
    if (baseVersion === null) return;
    const saved = await run("archive", () =>
      setWorkflowArchived(name, archived, baseVersion),
    );
    if (saved !== null) applyDetail(saved);
  }

  async function onExport(revision: string) {
    const exported = await run("export", () => exportWorkflowRevision(revision));
    if (exported === null) return;
    download(`${exported.name}.yaml`, exported.yaml);
  }

  function onDownloadDraft() {
    if (buffer === null) return;
    // Labelled a draft on purpose: it is the text in the editor, which is not
    // necessarily a valid workflow and is certainly not a saved revision.
    download(`${name}.draft.yaml`, buffer);
  }

  async function onDuplicate() {
    if (entry?.current_revision == null) return;
    const proposed = window.prompt(
      "Name for the copy (lowercase letters, digits, and hyphens):",
      `${name}-copy`,
    );
    if (proposed === null || proposed.trim() === "") return;
    const created = await run("duplicate", () =>
      createWorkflowEntry({
        name: proposed.trim(),
        source_revision: entry.current_revision as string,
      }),
    );
    if (created !== null) {
      reconcile("workflow_library_updated", created.entry);
      navigate(`/workflows/${encodeURIComponent(created.entry.name)}`);
    }
  }

  async function onInspect(revision: string) {
    setInspecting({ revision, definition: null, error: null });
    try {
      const loaded = await getWorkflowRevision(revision);
      setInspecting({ revision, definition: loaded.definition, error: null });
    } catch (err) {
      setInspecting({ revision, definition: null, error: errorText(err) });
    }
  }

  const state = useMemo(
    () => (summary === null ? null : workflowStateLabel(summary)),
    [summary],
  );

  if (summary === null && snapshotReady && detail === null && loadError !== null) {
    return (
      <div className="workflowsMain">
        <div className="headerRow">
          <h1>{name}</h1>
        </div>
        <p className="empty" data-testid="workflow-not-found">
          <strong>No workflow by that name</strong>
          <span>{loadError}</span>
        </p>
        <Link to="/workflows">Back to Workflows</Link>
      </div>
    );
  }

  if (detail === null || summary === null) {
    return (
      <div className="workflowsMain">
        <p className="empty" data-testid="workflow-detail-loading">
          <span>Loading {name}…</span>
        </p>
      </div>
    );
  }

  return (
    <div className="workflowsMain">
      <div className="headerRow">
        <h1>{summary.name}</h1>
        <span className={`originChip origin-${summary.origin}`}>{summary.origin}</span>
        {state !== null && (
          <span className={`stateChip state-${state.tone}`} data-testid="workflow-detail-state">
            {state.label}
          </span>
        )}
        <span className="spacer" />
        <Link className="ghostButton" to="/workflows">
          All workflows
        </Link>
        {summary.available && (
          <Link
            className="primaryButton"
            to="/spawn"
            state={{ workflow: summary.name }}
            data-testid="workflow-launch"
          >
            Launch in Spawn
          </Link>
        )}
      </div>

      <p className="subline">
        {summary.current_revision === null ? (
          "No executable revision — this workflow cannot be launched yet."
        ) : (
          <>
            Current revision{" "}
            <code className="mono workflowRevision">{summary.current_revision}</code> ·
            format {summary.current_format}
          </>
        )}
      </p>
      {summary.unavailable_detail !== null && (
        <p className="workflowUnavailable">{summary.unavailable_detail}</p>
      )}

      {summary.origin === "builtin" ? (
        <section className="panel">
          <h2 className="panelTitle">Packaged example</h2>
          <p className="hint">
            Built-in workflows ship with the daemon and are read-only. Duplicate this one
            to make a version you can edit.
          </p>
          <div className="formActions">
            <button
              type="button"
              className="primaryButton"
              onClick={() => void onDuplicate()}
              disabled={busy !== null || summary.current_revision === null}
              data-testid="workflow-duplicate"
            >
              Duplicate…
            </button>
            {summary.current_revision !== null && (
              <button
                type="button"
                className="ghostButton"
                onClick={() => void onExport(summary.current_revision as string)}
              >
                Export YAML
              </button>
            )}
          </div>
          <pre className="yamlReadonly" data-testid="workflow-packaged-yaml">
            {detail.draft_yaml ?? "This package no longer ships this definition."}
          </pre>
        </section>
      ) : (
        <section className="panel">
          <h2 className="panelTitle">
            YAML {summary.archived ? "(archived — read-only)" : "editor"}
          </h2>
          {dirty && (
            <p className="hint unsavedHint" data-testid="workflow-unsaved">
              Unsaved changes in this editor.
            </p>
          )}
          <label className="visuallyHidden" htmlFor="workflow-yaml">
            Workflow definition YAML
          </label>
          <textarea
            id="workflow-yaml"
            className="yamlEditor mono"
            spellCheck={false}
            readOnly={readOnly}
            value={buffer ?? ""}
            onChange={(e) => setText(e.target.value)}
            data-testid="workflow-editor"
          />
          <div className="formActions">
            <button
              type="button"
              className="ghostButton"
              onClick={() => void onSaveDraft()}
              disabled={readOnly || busy !== null}
              data-testid="workflow-save-draft"
            >
              {busy === "draft" ? "Saving…" : "Save draft"}
            </button>
            <button
              type="button"
              className="ghostButton"
              onClick={() => void onValidate()}
              disabled={busy !== null}
              data-testid="workflow-validate"
            >
              {busy === "validate" ? "Checking…" : "Validate"}
            </button>
            <button
              type="button"
              className="primaryButton"
              onClick={() => void onSaveRevision()}
              disabled={readOnly || busy !== null}
              data-testid="workflow-save-revision"
            >
              {busy === "revision" ? "Saving…" : "Save executable revision"}
            </button>
            <span className="spacer" />
            <button
              type="button"
              className="ghostButton"
              onClick={() => fileRef.current?.click()}
              disabled={readOnly}
              data-testid="workflow-import-into"
            >
              Import into editor…
            </button>
            <button type="button" className="ghostButton" onClick={onDownloadDraft}>
              Download draft
            </button>
          </div>
          <p className="hint">
            Saving a draft keeps your text, valid or not, and never changes what this
            workflow would launch. Validating checks this exact text and saves nothing.
            Only an executable save makes a new revision the current choice — and it never
            starts a task.
          </p>
          <input
            ref={fileRef}
            type="file"
            accept=".yaml,.yml,text/yaml,text/plain"
            className="visuallyHidden"
            aria-label="Import a workflow YAML file into the editor"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (!file) return;
              void readWorkflowFile(file)
                .then((text) =>
                  replaceBuffer(
                    text,
                    "Importing replaces the text in this editor. Your unsaved changes will be lost. Continue?",
                  ),
                )
                .catch((err: unknown) => setActionError(errorText(err)));
            }}
          />

          {conflict !== null && (
            <ConflictNotice
              message={conflict}
              buffer={buffer ?? ""}
              onReload={() => void load({ replaceBuffer: true })}
              onDismiss={() => setConflict(null)}
            />
          )}
          {actionError !== null && (
            <p className="submitError" data-testid="workflow-action-error">
              {actionError}
            </p>
          )}

          {checked.kind === "failed" && (
            <div className="checkFailed" data-testid="workflow-check-failed">
              <p>
                {checked.location !== null && (
                  <code className="mono">{checked.location}</code>
                )}
                {checked.line !== null && ` line ${checked.line}`}
                {checked.location !== null || checked.line !== null ? " — " : ""}
                {checked.message}
              </p>
              {checked.stale && (
                <p className="hint" data-testid="workflow-check-stale">
                  You have edited the text since this check. Validate again.
                </p>
              )}
            </div>
          )}
          {checked.kind === "ok" && (
            <div className="checkOk" data-testid="workflow-check-ok">
              <p>
                Valid format-{checked.result.format} definition ·{" "}
                <code className="mono workflowRevision">{checked.result.revision}</code>
              </p>
              <p className="hint">
                Structural only: this says the daemon can read the definition, not that
                the commands it names exist, that a model will comply, or that a run will
                succeed.
              </p>
              {checked.stale && (
                <p className="hint" data-testid="workflow-check-stale">
                  You have edited the text since this check. Validate again.
                </p>
              )}
              <WorkflowOutline definition={checked.result.definition} />
            </div>
          )}

          <div className="formActions archiveActions">
            {summary.archived ? (
              <button
                type="button"
                className="ghostButton"
                onClick={() => void onArchive(false)}
                disabled={busy !== null}
                data-testid="workflow-restore"
              >
                Restore
              </button>
            ) : (
              <button
                type="button"
                className="ghostButton"
                onClick={() => void onArchive(true)}
                disabled={busy !== null}
                data-testid="workflow-archive"
              >
                Archive
              </button>
            )}
            <span className="hint">
              Archiving removes this workflow from launch choices. Nothing is deleted:
              the draft, every saved revision, and every task that ran one stay exactly as
              they are.
            </span>
          </div>
        </section>
      )}

      <section className="panel">
        <h2 className="panelTitle">Saved revisions</h2>
        <RevisionList
          revisions={detail.revisions}
          current={summary.current_revision}
          onInspect={(revision) => void onInspect(revision)}
        />
        {inspecting !== null && (
          <div className="revisionInspector" data-testid="workflow-inspector">
            <div className="formActions">
              <code className="mono workflowRevision">{inspecting.revision}</code>
              <span className="spacer" />
              <button
                type="button"
                className="ghostButton"
                onClick={() => void onExport(inspecting.revision)}
              >
                Export YAML
              </button>
              <button
                type="button"
                className="ghostButton"
                onClick={() =>
                  void exportWorkflowRevision(inspecting.revision)
                    .then((exported) =>
                      replaceBuffer(
                        exported.yaml,
                        "Loading this revision replaces the text in the editor. Your unsaved changes will be lost. Continue?",
                      ),
                    )
                    .catch((err: unknown) => setActionError(errorText(err)))
                }
                disabled={readOnly}
                data-testid="workflow-load-revision"
              >
                Load into editor
              </button>
              <button
                type="button"
                className="ghostButton"
                onClick={() => setInspecting(null)}
              >
                Close
              </button>
            </div>
            {inspecting.error !== null ? (
              <p className="submitError">{inspecting.error}</p>
            ) : inspecting.definition === null ? (
              <p className="hint">Loading…</p>
            ) : (
              <WorkflowOutline definition={inspecting.definition} />
            )}
          </div>
        )}
      </section>
    </div>
  );
}
