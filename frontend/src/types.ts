export interface Project {
  name: string;
  title: string;
  upstream_url: string;
  fork_url: string | null;
  checkout_path: string;
}

/** Placeholder shape — the daemon has no task entity yet (ROADMAP chunk 3). */
export interface TaskRecord {
  id: string;
  [key: string]: unknown;
}

export interface SnapshotPayload {
  projects: Project[];
  tasks: TaskRecord[];
}

export interface Envelope<T = unknown> {
  seq: number;
  ts: string;
  type: string;
  payload: T;
}

export type ConnectionState = "connecting" | "connected" | "disconnected" | "reconnecting";
