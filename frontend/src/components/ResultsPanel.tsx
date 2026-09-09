import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  acceptTaskResult,
  captureTaskResult,
  fetchTaskResultDownload,
  getTaskResultDiff,
  getTaskResultFile,
  listTaskResults,
  purgeTaskResult,
} from "../lib/api";
import type {
  Task,
  TaskResult,
  TaskResultDiff,
  TaskResultFileContent,
  TaskResultLimits,
  TaskResultsProjection,
} from "../types";
import {
  REVISION_STATUS_LABEL,
  formatResultBytes as formatBytes,
  revisionStatus,
  shortResultId as shortId,
} from "../lib/resultPresentation";
import { ResultExport } from "./ResultExport";
import "./ResultsPanel.css";

/* The Results panel (ADR-0034): capture, inspect, accept, download, and purge
 * a task's durable results.
 *
 * Deliberately independent of Review and Ship flow. Accepting a result says
 * "keep this, I have read it" — it answers no workflow gate, grades no code,
 * and authorizes no publication — so routing it through the review surface
 * would misrepresent what the operator just decided.
 *
 * Everything displayed here is untrusted content the task's agent wrote. File
 * text is rendered as escaped source inside <pre>: React escapes it, and no
 * path in this component sets innerHTML, renders Markdown, or turns an
 * agent-authored link into something a click could follow. */

/** Hand the browser a file it already has in memory. The bytes came through an
 * authenticated fetch, so no token ever appears in a URL or in history. */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function CaptureForm({
  taskId,
  limits,
  disabled,
  disabledReason,
  onCaptured,
}: {
  taskId: number;
  limits: TaskResultLimits | null;
  disabled: boolean;
  disabledReason: string | null;
  onCaptured: (projection: TaskResultsProjection) => void;
}) {
  const [paths, setPaths] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function capture() {
    const selection = paths
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    if (selection.length === 0) {
      setError("Enter at least one repository-relative file or directory path.");
      return;
    }
    setPending(true);
    setError(null);
    try {
      // A fresh id per attempt: a *retry* is a new capture, while a lost
      // response to this one is recovered by the daemon's own replay rule.
      const requestId = crypto.randomUUID();
      onCaptured(await captureTaskResult(taskId, selection, requestId));
      setPaths("");
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setPending(false);
    }
  }

  if (disabled) {
    return (
      <p className="resultsHint" data-testid="results-capture-unavailable">
        {disabledReason}
      </p>
    );
  }

  return (
    <div className="resultsCapture">
      <label className="resultsLabel" htmlFor={`capture-paths-${taskId}`}>
        Repository-relative files or directories, one per line
      </label>
      <textarea
        id={`capture-paths-${taskId}`}
        className="resultsPaths"
        rows={3}
        value={paths}
        placeholder={"epics/my-epic\nchanges/my-change/SPEC.md"}
        onChange={(event) => setPaths(event.target.value)}
        data-testid="results-capture-paths"
      />
      {limits && (
        <p className="resultsHint" data-testid="results-limits">
          Captures {limits.supported_extensions.join(", ")} files only, up to{" "}
          {limits.max_files} files, {formatBytes(limits.max_file_bytes)} per file
          and {formatBytes(limits.max_total_bytes)} in total. Hidden paths such as{" "}
          <code>.git</code> are never captured, and an unsupported or unsafe
          entry fails the whole capture rather than silently omitting it.
        </p>
      )}
      {error && (
        <div className="resultsError" data-testid="results-capture-error">
          {error}
        </div>
      )}
      <button
        type="button"
        className="resultsAction"
        disabled={pending}
        onClick={() => void capture()}
        data-testid="results-capture"
      >
        {pending ? "Capturing…" : "Capture result"}
      </button>
    </div>
  );
}

function Provenance({ result }: { result: TaskResult }) {
  const provenance = result.provenance;
  if (!provenance) return null;
  return (
    <dl className="resultsProvenance" data-testid="results-provenance">
      <dt>captured</dt>
      <dd>{new Date(result.captured_at).toLocaleString()}</dd>
      <dt>captured by</dt>
      <dd>{provenance.capture_actor}</dd>
      <dt>workflow</dt>
      <dd>
        {provenance.workflow_name ?? "unknown"}
        {provenance.workflow_revision
          ? ` · ${provenance.workflow_revision.slice(0, 12)}`
          : ""}
      </dd>
      <dt>producing step</dt>
      {/* Named as unknown on purpose: a manual capture does not know which
          attempt wrote each file, and the run's latest step is not evidence
          that it did. */}
      <dd>{provenance.producing_step}</dd>
      <dt>launch base</dt>
      <dd>{provenance.launch_base_branch ?? "not recorded"}</dd>
      <dt>commit at capture</dt>
      <dd>
        {provenance.capture_head_commit
          ? provenance.capture_head_commit.slice(0, 12)
          : "unavailable"}
        <span className="resultsNote"> (observed at capture, not the spawn base)</span>
      </dd>
      {provenance.gaps.length > 0 && (
        <>
          <dt>unknown</dt>
          <dd data-testid="results-provenance-gaps">{provenance.gaps.join(", ")}</dd>
        </>
      )}
    </dl>
  );
}

