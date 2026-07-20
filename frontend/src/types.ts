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
  workshop_id: string | null;
  spawn_completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export type WorkshopStatus = "present" | "absent" | "unknown";

/** GET /api/tasks/:id — the derived status is only computed on detail fetches. */
export interface TaskDetail extends Task {
  workshop_status: WorkshopStatus | null;
}

export type SpawnStepName = "fetch" | "clone" | "branch" | "workshop" | "agent" | "prompt";

export interface SpawnStepPayload {
  task_id: number;
  step: SpawnStepName;
  status: "started" | "ok" | "failed";
  stderr?: string;
}

/** SPEC Decision 4 core subset; later chunks add waiting/reviewing/etc. */
export type SessionStatus = "starting" | "working" | "idle" | "failed";

export interface SessionInfo {
  status: SessionStatus;
  reason: string;
  since: string;
}

export interface StatusChangedPayload {
  task_id: number;
  from: SessionStatus | null;
  to: SessionStatus;
  reason: string;
}

export interface SnapshotPayload {
  projects: Project[];
  tasks: Task[];
  /** Keyed by task id (JSON object keys arrive as strings). */
  sessions: Record<string, SessionInfo>;
}

export interface Envelope<T = unknown> {
  seq: number;
  ts: string;
  type: string;
  payload: T;
}

export type ConnectionState = "connecting" | "connected" | "disconnected" | "reconnecting";
