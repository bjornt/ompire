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
  WorkflowDocumentConversion,
  WorkflowLibraryDetail,
  WorkflowLibraryEntry,
  WorkflowReadinessReason,
  WorkflowRevisionDetail,
  WorkflowStepDescriptor,
  WorkflowValidation,
  ReviewState,
  ShipEnding,
  ShipPreview,
  ShipProjection,
  Task,
  TaskResultDiff,
  TaskResultFile,
  TaskResultFileContent,
  TaskResultProvenance,
  TaskResultLimits,
  TaskResultsProjection,
  TaskDetail,
  TaskExecutionInputs,
  ThinkingLevel,
  WorkshopAdditionsSource,
} from "../types";
import { envelopeNumber, parseLossless, stringifyLossless } from "./losslessJson";
import { asObject, asString, type DraftObject } from "./workflowDocument";
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
    let structured: Record<string, unknown> | null = null;
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
        structured = data.detail as Record<string, unknown>;
      }
    } catch {
      /* non-JSON error body; keep the status code */
    }
    throw new DaemonError(detail, response.status, structured);
  }
  return (await response.json()) as T;
}

/** The same request, through the lossless JSON codec.
 *
 * Used only where a payload carries a workflow *definition*: a draft, a
 * retained document, or a validation result. Those contain executable literal
 * data, and `JSON.parse` would silently turn `1.0` into `1` and drop the
 * digits of a large integer — which changes a definition's canonical bytes,
 * and therefore its identity, without anybody editing it.
 *
 * Everything else keeps ordinary JSON: task, session, and settings numbers
 * are ordinary numbers, and handing a view a `LosslessNumber` where it
 * expects a `number` would buy nothing and break rendering.
 */
async function requestLossless(
  method: string,
  path: string,
  body?: unknown,
): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getDaemonToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : stringifyLossless(body),
  });
  const text = await response.text();
  if (!response.ok) {
    let detail = `${response.status}`;
    let structured: Record<string, unknown> | null = null;
    try {
      const data = JSON.parse(text) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
      else if (
        typeof data.detail === "object" &&
        data.detail !== null &&
        "message" in data.detail &&
        typeof data.detail.message === "string"
      ) {
        detail = data.detail.message;
        structured = data.detail as Record<string, unknown>;
      }
    } catch {
      /* non-JSON error body; keep the status code */
    }
    throw new DaemonError(detail, response.status, structured);
  }
  return parseLossless(text);
}

/** A refused command, with the daemon's structured reason kept alongside the
 * message.
 *
 * Still an `Error` with a readable `message`, so every existing caller is
 * unchanged. The extra fields exist for refusals a view has to *act* on
 * rather than only display — an edit conflict carrying the entry's current
 * version, or a validation error carrying the location in the document. */
export class DaemonError extends Error {
  readonly status: number;
  readonly detail: Record<string, unknown> | null;

  constructor(message: string, status: number, detail: Record<string, unknown> | null) {
    super(message);
    this.name = "DaemonError";
    this.status = status;
    this.detail = detail;
  }

  /** The daemon's machine-readable refusal reason, when it gave one. */
  get reason(): string | null {
    const value = this.detail?.reason;
    return typeof value === "string" ? value : null;
  }
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
  /** Accepted result revisions to install before the first step runs
   * (ADR-0035). Destinations are the manifest's own paths — the request never
   * chooses where retained bytes land. */
  result_attachments?: ResultAttachmentInput[];
  /** The operator acknowledging that a plan captured against a different or
   * unknown base has not been validated against this target. Bound by the
   * preview token to this exact selection and target commit. */
  acknowledge_result_base_difference?: boolean;
}

/** One selected revision. All three fields together: the manifest id is what
 * refuses a stale selection once a successor capture has landed, and the
 * producing task is what a refusal can name. */
export interface ResultAttachmentInput {
  producer_task_id: number;
  result_id: string;
  expected_manifest_id: string;
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
  /** The exact commit this launch resolved against. Null for a launch with no
   * attachments, which pins a branch exactly as before. */
  source_commit: string | null;
  /** True while an unvalidated base still needs the operator's explicit
   * acknowledgement. False both when nothing needs one and once it is given. */
  needs_base_acknowledgement: boolean;
  acknowledged_base_difference: boolean;
  result_attachments: LaunchAttachment[];
  base_comparisons: BaseComparison[];
}

