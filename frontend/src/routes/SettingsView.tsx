import { useState } from "react";
import {
  createTemplate,
  deleteTemplate,
  updateTemplate,
  type TemplateInput,
} from "../lib/api";
import { useDaemonState } from "../lib/daemonSocket";
import { REGISTERED_WORKFLOWS, THINKING_LEVELS, templateCheckout } from "../lib/templates";
import type { Project, Template, ThinkingLevel } from "../types";
import "./SettingsView.css";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

type EditorState = { mode: "create" } | { mode: "edit"; name: string };

function TemplateRow({
  template,
  projects,
  editing,
  onToggleEdit,
}: {
  template: Template;
  projects: Project[];
  editing: boolean;
  onToggleEdit: () => void;
}) {
  return (
    <div
      className={editing ? "templateRow editing" : "templateRow"}
      data-testid={`template-row-${template.name}`}
    >
      <span className="templateName">{template.name}</span>
      <span className="templateSummary">
        {templateCheckout(template, projects)} · {template.base_branch} · {template.branch_pattern}{" "}
        · {template.model ?? "omp default"} · wf:{template.workflow}
      </span>
      <button
        type="button"
        className={editing ? "editingToggle" : "editToggle"}
        onClick={onToggleEdit}
      >
        {editing ? "Editing…" : "Edit"}
      </button>
    </div>
  );
}

