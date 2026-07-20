import type { AgentStateData, AgentStatsData, Task, TaskDetail } from "../types";
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

export function spawnTask(input: {
  project_name: string;
  slug: string;
  prompt: string;
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
