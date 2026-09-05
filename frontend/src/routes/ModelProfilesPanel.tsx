import { useMemo, useState } from "react";
import {
  createModelProfile,
  deleteModelProfile,
  updateModelProfile,
  type ModelProfileRoles,
} from "../lib/api";
import { MODEL_PLACEHOLDERS, MODEL_ROLES, THINKING_LEVELS } from "../lib/models";
import { useDaemonReconcile, useDaemonState } from "../lib/useDaemonState";
import type { ModelProfile, ModelRole, ThinkingLevel } from "../types";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** What the transitional state is, stated wherever a profile is configured.
 * Saving a profile or a project default must not read as if it already
 * governs how tasks run — templates still do, until the next change. */
export const EXECUTION_BOUNDARY_NOTE =
  "Profiles are saved configuration only. Tasks still run with their template's model and thinking settings; nothing here changes a running or newly spawned task.";

/** The editor's own draft. Kept as free text, including the thinking level,
 * so a half-filled row survives while the operator is still typing — the
 * daemon is the authority on what is valid. */
type DraftRoles = Record<ModelRole, { model: string; thinking: ThinkingLevel | "" }>;

const EMPTY_DRAFT: DraftRoles = {
  default: { model: "", thinking: "" },
  smol: { model: "", thinking: "" },
  slow: { model: "", thinking: "" },
  plan: { model: "", thinking: "" },
};

function draftFrom(profile: ModelProfile | undefined): DraftRoles {
  if (profile === undefined) return EMPTY_DRAFT;
  return {
    default: { ...profile.roles.default },
    smol: { ...profile.roles.smol },
    slow: { ...profile.roles.slow },
    plan: { ...profile.roles.plan },
  };
}