function TemplateEditor({
  projects,
  existing,
  onClose,
}: {
  projects: Project[];
  /** Present in edit mode; absent in create mode. */
  existing?: Template;
  onClose: () => void;
}) {
  const [name, setName] = useState(existing?.name ?? "");
  const [projectName, setProjectName] = useState(
    existing?.project_name ?? projects[0]?.name ?? "",
  );
  const [baseBranch, setBaseBranch] = useState(existing?.base_branch ?? "main");
  // The daemon's shipped default_branch_pattern; the daemon still validates.
  const [branchPattern, setBranchPattern] = useState(
    existing?.branch_pattern ?? "ompire/<slug>",
  );
  const [workflow, setWorkflow] = useState(existing?.workflow ?? REGISTERED_WORKFLOWS[0].name);
  const [workshopAdditions, setWorkshopAdditions] = useState<"project" | "global">(
    existing?.workshop_additions ?? "project",
  );
  const [model, setModel] = useState(existing?.model ?? "");
  const [thinking, setThinking] = useState<ThinkingLevel | "">(existing?.thinking ?? "");
  const [preamble, setPreamble] = useState(existing?.preamble ?? "");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const pickedProject = projects.find((p) => p.name === projectName);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    const fields: TemplateInput = {
      project_name: projectName,
      base_branch: baseBranch.trim(),
      branch_pattern: branchPattern.trim(),
      workflow,
      workshop_additions: workshopAdditions,
      model: model.trim() || null,
      thinking: thinking === "" ? null : thinking,
      preamble,
    };
    try {
      if (existing === undefined) {
        await createTemplate({ name: name.trim(), ...fields });
      } else {
        await updateTemplate(existing.name, fields);
      }
      // The row updates from the daemon's broadcast, not the response.
      onClose();
    } catch (err) {
      // 409 (duplicate name / referenced template) and 422 (invalid field)
      // both land here with the daemon's detail; the editor stays open.
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function onRemove() {
    if (existing === undefined) return;
    if (
      !window.confirm(
        `Remove template ${existing.name}?\n\nTasks that already used it keep the name as history.`,
      )
    ) {
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await deleteTemplate(existing.name);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="panel templateEditor" data-testid="template-editor">
      <h2 className="panelTitle">
        {existing === undefined ? "New template" : `Template · ${existing.name}`}
      </h2>
      <form onSubmit={onSubmit}>
        {existing === undefined && (
          <label className="formField">
            <span className="fieldLabel">
              Name <span className="fieldHint">— short id, picked at spawn</span>
            </span>
            <input
              className="mono"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. maas"
              required
              data-testid="template-name"
            />
          </label>
        )}

        <label className="formField">
          <span className="fieldLabel">
            Project <span className="fieldHint">— checkout and remotes come from it</span>
          </span>
          <select
            value={projectName}
            onChange={(e) => setProjectName(e.target.value)}
            data-testid="template-project"
          >
            {projects.length === 0 ? (
              <option value="">no projects registered</option>
            ) : (
              projects.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))
            )}
          </select>
          {pickedProject && (
            <span className="derivedNote" data-testid="template-project-derived">
              checkout {pickedProject.checkout_path} · remote {pickedProject.upstream_url}
            </span>
          )}
        </label>

        <div className="formGrid formGridPair">
          <label className="formField">
            <span className="fieldLabel">Base branch</span>
            <input
              className="mono"
              value={baseBranch}
              onChange={(e) => setBaseBranch(e.target.value)}
              required
              data-testid="template-base-branch"
            />
          </label>
          <label className="formField">
            <span className="fieldLabel">
              Branch pattern <span className="fieldHint">— exactly one &lt;slug&gt;</span>
            </span>
            <input
              className="mono"
              value={branchPattern}
              onChange={(e) => setBranchPattern(e.target.value)}
              required
              data-testid="template-branch-pattern"
            />
          </label>
        </div>

        <label className="formField">
          <span className="fieldLabel">Workflow</span>
          <select
            value={workflow}
            onChange={(e) => setWorkflow(e.target.value)}
            data-testid="template-workflow"
          >
            {REGISTERED_WORKFLOWS.map((wf) => (
              <option key={wf.name} value={wf.name}>
                {wf.label}
              </option>
            ))}
          </select>
          <span className="fieldNote">
            Workflows are Python in the daemon — the template only picks one.
          </span>
        </label>

        <label className="formField">
          <span className="fieldLabel">Workshop additions</span>
          <select
            value={workshopAdditions}
            onChange={(e) => setWorkshopAdditions(e.target.value as "project" | "global")}
            data-testid="template-workshop"
          >
            <option value="project">project workshop.my.yaml</option>
            <option value="global">global ~/.config/my-workshop/my.yaml</option>
          </select>
          <span className="fieldNote">Injects omp SDK, omp-home mount, pi-auth-gateway tunnel.</span>
        </label>

        <div className="formGrid formGridPair">
          <label className="formField">
            <span className="fieldLabel">
              Model <span className="fieldHint">— omp fuzzy-matches</span>
            </span>
            <input
              className="mono"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="omp default"
              data-testid="template-model"
            />
          </label>
          <label className="formField">
            <span className="fieldLabel">Thinking</span>
            <select
              value={thinking}
              onChange={(e) => setThinking(e.target.value as ThinkingLevel | "")}
              data-testid="template-thinking"
            >
              <option value="">omp default</option>
              {THINKING_LEVELS.map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
          </label>
        </div>

        <label className="formField">
          <span className="fieldLabel">
            Prompt preamble <span className="fieldHint">— prepended to every spawn's prompt</span>
          </span>
          <textarea
            className="mono"
            rows={4}
            value={preamble}
            onChange={(e) => setPreamble(e.target.value)}
            data-testid="template-preamble"
          />
        </label>

        <div className="formActions">
          <button
            type="submit"
            className="primaryButton"
            disabled={submitting || projects.length === 0}
          >
            {existing === undefined ? "Create template" : "Save template"}
          </button>
          <button type="button" className="ghostButton" onClick={onClose}>
            {existing === undefined ? "Discard" : "Discard changes"}
          </button>
          {existing !== undefined && (
            <>
              <span className="spacer" />
              <button
                type="button"
                className="removeButton"
                onClick={() => void onRemove()}
                disabled={submitting}
                data-testid={`remove-template-${existing.name}`}
              >
                Remove template…
              </button>
            </>
          )}
        </div>
        {error && (
          <div className="submitError" role="alert" data-testid="template-editor-error">
            {error}
          </div>
        )}
      </form>
    </section>
  );
}

export function SettingsView() {
  const { projects, templates } = useDaemonState();
  const [editor, setEditor] = useState<EditorState | null>(null);
  const sorted = [...templates].sort((a, b) => a.name.localeCompare(b.name));
  const editingTemplate =
    editor?.mode === "edit" ? templates.find((t) => t.name === editor.name) : undefined;

  return (
    <div className="settingsMain">
      <div className="headerRow">
        <h1>Templates &amp; settings</h1>
        <span className="subline">what spawn needs, and how attention reaches you</span>
      </div>

      {/* Two-column grid per the mockup; the right column (notifications,
       * watchdogs, daemon panel) is ROADMAP chunk 20 and slots in here. */}
      <div className="settingsGrid">
        <div className="settingsColumn">
          <section className="panel" data-testid="templates-panel">
            <h2 className="panelTitle">Project templates</h2>
            {sorted.length === 0 ? (
              <div className="empty templatesEmpty" data-testid="templates-empty-state">
                <strong>No templates yet</strong>
                <span>
                  A template carries everything spawn needs — project, branches, workflow, omp
                  settings.
                </span>
              </div>
            ) : (
              <div className="templateList" data-testid="templates-list">
                {sorted.map((template) => (
                  <TemplateRow
                    key={template.name}
                    template={template}
                    projects={projects}
                    editing={editor?.mode === "edit" && editor.name === template.name}
                    onToggleEdit={() =>
                      setEditor((current) =>
                        current?.mode === "edit" && current.name === template.name
                          ? null
                          : { mode: "edit", name: template.name },
                      )
                    }
                  />
                ))}
              </div>
            )}
            <button
              type="button"
              className="newTemplateButton"
              onClick={() =>
                setEditor((current) => (current?.mode === "create" ? null : { mode: "create" }))
              }
              data-testid="new-template-toggle"
            >
              New template
            </button>
          </section>

          {editor?.mode === "create" && (
            <TemplateEditor projects={projects} onClose={() => setEditor(null)} />
          )}
          {editor?.mode === "edit" && editingTemplate !== undefined && (
            <TemplateEditor
              projects={projects}
              existing={editingTemplate}
              onClose={() => setEditor(null)}
            />
          )}
        </div>
      </div>
    </div>
  );
}
