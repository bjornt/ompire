import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { spawnTask } from "../lib/api";
import { useDaemonState } from "../lib/useDaemonState";
import { REGISTERED_WORKFLOWS, THINKING_LEVELS, templateCheckout } from "../lib/templates";
import type { SpawnStepName, SpawnStepPayload, StepRecord, Task, ThinkingLevel } from "../types";
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

/** A workflow step record maps onto the pipeline's visual row states; a
 * parked gate reads as running (the row pulses like any in-flight step). */
function workflowRowStatus(record: StepRecord): StepStatus {
  return record.status === "waiting" ? "running" : record.status;
}

export function SpawnView() {
  const { projects, templates, tasks, spawnProgress, workflows } = useDaemonState();
  const [templateName, setTemplateName] = useState("");
  const [slug, setSlug] = useState("");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [thinking, setThinking] = useState<ThinkingLevel | "">("");
  const [spawnedId, setSpawnedId] = useState<number | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const template = templates.find((t) => t.name === templateName) ?? templates[0];
  const branchPreview = useMemo(() => {
    if (!template || !slug) return null;
    return template.branch_pattern.replace("<slug>", slug);
  }, [template, slug]);
  const workflowLabel = template
    ? (REGISTERED_WORKFLOWS.find((w) => w.name === template.workflow)?.label ?? template.workflow)
    : null;

  const spawnedTask = spawnedId === null ? null : tasks.find((t) => t.id === spawnedId) ?? null;
  const steps = spawnedId === null ? [] : spawnProgress[spawnedId] ?? [];
  // Once the spawn pipeline hands off, the workflow run's `workflow_step`
  // events keep reporting in the same inline list (task-spawn spec).
  const workflowSteps = spawnedId === null ? [] : (workflows[spawnedId]?.steps ?? []);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!template) return;
    setSubmitError(null);
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
      setSpawnedId(task.id);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error));
    }
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
                  placeholder={`template default (${template.model ?? "omp default"})`}
                />
              </div>
              <div className="field">
                <label htmlFor="spawn-thinking">Thinking</label>
                <select
                  id="spawn-thinking"
                  value={thinking}
                  onChange={(e) => setThinking(e.target.value as ThinkingLevel | "")}
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

          <button className="primary" type="submit" disabled={!template || !slug}>
            Spawn task
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
              Submit to run the spawn pipeline; each step reports here as it runs.
            </p>
          ) : (
            <div className="pipeline">
              {stepsFor(spawnedTask).map((stepDef, index, pipelineSteps) => {
                const status = statusOf(steps, stepDef.name);
                const failed = stepStatus(steps, stepDef.name);
                return (
                  <div className="step" key={stepDef.name} data-step-status={status}>
                    {(index < pipelineSteps.length - 1 || workflowSteps.length > 0) && (
                      <span className="rail" />
                    )}
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
              {workflowSteps.map((record, index) => {
                const status = workflowRowStatus(record);
                return (
                  <div
                    className="step"
                    key={`workflow-${record.seq}`}
                    data-step-status={status}
                    data-testid={`workflow-step-${record.step}`}
                  >
                    {index < workflowSteps.length - 1 && <span className="rail" />}
                    <span className={`bullet ${status}`}>
                      {status === "ok" ? "✓" : status === "failed" ? "✕" : "▸"}
                    </span>
                    <div className="stepBody">
                      <div className={`stepLabel ${status}`}>
                        {record.step}
                        {record.status === "waiting" ? " — waiting at gate" : ""}
                      </div>
                      <div className="stepDetail">
                        {record.kind}
                        {record.session ? ` · session ${record.session}` : ""}
                      </div>
                      {record.status === "failed" && record.error && (
                        <pre className="stderr" data-testid={`stderr-workflow-${record.step}`}>
                          {record.error}
                        </pre>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {spawnedTask?.state === "failed" && (
            <div className="failedNote">
              Task landed as <span className="failedPill">failed</span> —{" "}
              <Link to="/tasks">see it on the dashboard</Link>
            </div>
          )}
          {spawnedTask && spawnedTask.state === "created" && spawnedTask.spawn_completed_at && (
            <div className="doneNote">
              Agent launched in <code>{spawnedTask.clone_path}</code>
              {spawnedTask.prompt ? " — working on the prompt" : ""} —{" "}
              <Link to="/tasks">back to Tasks</Link>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
