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
import { WorkflowFlow } from "../components/workflow/WorkflowFlow";
import { WorkflowEditor } from "../components/workflow/WorkflowEditor";
import { useDaemonReconcile, useDaemonState } from "../lib/useDaemonState";
import { useWorkflowDraft } from "../lib/workflowDraft";
import { readWorkflowFile, workflowStateLabel } from "../lib/workflowLibrary";
import type {
  WorkflowLibraryDetail,
  WorkflowRevisionSummary,
  WorkflowValidation,
} from "../types";
import type { DraftObject } from "../lib/workflowDocument";
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

/** A built-in's procedure, read the same way every other flow is read.
 *
 * Its packaged text is still available below, but the flow is what an
 * operator compares before duplicating it — a read-only entry is not a reason
 * to make its definition harder to understand. */
function BuiltinFlow({ revision }: { revision: string }) {
  const [definition, setDefinition] = useState<DraftObject | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setDefinition(null);
    setError(null);
    getWorkflowRevision(revision)
      .then((loaded) => {
        if (live) setDefinition(loaded.definition);
      })
      .catch((err: unknown) => {
        if (live) setError(errorText(err));
      });
    return () => {
      live = false;
    };
  }, [revision]);
  if (error !== null) return <p className="submitError">{error}</p>;
  if (definition === null) return <p className="hint">Reading the definition…</p>;
  return <WorkflowFlow definition={definition} testId="workflow-builtin-flow" />;
}

