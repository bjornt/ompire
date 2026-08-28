import { useEffect, useMemo, useRef, useState } from "react";
import {
  createTemplate,
  deleteSetting,
  deleteTemplate,
  getDaemonInfo,
  getSettings,
  getToken,
  recheckGitHub,
  recheckGpg,
  rotateToken,
  updateSettings,
  updateTemplate,
  type TemplateInput,
} from "../lib/api";
import { setDaemonToken } from "../lib/token";
import { useDaemonState } from "../lib/useDaemonState";
import { githubIdentityPresentation, safeGitHubDetail } from "../lib/githubPresentation";
import { gpgPresentation, selectionSourceLabel } from "../lib/gpgPresentation";
import { REGISTERED_WORKFLOWS, THINKING_LEVELS, templateCheckout } from "../lib/templates";
import type {
  AttentionTier,
  DaemonInfo,
  DaemonSettings,
  Project,
  Template,
  ThinkingLevel,
} from "../types";
import "./SettingsView.css";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

type EditorState = { mode: "create" } | { mode: "edit"; name: string };

type Provenance = Record<string, "default" | "config" | "override">;

const RENOTIFY_OPTIONS: { label: string; value: number }[] = [
  { label: "3 minutes", value: 180 },
  { label: "5 minutes", value: 300 },
  { label: "10 minutes", value: 600 },
  { label: "Never", value: 0 },
];

const TIER_ROWS: { tier: AttentionTier; hint: string }[] = [
  { tier: "interrupt", hint: "needs you now" },
  { tier: "notify", hint: "needs you soon" },
  { tier: "badge", hint: "counts in the tab" },
  { tier: "silent", hint: "no alert" },
];

const TIER_KIND_LABELS: Record<string, string> = {
  desktop: "Desktop",
  sound: "Sound",
  badge: "Tab badge",
};

function capitalize(s: string): string {
  return (s[0]?.toUpperCase() ?? "") + s.slice(1);
}

function OverrideTag({
  provenance,
  settingKey,
  testId,
}: {
  provenance: Provenance;
  settingKey: string;
  testId: string;
}) {
  if (provenance[settingKey] !== "override") return null;
  return (
    <span className="overrideTag" data-testid={`override-${testId}`}>
      override
    </span>
  );
}

