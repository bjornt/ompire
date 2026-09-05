import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { previewTask, spawnTask } from "../lib/api";
import type { LaunchInput, LaunchPreview, WorkspaceOverridesInput } from "../lib/api";
import { PromptMentions } from "./PromptMentions";
import { useDaemonState } from "../lib/useDaemonState";
import {
  WORKSPACE_FIELDS,
  loadSpawnDraft,
  saveSpawnDraft,
  type SpawnDraft,
} from "../lib/spawnDraft";
import type { SpawnStepName, SpawnStepPayload, Task } from "../types";
import "./SpawnView.css";

const PIPELINE_STEPS: { name: SpawnStepName; label: string; detail: (task: Task) => string }[] = [
  { name: "fetch", label: "Fetch", detail: (t) => `git fetch (project ${t.project_name})` },
  { name: "clone", label: "Clone", detail: (t) => `git clone → ${t.clone_path}` },
  { name: "branch", label: "Branch", detail: (t) => `${t.branch} off origin base` },
  { name: "workshop", label: "Workshop", detail: () => "my-workshop: container + SDKs (can take a while)" },
  { name: "agent", label: "Agent", detail: () => "omp --mode rpc-ui: spawn + ready handshake" },
  { name: "prompt", label: "Prompt", detail: () => "deliver the stored prompt to the agent" },
];

/** An empty prompt skips the prompt step server-side; hide it too. */
function stepsFor(task: Task) {
  return task.prompt ? PIPELINE_STEPS : PIPELINE_STEPS.filter((s) => s.name !== "prompt");
}

type StepStatus = "pending" | "running" | "ok" | "failed";

function stepStatus(steps: SpawnStepPayload[], name: SpawnStepName): SpawnStepPayload | undefined {
  return [...steps].reverse().find((s) => s.step === name);
}

function statusOf(steps: SpawnStepPayload[], name: SpawnStepName): StepStatus {
  const last = stepStatus(steps, name);
  if (!last) return "pending";
  if (last.status === "started") return "running";
  return last.status;
}

/** The form owns one submission at a time. Every non-idle phase locks it; the
 * terminal edge is read from the daemon's task projection rather than from the
 * events that delivered it. */
type SpawnPhase =
  | { kind: "idle" }
  | { kind: "creating" }
  | { kind: "launching"; taskId: number }
  | { kind: "failed"; taskId: number };

const WORKSPACE_LABELS: Record<(typeof WORKSPACE_FIELDS)[number], string> = {
  base_branch: "Base branch",
  branch_pattern: "Branch pattern",
  workshop_additions: "Workshop additions",
  preamble: "Prompt preamble",
};