export function WorkflowDetailView() {
  const { name = "" } = useParams();
  const navigate = useNavigate();
  const reconcile = useDaemonReconcile();
  const { snapshotReady, workflowLibrary } = useDaemonState();

  const entry = workflowLibrary.find((candidate) => candidate.name === name) ?? null;
  const [detail, setDetail] = useState<WorkflowLibraryDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** The editor's own working copy, in one representation at a time. Local
   * until a save succeeds: a snapshot, an event, or a slow response must never
   * overwrite what somebody is editing. */
  const draft = useWorkflowDraft(name);
  const {
    reset: resetDraft,
    setText: draftSetText,
    edit: draftEdit,
    generation,
  } = draft;
  /** The generation as it stands *now*, so an answer can tell whether the
   * draft it describes is still on screen. Reading state inside an async
   * handler would read the value captured when it started. */
  const generationRef = useRef(generation);
  useEffect(() => {
    generationRef.current = generation;
  }, [generation]);
  const [loaded, setLoaded] = useState(false);
  const [checked, setChecked] = useState<Checked>({ kind: "none" });
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  /** A refused save the operator has to reconcile. Held as the daemon's own
   * message — it already names both versions — because the only recovery is
   * one they choose: their text is never replaced automatically. */
  const [conflict, setConflict] = useState<string | null>(null);
  const [inspecting, setInspecting] = useState<{
    revision: string;
    definition: DraftObject | null;
    error: string | null;
  } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
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
          resetDraft(loaded.draft_yaml ?? "");
          setLoaded(true);
          setChecked({ kind: "none" });
          setConflict(null);
        }
      } catch (err) {
        setLoadError(errorText(err));
      }
    },
    [name, resetDraft],
  );

  // Loaded once per entry. A later library update refreshes the *summary*
  // through daemon state; it deliberately never refetches over local work.
  useEffect(() => {
    setDetail(null);
    setLoaded(false);
    void load({ replaceBuffer: true });
  }, [load]);

  /** Unsaved work, whichever mode it was done in. A visual change counts even
   * before it has been serialized: the text has not caught up, but the draft
   * has moved. */
  const dirty =
    detail !== null &&
    loaded &&
    (draft.visualChanged || draft.text !== (detail.draft_yaml ?? ""));

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

  const setText = useCallback(
    (next: string) => {
      draftSetText(next);
      // An edit does not discard the last result — it marks it as describing
      // text that is no longer what is on screen.
      setChecked((current) =>
        current.kind === "none" ? current : { ...current, stale: true },
      );
    },
    [draftSetText],
  );

  const editDocument = useCallback(
    (update: (document: DraftObject) => DraftObject) => {
      draftEdit(update);
      setChecked((current) =>
        current.kind === "none" ? current : { ...current, stale: true },
      );
    },
    [draftEdit],
  );

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

  /** The exact text of the edit currently on screen.
   *
   * In visual mode this serializes the current generation rather than reusing
   * an older answer: a save must submit what the operator is looking at, and
   * a conversion that cannot produce it refuses the save instead of sending
   * a stale document. */
  async function submittedText(): Promise<string | null> {
    const text = await draft.currentText();
    if (text === null) {
      setActionError(
        "Your visual changes could not be turned back into a document, so nothing was submitted. Your work is still here — retry, or download it.",
      );
    }
    return text;
  }

  async function onSaveDraft() {
    if (baseVersion === null) return;
    const text = await submittedText();
    if (text === null) return;
    const saved = await run("draft", () => saveWorkflowDraft(name, text, baseVersion));
    if (saved !== null) applyDetail(saved);
  }

  async function onValidate() {
    if (busy !== null) return;
    const submitted = await submittedText();
    if (submitted === null) return;
    // Tied to the exact text submitted, so a result can never describe a
    // different draft than the one it was asked about.
    const submittedGeneration = generationRef.current;
    setBusy("validate");
    setActionError(null);
    try {
      const result = await validateWorkflow(submitted, name);
      setChecked({
        kind: "ok",
        result,
        stale: submittedGeneration !== generationRef.current,
      });
    } catch (err) {
      const detailBody = err instanceof DaemonError ? err.detail : null;
      setChecked({
        kind: "failed",
        message: errorText(err),
        location: typeof detailBody?.location === "string" ? detailBody.location : null,
        line: typeof detailBody?.line === "number" ? detailBody.line : null,
        stale: submittedGeneration !== generationRef.current,
      });
    } finally {
      setBusy(null);
    }
  }

  async function onSaveRevision() {
    if (baseVersion === null) return;
    const text = await submittedText();
    if (text === null) return;
    const saved = await run("revision", () =>
      saveWorkflowRevision(name, text, baseVersion),
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

  async function onDownloadDraft() {
    // Labelled a draft on purpose: it is the work in the editor, which is not
    // necessarily a valid workflow and is certainly not a saved revision.
    const text = await draft.currentText();
    if (text !== null) {
      download(`${name}.draft.yaml`, text);
      return;
    }
    // The conversion service could not be reached. Rather than leave somebody
    // with no way to keep visual work, hand them the document as JSON — which
    // is valid YAML, so it imports again — and say that is what it is.
    const fallback = draft.fallbackText();
    if (fallback === null) return;
    download(`${name}.draft.json.yaml`, fallback);
    setActionError(
      "The daemon could not turn this draft back into YAML, so it was downloaded as JSON instead. JSON is valid YAML, so importing the file works; the layout is not what you typed.",
    );
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
          {summary.current_revision !== null && (
            <BuiltinFlow revision={summary.current_revision} />
          )}
          <details>
            <summary>Read its YAML</summary>
            <pre className="yamlReadonly" data-testid="workflow-packaged-yaml">
              {detail.draft_yaml ?? "This package no longer ships this definition."}
            </pre>
          </details>
        </section>
      ) : (
        <section className="panel">
          <h2 className="panelTitle">
            {summary.archived ? "Editor (archived — read-only)" : "Editor"}
          </h2>
          {dirty && (
            <p className="hint unsavedHint" data-testid="workflow-unsaved">
              Unsaved changes in this editor.
            </p>
          )}
          {/* Two views of one draft, never two drafts. Switching is not a save
              and not a launch; what changes is which controls you get. */}
          <div className="formActions" role="group" aria-label="Editing mode">
            <button
              type="button"
              className={draft.mode === "visual" ? "primaryButton" : "ghostButton"}
              aria-pressed={draft.mode === "visual"}
              disabled={draft.converting}
              data-testid="workflow-mode-visual"
              onClick={() => void draft.enterVisual()}
            >
              {draft.mode === "visual" ? "Visual" : "Switch to visual"}
            </button>
            <button
              type="button"
              className={draft.mode === "yaml" ? "primaryButton" : "ghostButton"}
              aria-pressed={draft.mode === "yaml"}
              disabled={draft.converting}
              data-testid="workflow-mode-yaml"
              onClick={() => void draft.leaveVisual()}
            >
              {draft.mode === "yaml" ? "YAML" : "Switch to YAML"}
            </button>
            {draft.converting && <span className="hint">Working…</span>}
          </div>
          {draft.mode === "visual" && (
            <p className="hint" data-testid="workflow-visual-warning">
              Editing here rewrites the document when you save, which normalizes
              its layout and drops YAML comments. What it does not change is
              what the workflow means. Download the draft first if you want to
              keep your own formatting.
            </p>
          )}
          {draft.conversionError !== null && (
            <div className="checkFailed" data-testid="workflow-conversion-error">
              <p>{draft.conversionError}</p>
              <p className="hint">
                Nothing was changed and nothing was saved. Your work is exactly
                as you left it.
              </p>
              <button
                type="button"
                className="ghostButton"
                data-testid="workflow-conversion-retry"
                onClick={() =>
                  void (draft.mode === "visual" ? draft.revalidate() : draft.enterVisual())
                }
              >
                Try again
              </button>
            </div>
          )}
          {draft.mode === "visual" && draft.document !== null ? (
            <WorkflowEditor
              document={draft.document}
              onChange={editDocument}
              validation={draft.validation}
              readOnly={readOnly}
              stale={draft.converting}
            />
          ) : (
            <>
              <label className="visuallyHidden" htmlFor="workflow-yaml">
                Workflow definition YAML
              </label>
              <textarea
                id="workflow-yaml"
                className="yamlEditor mono"
                spellCheck={false}
                readOnly={readOnly}
                value={draft.text}
                onChange={(e) => setText(e.target.value)}
                data-testid="workflow-editor"
              />
            </>
          )}
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
            <button
              type="button"
              className="ghostButton"
              onClick={() => void onDownloadDraft()}
              data-testid="workflow-download-draft"
            >
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
              buffer={draft.text}
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
              <WorkflowFlow definition={checked.result.definition} testId="workflow-check-flow" />
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
              <WorkflowFlow definition={inspecting.definition} testId="workflow-inspect-flow" />
            )}
          </div>
        )}
      </section>
    </div>
  );
}