function TierMatrix({
  settings,
  provenance,
  onChange,
}: {
  settings: DaemonSettings;
  provenance: Provenance;
  onChange: (key: string, value: boolean) => void;
}) {
  return (
    <div className="tierMatrix" data-testid="tier-matrix">
      {TIER_ROWS.map(({ tier, hint }) => (
        <div key={tier} className="tierRow" data-testid={`tier-row-${tier}`}>
          <div className="tierRowMeta">
            <span
              className={`tierPill tierPill${capitalize(tier)}`}
              data-testid={`tier-pill-${tier}`}
            >
              {tier}
            </span>
            <span className="tierHint">{hint}</span>
          </div>
          <div className="tierChecks">
            {(["desktop", "sound", "badge"] as const).map((kind) => {
              const key = `tier.${tier}.${kind}`;
              const checked = Boolean(settings[key]);
              const testId = `tier-${tier}-${kind}`;
              return (
                <label key={kind} className="checkButton" data-testid={`check-button-${testId}`}>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => onChange(key, !checked)}
                    data-testid={testId}
                  />
                  <span>{TIER_KIND_LABELS[kind]}</span>
                  <OverrideTag provenance={provenance} settingKey={key} testId={testId} />
                </label>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function WatchdogInputs({
  settings,
  provenance,
  onChange,
}: {
  settings: DaemonSettings;
  provenance: Provenance;
  onChange: (key: string, value: number) => void;
}) {
  const stallSetting = typeof settings.stall_threshold === "number" ? settings.stall_threshold : 300;
  const contextSetting =
    typeof settings.context_advisory_threshold === "number"
      ? settings.context_advisory_threshold
      : 80;

  const [stall, setStall] = useState(String(stallSetting));
  const [context, setContext] = useState(String(contextSetting));

  useEffect(() => {
    setStall(String(stallSetting));
  }, [stallSetting]);

  useEffect(() => {
    setContext(String(contextSetting));
  }, [contextSetting]);

  function commitStall() {
    const n = Number.parseInt(stall, 10);
    if (!Number.isNaN(n) && n >= 0) onChange("stall_threshold", n);
  }

  function commitContext() {
    const n = Number.parseInt(context, 10);
    if (!Number.isNaN(n) && n >= 0 && n <= 100) onChange("context_advisory_threshold", n);
  }

  return (
    <div className="watchdogGrid">
      <label className="formField">
        <span className="fieldLabel">
          Stall threshold{" "}
          <span className="fieldHint">seconds before a session is considered stalled</span>
          <OverrideTag provenance={provenance} settingKey="stall_threshold" testId="stall-threshold" />
        </span>
        <input
          type="number"
          className="mono"
          min={0}
          value={stall}
          onChange={(e) => setStall(e.target.value)}
          onBlur={commitStall}
          data-testid="stall-threshold"
        />
      </label>

      <label className="formField">
        <span className="fieldLabel">
          Context advisory threshold{" "}
          <span className="fieldHint">percent at which to surface context load</span>
          <OverrideTag
            provenance={provenance}
            settingKey="context_advisory_threshold"
            testId="context-advisory-threshold"
          />
        </span>
        <input
          type="number"
          className="mono"
          min={0}
          max={100}
          value={context}
          onChange={(e) => setContext(e.target.value)}
          onBlur={commitContext}
          data-testid="context-advisory-threshold"
        />
      </label>
    </div>
  );
}

function maskToken(token: string): string {
  if (token.length <= 8) return token;
  const prefix = token.startsWith("ompire_tok_") ? "ompire_tok_" : "";
  const secret = prefix ? token.slice(prefix.length) : token;
  const first = secret.slice(0, 4);
  const last = secret.slice(-4);
  const filler = "•".repeat(Math.max(secret.length - 8, 4));
  return `${prefix}${first}${filler}${last}`;
}

function CommitSigningPanel() {
  const { gpg } = useDaemonState();
  const [rechecking, setRechecking] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);

  const signing = gpgPresentation(gpg);
  const selected = gpg?.selected ?? null;
  const candidates = gpg?.candidates ?? [];
  const source = selectionSourceLabel(gpg);
  const detail = error ?? gpg?.detail ?? null;

  async function guarded(work: () => Promise<void>, flag: (v: boolean) => void) {
    if (busy.current) return;
    busy.current = true;
    flag(true);
    setError(null);
    try {
      await work();
    } catch (err: unknown) {
      setError(errorText(err));
    } finally {
      busy.current = false;
      flag(false);
    }
  }

  // Selecting re-probes on the daemon side, so the shared chip and Ship flow
  // follow from the broadcast rather than from local state here.
  const onSelect = (value: string) =>
    void guarded(
      () =>
        value
          ? updateSettings({ gpg_signing_key: value }).then(() => undefined)
          : deleteSetting("gpg_signing_key").then(() => undefined),
      setSelecting,
    );

  return (
    <div className="daemonGithub" data-testid="daemon-signing-panel">
      <h3 className="daemonGithubTitle">Commit signing</h3>
      <div
        className="daemonGithubStatus"
        role="status"
        aria-live="polite"
        aria-label={signing.description}
        data-testid="daemon-gpg-state"
      >
        <span className="dot" style={{ background: signing.dot }} />
        {signing.label}
      </div>

      {candidates.length > 0 && (
        <label className="daemonInfoRow" htmlFor="gpg-signing-key">
          <span>Signing key</span>
          <select
            id="gpg-signing-key"
            value={selected?.fingerprint ?? ""}
            disabled={selecting}
            onChange={(e) => onSelect(e.target.value)}
            data-testid="gpg-key-select"
          >
            <option value="">Detect automatically</option>
            {candidates.map((candidate) => (
              <option key={candidate.fingerprint} value={candidate.fingerprint}>
                {candidate.uid ?? candidate.key_id} · {candidate.key_id}
              </option>
            ))}
          </select>
        </label>
      )}

      <div className="daemonInfoGrid">
        {selected && (
          <div className="daemonInfoRow" data-testid="daemon-gpg-fingerprint">
            <span>Fingerprint</span>
            <code>{selected.fingerprint}</code>
          </div>
        )}
        {selected?.uid && (
          <div className="daemonInfoRow" data-testid="daemon-gpg-uid">
            <span>User ID</span>
            <code>{selected.uid}</code>
          </div>
        )}
        {source && (
          <div className="daemonInfoRow" data-testid="daemon-gpg-source">
            <span>Chosen by</span>
            <code>{source}</code>
          </div>
        )}
        <div className="daemonInfoRow" data-testid="daemon-gpg-checked-at">
          <span>Last checked</span>
          <code>{gpg?.checked_at ?? "Not checked yet"}</code>
        </div>
      </div>

      {/* The ambiguous recovery is "choose a key in Settings" — pointless
          here, where the selector above already is the action. Every other
          state's recovery happens elsewhere, so it still belongs. */}
      {signing.recovery && gpg?.state !== "ambiguous" && (
        <p className="daemonGithubDetail" data-testid="daemon-gpg-recovery">
          {signing.recovery}
        </p>
      )}
      {signing.command && (
        <code data-testid="daemon-gpg-command">{signing.command}</code>
      )}
      {detail && (
        <p className="daemonGithubDetail" role="alert" data-testid="daemon-gpg-detail">
          {detail}
        </p>
      )}

      <button
        type="button"
        className="ghostButton"
        disabled={rechecking}
        onClick={() =>
          void guarded(() => recheckGpg().then(() => undefined), setRechecking)
        }
        data-testid="recheck-gpg-button"
      >
        {rechecking ? "Checking key…" : "Re-check key"}
      </button>
    </div>
  );
}


function DaemonPanel() {
  const { gh } = useDaemonState();
  const [info, setInfo] = useState<DaemonInfo | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [recheckingGitHub, setRecheckingGitHub] = useState(false);
  const [gitHubRecheckError, setGitHubRecheckError] = useState<string | null>(null);
  const gitHubRecheckLock = useRef(false);
  const githubChip = githubIdentityPresentation(gh);
  const identity = gh?.identity;
  const githubDetail = safeGitHubDetail(identity?.detail) ?? gitHubRecheckError;

  useEffect(() => {
    getDaemonInfo().then((i) => setInfo(i)).catch(() => {});
    getToken().then((res) => setToken(res.token)).catch(() => {});
  }, []);

  async function copyToken() {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
    } catch {
      // Ignore missing clipboard permission.
    }
  }

  async function rotate() {
    if (
      !window.confirm(
        "Rotating the daemon token will invalidate the old token immediately. Any other client using it will be disconnected. Continue?",
      )
    ) {
      return;
    }
    const res = await rotateToken();
    setDaemonToken(res.token);
    setToken(res.token);
  }

  async function recheckGithub() {
    if (gitHubRecheckLock.current) return;
    gitHubRecheckLock.current = true;
    setRecheckingGitHub(true);
    setGitHubRecheckError(null);
    try {
      await recheckGitHub();
    } catch (error: unknown) {
      setGitHubRecheckError(safeGitHubDetail(errorText(error)));
    } finally {
      gitHubRecheckLock.current = false;
      setRecheckingGitHub(false);
    }
  }

  return (
    <>
      {info && (
        <div className="daemonInfoGrid">
          <div className="daemonInfoRow" data-testid="daemon-info-bind">
            <span>Bind</span>
            <code>{info.bind}</code>
          </div>
          <div className="daemonInfoRow" data-testid="daemon-info-port">
            <span>Port</span>
            <code>{info.port}</code>
          </div>
          <div className="daemonInfoRow" data-testid="daemon-info-version">
            <span>Version</span>
            <code>{info.version}</code>
          </div>
          <div className="daemonInfoRow" data-testid="daemon-info-config-path">
            <span>Config path</span>
            <code>{info.config_path}</code>
          </div>
          <div className="daemonInfoRow" data-testid="daemon-info-data-dir">
            <span>Data dir</span>
            <code>{info.data_dir}</code>
          </div>
          {info.audit_log_path !== null && info.audit_log_path !== undefined && (
            <div className="daemonInfoRow" data-testid="daemon-info-audit-log-path">
              <span>Audit log</span>
              <code>{info.audit_log_path}</code>
            </div>
          )}
        </div>
      )}

      <div className="daemonGithub" data-testid="daemon-github-panel">
        <h3 className="daemonGithubTitle">GitHub CLI</h3>
        <div
          className="daemonGithubStatus"
          role="status"
          aria-live="polite"
          aria-label={githubChip.description}
          data-testid="daemon-gh-state"
        >
          <span className="dot" style={{ background: githubChip.dot }} />
          {githubChip.label}
        </div>
        <div className="daemonInfoGrid">
          {identity?.login && (
            <div className="daemonInfoRow" data-testid="daemon-gh-login">
              <span>Account</span>
              <code>@{identity.login}</code>
            </div>
          )}
          <div className="daemonInfoRow" data-testid="daemon-gh-host">
            <span>Host</span>
            <code>{identity?.host ?? "Not checked yet"}</code>
          </div>
          {identity?.credential_source && (
            <div className="daemonInfoRow" data-testid="daemon-gh-source">
              <span>Credential source</span>
              <code>{identity.credential_source}</code>
            </div>
          )}
          {identity?.executable_path && (
            <div className="daemonInfoRow" data-testid="daemon-gh-executable">
              <span>Executable</span>
              <code>{identity.executable_path}</code>
            </div>
          )}
          {identity?.version && (
            <div className="daemonInfoRow" data-testid="daemon-gh-version">
              <span>Version</span>
              <code>{identity.version}</code>
            </div>
          )}
          <div className="daemonInfoRow" data-testid="daemon-gh-checked-at">
            <span>Last checked</span>
            <code>{identity?.checked_at ?? "Not checked yet"}</code>
          </div>
        </div>
        {githubDetail && (
          <p className="daemonGithubDetail" role="alert" data-testid="daemon-gh-detail">
            {githubDetail}
          </p>
        )}
        <button
          type="button"
          className="ghostButton"
          disabled={recheckingGitHub}
          onClick={() => void recheckGithub()}
          data-testid="recheck-github-button"
        >
          {recheckingGitHub ? "Checking GitHub…" : "Re-check GitHub"}
        </button>
      </div>

      <CommitSigningPanel />

      <div className="tokenRow">
        <code className="tokenValue" data-testid="daemon-token">
          {token ? maskToken(token) : "••••"}
        </code>
        <div className="tokenActions">
          <button
            type="button"
            className="ghostButton"
            onClick={() => void copyToken()}
            data-testid="copy-daemon-token"
          >
            Copy
          </button>
          <button
            type="button"
            className="ghostButton"
            onClick={() => void rotate()}
            data-testid="rotate-daemon-token"
          >
            Rotate
          </button>
        </div>
      </div>
    </>
  );
}

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
  const { projects, templates, settings } = useDaemonState();
  const [provenance, setProvenance] = useState<Provenance>({});
  const [editor, setEditor] = useState<EditorState | null>(null);

  useEffect(() => {
    getSettings()
      .then((res) => setProvenance(res.provenance ?? {}))
      .catch(() => {});
  }, []);

  const sorted = useMemo(
    () => [...templates].sort((a, b) => a.name.localeCompare(b.name)),
    [templates],
  );
  const editingTemplate =
    editor?.mode === "edit" ? templates.find((t) => t.name === editor.name) : undefined;

  async function putSetting(key: string, value: boolean | number) {
    try {
      const res = await updateSettings({ [key]: value });
      setProvenance(res.provenance ?? {});
    } catch {
      // The control state comes from the daemon's settings_changed event; if
      // the PUT fails we leave the UI as-is and let the next broadcast sync it.
    }
  }

  const renotify =
    typeof settings.renotify_interval === "number" ? settings.renotify_interval : 300;

  return (
    <div className="settingsMain">
      <div className="headerRow">
        <h1>Templates &amp; settings</h1>
        <span className="subline">what spawn needs, and how attention reaches you</span>
      </div>

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

        <div className="settingsColumn">
          <section className="panel" data-testid="notifications-panel">
            <h2 className="panelTitle">Notifications · per attention tier</h2>
            <TierMatrix settings={settings} provenance={provenance} onChange={putSetting} />
          </section>

          <section className="panel" data-testid="renotify-panel">
            <h2 className="panelTitle">Re-notify</h2>
            <label className="formField">
              <span className="fieldLabel">
                Re-notify interval
                <OverrideTag
                  provenance={provenance}
                  settingKey="renotify_interval"
                  testId="renotify-interval"
                />
              </span>
              <select
                value={renotify}
                onChange={(e) => putSetting("renotify_interval", Number(e.target.value))}
                data-testid="renotify-interval"
              >
                {RENOTIFY_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
              <span className="fieldHint">How long to wait before reminding you again</span>
            </label>
          </section>

          <section className="panel" data-testid="watchdogs-panel">
            <h2 className="panelTitle">Watchdogs &amp; thresholds</h2>
            <WatchdogInputs settings={settings} provenance={provenance} onChange={putSetting} />
          </section>

          <section className="panel" data-testid="daemon-panel">
            <h2 className="panelTitle">Daemon</h2>
            <DaemonPanel />
          </section>
        </div>
      </div>
    </div>
  );
}
