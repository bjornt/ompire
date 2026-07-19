import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { spawnTask } from "../lib/api";
import { useDaemonState } from "../lib/daemonSocket";
import type { SpawnStepName, SpawnStepPayload, Task } from "../types";
import "./SpawnView.css";

const PIPELINE_STEPS: { name: SpawnStepName; label: string; detail: (task: Task) => string }[] = [
  { name: "fetch", label: "Fetch", detail: (t) => `git fetch origin (project ${t.project_name})` },
  { name: "clone", label: "Clone", detail: (t) => `git clone → ${t.clone_path}` },
  { name: "branch", label: "Branch", detail: (t) => `${t.branch} off origin base` },
  { name: "workshop", label: "Workshop", detail: () => "my-workshop: container + SDKs (can take a while)" },
];

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

export function SpawnView() {
  const { projects, tasks, spawnProgress } = useDaemonState();
  const [projectName, setProjectName] = useState("");
  const [slug, setSlug] = useState("");
  const [prompt, setPrompt] = useState("");
  const [spawnedId, setSpawnedId] = useState<number | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const project = projects.find((p) => p.name === projectName) ?? projects[0];
  const branchPreview = useMemo(() => {
    if (!project || !slug) return null;
    return project.branch_pattern.replace("<slug>", slug);
  }, [project, slug]);

  const spawnedTask = spawnedId === null ? null : tasks.find((t) => t.id === spawnedId) ?? null;
  const steps = spawnedId === null ? [] : spawnProgress[spawnedId] ?? [];

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!project) return;
    setSubmitError(null);
    try {
      const task = await spawnTask({ project_name: project.name, slug, prompt });
      setSpawnedId(task.id);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <>
      <div className="headerRow">
        <h1>Spawn task</h1>
        <span className="subline">clone → branch → workshop container (agents come in later chunks)</span>
      </div>

      <div className="spawnGrid">
        <form className="panel" onSubmit={onSubmit} data-testid="spawn-form">
          <h2 className="panelTitle">New task</h2>

          <div className="field">
            <label htmlFor="spawn-project">Project</label>
            <select
              id="spawn-project"
              value={project?.name ?? ""}
              onChange={(e) => setProjectName(e.target.value)}
            >
              {projects.length === 0 ? (
                <option value="">no projects registered</option>
              ) : (
                projects.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name} — {p.checkout_path} · base {p.base_branch}
                  </option>
                ))
              )}
            </select>
          </div>

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
            {branchPreview && project && (
              <>
                <div>
                  <span className="branchPreview" data-testid="branch-preview">
                    branch: {branchPreview} · off origin/{project.base_branch}
                  </span>
                </div>
                <div className="hint">
                  clone → <code>~/tasks/{project.name}/{slug}</code>
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
              placeholder="What should the agent do? (stored now, sent to the agent from chunk 7 on)"
            />
          </div>

          <button className="primary" type="submit" disabled={!project || !slug}>
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
              {PIPELINE_STEPS.map((stepDef, index) => {
                const status = statusOf(steps, stepDef.name);
                const failed = stepStatus(steps, stepDef.name);
                return (
                  <div className="step" key={stepDef.name} data-step-status={status}>
                    {index < PIPELINE_STEPS.length - 1 && <span className="rail" />}
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
          {spawnedTask?.state === "failed" && (
            <div className="failedNote">
              Task landed as <span className="failedPill">failed</span> —{" "}
              <Link to="/tasks">see it on the dashboard</Link>
            </div>
          )}
          {spawnedTask && spawnedTask.state === "created" && spawnedTask.spawn_completed_at && (
            <div className="doneNote">
              Workspace ready at <code>{spawnedTask.clone_path}</code> —{" "}
              <Link to="/tasks">back to Tasks</Link>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