export function SpawnView() {
  const { snapshotReady, projects, modelProfiles, workflowCatalog, tasks, spawnProgress } =
    useDaemonState();
  const navigate = useNavigate();
  const location = useLocation();

  // The draft outlives a route unmount so a trip to Settings to create a
  // profile comes back to everything the operator typed. It is transient
  // frontend state, not authoritative registry data, so it lives here rather
  // than in a new server-side entity.
  const [draft, setDraft] = useState<SpawnDraft>(() =>
    loadSpawnDraft((location.state as { project?: string } | null)?.project),
  );
  const [phase, setPhase] = useState<SpawnPhase>({ kind: "idle" });
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [preview, setPreview] = useState<LaunchPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [staleReview, setStaleReview] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const submitLockRef = useRef(false);
  const seenRef = useRef(false);
  // Monotonic request id: a slow preview response must never replace a newer
  // draft's resolution.
  const previewGenerationRef = useRef(0);

  useEffect(() => saveSpawnDraft(draft), [draft]);

  const project = projects.find((p) => p.name === draft.project) ?? null;

  const update = useCallback((patch: Partial<SpawnDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
  }, []);

  const launchInput: LaunchInput | null = useMemo(() => {
    if (!draft.project || !draft.workflow || !draft.slug) return null;
    const overrides: WorkspaceOverridesInput = {};
    for (const field of WORKSPACE_FIELDS) {
      const value = draft.overrides[field];
      if (value !== undefined) {
        // An explicitly empty preamble is an override to "no preamble", so it
        // is sent as an empty string rather than dropped.
        overrides[field] = value as never;
      }
    }
    return {
      project_name: draft.project,
      workflow_name: draft.workflow,
      slug: draft.slug,
      prompt: draft.prompt,
      ...(draft.profile ? { model_profile: draft.profile } : {}),
      ...(Object.keys(overrides).length > 0 ? { workspace_overrides: overrides } : {}),
    };
  }, [draft]);

  // Every change to an effective choice re-resolves. Stale responses are
  // dropped by generation, so the rows on screen always describe the draft
  // as it stands.
  useEffect(() => {
    if (launchInput === null) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const generation = ++previewGenerationRef.current;
    let cancelled = false;
    previewTask(launchInput)
      .then((resolved) => {
        if (cancelled || generation !== previewGenerationRef.current) return;
        setPreview(resolved);
        setPreviewError(null);
        setStaleReview(false);
      })
      .catch((error: unknown) => {
        if (cancelled || generation !== previewGenerationRef.current) return;
        setPreview(null);
        setPreviewError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [launchInput]);

  const locked = phase.kind !== "idle";
  const spawnedId = phase.kind === "launching" || phase.kind === "failed" ? phase.taskId : null;
  const spawnedTask = spawnedId === null ? null : (tasks.find((t) => t.id === spawnedId) ?? null);
  const steps = spawnedId === null ? [] : (spawnProgress[spawnedId] ?? []);

  useEffect(() => {
    if (phase.kind !== "launching") return;
    const task = tasks.find((candidate) => candidate.id === phase.taskId);
    if (task === undefined) {
      if (!snapshotReady || !seenRef.current) return;
      submitLockRef.current = false;
      seenRef.current = false;
      setPhase({ kind: "idle" });
      setSubmitError(`Task ${phase.taskId} is no longer present — it was deleted or purged.`);
      return;
    }
    seenRef.current = true;
    if (task.spawn_completed_at === null) return;
    if (task.state === "failed") {
      setPhase({ kind: "failed", taskId: phase.taskId });
      return;
    }
    navigate(`/tasks/${phase.taskId}`, { replace: true });
  }, [phase, tasks, snapshotReady, navigate]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (launchInput === null || preview === null || submitLockRef.current) return;
    submitLockRef.current = true;
    seenRef.current = false;
    setSubmitError(null);
    setPhase({ kind: "creating" });
    try {
      const task = await spawnTask({ ...launchInput, preview_token: preview.preview_token });
      saveSpawnDraft(null);
      setPhase({ kind: "launching", taskId: task.id });
    } catch (error) {
      // Nothing was created, so the form is immediately usable again with
      // everything the operator typed still in place. A refused submission
      // never retries under different settings — a changed resolution is a
      // fresh review.
      submitLockRef.current = false;
      setPhase({ kind: "idle" });
      const message = error instanceof Error ? error.message : String(error);
      setSubmitError(message);
      if (message.includes("changed since it was previewed")) {
        setStaleReview(true);
        previewGenerationRef.current += 1;
        previewTask(launchInput)
          .then((resolved) => setPreview(resolved))
          .catch(() => undefined);
      }
    }
  }

  function startAnother() {
    submitLockRef.current = false;
    seenRef.current = false;
    setPhase({ kind: "idle" });
    setSubmitError(null);
  }

  const noProfiles = snapshotReady && modelProfiles.length === 0;
  const projectBlocked =
    project !== null &&
    (project.setup_state !== "ready" || project.launch_config_state !== "reconciled");

  return (
    <>
      <div className="headerRow">
        <h1>Spawn task</h1>
        <span className="subline">workflow + project + model profile → clone, container, agent</span>
      </div>

      <div className="spawnGrid">
        <form className="panel" onSubmit={onSubmit} data-testid="spawn-form">
          <h2 className="panelTitle">New task</h2>

          <div className="field">
            <label htmlFor="spawn-workflow">Workflow</label>
            <select
              id="spawn-workflow"
              value={draft.workflow}
              onChange={(e) => update({ workflow: e.target.value })}
              disabled={locked}
              data-testid="spawn-workflow"
            >
              <option value="">select a workflow</option>
              {workflowCatalog.map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.name} — {candidate.steps.length} step
                  {candidate.steps.length === 1 ? "" : "s"},{" "}
                  {candidate.sessions.length} session
                  {candidate.sessions.length === 1 ? "" : "s"}
                </option>
              ))}
            </select>
            <div className="hint">
              Every workflow is available to every ready project — no template setup.
            </div>
          </div>

          <div className="field">
            <label htmlFor="spawn-project">Project</label>
            <select
              id="spawn-project"
              value={draft.project}
              onChange={(e) => update({ project: e.target.value })}
              disabled={locked}
              data-testid="spawn-project"
            >
              <option value="">select a project</option>
              {projects.map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.name} — {candidate.checkout_path}
                  {candidate.setup_state !== "ready" ? ` · setup ${candidate.setup_state}` : ""}
                  {candidate.launch_config_state !== "reconciled"
                    ? " · needs reconciliation"
                    : ""}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="spawn-profile">Model profile</label>
            <select
              id="spawn-profile"
              value={draft.profile}
              onChange={(e) => update({ profile: e.target.value })}
              disabled={locked}
              data-testid="spawn-profile"
            >
              <option value="">
                {project?.default_model_profile
                  ? `inherit from project — ${project.default_model_profile}`
                  : "inherit from project — none set"}
              </option>
              {modelProfiles.map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.name} — {candidate.roles.default.model} ·{" "}
                  {candidate.roles.default.thinking}
                </option>
              ))}
            </select>
            {draft.profile !== "" && (
              <button
                className="linkButton"
                type="button"
                onClick={() => update({ profile: "" })}
                disabled={locked}
                data-testid="reset-profile"
              >
                Reset to project default
              </button>
            )}
            {noProfiles && (
              <div className="hint" data-testid="no-profiles">
                No model profiles exist yet, and nothing is inferred from a project or a
                credential. <Link to="/settings">Create one in Settings</Link> — this draft is
                kept for when you come back.
              </div>
            )}
          </div>

          <div className="field">
            <label htmlFor="spawn-slug">Task slug</label>
            <input
              id="spawn-slug"
              className="mono"
              type="text"
              value={draft.slug}
              onChange={(e) => update({ slug: e.target.value })}
              disabled={locked}
              placeholder="fix-the-bug"
            />
            {preview && (
              <>
                <div>
                  <span className="branchPreview" data-testid="branch-preview">
                    branch: {preview.branch} · off origin/{preview.workspace.base_branch}
                  </span>
                </div>
                <div className="hint">
                  clone → <code>~/tasks/{preview.project_name}/{draft.slug}</code>
                </div>
              </>
            )}
          </div>

          <div className="field">
            <label htmlFor="spawn-prompt">Prompt</label>
            <PromptMentions
              id="spawn-prompt"
              rows={9}
              value={draft.prompt}
              onChange={(value) => update({ prompt: value })}
              projectName={draft.project || null}
              disabled={locked}
              placeholder="What should the agent do? (delivered once the agent is ready)"
            />
            <div className="hint">
              Type <code>@</code> to attach a file from the project&apos;s repository.
            </div>
          </div>

          <details
            className="advanced"
            open={advancedOpen}
            onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
            data-testid="advanced"
          >
            <summary>Advanced — workspace overrides for this task</summary>
            {WORKSPACE_FIELDS.map((field) => {
              const overridden = draft.overrides[field] !== undefined;
              const inherited = preview?.inherited_workspace[field] ?? "";
              return (
                <div className="field" key={field}>
                  <label htmlFor={`spawn-${field}`}>{WORKSPACE_LABELS[field]}</label>
                  {field === "workshop_additions" ? (
                    <select
                      id={`spawn-${field}`}
                      value={overridden ? String(draft.overrides[field]) : ""}
                      onChange={(e) =>
                        update({
                          overrides: {
                            ...draft.overrides,
                            workshop_additions: e.target.value
                              ? (e.target.value as "project" | "global")
                              : undefined,
                          },
                        })
                      }
                      disabled={locked}
                    >
                      <option value="">inherit — {String(inherited)}</option>
                      <option value="project">project — this repository&apos;s additions</option>
                      <option value="global">global — your own additions file</option>
                    </select>
                  ) : field === "preamble" ? (
                    <textarea
                      id={`spawn-${field}`}
                      rows={3}
                      value={overridden ? String(draft.overrides[field]) : String(inherited)}
                      onChange={(e) =>
                        update({
                          overrides: { ...draft.overrides, preamble: e.target.value },
                        })
                      }
                      disabled={locked}
                    />
                  ) : (
                    <input
                      id={`spawn-${field}`}
                      className="mono"
                      type="text"
                      value={overridden ? String(draft.overrides[field]) : String(inherited)}
                      onChange={(e) =>
                        update({
                          overrides: { ...draft.overrides, [field]: e.target.value },
                        })
                      }
                      disabled={locked}
                    />
                  )}
                  {overridden ? (
                    <button
                      className="linkButton"
                      type="button"
                      disabled={locked}
                      data-testid={`reset-${field}`}
                      onClick={() =>
                        update({
                          overrides: { ...draft.overrides, [field]: undefined },
                        })
                      }
                    >
                      Reset to project default
                    </button>
                  ) : (
                    <div className="hint">inherited from {draft.project || "the project"}</div>
                  )}
                </div>
              );
            })}
          </details>

          <button
            className="primary"
            type="submit"
            disabled={locked || preview === null || projectBlocked}
          >
            {phase.kind === "creating"
              ? "Creating…"
              : phase.kind === "launching"
                ? "Launching…"
                : "Spawn task"}
          </button>
          {project && project.setup_state !== "ready" && (
            <div className="submitError" role="alert" data-testid="project-not-ready">
              {project.name}&apos;s checkout is {project.setup_state} — there is nothing to
              clone a workspace from yet. <Link to="/projects">Open Projects</Link>
            </div>
          )}
          {project && project.launch_config_state !== "reconciled" && (
            <div className="submitError" role="alert" data-testid="project-unreconciled">
              {project.name} still needs launch-configuration reconciliation after the
              template upgrade. <Link to="/projects">Resolve it in Projects</Link>
            </div>
          )}
          {previewError && (
            <div className="submitError" role="alert" data-testid="preview-error">
              {previewError}
            </div>
          )}
          {submitError && (
            <div className="submitError" role="alert">
              {submitError}
            </div>
          )}
        </form>

        <div className="panel" data-testid="spawn-progress">
          <h2 className="panelTitle">
            {spawnedTask
              ? `Launching · ${spawnedTask.project_name}/${spawnedTask.slug}`
              : "Before launch"}
          </h2>

          {spawnedTask === null && (
            <>
              {staleReview && (
                <div className="submitError" role="alert" data-testid="stale-review">
                  The configuration changed since you reviewed it. These are the current
                  choices — submit again to launch with them.
                </div>
              )}
              {preview === null ? (
                <p className="hint">
                  Choose a workflow, a project and a slug to see every step this run can
                  execute and the model each one would use.
                </p>
              ) : (
                <>
                  <div className="hint" data-testid="profile-source">
                    Profile <code>{preview.model_profile}</code>{" "}
                    {preview.model_profile_source === "task"
                      ? "— selected for this task"
                      : "— inherited from the project"}
                  </div>
                  <table className="stepTable" data-testid="step-preview">
                    <thead>
                      <tr>
                        <th>Step</th>
                        <th>Kind</th>
                        <th>Session</th>
                        <th>Role</th>
                        <th>Model</th>
                        <th>Thinking</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.steps.map((step) => (
                        <tr key={`${step.kind}-${step.step}`} data-testid={`step-${step.step}`}>
                          <td className="mono">
                            {step.step}
                            {step.conditional && (
                              <span className="conditional" title="a decision may route past this step">
                                {" "}
                                conditional
                              </span>
                            )}
                          </td>
                          <td>{step.kind}</td>
                          <td className="mono">{step.session ?? "—"}</td>
                          <td>{step.role ?? "—"}</td>
                          <td className="mono">{step.model ?? "—"}</td>
                          <td>{step.thinking ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="hint">
                    Every step this workflow declares, in order. A conditional step may be
                    routed past; the judge runs only when a route or outcome cannot be
                    resolved. Thinking is the policy you chose — omp may resolve{" "}
                    <code>auto</code> and <code>max</code> to a model-specific level.
                  </p>
                </>
              )}
            </>
          )}

          {spawnedTask !== null && (
            <div className="pipeline">
              {stepsFor(spawnedTask).map((stepDef, index, pipelineSteps) => {
                const status = statusOf(steps, stepDef.name);
                const failed = stepStatus(steps, stepDef.name);
                return (
                  <div className="step" key={stepDef.name} data-step-status={status}>
                    {index < pipelineSteps.length - 1 && <span className="rail" />}
                    <span className={`bullet ${status}`}>
                      {status === "ok" ? "✓" : status === "failed" ? "✕" : index + 1}
                    </span>
                    <div className="stepBody">
                      <div className={`stepLabel ${status}`}>{stepDef.label}</div>
                      <div className="stepDetail">{stepDef.detail(spawnedTask)}</div>
                      {status === "failed" && failed?.stderr && (
                        <pre className="stderr" data-testid={`stderr-${stepDef.name}`}>
                          {failed.stderr}
                        </pre>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {phase.kind === "failed" && spawnedTask && (
            <div className="failedNote" role="status" data-testid="spawn-failed">
              <div>
                Task landed as <span className="failedPill">failed</span>
                {spawnedTask.error ? ` — ${spawnedTask.error}` : ""}
              </div>
              <div className="failedActions">
                <Link
                  className="failedAction"
                  to={`/tasks/${spawnedTask.id}`}
                  data-testid="spawn-open-failed"
                >
                  Open failed task
                </Link>
                <button
                  className="failedAction"
                  type="button"
                  onClick={startAnother}
                  data-testid="spawn-start-another"
                >
                  Start another task
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
