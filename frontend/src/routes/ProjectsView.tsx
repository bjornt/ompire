import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { createProject, deleteProject, updateProject } from "../lib/api";
import { useDaemonReconcile, useDaemonState } from "../lib/useDaemonState";
import type { Project, Task } from "../types";
import "./ProjectsView.css";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** A `201`/`200` body is authoritative only if it is actually a project. A
 * malformed one must surface as a failure rather than becoming an unusable
 * card. */
function asProject(value: unknown): Project | null {
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as Partial<Project>;
  if (typeof candidate.name !== "string" || candidate.name === "") return null;
  return candidate as Project;
}

const MALFORMED_PROJECT_RESPONSE =
  "The daemon returned an unusable project record. Nothing was added — check the daemon log and try again.";

function NewProjectForm({ onClose }: { onClose: () => void }) {
  const { projects } = useDaemonState();
  const reconcile = useDaemonReconcile();
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [upstream, setUpstream] = useState("");
  const [fork, setFork] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [createdName, setCreatedName] = useState<string | null>(null);

  // The confirmed state ends when the project is really in daemon state, not
  // on a timer: the form never closes into a list that lacks the new card.
  const confirmed = createdName !== null && projects.some((p) => p.name === createdName);
  useEffect(() => {
    if (confirmed) onClose();
  }, [confirmed, onClose]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting || createdName !== null) return;
    setError(null);
    setSubmitting(true);
    try {
      const created = asProject(
        await createProject({
          name: name.trim(),
          title: title.trim(),
          upstream_url: upstream.trim(),
          fork_url: fork.trim() || null,
        }),
      );
      if (created === null) {
        setError(MALFORMED_PROJECT_RESPONSE);
        return;
      }
      // Feed the daemon's own answer into daemon state through the same
      // reducer case `project_created` uses, so the card is present now
      // rather than whenever the event lands.
      reconcile("project_created", created);
      setCreatedName(created.name);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

  const busy = submitting || createdName !== null;

  return (
    <section className="newProject" data-testid="new-project-form">
      <div className="newProjectTitle">New project</div>
      <form onSubmit={onSubmit}>
        <div className="formGrid formGridNameTitle">
          <label className="formField">
            <span className="fieldLabel">
              Name <span className="fieldHint">— short id, used in task cards</span>
            </span>
            <input
              className="mono"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. maas"
              required
              disabled={busy}
              data-testid="new-project-name"
            />
          </label>
          <label className="formField">
            <span className="fieldLabel">Title</span>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Human-readable description"
              required
              disabled={busy}
              data-testid="new-project-title"
            />
          </label>
        </div>
        <div className="formGrid formGridUrls">
          <label className="formField">
            <span className="fieldLabel">
              Upstream git URL <span className="fieldHint">— where PRs land</span>
            </span>
            <input
              className="mono urlInput"
              value={upstream}
              onChange={(e) => setUpstream(e.target.value)}
              placeholder="https://github.com/org/repo.git"
              required
              disabled={busy}
              data-testid="new-project-upstream"
            />
          </label>
          <label className="formField">
            <span className="fieldLabel">
              Your fork <span className="fieldHint">— optional; branches push here</span>
            </span>
            <input
              className="mono urlInput"
              value={fork}
              onChange={(e) => setFork(e.target.value)}
              placeholder="git@github.com:you/repo.git"
              disabled={busy}
              data-testid="new-project-fork"
            />
          </label>
        </div>
        <div className="formActions">
          <button
            type="submit"
            className="primaryButton"
            disabled={busy}
            data-testid="new-project-submit"
          >
            {createdName !== null ? "Created" : submitting ? "Creating…" : "Create project"}
          </button>
          {/* Only the in-flight request locks Cancel: the confirmed state is
              gated on daemon state rather than a timer, so the operator keeps a
              way out even if that state never arrives. */}
          <button type="button" className="ghostButton" onClick={onClose} disabled={submitting}>
            Cancel
          </button>
          <span className="cliHint">omp project add &lt;name&gt; --upstream … --fork …</span>
        </div>
        {createdName !== null && (
          <div className="submitConfirmed" role="status" data-testid="new-project-confirmed">
            Created {createdName}.
          </div>
        )}
        {error && (
          <div className="submitError" role="alert" data-testid="new-project-error">
            {error}
          </div>
        )}
      </form>
    </section>
  );
}

function ProjectEditPanel({
  project,
  referencingCount,
  onClose,
}: {
  project: Project;
  referencingCount: number;
  onClose: () => void;
}) {
  const [name, setName] = useState(project.name);
  const [title, setTitle] = useState(project.title);
  const [upstream, setUpstream] = useState(project.upstream_url);
  const [fork, setFork] = useState(project.fork_url ?? "");
  const reconcile = useDaemonReconcile();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // The UI guard mirrors the daemon's: rename is only offered while no task
  // row (any state) references the project; the daemon's 409 stays
  // authoritative against races.
  const renameBlocked = referencingCount > 0;

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    const newName = name.trim();
    const renaming = !renameBlocked && newName !== project.name;
    try {
      const saved = asProject(
        await updateProject(project.name, {
          title: title.trim(),
          upstream_url: upstream.trim(),
          fork_url: fork.trim() || null,
          checkout_path: project.checkout_path,
          ...(renaming ? { new_name: newName } : {}),
        }),
      );
      if (saved === null) {
        setError(MALFORMED_PROJECT_RESPONSE);
        return;
      }
      // A rename changes the key the list matches on, so it reconciles through
      // the same `project_renamed` shape the event carries.
      if (renaming) reconcile("project_renamed", { old_name: project.name, project: saved });
      else reconcile("project_updated", saved);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function onRemove() {
    if (
      !window.confirm(
        `Remove project ${project.name}?\n\nThis removes the registry entry. Clones on disk are not touched.`,
      )
    ) {
      return;
    }
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      await deleteProject(project.name);
      reconcile("project_deleted", { name: project.name });
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="editPanel" onSubmit={onSave} data-testid={`edit-panel-${project.name}`}>
      <div className="formGrid formGridNameTitle">
        <label className="formField">
          <span className="fieldLabel">Name</span>
          <input
            className="mono"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={renameBlocked || submitting}
            required
            data-testid={`edit-name-${project.name}`}
          />
          {renameBlocked && (
            <span className="renameNote" data-testid={`rename-note-${project.name}`}>
              Referenced by {referencingCount} tasks — rename via <code>omp project rename</code>
            </span>
          )}
        </label>
        <label className="formField">
          <span className="fieldLabel">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            disabled={submitting}
            data-testid={`edit-title-${project.name}`}
          />
        </label>
      </div>
      <div className="formGrid formGridUrls">
        <label className="formField">
          <span className="fieldLabel">Upstream git URL</span>
          <input
            className="mono urlInput"
            value={upstream}
            onChange={(e) => setUpstream(e.target.value)}
            required
            disabled={submitting}
            data-testid={`edit-upstream-${project.name}`}
          />
        </label>
        <label className="formField">
          <span className="fieldLabel">Your fork</span>
          <input
            className="mono urlInput"
            value={fork}
            onChange={(e) => setFork(e.target.value)}
            disabled={submitting}
            data-testid={`edit-fork-${project.name}`}
          />
        </label>
      </div>
      <div className="formActions">
        <button
          type="submit"
          className="primaryButton"
          disabled={submitting}
          data-testid={`edit-save-${project.name}`}
        >
          {submitting ? "Saving…" : "Save"}
        </button>
        <button type="button" className="ghostButton" onClick={onClose} disabled={submitting}>
          Cancel
        </button>
        <span className="spacer" />
        <button
          type="button"
          className="removeButton"
          onClick={() => void onRemove()}
          disabled={submitting}
          data-testid={`remove-project-${project.name}`}
        >
          Remove project…
        </button>
      </div>
      {error && (
        <div className="submitError" role="alert" data-testid={`edit-error-${project.name}`}>
          {error}
        </div>
      )}
    </form>
  );
}

function ProjectCard({
  project,
  tasks,
  editing,
  onToggleEdit,
}: {
  project: Project;
  tasks: Task[];
  editing: boolean;
  onToggleEdit: () => void;
}) {
  const activeCount = tasks.filter(
    (t) => t.project_name === project.name && t.state !== "archived",
  ).length;
  const referencingCount = tasks.filter((t) => t.project_name === project.name).length;

  return (
    <article className="projectCard" data-testid={`project-card-${project.name}`}>
      <div className="projectCardHead">
        <span className="projectName">{project.name}</span>
        <span className="projectTitle">{project.title}</span>
        <span className="spacer" />
        <Link
          to={`/tasks?project=${encodeURIComponent(project.name)}`}
          className="activeTasksPill"
          data-testid={`active-tasks-${project.name}`}
        >
          {activeCount} active {activeCount === 1 ? "task" : "tasks"}
        </Link>
        <button type="button" className="editButton" onClick={onToggleEdit}>
          Edit
        </button>
      </div>
      <div className="projectMeta">
        <span className="metaLabel">upstream</span>
        <span className="metaValue">
          {project.upstream_url}
          {project.fork_url === null && (
            <span className="noForkNote"> · you own upstream — no fork needed</span>
          )}
        </span>
        {project.fork_url !== null && (
          <>
            <span className="metaLabel">fork</span>
            <span className="metaValue">{project.fork_url}</span>
          </>
        )}
      </div>
      {editing && (
        <ProjectEditPanel
          project={project}
          referencingCount={referencingCount}
          onClose={onToggleEdit}
        />
      )}
    </article>
  );
}

export function ProjectsView() {
  const { projects, tasks } = useDaemonState();
  const [newOpen, setNewOpen] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const sorted = [...projects].sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div className="projectsMain">
      <div className="headerRow">
        <h1>Projects</h1>
        <span className="subline">
          {projects.length} projects · a project names the repo pair tasks run against
        </span>
        <span className="spacer" />
        <button
          type="button"
          className="primaryButton"
          onClick={() => setNewOpen((v) => !v)}
          data-testid="new-project-toggle"
        >
          New project
        </button>
      </div>

      {newOpen && <NewProjectForm onClose={() => setNewOpen(false)} />}

      {sorted.length === 0 ? (
        <div className="empty" data-testid="projects-empty-state">
          <strong>No projects yet</strong>
          <span>Create one to register the repo pair tasks run against.</span>
        </div>
      ) : (
        <div className="projectList" data-testid="projects-list">
          {sorted.map((project) => (
            <ProjectCard
              key={project.name}
              project={project}
              tasks={tasks}
              editing={editingName === project.name}
              onToggleEdit={() =>
                setEditingName((current) => (current === project.name ? null : project.name))
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}
