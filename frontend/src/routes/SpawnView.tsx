import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { spawnTask } from "../lib/api";
import { useDaemonState } from "../lib/useDaemonState";
import { REGISTERED_WORKFLOWS, THINKING_LEVELS, templateCheckout } from "../lib/templates";
import type { SpawnStepName, SpawnStepPayload, Task, ThinkingLevel } from "../types";
import "./SpawnView.css";

const PIPELINE_STEPS: { name: SpawnStepName; label: string; detail: (task: Task) => string }[] = [
  { name: "fetch", label: "Fetch", detail: (t) => `git fetch origin (project ${t.project_name})` },
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

export function SpawnView() {
  const { snapshotReady, projects, templates, tasks, spawnProgress } = useDaemonState();
  const navigate = useNavigate();
  const [templateName, setTemplateName] = useState("");
  const [slug, setSlug] = useState("");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [thinking, setThinking] = useState<ThinkingLevel | "">("");
  const [phase, setPhase] = useState<SpawnPhase>({ kind: "idle" });
  const [submitError, setSubmitError] = useState<string | null>(null);
  // A second activation in the same tick must not reach the daemon: the
  // disabled button cannot stop it before React has re-rendered.
  const submitLockRef = useRef(false);
  // Absence means "gone" only once the projection has actually carried the
  // task — the REST response can win the race against `task_created`.
  const seenRef = useRef(false);

  const template = templates.find((t) => t.name === templateName) ?? templates[0];
  const branchPreview = useMemo(() => {
    if (!template || !slug) return null;
    return template.branch_pattern.replace("<slug>", slug);
  }, [template, slug]);
  const workflowLabel = template
    ? (REGISTERED_WORKFLOWS.find((w) => w.name === template.workflow)?.label ?? template.workflow)
    : null;

  const locked = phase.kind !== "idle";
  const spawnedId = phase.kind === "launching" || phase.kind === "failed" ? phase.taskId : null;
  const spawnedTask = spawnedId === null ? null : tasks.find((t) => t.id === spawnedId) ?? null;
  const steps = spawnedId === null ? [] : spawnProgress[spawnedId] ?? [];

  // `spawn_completed_at` is stamped for success and failure alike and `state`
  // separates them, so one projection read covers both terminal edges. Reading
  // state rather than events is what makes REST/`task_created` ordering and a
  // reconnect — which drops transient `spawn_step` events but not the task —
  // irrelevant here.
  useEffect(() => {
    if (phase.kind !== "launching") return;
    const task = tasks.find((candidate) => candidate.id === phase.taskId);
    if (task === undefined) {
      // Absence is snapshot-gated like the ship routes: never decide it from a
      // projection the current connection has not authoritatively replaced.
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
    // The workspace is ready and the run has been handed to the workflow
    // engine: the transcript is the earliest useful surface. Replace, because
    // the submitted form is spent.
    navigate(`/tasks/${phase.taskId}`, { replace: true });
  }, [phase, tasks, snapshotReady, navigate]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!template || submitLockRef.current) return;
    submitLockRef.current = true;
    seenRef.current = false;
    setSubmitError(null);
    setPhase({ kind: "creating" });
    // Empty overrides are omitted from the POST so the daemon falls back to
    // the template value, then to the omp default (task-spawn capability).
    const modelOverride = model.trim();
    try {
      const task = await spawnTask({
        template_name: template.name,
        slug,
        prompt,
        ...(modelOverride ? { model: modelOverride } : {}),
        ...(thinking ? { thinking } : {}),
      });
      setPhase({ kind: "launching", taskId: task.id });
    } catch (error) {
      // Nothing was created, so the form is immediately usable again with
      // everything the operator typed still in place.
      submitLockRef.current = false;
      setPhase({ kind: "idle" });
      setSubmitError(error instanceof Error ? error.message : String(error));
    }
  }

  function startAnother() {
    submitLockRef.current = false;
    seenRef.current = false;
    setPhase({ kind: "idle" });
    setSubmitError(null);
  }

  return (
    <>
      <div className="headerRow">
        <h1>Spawn task</h1>
        <span className="subline">clone → branch → workshop container → agent + prompt</span>
      </div>

      <div className="spawnGrid">
        <form className="panel" onSubmit={onSubmit} data-testid="spawn-form">
          <h2 className="panelTitle">New task</h2>

          <div className="field">
            <label htmlFor="spawn-template">Project template</label>
            <select
              id="spawn-template"
              value={template?.name ?? ""}
              onChange={(e) => setTemplateName(e.target.value)}
              disabled={locked}
              data-testid="spawn-template"
            >
              {templates.length === 0 ? (
                <option value="">no templates registered</option>
              ) : (
                templates.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.name} — {templateCheckout(t, projects)} · base {t.base_branch} ·{" "}
                    {t.model ?? "omp default"} · wf:{t.workflow}
                  </option>
                ))
              )}
            </select>
            <div className="hint">
              Preamble, workshop additions, omp settings and the workflow come from the template.{" "}
              <Link to="/settings">Edit templates</Link>
            </div>
          </div>

          {template && workflowLabel && (
            <div className="field">
              <span className="fieldLabel">Workflow — from template</span>
              <div className="workflowBlock" data-testid="workflow-block">
                {workflowLabel}
              </div>
            </div>
          )}

          <div className="field">
            <label htmlFor="spawn-slug">Task slug</label>
            <input
              id="spawn-slug"
              className="mono"
              type="text"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              disabled={locked}
              placeholder="fix-the-bug"
            />
            {branchPreview && template && (
              <>
                <div>
                  <span className="branchPreview" data-testid="branch-preview">
                    branch: {branchPreview} · off origin/{template.base_branch}
                  </span>
                </div>
                <div className="hint">
                  clone → <code>~/tasks/{template.project_name}/{slug}</code>
                </div>
              </>
            )}
          </div>

          <div className="field">
            <label htmlFor="spawn-prompt">Prompt</label>
            <textarea
              id="spawn-prompt"
              className="mono"
              rows={9}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              disabled={locked}
              placeholder="What should the agent do? (delivered once the agent is ready)"
            />
          </div>

          {template && (
            <div className="overrideGrid">
              <div className="field">
                <label htmlFor="spawn-model">Model override</label>
                <input
                  id="spawn-model"
                  className="mono"
                  type="text"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  disabled={locked}
                  placeholder={`template default (${template.model ?? "omp default"})`}
                />
              </div>
              <div className="field">
                <label htmlFor="spawn-thinking">Thinking</label>
                <select
                  id="spawn-thinking"
                  value={thinking}
                  onChange={(e) => setThinking(e.target.value as ThinkingLevel | "")}
                  disabled={locked}
                >
                  <option value="">
                    template default ({template.thinking ?? "omp default"})
                  </option>
                  {THINKING_LEVELS.map((level) => (
                    <option key={level} value={level}>
                      {level}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}

          <button className="primary" type="submit" disabled={locked || !template || !slug}>
            {phase.kind === "creating"
              ? "Creating…"
              : phase.kind === "launching"
                ? "Launching…"
                : "Spawn task"}
          </button>
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
              : "Pipeline"}
          </h2>
          {spawnedTask === null ? (
            <p className="hint">
              {phase.kind === "creating"
                ? "Creating the task…"
                : "Submit to run the spawn pipeline; each step reports here as it runs."}
            </p>
          ) : (
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
