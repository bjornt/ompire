import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  createProject,
  deleteProject,
  inspectCheckout,
  retryProjectSetup,
  updateProject,
} from "../lib/api";
import { useDaemonReconcile, useDaemonState } from "../lib/useDaemonState";
import type { CheckoutInspection, Project, ProjectSetupStep, Task } from "../types";
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

const SETUP_LABEL: Record<Project["setup_state"], string> = {
  ready: "ready",
  cloning: "cloning…",
  failed: "setup failed",
};

/** The last step event for a project, which is what "cloning…" should say. */
function currentStep(steps: ProjectSetupStep[] | undefined): ProjectSetupStep | null {
  return steps && steps.length > 0 ? steps[steps.length - 1] : null;
}

/** Confirm and unregister. Returns false when the operator backed out.
 *
 * One implementation for both places removal is offered, so the promise that
 * the checkout survives is made in exactly one sentence (ADR-0022).
 */
async function confirmAndRemove(
  project: Project,
  reconcile: ReturnType<typeof useDaemonReconcile>,
): Promise<boolean> {
  if (
    !window.confirm(
      `Remove project ${project.name}?\n\nThis removes the registry entry only. ` +
        `The checkout at ${project.checkout_path} stays on disk, whether you ` +
        `registered it or Ompire cloned it.`,
    )
  ) {
    return false;
  }
  await deleteProject(project.name);
  reconcile("project_deleted", { name: project.name });
  return true;
}

