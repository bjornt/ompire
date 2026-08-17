import type {
  AgentStateData,
  AgentStatsData,
  GpgStatus,
  Project,
  ReviewState,
  ShipState,
  Task,
  TaskDetail,
  Template,
  ThinkingLevel,
} from "../types";
import { getDaemonToken } from "./token";

/** Minimal authenticated REST client. Commands go over REST, events come
 * back over the WebSocket (SPEC Decision 2) — callers should render results
 * from daemon state, not from these return values. */
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

/** Composer modes — each proxies to the live agent (agent-interaction).
 * `interrupt` aborts the current turn and re-prompts (`abort_and_prompt`). */
export function steerAgent(id: number, message: string): Promise<unknown> {
  return request("POST", `/api/tasks/${id}/agent/steer`, { message });
}

export function followUpAgent(id: number, message: string): Promise<unknown> {
  return request("POST", `/api/tasks/${id}/agent/follow-up`, { message });
}

export function interruptAgent(id: number, message: string): Promise<unknown> {
  return request("POST", `/api/tasks/${id}/agent/interrupt`, { message });
}

export function getAgentState(id: number): Promise<AgentStateData> {
  return request<AgentStateData>("GET", `/api/tasks/${id}/agent/state`);
}

export function getAgentStats(id: number): Promise<AgentStatsData> {
  return request<AgentStatsData>("GET", `/api/tasks/${id}/agent/stats`);
}

/** Answers a task's pending ask/approval question (ask-approvals capability). */
export function answerAgent(
  id: number,
  answer: { question_id: string; selections?: string[]; text?: string; approved?: boolean },
): Promise<unknown> {
  return request("POST", `/api/tasks/${id}/agent/answer`, answer);
}

/** Start an llmvet review for an idle task (review capability). */
export function startReview(id: number): Promise<ReviewState> {
  return request<ReviewState>("POST", `/api/tasks/${id}/review`);
}

/** Cancel an open llmvet review (review capability). */
export function cancelReview(id: number): Promise<ReviewState> {
  return request<ReviewState>("POST", `/api/tasks/${id}/review/cancel`);
}

/** Draft a commit message + PR title/body via the live agent (ship capability). */
export function draftShip(id: number): Promise<ShipState> {
  return request<ShipState>("POST", `/api/tasks/${id}/ship/draft`);
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

export function deleteProject(name: string): Promise<{ deleted: string }> {
  return request("DELETE", `/api/projects/${encodeURIComponent(name)}`);
}

/** Template CRUD (templates capability). The list itself arrives via the
 * WebSocket snapshot — these are commands only; render results from daemon
 * state, not from these return values. */
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
