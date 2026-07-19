import type { Task, TaskDetail } from "../types";
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
