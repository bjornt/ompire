import type {
  AgentStateData,
  AgentStatsData,
  DaemonInfo,
  DaemonSettings,
  GpgStatus,
  GitHubStatus,
  Project,
  ProjectFiles,
  ReviewState,
  ShipState,
  Task,
  TaskDetail,
  Template,
  ThinkingLevel,
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

/** Spawn is template-driven (task-spawn capability): the daemon resolves the
 * template, denormalizes its project onto the task, and derives the branch
 * from the template's pattern. `model`/`thinking` are per-spawn overrides —
 * omitted entirely when unset so the template value (or omp default) wins. */
export function spawnTask(input: {
  template_name: string;
  slug: string;
  prompt: string;
  model?: string;
  thinking?: ThinkingLevel;
}): Promise<Task> {
  return request<Task>("POST", "/api/tasks", input);
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

/** Resumes a workflow run parked at a gate (workflow-engine capability);
 * 409 when the run has already moved on. */
export function resumeWorkflow(
  id: number,
  note?: string,
): Promise<{ task_id: number; workflow: string; step: string | null }> {
  return request("POST", `/api/tasks/${id}/workflow/resume`, { note: note ?? null });
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
    new_name?: string;
  },
): Promise<Project> {
  return request<Project>("PUT", `/api/projects/${encodeURIComponent(name)}`, input);
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

/** Template CRUD (templates capability). The list itself arrives via the
 * WebSocket snapshot — these are commands only; render results from daemon
 * state, not from these return values. Templates have no view that reconciles
 * responses, so they rely on the event alone. */
export interface TemplateInput {
  project_name: string;
  base_branch: string;
  branch_pattern: string;
  workflow: string;
  workshop_additions: "project" | "global";
  model: string | null;
  thinking: ThinkingLevel | null;
  preamble: string;
}

export function createTemplate(input: TemplateInput & { name: string }): Promise<Template> {
  return request<Template>("POST", "/api/templates", input);
}

export function updateTemplate(name: string, input: TemplateInput): Promise<Template> {
  return request<Template>("PUT", `/api/templates/${encodeURIComponent(name)}`, input);
}

export function deleteTemplate(name: string): Promise<{ deleted: string }> {
  return request("DELETE", `/api/templates/${encodeURIComponent(name)}`);
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