/** One pinned bundle, as the preview and the accepted task both describe it. */
export interface LaunchAttachment {
  result_id: string;
  producer_task_id: number;
  manifest_id: string;
  content_id: string | null;
  accepted_at: string;
  /** The project label the producer's manifest recorded. Provenance only —
   * membership is decided through current task and project records, so a
   * rename is not mistaken for a cross-project transfer. */
  manifest_project_name: string;
  /** Always `handoff-input`, and always non-publishable. Rendered as a label,
   * never as a control: there is no declassification. */
  classification: string;
  publishable: false;
  files: TaskResultFile[];
  destinations: string[];
  provenance: TaskResultProvenance | null;
}

/** How one attachment's recorded base observation relates to the commit this
 * task is actually built from. Per attachment, because two bundles can have
 * been captured against different bases and one must not vouch for the
 * other. */
export interface BaseComparison {
  result_id: string;
  state: "match" | "different" | "unknown";
  target_commit: string;
  /** The producer's `capture_merge_base` — what Git said when its files were
   * captured, deliberately not the commit the producing task was launched
   * from. */
  producer_observation: string | null;
  changed_paths: string[];
  truncated: boolean;
  detail: string | null;
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

/** --- Durable task results (ADR-0034) ------------------------------------
 *
 * Every one of these is an ordinary authenticated request: the bearer token
 * travels in the header, never in a URL. Downloads therefore go through
 * `fetch` and a short-lived object URL rather than a plain link, which would
 * put a token-bearing address in browser history. */

/** A capture's whole response: the task's result document plus the fixed
 * bounds the form states before submission. */
export interface TaskResultsResponse extends TaskResultsProjection {
  limits: TaskResultLimits;
}

export function listTaskResults(taskId: number): Promise<TaskResultsResponse> {
  return request<TaskResultsResponse>("GET", `/api/tasks/${taskId}/results`);
}

/** Ask for a capture. `requestId` is the replay key: repeating a request with
 * the same selection answers with the original operation — including its
 * failure — so a lost response is recovered from result history rather than by
 * capturing whatever the workspace holds now. */
export function captureTaskResult(
  taskId: number,
  paths: string[],
  requestId: string,
): Promise<TaskResultsResponse> {
  return request<TaskResultsResponse>("POST", `/api/tasks/${taskId}/results`, {
    paths,
    request_id: requestId,
  });
}

export function getTaskResultFile(
  taskId: number,
  resultId: string,
  path: string,
): Promise<TaskResultFileContent> {
  return request<TaskResultFileContent>(
    "GET",
    `/api/tasks/${taskId}/results/${resultId}/file?path=${encodeURIComponent(path)}`,
  );
}

export function getTaskResultDiff(
  taskId: number,
  resultId: string,
): Promise<TaskResultDiff> {
  return request<TaskResultDiff>(
    "GET",
    `/api/tasks/${taskId}/results/${resultId}/diff`,
  );
}

/** Record the operator's decision about exactly this revision. The manifest
 * identity is what makes it exact: a stale page is refused rather than
 * retargeted at a newer capture. */
export function acceptTaskResult(
  taskId: number,
  resultId: string,
  expectedManifestId: string,
): Promise<TaskResultsResponse> {
  return request<TaskResultsResponse>(
    "POST",
    `/api/tasks/${taskId}/results/${resultId}/accept`,
    { expected_manifest_id: expectedManifestId },
  );
}

/** Delete one revision's retained files, permanently. Both expectations and
 * the acknowledgement are required by the daemon; there is no force variant. */
export function purgeTaskResult(
  taskId: number,
  resultId: string,
  expectedManifestId: string,
  expectedVersion: number,
): Promise<TaskResultsResponse> {
  return request<TaskResultsResponse>(
    "DELETE",
    `/api/tasks/${taskId}/results/${resultId}`,
    {
      expected_manifest_id: expectedManifestId,
      expected_version: expectedVersion,
      acknowledge_purge: true,
    },
  );
}

/** Fetch a download's bytes with the ordinary bearer header.
 *
 * Returns the blob and the filename the daemon named, so the caller can hand
 * the browser a short-lived object URL. Nothing here builds a URL a viewer
 * could share, and the daemon serves no unauthenticated result directory.
 */
export async function fetchTaskResultDownload(
  taskId: number,
  resultId: string,
  path?: string,
): Promise<{ blob: Blob; filename: string }> {
  const headers: Record<string, string> = {};
  const token = getDaemonToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const query = path === undefined ? "" : `?path=${encodeURIComponent(path)}`;
  const response = await fetch(
    `/api/tasks/${taskId}/results/${resultId}/download${query}`,
    { headers },
  );
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const data = (await response.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
    } catch {
      /* non-JSON error body; keep the status code */
    }
    throw new DaemonError(detail, response.status, null);
  }
  const disposition = response.headers.get("content-disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  return {
    blob: await response.blob(),
    filename: match ? match[1] : `${resultId}.zip`,
  };
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
export async function getWorkflowRevision(
  revision: string,
): Promise<WorkflowRevisionDetail> {
  const body = asObject(
    await requestLossless("GET", `/api/workflows/revisions/${encodeURIComponent(revision)}`),
  );
  return {
    revision: asString(body?.revision) ?? revision,
    name: asString(body?.name) ?? "",
    format: envelopeNumber(body?.format),
    primary_session: asString(body?.primary_session) ?? "",
    sessions: (Array.isArray(body?.sessions) ? body.sessions : []).map(
      (name) => asString(name) ?? "",
    ),
    definition: asObject(body?.definition) ?? {},
  };
}

/** Export one retained revision as a standalone YAML definition.
 *
 * Emitted from the retained document and verified to load back to the same
 * identity, so an export and a re-import are the same procedure. It is not the
 * text anybody typed — formatting and comments live in the entry's draft. */
export function exportWorkflowRevision(
  revision: string,
): Promise<{ revision: string; name: string; format: number; yaml: string }> {
  return request("GET", `/api/workflows/revisions/${encodeURIComponent(revision)}/yaml`);
}

/** Every library entry, archived and draft-only included (ADR-0031). */
export function listWorkflowLibrary(): Promise<WorkflowLibraryEntry[]> {
  return request<WorkflowLibraryEntry[]>("GET", "/api/workflow-library");
}

/** One entry with its raw draft text and retained revision history. */
export function getWorkflowEntry(name: string): Promise<WorkflowLibraryDetail> {
  return request<WorkflowLibraryDetail>(
    "GET",
    `/api/workflow-library/${encodeURIComponent(name)}`,
  );
}

/** Create a custom entry: from pasted or imported text, by duplicating a
 * retained revision, or — supplying neither — from the starter.
 *
 * None of the three validates or selects a revision. What is created is a
 * draft, and a draft cannot launch. */
export function createWorkflowEntry(input: {
  name: string;
  yaml?: string;
  source_revision?: string;
}): Promise<WorkflowLibraryDetail> {
  return request<WorkflowLibraryDetail>("POST", "/api/workflow-library", input);
}

/** Persist the editor's text as it stands — valid or not. Never changes what
 * the entry would launch. */
export function saveWorkflowDraft(
  name: string,
  yaml: string,
  expectedVersion: number,
): Promise<WorkflowLibraryDetail> {
  return request<WorkflowLibraryDetail>(
    "PUT",
    `/api/workflow-library/${encodeURIComponent(name)}/draft`,
    { yaml, expected_version: expectedVersion },
  );
}

/** Check this exact text. Nothing is saved, and the answer authorizes
 * nothing: an executable save re-validates what it is given. */
export async function validateWorkflow(
  yaml: string,
  name?: string,
): Promise<WorkflowValidation> {
  const body = await requestLossless("POST", "/api/workflow-library/validate", {
    yaml,
    ...(name !== undefined ? { name } : {}),
  });
  return readValidation(asObject(body) ?? {});
}

function readValidation(body: DraftObject): WorkflowValidation {
  return {
    revision: asString(body.revision) ?? "",
    name: asString(body.name) ?? "",
    format: envelopeNumber(body.format),
    definition: asObject(body.definition) ?? {},
    descriptor: body.descriptor as unknown as WorkflowDescriptor,
  };
}

/** Translate one draft between YAML text and structured data, and say what it
 * currently means.
 *
 * Stateless and inert: it persists nothing, retains no revision, and
 * authorizes no later save — an executable save still re-validates the exact
 * text it is handed. A draft that parses but is not yet a workflow comes back
 * whole, with a located reason, which is what lets half-built visual work be
 * saved and reopened. */
export async function convertWorkflowDocument(input: {
  yaml?: string;
  document?: DraftObject;
  name?: string;
}): Promise<WorkflowDocumentConversion> {
  const body = asObject(
    await requestLossless("POST", "/api/workflow-library/document", input),
  );
  const validation = asObject(body?.validation) ?? {};
  return {
    document: asObject(body?.document) ?? {},
    yaml: asString(body?.yaml) ?? "",
    validation:
      validation.ok === true
        ? { ok: true, ...readValidation(validation) }
        : {
            ok: false,
            reason: asString(validation.reason) ?? "workflow_document_invalid",
            location: asString(validation.location),
            message: asString(validation.message) ?? "This draft is not a workflow yet.",
            line: validation.line === undefined || validation.line === null
              ? null
              : envelopeNumber(validation.line),
            column: validation.column === undefined || validation.column === null
              ? null
              : envelopeNumber(validation.column),
            format: validation.format ?? null,
          },
  };
}

/** Validate this text, retain it, and make it the entry's current choice —
 * atomically, and without starting anything. */
export function saveWorkflowRevision(
  name: string,
  yaml: string,
  expectedVersion: number,
): Promise<WorkflowLibraryDetail> {
  return request<WorkflowLibraryDetail>(
    "POST",
    `/api/workflow-library/${encodeURIComponent(name)}/revisions`,
    { yaml, expected_version: expectedVersion },
  );
}

/** Take an entry out of future launch choices, or put it back. Nothing is
 * deleted either way. */
export function setWorkflowArchived(
  name: string,
  archived: boolean,
  expectedVersion: number,
): Promise<WorkflowLibraryDetail> {
  return request<WorkflowLibraryDetail>(
    "POST",
    `/api/workflow-library/${encodeURIComponent(name)}/${archived ? "archive" : "restore"}`,
    { expected_version: expectedVersion },
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

/** Ensure or explicitly replace publication text through the live agent. */
export function draftShip(
  id: number,
  options?: { replace: boolean },
): Promise<ShipProjection> {
  return request<ShipProjection>("POST", `/api/tasks/${id}/ship/draft`, options);
}

/** Persist operator-entered publication text. Inert: authorizes nothing. */
export function saveShipDraft(
  id: number,
  body: { commit_message: string; pr_title: string; pr_body: string },
): Promise<ShipProjection> {
  return request<ShipProjection>("PUT", `/api/tasks/${id}/ship/draft`, body);
}

/** Resolve one requested ending read-only.
 *
 * Returns the candidate and review identity, the remaining actions, the safe
 * targets and identities, every reason the delivery is refused, and the token a
 * confirmation must carry. It authorizes nothing. */
export function previewShip(
  id: number,
  body: {
    /** Which question is being previewed, and with which answer. Required
     * when the run is waiting at an approval: a preview that did not name
     * them would describe "whatever this task could publish", which is not a
     * decision anyone can confirm. */
    gate_seq?: number | null;
    choice_id?: string | null;
    /** Not requests. The run's pinned chain decides how far a delivery goes
     * and how it composes history; supplying either states what the caller
     * believes, and a disagreement is reported rather than obeyed. */
    ending?: ShipEnding | null;
    mode?: "squash" | "retain" | null;
    message?: string;
    pr_title?: string;
    pr_body?: string;
    request_id: string;
    delivery_id?: number | null;
  },
): Promise<ShipPreview> {
  return request<ShipPreview>("POST", `/api/tasks/${id}/ship/preview`, body);
}

/** Authorize one delivery and start its action prefix (ADR-0032). */
export function shipCommit(
  id: number,
  body: {
    gate_seq?: number | null;
    choice_id?: string | null;
    /** Feedback recorded with the decision, exactly as a gate answer's is. */
    note?: string | null;
    ending?: ShipEnding | null;
    mode?: "squash" | "retain" | null;
    message?: string;
    pr_title?: string;
    pr_body?: string;
    request_id: string;
    preview_token: string;
    delivery_id?: number | null;
    expected_version?: number | null;
  },
): Promise<ShipProjection> {
  return request<ShipProjection>("POST", `/api/tasks/${id}/ship/commit`, body);
}

/** Push an existing verified signed result, optionally continuing to a PR. */
export function shipPush(
  id: number,
  body: {
    ending?: ShipEnding | null;
    pr_title?: string;
    pr_body?: string;
    request_id: string;
    preview_token: string;
    delivery_id: number;
    expected_version: number;
  },
): Promise<ShipProjection> {
  return request<ShipProjection>("POST", `/api/tasks/${id}/ship/push`, body);
}

/** Open a pull request for an existing verified pushed result. */
export function shipPr(
  id: number,
  body: {
    ending?: ShipEnding | null;
    pr_title?: string;
    pr_body?: string;
    request_id: string;
    preview_token: string;
    delivery_id: number;
    expected_version: number;
  },
): Promise<ShipProjection> {
  return request<ShipProjection>("POST", `/api/tasks/${id}/ship/pr`, body);
}

/** Record one operator decision about an unresolved delivery effect.
 *
 * None of these write anything privileged: `retry` only makes a
 * proven-not-executed action eligible for a fresh preview and confirmation. */
export function shipReconcile(
  id: number,
  body: {
    delivery_id: number;
    action_id: number;
    expected_version: number;
    decision: "recheck" | "adopt" | "retry" | "abandon";
    note?: string | null;
    adopt_reference?: string | null;
  },
): Promise<ShipProjection> {
  return request<ShipProjection>("POST", `/api/tasks/${id}/ship/reconcile`, body);
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
