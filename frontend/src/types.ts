export interface Project {
  name: string;
  title: string;
  upstream_url: string;
  fork_url: string | null;
  checkout_path: string;
  base_branch: string;
  branch_pattern: string;
}

export type TaskState = "created" | "failed" | "archived";

export interface Task {
  id: number;
  project_name: string;
  slug: string;
  branch: string;
  clone_path: string;
  state: TaskState;
  prompt: string;
  error: string | null;
  spawn_completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export type SpawnStepName = "fetch" | "clone" | "branch";

export interface SpawnStepPayload {
  task_id: number;
  step: SpawnStepName;
  status: "started" | "ok" | "failed";
  stderr?: string;
}

export interface SnapshotPayload {
  projects: Project[];
  tasks: Task[];
}

export interface Envelope<T = unknown> {
  seq: number;
  ts: string;
  type: string;
  payload: T;
}

export type ConnectionState = "connecting" | "connected" | "disconnected" | "reconnecting";