function FileViewer({
  taskId,
  result,
}: {
  taskId: number;
  result: TaskResult;
}) {
  const [path, setPath] = useState<string | null>(null);
  const [content, setContent] = useState<TaskResultFileContent | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A late response must never land under a different revision or file. The
  // request records which it was for, and anything else is dropped.
  useEffect(() => {
    setContent(null);
    setError(null);
    if (path === null) return;
    const wanted = { resultId: result.id, path };
    let cancelled = false;
    getTaskResultFile(taskId, result.id, path)
      .then((loaded) => {
        if (cancelled) return;
        if (loaded.result_id !== wanted.resultId || loaded.path !== wanted.path) return;
        setContent(loaded);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : String(caught));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, result.id, path]);

  // Selecting a different revision clears whatever file was open in the last.
  useEffect(() => setPath(null), [result.id]);

  if (result.files.length === 0) {
    return null;
  }

  return (
    <div className="resultsFiles">
      <ul className="resultsFileList" data-testid="results-file-list">
        {result.files.map((file) => (
          <li key={file.path}>
            <button
              type="button"
              className={`resultsFileButton${path === file.path ? " selected" : ""}`}
              onClick={() => setPath(path === file.path ? null : file.path)}
              disabled={!result.available}
            >
              <span className="resultsFilePath">{file.path}</span>
              <span className="resultsFileMeta">
                {formatBytes(file.length)} · {file.media_type}
              </span>
            </button>
            <span className="resultsChecksum" title={file.sha256}>
              {file.sha256.slice(0, 12)}
            </span>
          </li>
        ))}
      </ul>
      {error && (
        <div className="resultsError" data-testid="results-file-error">
          {error}
        </div>
      )}
      {content && (
        <>
          <p className="resultsHint">
            Showing <code>{content.path}</code> as source. Agent-authored content
            is never rendered or executed here.
          </p>
          {/* React escapes this; nothing sets innerHTML. */}
          <pre className="resultsSource" data-testid="results-file-source">
            {content.text}
          </pre>
        </>
      )}
    </div>
  );
}

function DiffView({ taskId, result }: { taskId: number; result: TaskResult }) {
  const [diff, setDiff] = useState<TaskResultDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setDiff(null);
    setError(null);
    setOpen(false);
  }, [result.id]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    getTaskResultDiff(taskId, result.id)
      .then((loaded) => {
        // Guarded by revision: a slow response for the previously selected
        // revision must not be shown beside this one.
        if (!cancelled && loaded.result_id === result.id) setDiff(loaded);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [open, taskId, result.id]);

  if (result.predecessor_id === null) return null;

  return (
    <div className="resultsDiff">
      <button
        type="button"
        className="resultsLink"
        onClick={() => setOpen(!open)}
        data-testid="results-diff-toggle"
      >
        {open ? "Hide comparison" : "Compare with previous revision"}
      </button>
      {error && <div className="resultsError">{error}</div>}
      {open && diff && (
        <div data-testid="results-diff">
          {!diff.predecessor_available ? (
            <p className="resultsHint" data-testid="results-diff-unavailable">
              No comparison: {diff.predecessor_reason}. This revision&apos;s own
              files are still complete and readable.
            </p>
          ) : (
            <>
              <ul className="resultsDiffSummary">
                <li>added: {diff.added.join(", ") || "none"}</li>
                <li>changed: {diff.changed.join(", ") || "none"}</li>
                <li>
                  not in this revision: {diff.omitted.join(", ") || "none"}
                  {diff.omitted.length > 0 && (
                    <span className="resultsNote">
                      {" "}
                      — a difference between bundles, not an instruction to
                      delete anything
                    </span>
                  )}
                </li>
              </ul>
              {diff.truncated && (
                <p className="resultsHint" data-testid="results-diff-truncated">
                  The comparison was too large to show in full and is truncated
                  here. Every file&apos;s complete source and download are
                  unaffected.
                </p>
              )}
              <pre className="resultsSource">{diff.text}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function RevisionActions({
  taskId,
  projectName,
  result,
  version,
  onUpdated,
}: {
  taskId: number;
  projectName: string;
  result: TaskResult;
  version: number;
  onUpdated: (projection: TaskResultsProjection) => void;
}) {
  const navigate = useNavigate();
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const locked = useRef(false);

  useEffect(() => {
    setError(null);
  }, [result.id]);

  async function run(label: string, action: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setPending(label);
    setError(null);
    try {
      await action();
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      locked.current = false;
      setPending(null);
    }
  }

  const manifestId = result.manifest_id;
  const unfinishedExports = result.exports.filter(
    (record) => record.state === "running" || record.state === "unresolved",
  );

  function purge() {
    if (manifestId === null) return;
    const confirmed = window.confirm(
      `Purge revision ${shortId(result.id)}?\n\n` +
        `This permanently deletes its ${result.file_count} retained ` +
        `file(s) (${formatBytes(result.total_bytes)}).\n` +
        (result.accepted_at !== null
          ? "You accepted this revision; the record of that decision is kept, but its files are not.\n"
          : "This revision was never accepted.\n") +
        "\nThe files cannot be recovered from the workspace afterwards.",
    );
    if (!confirmed) return;
    void run("purging", async () => {
      onUpdated(await purgeTaskResult(taskId, result.id, manifestId, version));
    });
  }

  return (
    <div className="resultsRevisionActions">
      {error && (
        <div className="resultsError" data-testid="results-action-error">
          {error}
        </div>
      )}
      {result.available && result.accepted_at !== null && manifestId !== null && (
        <>
          <button
            type="button"
            className="resultsAction"
            disabled={pending !== null}
            onClick={() =>
              navigate("/spawn", {
                state: {
                  project: projectName,
                  attachment: {
                    producer_task_id: result.task_id,
                    result_id: result.id,
                    expected_manifest_id: manifestId,
                  },
                },
              })
            }
            data-testid="results-start-task"
          >
            Start task from this result
          </button>
          <p className="resultsHint" data-testid="results-start-task-scope">
            Opens the ordinary launch form with exactly this revision attached.
            Nothing starts until you review and submit it there. The files are
            installed as handoff inputs and are never published.
          </p>
        </>
      )}
      {result.available && result.accepted_at === null && manifestId !== null && (
        <>
          <button
            type="button"
            className="resultsAction"
            disabled={pending !== null}
            onClick={() =>
              void run("accepting", async () => {
                onUpdated(await acceptTaskResult(taskId, result.id, manifestId));
              })
            }
            data-testid="results-accept"
          >
            {pending === "accepting" ? "Accepting…" : "Accept this revision"}
          </button>
          <p className="resultsHint" data-testid="results-accept-scope">
            Accepting records that you reviewed and are keeping exactly these
            files. It does not approve code, answer a workflow question, or
            allow anything to be published.
          </p>
        </>
      )}
      {result.available && (
        <div className="resultsDownloads">
          <button
            type="button"
            className="resultsAction"
            disabled={pending !== null}
            onClick={() =>
              void run("downloading", async () => {
                const { blob, filename } = await fetchTaskResultDownload(
                  taskId,
                  result.id,
                );
                saveBlob(blob, filename);
              })
            }
            data-testid="results-download-zip"
          >
            {pending === "downloading" ? "Preparing…" : "Download all (ZIP)"}
          </button>
        </div>
      )}
      {result.consumer_task_ids.length > 0 && (
        <p className="resultsHint" data-testid="results-consumers">
          {result.consumer_task_ids.length === 1
            ? `Pinned as a launch input by task ${result.consumer_task_ids[0]}. These files cannot be purged while that task's record exists — purge the task itself first.`
            : `Pinned as a launch input by tasks ${result.consumer_task_ids.join(", ")}. These files cannot be purged while those tasks' records exist — purge the tasks themselves first.`}{" "}
          Cleaning a consumer up or archiving it releases nothing: its record
          still says it ran with these files.
        </p>
      )}
      {unfinishedExports.length > 0 && (
        <p className="resultsHint" data-testid="results-export-blockers">
          {unfinishedExports.length === 1
            ? "A checkout export of this revision has not finished."
            : `${unfinishedExports.length} checkout exports of this revision have not finished.`}{" "}
          These files cannot be purged until each one is resolved or closed —
          see the export history below. Unlike a launch input, that hold is
          temporary: a settled export releases it, because the copies it
          delivered are ordinary files in your checkout.
        </p>
      )}
      {result.state !== "purged" && result.state !== "capturing" && manifestId !== null && (
        <button
          type="button"
          className="resultsAction danger"
          disabled={
            pending !== null ||
            result.consumer_task_ids.length > 0 ||
            unfinishedExports.length > 0
          }
          onClick={purge}
          data-testid="results-purge"
        >
          {pending === "purging" ? "Purging…" : "Purge this revision"}
        </button>
      )}
    </div>
  );
}

function Revision({
  taskId,
  projectName,
  result,
  version,
  onUpdated,
}: {
  taskId: number;
  projectName: string;
  result: TaskResult;
  version: number;
  onUpdated: (projection: TaskResultsProjection) => void;
}) {
  const status = revisionStatus(result);
  return (
    <div className="resultsRevision" data-testid="results-revision">
      <div className="resultsRevisionHead">
        <span className={`resultsStatus ${status}`} data-testid="results-status">
          {REVISION_STATUS_LABEL[status]}
        </span>
        <code className="resultsId">{shortId(result.id)}</code>
        <span className="resultsRevisionMeta">
          {result.file_count} file{result.file_count === 1 ? "" : "s"} ·{" "}
          {formatBytes(result.total_bytes)} · {result.selection.join(", ")}
        </span>
      </div>
      {result.error && (
        <div className="resultsError" data-testid="results-revision-error">
          {result.error}
        </div>
      )}
      {result.unavailable_reason && (
        <div className="resultsError" data-testid="results-unavailable">
          Unavailable: {result.unavailable_reason}. This revision cannot be
          accepted or downloaded, and is never rebuilt from the workspace.
        </div>
      )}
      {result.purged_at !== null && (
        <p className="resultsHint" data-testid="results-purged">
          Purged {new Date(result.purged_at).toLocaleString()} by{" "}
          {result.purged_by ?? "operator"}. The record of what this revision was
          {result.accepted_at !== null ? ", and that it was accepted," : ""} is
          kept; its files are gone.
        </p>
      )}
      {result.accepted_at !== null && (
        <p className="resultsAccepted" data-testid="results-accepted">
          Accepted {new Date(result.accepted_at).toLocaleString()} by{" "}
          {result.accepted_by ?? "operator"}.
        </p>
      )}
      <Provenance result={result} />
      <FileViewer taskId={taskId} result={result} />
      <DiffView taskId={taskId} result={result} />
      <RevisionActions
        taskId={taskId}
        projectName={projectName}
        result={result}
        version={version}
        onUpdated={onUpdated}
      />
      <ResultExport
        taskId={taskId}
        result={result}
        version={version}
        onUpdated={onUpdated}
      />
    </div>
  );
}

export function ResultsPanel({
  task,
  projection,
  onProjection,
}: {
  task: Task;
  /** From daemon state, so a reconnect and a restart both restore what is
   * shown here without this component holding its own copy of the truth. */
  projection: TaskResultsProjection | undefined;
  onProjection: (projection: TaskResultsProjection) => void;
}) {
  const [limits, setLimits] = useState<TaskResultLimits | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  // Only the fixed bounds are fetched. Which revisions exist comes from daemon
  // state, which the snapshot and every delta already keep current — reading
  // the list here as well would give the panel a second, independently-timed
  // copy of the same truth, and a slow response could put a stale list back
  // over a newer one.
  useEffect(() => {
    let cancelled = false;
    listTaskResults(task.id)
      .then((response) => {
        if (!cancelled) setLimits(response.limits);
      })
      .catch(() => {
        /* The panel still renders from daemon state; limits stay unstated. */
      });
    return () => {
      cancelled = true;
    };
  }, [task.id]);

  const results = projection?.results ?? [];
  const version = projection?.version ?? 0;
  const active =
    results.find((result) => result.id === selected) ?? results[0] ?? null;
  const archived = task.state === "archived";

  return (
    <section className="panel resultsPanel" data-testid="task-detail-results">
      <h2 className="panelTitle">Results</h2>
      <p className="resultsHint">
        Files this task produced, kept outside its workspace. Capturing needs no
        commit, push, or pull request, and a captured result survives cleanup.
      </p>

      <CaptureForm
        taskId={task.id}
        limits={limits}
        disabled={archived}
        disabledReason={
          archived
            ? "This task has been cleaned up, so there is no workspace to capture from. Existing revisions below stay readable."
            : null
        }
        onCaptured={onProjection}
      />

      {results.length === 0 ? (
        <p className="resultsEmpty" data-testid="results-empty">
          No captured results. Workspace files are not retained until you
          capture them.
        </p>
      ) : (
        <>
          {results.length > 1 && (
            <ul className="resultsRevisionTabs" data-testid="results-revisions">
              {results.map((result) => (
                <li key={result.id}>
                  <button
                    type="button"
                    className={`resultsRevisionTab${active?.id === result.id ? " selected" : ""}`}
                    onClick={() => setSelected(result.id)}
                  >
                    <code>{shortId(result.id)}</code>{" "}
                    <span className={`resultsStatus ${revisionStatus(result)}`}>
                      {REVISION_STATUS_LABEL[revisionStatus(result)]}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {active && (
            <Revision
              key={active.id}
              taskId={task.id}
              projectName={task.project_name}
              result={active}
              version={version}
              onUpdated={onProjection}
            />
          )}
        </>
      )}
    </section>
  );
}
