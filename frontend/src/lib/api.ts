import type {
  AgentStateData,
  AgentStatsData,
  DaemonInfo,
  DaemonSettings,
  GitHubStatus,
  CheckoutInspection,
  ConsumerBinding,
  GpgStatus,
  ModelProfile,
  ModelRole,
  ModelRoleBinding,
  Project,
  ProjectFiles,
  WorkflowDescriptor,
  WorkflowReadinessReason,
  WorkflowRevisionDetail,
  WorkflowStepDescriptor,
  ReviewState,
  ShipState,
  Task,
  TaskDetail,
  TaskExecutionInputs,
  ThinkingLevel,
  WorkshopAdditionsSource,
} from "../types";
import { getDaemonToken } from "./token";

/** Minimal authenticated REST client. Commands go over REST, events come back
 * over the WebSocket (ADR-0004). Components render from daemon state, never
 * from these return values directly — but a response *is* an authoritative
 * command outcome, so a caller may feed it into daemon state through
 * `useDaemonReconcile` rather than waiting for the matching event.
 *
 * Architecture: docs/adr/0004-use-rest-and-websocket-snapshot-deltas.md */
async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getDaemonToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const data = (await response.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
      else if (
        typeof data.detail === "object" &&
        data.detail !== null &&
        "message" in data.detail &&
        typeof data.detail.message === "string"
      ) {
        detail = data.detail.message;
      }
    } catch {
      /* non-JSON error body; keep the status code */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

/** Task-local overrides of the project's workspace defaults (ADR-0026).
 * Leaving a key out inherits; an explicit empty `preamble` is an override to
 * "no preamble". The other three have no meaningful empty value, so the
 * daemon refuses an explicit null rather than reading it as a reset. */
export interface WorkspaceOverridesInput {
  base_branch?: string;
  branch_pattern?: string;
  workshop_additions?: WorkshopAdditionsSource;
  preamble?: string;
}

/** One model consumer's row-level selections (ADR-0027). Each dimension is
 * independently optional: omitting a field inherits it. There is deliberately
 * no concrete model or thinking field — those are profile settings, not a
 * third override hierarchy. */
export interface ConsumerOverrideInput {
  model_profile?: string;
  role?: ModelRole;
}

/** What the operator selected. `model_profile` omitted means "inherit the
 * project default"; a name replaces that inheritance for this task.
 *
 * `step_overrides` is keyed by declared agent step name. Every model consumer
 * is a declared step now; `auxiliary_overrides` is kept only so a request
 * still naming the retired judge is refused with a field-level error rather
 * than having its choice silently dropped (ADR-0028). */
export interface LaunchInput {
  project_name: string;
  workflow_name: string;
  slug: string;
  prompt: string;
  model_profile?: string;
  workspace_overrides?: WorkspaceOverridesInput;
  step_overrides?: Record<string, ConsumerOverrideInput>;
  auxiliary_overrides?: Record<string, ConsumerOverrideInput>;
}

/** One row of the launch preview. A command, decision, or gate carries no
 * model — showing one would be a fiction, since those steps never reach a
 * provider. `thinking` is the accepted *policy*, which omp may resolve to a
 * model-specific level at run time. */
export interface LaunchPreviewStep {
  step: string;
  kind: "agent" | "command" | "decision" | "gate";
  session: string | null;
  conditional: boolean;
  /** The role the workflow declares, so the form can say what resetting the
   * role override restores. Null for a model-free step. */
  declared_role: ModelRole | null;
  /** The resolved binding, byte-identical to what acceptance pins for this
   * consumer. Null for a command, decision, or gate — those rows carry no
   * model and get no override controls. */
  binding: ConsumerBinding | null;
  role: ModelRole | null;
  model: string | null;
  thinking: ThinkingLevel | null;
}

/** The daemon's resolution of one launch. `preview_token` names exactly what
 * was reviewed: it authorizes nothing, and creation compares it so a
 * configuration change between review and submission is refused rather than
 * retried under settings nobody looked at. */
export interface LaunchPreview {
  preview_token: string;
  project_name: string;
  workflow_name: string;
  model_profile: string | null;
  model_profile_source: "task" | "project" | "legacy-confirmed";
  project_default_model_profile: string | null;
  /** The exact definition this launch would pin, and the semantics version it
   * is read under. Editing a prompt or a route changes this and invalidates
   * the preview; an unrelated change does not (ADR-0028). */
  workflow_revision: string;
  workflow_format: number;
  workflow_primary_session: string;
  workflow_sessions: string[];
  /** Engine-reserved consumer names. Empty: there are none left. */
  auxiliary_consumers: string[];
  /** The task-wide profile's own map: what a row inheriting both dimensions
   * resolves against. */
  roles: Record<ModelRole, ModelRoleBinding>;
  workspace: {
    base_branch: string;
    branch_pattern: string;
    workshop_additions: WorkshopAdditionsSource;
    preamble: string;
  };
  /** What the project would supply, so the form can show what "reset" gives
   * back without guessing. */
  inherited_workspace: LaunchPreview["workspace"];
  workspace_overrides: string[];
  branch: string;
  steps: LaunchPreviewStep[];
}

/** Resolve the operator's selections without creating anything. Same rules,
 * same daemon module, same output as acceptance (ADR-0026). */
export function previewTask(input: LaunchInput): Promise<LaunchPreview> {
  return request<LaunchPreview>("POST", "/api/tasks/preview", input);
}

/** Accept the reviewed resolution. A stale token comes back as a 409 whose
 * detail carries the current preview, so the form can show what changed. */
export function spawnTask(input: LaunchInput & { preview_token: string }): Promise<Task> {
  return request<Task>("POST", "/api/tasks", input);
}

/** Registered built-in workflows. Also present in the WebSocket snapshot;
 * this is the reload path for a view mounted before the socket connects. */
export function listWorkflows(): Promise<WorkflowDescriptor[]> {
  return request<WorkflowDescriptor[]>("GET", "/api/workflows");
}

export function cleanupTask(id: number): Promise<Task> {
  return request<Task>("POST", `/api/tasks/${id}/cleanup`);
}

export function getTaskDetail(id: number): Promise<TaskDetail> {
  return request<TaskDetail>("GET", `/api/tasks/${id}`);
}

/** Session-scoped agent endpoints (workflow-engine design D-1): every agent
 * interaction addresses one of the task's declared sessions. Session names
 * are slug-format; encode defensively anyway. */
function sessionAgentUrl(id: number, session: string, op: string): string {
  return `/api/tasks/${id}/sessions/${encodeURIComponent(session)}/agent/${op}`;
}

/** Composer modes — each proxies to the session's live agent
 * (agent-interaction). `interrupt` aborts the current turn and re-prompts
 * (`abort_and_prompt`). */
export function steerAgent(id: number, session: string, message: string): Promise<unknown> {
  return request("POST", sessionAgentUrl(id, session, "steer"), { message });
}

export function followUpAgent(id: number, session: string, message: string): Promise<unknown> {
  return request("POST", sessionAgentUrl(id, session, "follow-up"), { message });
}

export function interruptAgent(id: number, session: string, message: string): Promise<unknown> {
  return request("POST", sessionAgentUrl(id, session, "interrupt"), { message });
}

export function getAgentState(id: number, session: string): Promise<AgentStateData> {
  return request<AgentStateData>("GET", sessionAgentUrl(id, session, "state"));
}

export function getAgentStats(id: number, session: string): Promise<AgentStatsData> {
  return request<AgentStatsData>("GET", sessionAgentUrl(id, session, "stats"));
}

/** Answers a session's pending ask/approval question (ask-approvals capability). */
export function answerAgent(
  id: number,
  session: string,
  answer: { question_id: string; selections?: string[]; text?: string; approved?: boolean },
): Promise<unknown> {
  return request("POST", sessionAgentUrl(id, session, "answer"), answer);
}

/** Advances a waiting run: resumes a declared gate, or retries the attempt an
 * uncertainty pause is waiting on (ADR-0028). The daemon picks which from the
 * waiting record, so the caller never has to guess.
 *
 * `expectedSeq` names the attempt the operator was actually looking at. It is
 * required: a stale tab and a double submit are indistinguishable otherwise,
 * and both would apply a decision to evidence nobody saw. 409 when the run
 * has moved on. */
/** Answer whatever the run is waiting on.
 *
 * One endpoint, three different waits, and the daemon decides which this is
 * from what it is actually waiting on rather than from what the caller sends:
 * an uncertainty pause retries its blocked step, a format-1 gate resumes with
 * an optional note, and a format-2 gate needs the id of a declared choice.
 * `expectedSeq` names the attempt the operator was looking at, so a stale tab
 * or a double submit is refused instead of applied to a different one. */
export function resumeWorkflow(
  id: number,
  expectedSeq: number,
  note?: string,
  choiceId?: string,
): Promise<{
  task_id: number;
  workflow: "resumed" | "retried" | "answered";
  choice_id?: string;
  step: string | null;
  result?: string | null;
}> {
  return request("POST", `/api/tasks/${id}/workflow/resume`, {
    expected_seq: expectedSeq,
    choice_id: choiceId ?? null,
    note: note ?? null,
  });
}

/** Read one retained definition by content identity. Deliberately not
 * addressable by workflow name: a name says what a *new* launch would get,
 * and this answers "what did that task accept". */
export function getWorkflowRevision(revision: string): Promise<WorkflowRevisionDetail> {
  return request<WorkflowRevisionDetail>(
    "GET",
    `/api/workflows/revisions/${encodeURIComponent(revision)}`,
  );
}

/** Start an llmvet review for an idle task (review capability). */
export function startReview(id: number): Promise<ReviewState> {
  return request<ReviewState>("POST", `/api/tasks/${id}/review`);
}

/** Cancel an open llmvet review (review capability). */
export function cancelReview(id: number): Promise<ReviewState> {
  return request<ReviewState>("POST", `/api/tasks/${id}/review/cancel`);
}

/** Ensure or explicitly replace commit/PR metadata through the live agent. */
export function draftShip(id: number, options?: { replace: boolean }): Promise<ShipState> {
  return request<ShipState>("POST", `/api/tasks/${id}/ship/draft`, options);
}

/** Run the signed squash commit → push → PR flow (ship capability). */
export function shipCommit(
  id: number,
  body: {
    message: string;
    pr_title: string;
    pr_body: string;
    mode?: "squash" | "retain";
  },
): Promise<ShipState> {
  return request<ShipState>("POST", `/api/tasks/${id}/ship/commit`, body);
}

/** Force a fresh gpg-agent cache probe (ship capability). */
export function recheckGpg(): Promise<GpgStatus> {
  return request<GpgStatus>("POST", "/api/gpg/recheck");
}

/** Current daemon-owned GitHub CLI and repository eligibility observation. */
export function getGitHubStatus(): Promise<GitHubStatus> {
  return request<GitHubStatus>("GET", "/api/gh");
}

/** Recheck the global identity, or one task's trusted registered upstream. */
export function recheckGitHub(taskId?: number): Promise<GitHubStatus> {
  return request<GitHubStatus>(
    "POST",
    "/api/gh/recheck",
    taskId === undefined ? undefined : { task_id: taskId },
  );
}

/** Project CRUD (projects capability). `newName` on update triggers the
 * guarded rename — the daemon 409s while any task row references it. */
export function createProject(input: {
  name: string;
  title: string;
  upstream_url: string;
  fork_url: string | null;
  /** `adopt` validates an existing checkout; `clone` derives the destination
   * from the effective checkout root and creates it (ADR-0022). */
  checkout_mode?: "adopt" | "clone";
  checkout_path?: string | null;
  fetch_remote?: string;
  /** Optional global model profile (ADR-0025); omitted or null means no
   * default. Nothing is auto-selected. */
  default_model_profile?: string | null;
  /** Workspace and prompt defaults (ADR-0026); the branch pattern defaults
   * to the daemon's configured seed when omitted. */
  base_branch?: string;
  branch_pattern?: string;
  workshop_additions?: WorkshopAdditionsSource;
  preamble?: string;
}): Promise<Project> {
  return request<Project>("POST", "/api/projects", input);
}

export function updateProject(
  name: string,
  input: {
    title: string;
    upstream_url: string;
    fork_url: string | null;
    checkout_path: string;
    fetch_remote?: string;
    new_name?: string;
    /** Three-valued: leave the key out to preserve the stored reference, pass
     * null to clear it, pass a name to select that profile. */
    default_model_profile?: string | null;
    /** Workspace and prompt defaults (ADR-0026). Omitting a key preserves
     * the stored value, so a caller written before these existed cannot
     * blank them; an empty `preamble` is the value "no preamble". */
    base_branch?: string;
    branch_pattern?: string;
    workshop_additions?: WorkshopAdditionsSource;
    preamble?: string;
  },
): Promise<Project> {
  return request<Project>("PUT", `/api/projects/${encodeURIComponent(name)}`, input);
}

/** Look at a candidate checkout without registering anything. A refusal comes
 * back as `ok: false` with a reason, not as a thrown error — the operator is
 * still typing. */
export function inspectCheckout(input: {
  checkout_path: string;
  fetch_remote?: string;
}): Promise<CheckoutInspection> {
  return request<CheckoutInspection>("POST", "/api/projects/checkout-inspect", input);
}

/** Re-arm and restart a failed clone-mode setup. */
export function retryProjectSetup(name: string): Promise<Project> {
  return request<Project>(
    "POST",
    `/api/projects/${encodeURIComponent(name)}/setup/retry`,
  );
}

/** Repository paths for the Spawn prompt's `@` mentions. Rooted at the
 * project's checkout and filtered by `q`; the daemon caps `limit` itself and
 * answers 409 when the checkout is missing or is not a git repository. */
export function searchProjectFiles(
  name: string,
  q: string,
  limit?: number,
): Promise<ProjectFiles> {
  const params = new URLSearchParams({ q });
  if (limit !== undefined) params.set("limit", String(limit));
  return request<ProjectFiles>(
    "GET",
    `/api/projects/${encodeURIComponent(name)}/files?${params}`,
  );
}

export function deleteProject(name: string): Promise<{ deleted: string }> {
  return request("DELETE", `/api/projects/${encodeURIComponent(name)}`);
}

/** Upgrade reconciliation (ADR-0026): what a project still owes a decision
 * about after the template retirement, and the decision itself. Candidates
 * are shown with their source and never pre-selected — the operator supplies
 * the final value. */
export interface ProjectReconciliation {
  project_name: string;
  state: "reconciled" | "needs-reconciliation";
  needs_reconciliation: boolean;
  evidence_fingerprint: string;
  current: {
    base_branch: string;
    branch_pattern: string;
    workshop_additions: WorkshopAdditionsSource;
    preamble: string;
    default_model_profile: string | null;
  };
  /** Distinct old values per field, in the order the migration found them. */
  workspace_conflicts: Record<string, (string | null)[]>;
  /** Old concrete model/thinking pairs. Candidates for at most one binding;
   * never turned into a profile. */
  model_candidates: { source: string; model: string | null; thinking: string | null }[];
  retired_judge_model: string | null;
  /** True: nothing replaces it. The engine runs no implicit model at all, so
   * there is no role to point the operator at as "where it went". */
  judge_removed: boolean;
  /** Inert history, kept after reconciliation so an unselected preamble or
   * candidate is not lost. */
  source_templates: { source: string; values: Record<string, unknown> }[];
}

export function getProjectReconciliation(name: string): Promise<ProjectReconciliation> {
  return request<ProjectReconciliation>(
    "GET",
    `/api/projects/${encodeURIComponent(name)}/launch-reconciliation`,
  );
}

export function confirmProjectReconciliation(
  name: string,
  input: {
    evidence_fingerprint: string;
    base_branch: string;
    branch_pattern: string;
    workshop_additions: WorkshopAdditionsSource;
    preamble: string;
    default_model_profile: string | null;
    acknowledge_model_candidates?: boolean;
    acknowledge_judge_model?: boolean;
  },
): Promise<Project> {
  return request<Project>(
    "POST",
    `/api/projects/${encodeURIComponent(name)}/launch-reconciliation`,
    input,
  );
}

/** What a legacy task's records hold, what was reconstructed as a candidate,
 * and what is simply unknown and cannot be recovered. */
export interface TaskConfiguration {
  task_id: number;
  needs_configuration: boolean;
  /** The task predates retained definitions and has no pinned revision. */
  needs_workflow_confirmation: boolean;
  workflow_readiness: {
    ready: boolean;
    reason: WorkflowReadinessReason | null;
    detail: string | null;
    /** Only a missing legacy binding is something a confirmation can fix. */
    confirmable: boolean;
  };
  workflow_candidate: WorkflowContinuationCandidate | null;
  archived: boolean;
  known: Record<string, unknown>;
  source_attribution: { source: string; values: Record<string, unknown> }[];
  unknown_inputs: string[];
  candidates: Record<string, string | null>;
  accepted?: TaskExecutionInputs;
}

/** The current definition of *this task's own* workflow name, offered as a
 * candidate to continue under. `problems` is why it cannot explain the
 * task's recorded steps and sessions; a non-empty list blocks confirmation
 * rather than remapping anything. */
export interface WorkflowContinuationCandidate {
  workflow_name: string;
  revision: string | null;
  format?: number;
  available: boolean;
  compatible: boolean;
  problems: string[];
  primary_session?: string;
  sessions?: string[];
  steps?: WorkflowStepDescriptor[];
  current_step?: string | null;
  workflow_status?: string | null;
  /** Everything through this attempt ran under a definition nobody kept. */
  legacy_through_seq: number;
  /** The one attempt that spans the boundary, if a run is in flight. */
  interrupted_legacy_seq: number | null;
  uncertainty_notice: string;
}

/** The launch fields are needed only by a task that was never configured. A
 * task that merely predates retained definitions supplies none of them: its
 * model, branch, and preamble were reviewed once and are not re-decided. */
export interface TaskContinuationInput {
  model_profile?: string;
  base_branch?: string;
  workshop_additions?: WorkshopAdditionsSource;
  preamble?: string;
}

export function getTaskConfiguration(id: number): Promise<TaskConfiguration> {
  return request<TaskConfiguration>("GET", `/api/tasks/${id}/configuration`);
}

export function previewTaskConfiguration(
  id: number,
  input: TaskContinuationInput,
): Promise<{
  task_id: number;
  preview_token: string;
  inputs: TaskExecutionInputs | null;
  workflow: {
    name: string;
    revision: string;
    format: number;
    compatible: boolean;
    problems: string[];
    legacy_through_seq: number;
    interrupted_legacy_seq: number | null;
    uncertainty_notice: string;
  };
  unknown_inputs: string[];
}> {
  return request("POST", `/api/tasks/${id}/configuration/preview`, input);
}

/** Pins what happens next. It does not claim the turns already taken used
 * these values, and it recreates no workspace, branch, or session identity. */
export function confirmTaskConfiguration(
  id: number,
  input: TaskContinuationInput & {
    preview_token: string;
    acknowledge_unknown: boolean;
    acknowledge_workflow: boolean;
  },
): Promise<Task> {
  return request<Task>("POST", `/api/tasks/${id}/configuration/confirm`, input);
}

/** Resume a confirmed task whose run was left in place while it was
 * unconfigured. Only a previously running or waiting run is eligible. */
export function continueTask(id: number): Promise<Task> {
  return request<Task>("POST", `/api/tasks/${id}/continue`);
}

/** Model-profile CRUD (ADR-0025). Commands only: the list itself arrives in
 * the WebSocket snapshot, and a mutation's response is fed back through
 * `useDaemonReconcile` so the panel need not wait for the matching event.
 *
 * The daemon owns binding validation — these helpers send what the operator
 * typed and surface its refusal. */
export type ModelProfileRoles = Record<ModelRole, ModelRoleBinding>;

export function createModelProfile(input: {
  name: string;
  roles: ModelProfileRoles;
}): Promise<ModelProfile> {
  return request<ModelProfile>("POST", "/api/model-profiles", input);
}

/** Replaces all four bindings at once; the name is immutable. */
export function updateModelProfile(
  name: string,
  roles: ModelProfileRoles,
): Promise<ModelProfile> {
  return request<ModelProfile>(
    "PUT",
    `/api/model-profiles/${encodeURIComponent(name)}`,
    { roles },
  );
}

/** 409 with the referencing project names while any project still selects it. */
export function deleteModelProfile(name: string): Promise<{ deleted: string }> {
  return request("DELETE", `/api/model-profiles/${encodeURIComponent(name)}`);
}

/** Settings CRUD (daemon-settings capability). The effective map and
 * provenance come from GET; PUT persists overrides and broadcasts
 * `settings_changed`; DELETE reverts one override to its lower layer. */
export interface SettingsResponse {
  settings: DaemonSettings;
  provenance: Record<string, "default" | "config" | "override">;
}

export function getSettings(): Promise<SettingsResponse> {
  return request<SettingsResponse>("GET", "/api/settings");
}

export function updateSettings(changes: DaemonSettings): Promise<SettingsResponse> {
  return request<SettingsResponse>("PUT", "/api/settings", changes);
}

export function deleteSetting(key: string): Promise<SettingsResponse> {
  return request<SettingsResponse>("DELETE", `/api/settings/${encodeURIComponent(key)}`);
}

/** Daemon info (daemon-settings capability): read-only identity/paths. */
export function getDaemonInfo(): Promise<DaemonInfo> {
  return request<DaemonInfo>("GET", "/api/daemon/info");
}

/** Token show/rotate (daemon-settings capability). */
export interface TokenResponse {
  token: string;
}

export function getToken(): Promise<TokenResponse> {
  return request<TokenResponse>("GET", "/api/settings/token");
}

export function rotateToken(): Promise<TokenResponse> {
  return request<TokenResponse>("POST", "/api/settings/token/rotate");
}