function ProfileRow({
  profile,
  editing,
  onToggleEdit,
}: {
  profile: ModelProfile;
  editing: boolean;
  onToggleEdit: () => void;
}) {
  return (
    <div
      className={editing ? "profileRow editing" : "profileRow"}
      data-testid={`model-profile-row-${profile.name}`}
    >
      <div className="profileRowHead">
        <span className="profileName">{profile.name}</span>
        <button
          type="button"
          className={editing ? "editingToggle" : "editToggle"}
          onClick={onToggleEdit}
        >
          {editing ? "Editing…" : "Edit"}
        </button>
      </div>
      {/* Every binding shows its thinking level: a model alone would read as
          if the level came from somewhere else. */}
      <dl className="profileBindings">
        {MODEL_ROLES.map(({ role }) => (
          <div key={role} className="profileBinding">
            <dt>{role}</dt>
            <dd data-testid={`model-profile-${profile.name}-${role}`}>
              {profile.roles[role].model} · {profile.roles[role].thinking}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function ProfileEditor({
  /** The name being edited; absent in create mode. Held by the parent as a
   * stable string, so the editor survives the profile disappearing from
   * saved state. */
  existingName,
  initialDraft,
  /** False once the saved profile this editor opened on is gone — deleted in
   * another browser. The draft stays; saving reports the truth. */
  stillSaved,
  onClose,
}: {
  existingName?: string;
  initialDraft: DraftRoles;
  stillSaved: boolean;
  onClose: () => void;
}) {
  const reconcile = useDaemonReconcile();
  const [name, setName] = useState(existingName ?? "");
  const [roles, setRoles] = useState<DraftRoles>(initialDraft);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  function setRole(role: ModelRole, patch: Partial<DraftRoles[ModelRole]>) {
    setRoles((current) => ({ ...current, [role]: { ...current[role], ...patch } }));
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    // The four rows go up together — a profile is replaced whole, never
    // merged field by field.
    const payload = Object.fromEntries(
      MODEL_ROLES.map(({ role }) => [
        role,
        { model: roles[role].model.trim(), thinking: roles[role].thinking },
      ]),
    ) as ModelProfileRoles;
    try {
      const saved =
        existingName === undefined
          ? await createModelProfile({ name: name.trim(), roles: payload })
          : await updateModelProfile(existingName, payload);
      // The daemon's own answer, applied through the reducer case its event
      // uses — so the row is right now, and the event cannot duplicate it.
      reconcile(
        existingName === undefined ? "model_profile_created" : "model_profile_updated",
        saved,
      );
      onClose();
    } catch (err) {
      // 409 (duplicate) and 422 (bad name, role set, or binding) both land
      // here with the daemon's detail. The draft stays exactly as typed.
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function onRemove() {
    if (existingName === undefined || submitting) return;
    if (
      !window.confirm(
        `Remove model profile ${existingName}?\n\nProjects that use it as their default must be cleared or reassigned first.`,
      )
    ) {
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await deleteModelProfile(existingName);
      reconcile("model_profile_deleted", { name: existingName });
      onClose();
    } catch (err) {
      // A 409 names every referencing project; keep it on screen.
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="panel profileEditor" data-testid="model-profile-editor">
      <h2 className="panelTitle">
        {existingName === undefined ? "New model profile" : `Model profile · ${existingName}`}
      </h2>
      <form onSubmit={onSubmit}>
        {existingName === undefined ? (
          <label className="formField">
            <span className="fieldLabel">
              Name <span className="fieldHint">— lowercase, hyphens; cannot be changed later</span>
            </span>
            <input
              className="mono"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. balanced"
              required
              disabled={submitting}
              data-testid="model-profile-name"
            />
          </label>
        ) : (
          <div className="formField">
            <span className="fieldLabel">
              Name <span className="fieldHint">— a profile's name is its identifier</span>
            </span>
            <code data-testid="model-profile-fixed-name">{existingName}</code>
          </div>
        )}

        {!stillSaved && existingName !== undefined && (
          <div className="submitError" role="alert" data-testid="model-profile-gone">
            {existingName} was removed elsewhere. Your changes are still here, but saving
            will report that the original no longer exists — copy what you need.
          </div>
        )}

        <div className="roleGrid" data-testid="model-profile-roles">
          {MODEL_ROLES.map(({ role, hint }) => (
            <div key={role} className="roleRow" data-testid={`model-profile-role-${role}`}>
              <span className="roleName">
                {role} <span className="fieldHint">— {hint}</span>
              </span>
              <label className="formField">
                <span className="fieldLabel">Model</span>
                <input
                  className="mono"
                  value={roles[role].model}
                  onChange={(e) => setRole(role, { model: e.target.value })}
                  placeholder={MODEL_PLACEHOLDERS[role]}
                  required
                  disabled={submitting}
                  data-testid={`model-profile-model-${role}`}
                />
              </label>
              <label className="formField">
                <span className="fieldLabel">Thinking</span>
                <select
                  value={roles[role].thinking}
                  onChange={(e) =>
                    setRole(role, { thinking: e.target.value as ThinkingLevel | "" })
                  }
                  required
                  disabled={submitting}
                  data-testid={`model-profile-thinking-${role}`}
                >
                  {/* Not a value: a placeholder the operator must replace.
                      There is no "omp default" for a profile binding. */}
                  <option value="">choose a level…</option>
                  {THINKING_LEVELS.map((level) => (
                    <option key={level} value={level}>
                      {level}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          ))}
        </div>

        <span className="fieldNote">
          Provider-qualified identifiers, e.g. <code>openai/o3</code>. Ompire checks the
          shape only — it never contacts a provider, so it cannot tell you whether the
          model exists, your credentials work, or the level is supported.
        </span>

        <div className="formActions">
          <button
            type="submit"
            className="primaryButton"
            disabled={submitting}
            data-testid="model-profile-save"
          >
            {submitting
              ? "Saving…"
              : existingName === undefined
                ? "Create profile"
                : "Save profile"}
          </button>
          <button
            type="button"
            className="ghostButton"
            onClick={onClose}
            disabled={submitting}
          >
            {existingName === undefined ? "Discard" : "Discard changes"}
          </button>
          {existingName !== undefined && (
            <>
              <span className="spacer" />
              <button
                type="button"
                className="removeButton"
                onClick={() => void onRemove()}
                disabled={submitting}
                data-testid={`remove-model-profile-${existingName}`}
              >
                Remove profile…
              </button>
            </>
          )}
        </div>
        {error && (
          <div className="submitError" role="alert" data-testid="model-profile-editor-error">
            {error}
          </div>
        )}
      </form>
    </section>
  );
}

type EditorState = { mode: "create" } | { mode: "edit"; name: string; draft: DraftRoles };

export function ModelProfilesPanel() {
  const { modelProfiles, snapshotReady } = useDaemonState();
  const [editor, setEditor] = useState<EditorState | null>(null);

  const sorted = useMemo(
    () => [...modelProfiles].sort((a, b) => a.name.localeCompare(b.name)),
    [modelProfiles],
  );
  const editingName = editor?.mode === "edit" ? editor.name : null;
  const stillSaved =
    editingName === null || modelProfiles.some((p) => p.name === editingName);

  return (
    <>
      <section className="panel" data-testid="model-profiles-panel">
        <h2 className="panelTitle">Model profiles</h2>
        <p className="fieldNote" data-testid="model-profiles-boundary">
          A profile names four model roles once and can be reused by several projects.{" "}
          {EXECUTION_BOUNDARY_NOTE}
        </p>

        {/* An empty list is a claim about saved state, so it waits for the
            first authoritative snapshot. Before that the truthful answer is
            "still loading", not "you have none". */}
        {!snapshotReady ? (
          <div className="empty templatesEmpty" role="status" data-testid="model-profiles-loading">
            <strong>Loading model profiles…</strong>
            <span>Waiting for the daemon's current state.</span>
          </div>
        ) : sorted.length === 0 ? (
          <div className="empty templatesEmpty" data-testid="model-profiles-empty-state">
            <strong>No model profiles yet</strong>
            <span>
              Create one to name a model and thinking level for each of the four roles, then
              pick it as a project's default.
            </span>
          </div>
        ) : (
          <div className="profileList" data-testid="model-profiles-list">
            {sorted.map((profile) => (
              <ProfileRow
                key={profile.name}
                profile={profile}
                editing={editingName === profile.name}
                onToggleEdit={() =>
                  setEditor((current) =>
                    current?.mode === "edit" && current.name === profile.name
                      ? null
                      : { mode: "edit", name: profile.name, draft: draftFrom(profile) },
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
          data-testid="new-model-profile-toggle"
        >
          New model profile
        </button>
      </section>

      {/* Keyed by name so switching rows starts a fresh draft, while an
          update to — or deletion of — the open profile leaves the draft
          alone. The editor is never unmounted because the saved row went
          away. */}
      {editor?.mode === "create" && (
        <ProfileEditor
          key="create"
          initialDraft={EMPTY_DRAFT}
          stillSaved
          onClose={() => setEditor(null)}
        />
      )}
      {editor?.mode === "edit" && (
        <ProfileEditor
          key={`edit:${editor.name}`}
          existingName={editor.name}
          initialDraft={editor.draft}
          stillSaved={stillSaved}
          onClose={() => setEditor(null)}
        />
      )}
    </>
  );
}