function NewProjectForm({ onClose }: { onClose: () => void }) {
  const { projects, settings } = useDaemonState();
  const reconcile = useDaemonReconcile();
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [upstream, setUpstream] = useState("");
  const [fork, setFork] = useState("");
  const [mode, setMode] = useState<"adopt" | "clone">("adopt");
  const [checkoutPath, setCheckoutPath] = useState("");
  const [fetchRemote, setFetchRemote] = useState("origin");
  const [inspection, setInspection] = useState<CheckoutInspection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [createdName, setCreatedName] = useState<string | null>(null);
  const inspectSeq = useRef(0);

  // The confirmed state ends when the project is really in daemon state, not
  // on a timer: the form never closes into a list that lacks the new card.
  const confirmed = createdName !== null && projects.some((p) => p.name === createdName);
  useEffect(() => {
    if (confirmed) onClose();
  }, [confirmed, onClose]);

  const checkoutRoot =
    typeof settings.checkout_root === "string" ? settings.checkout_root : "";
  const derivedPath = name.trim() ? `${checkoutRoot}/${name.trim()}` : `${checkoutRoot}/…`;

  /** Discard the current inspection *and* any in-flight one.
   *
   * Clearing the state alone is not enough: clicking the mode radio blurs the
   * path field first, which starts an inspection whose result would land
   * afterwards and restore the message.
   */
  function cancelInspection() {
    inspectSeq.current += 1;
    setInspection(null);
  }

  /** Look at what the operator typed, and offer what it found. Never applies
   * a detected value on its own — the operator confirms. */
  async function onInspect() {
    const seq = ++inspectSeq.current;
    try {
      const result = await inspectCheckout({
        checkout_path: checkoutPath.trim(),
        fetch_remote: fetchRemote.trim() || "origin",
      });
      if (seq !== inspectSeq.current) return;
      setInspection(result);
      if (result.ok) {
        if (!upstream.trim() && result.suggested_upstream)
          setUpstream(result.suggested_upstream);
        if (!fork.trim() && result.suggested_fork) setFork(result.suggested_fork);
      }
    } catch (err) {
      if (seq === inspectSeq.current) setError(errorText(err));
    }
  }

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
          checkout_mode: mode,
          ...(mode === "adopt"
            ? {
                checkout_path: checkoutPath.trim() || null,
                fetch_remote: fetchRemote.trim() || "origin",
              }
            : {}),
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
        <fieldset className="modeChoice" disabled={busy}>
          <legend className="fieldLabel">Base checkout</legend>
          <label className="modeOption">
            <input
              type="radio"
              name="checkout-mode"
              checked={mode === "adopt"}
              onChange={() => {
                setMode("adopt");
                cancelInspection();
              }}
              data-testid="new-project-mode-adopt"
            />
            <span>
              Use an existing checkout
              <span className="fieldHint"> — Ompire only reads it</span>
            </span>
          </label>
          <label className="modeOption">
            <input
              type="radio"
              name="checkout-mode"
              checked={mode === "clone"}
              onChange={() => {
                // The inspection describes a checkout path clone mode does
                // not use; leaving it up would explain the wrong thing.
                setMode("clone");
                cancelInspection();
              }}
              data-testid="new-project-mode-clone"
            />
            <span>
              Clone it for me
              <span className="fieldHint"> — into the checkout root</span>
            </span>
          </label>
        </fieldset>
        {mode === "adopt" ? (
          <div className="formGrid formGridUrls">
            <label className="formField">
              <span className="fieldLabel">
                Checkout path <span className="fieldHint">— absolute; must already exist</span>
              </span>
              <input
                className="mono urlInput"
                value={checkoutPath}
                onChange={(e) => {
                  setCheckoutPath(e.target.value);
                  cancelInspection();
                }}
                onBlur={() => {
                  if (checkoutPath.trim()) void onInspect();
                }}
                placeholder={`${checkoutRoot}/repo`}
                disabled={busy}
                data-testid="new-project-checkout-path"
              />
            </label>
            <label className="formField">
              <span className="fieldLabel">
                Fetch remote <span className="fieldHint">— in that checkout</span>
              </span>
              <input
                className="mono"
                value={fetchRemote}
                onChange={(e) => {
                  setFetchRemote(e.target.value);
                  cancelInspection();
                }}
                onBlur={() => {
                  if (checkoutPath.trim()) void onInspect();
                }}
                placeholder="origin"
                disabled={busy}
                data-testid="new-project-fetch-remote"
              />
            </label>
          </div>
        ) : (
          <div className="clonePreview" data-testid="new-project-clone-preview">
            <span className="metaLabel">will clone into</span>
            <span className="metaValue">{derivedPath}</span>
            <span className="cloneNote">
              Nothing is written if that path already exists. Change the location in
              Settings → Checkout root.
            </span>
          </div>
        )}
        {inspection && (
          <div
            className={inspection.ok ? "inspectOk" : "inspectProblem"}
            role="status"
            data-testid="new-project-inspection"
          >
            {inspection.ok
              ? `Looks good — remotes: ${inspection.remotes
                  .map((r) => r.name)
                  .join(", ")}`
              : inspection.detail}
          </div>
        )}
        <div className="formActions">
          <button
            type="submit"
            className="primaryButton"
            disabled={busy}
            data-testid="new-project-submit"
          >
            {createdName !== null
              ? "Created"
              : submitting
                ? mode === "clone"
                  ? "Starting clone…"
                  : "Creating…"
                : "Create project"}
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
  const [checkoutPath, setCheckoutPath] = useState(project.checkout_path);
  const [fetchRemote, setFetchRemote] = useState(project.fetch_remote);
  const reconcile = useDaemonReconcile();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // The UI guard mirrors the daemon's: rename is only offered while no task
  // row (any state) references the project; the daemon's 409 stays
  // authoritative against races.
  const renameBlocked = referencingCount > 0;
  // A cloned checkout is Ompire's own derived path; repointing it would
  // orphan what was created, and the daemon refuses it (ADR-0022).
  const pathLocked = project.checkout_mode === "cloned";

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
          checkout_path: checkoutPath.trim(),
          fetch_remote: fetchRemote.trim() || "origin",
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
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      if (await confirmAndRemove(project, reconcile)) onClose();
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
      <div className="formGrid formGridUrls">
        <label className="formField">
          <span className="fieldLabel">Checkout path</span>
          <input
            className="mono urlInput"
            value={checkoutPath}
            onChange={(e) => setCheckoutPath(e.target.value)}
            required
            disabled={pathLocked || submitting}
            data-testid={`edit-checkout-${project.name}`}
          />
          {pathLocked && (
            <span className="renameNote" data-testid={`checkout-note-${project.name}`}>
              Ompire created this checkout — its path is fixed
            </span>
          )}
        </label>
        <label className="formField">
          <span className="fieldLabel">Fetch remote</span>
          <input
            className="mono"
            value={fetchRemote}
            onChange={(e) => setFetchRemote(e.target.value)}
            required
            disabled={submitting}
            data-testid={`edit-fetch-remote-${project.name}`}
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

function ProjectSetupPanel({ project }: { project: Project }) {
  const { projectSetupProgress } = useDaemonState();
  const reconcile = useDaemonReconcile();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function guarded(work: () => Promise<void>) {
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      await work();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const onRetry = () =>
    guarded(async () => {
      reconcile("project_updated", await retryProjectSetup(project.name));
    });

  // Retry and give up are the two answers to a failed setup, so both belong
  // here rather than one of them being a click deeper in the edit panel.
  const onRemove = () =>
    guarded(async () => {
      await confirmAndRemove(project, reconcile);
    });

  if (project.setup_state === "cloning") {
    const step = currentStep(projectSetupProgress[project.name]);
    return (
      <div className="setupPanel" role="status" data-testid={`setup-${project.name}`}>
        <span className="setupBusy">
          Cloning{step ? ` — ${step.step}` : ""}…
        </span>
        <span className="cloneNote">
          Tasks cannot be spawned against this project until the checkout is ready.
        </span>
      </div>
    );
  }
  if (project.setup_state === "failed") {
    return (
      <div className="setupPanel" role="alert" data-testid={`setup-${project.name}`}>
        <span className="setupFailed">Setup failed</span>
        {project.setup_error && (
          <pre className="setupError" data-testid={`setup-error-${project.name}`}>
            {project.setup_error}
          </pre>
        )}
        <div className="formActions">
          <button
            type="button"
            className="primaryButton"
            onClick={() => void onRetry()}
            disabled={busy}
            data-testid={`retry-setup-${project.name}`}
          >
            {busy ? "Working…" : "Retry setup"}
          </button>
          <button
            type="button"
            className="removeButton"
            onClick={() => void onRemove()}
            disabled={busy}
            data-testid={`remove-failed-${project.name}`}
          >
            Remove project…
          </button>
        </div>
        {error && (
          <div className="submitError" role="alert" data-testid={`retry-error-${project.name}`}>
            {error}
          </div>
        )}
      </div>
    );
  }
  return null;
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
        <span
          className={`setupPill setupPill-${project.setup_state}`}
          data-testid={`setup-state-${project.name}`}
        >
          {SETUP_LABEL[project.setup_state]}
        </span>
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
        <span className="metaLabel">checkout</span>
        <span className="metaValue" data-testid={`checkout-path-${project.name}`}>
          {project.checkout_path}
          <span className="noForkNote">
            {" · "}
            {project.checkout_mode === "cloned" ? "cloned by Ompire" : "your checkout"}
            {" · fetches "}
            {project.fetch_remote}
          </span>
        </span>
      </div>
      <ProjectSetupPanel project={project} />
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
