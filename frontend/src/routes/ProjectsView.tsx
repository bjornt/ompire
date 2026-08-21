import { useState } from "react";
import { Link } from "react-router-dom";
import { createProject, deleteProject, updateProject } from "../lib/api";
import { useDaemonState } from "../lib/useDaemonState";
import type { Project, Task } from "../types";
import "./ProjectsView.css";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function NewProjectForm({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [upstream, setUpstream] = useState("");
  const [fork, setFork] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await createProject({
        name: name.trim(),
        title: title.trim(),
        upstream_url: upstream.trim(),
        fork_url: fork.trim() || null,
      });
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

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
              data-testid="new-project-fork"
            />
          </label>
        </div>
        <div className="formActions">
          <button type="submit" className="primaryButton" disabled={submitting}>
            Create project
          </button>
          <button type="button" className="ghostButton" onClick={onClose}>
            Cancel
          </button>
          <span className="cliHint">omp project add &lt;name&gt; --upstream … --fork …</span>
        </div>
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
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // The UI guard mirrors the daemon's: rename is only offered while no task
  // row (any state) references the project; the daemon's 409 stays
  // authoritative against races.
  const renameBlocked = referencingCount > 0;

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    const newName = name.trim();
    try {
      await updateProject(project.name, {
        title: title.trim(),
        upstream_url: upstream.trim(),
        fork_url: fork.trim() || null,
        checkout_path: project.checkout_path,
        ...(!renameBlocked && newName !== project.name ? { new_name: newName } : {}),
      });
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
    setError(null);
    setSubmitting(true);
    try {
      await deleteProject(project.name);
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
            disabled={renameBlocked}
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
            data-testid={`edit-upstream-${project.name}`}
          />
        </label>
        <label className="formField">
          <span className="fieldLabel">Your fork</span>
          <input
            className="mono urlInput"
            value={fork}
            onChange={(e) => setFork(e.target.value)}
            data-testid={`edit-fork-${project.name}`}
          />
        </label>
      </div>
      <div className="formActions">
        <button type="submit" className="primaryButton" disabled={submitting}>
          Save
        </button>
        <button type="button" className="ghostButton" onClick={onClose}>
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
