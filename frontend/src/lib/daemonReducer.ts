import type {
  ConnectionState,
  Envelope,
  Project,
  SnapshotPayload,
  SpawnStepPayload,
  Task,
} from "../types";

export interface DaemonState {
  connectionState: ConnectionState;
  projects: Project[];
  tasks: Task[];
  /** Transient per-task spawn pipeline progress, keyed by task id. Fed by
   * `spawn_step` events, never part of the snapshot — a reconnect drops it,
   * and the persisted task state is authoritative from then on. */
  spawnProgress: Record<number, SpawnStepPayload[]>;
}

export const initialDaemonState: DaemonState = {
  connectionState: "connecting",
  projects: [],
  tasks: [],
  spawnProgress: {},
};

/** Applies one envelope from the daemon's WebSocket. `snapshot` is a full
 * state replacement; every other `type` is an incremental delta. Unknown
 * types are ignored so the frontend forward-compatibly tolerates event
 * types added by later ROADMAP chunks. */
export function applyEnvelope(state: DaemonState, envelope: Envelope): DaemonState {
  switch (envelope.type) {
    case "snapshot": {
      const payload = envelope.payload as SnapshotPayload;
      return { ...state, projects: payload.projects, tasks: payload.tasks, spawnProgress: {} };
    }
    case "project_created": {
      const project = envelope.payload as Project;
      return { ...state, projects: [...state.projects, project] };
    }
    case "project_updated": {
      const project = envelope.payload as Project;
      return {
        ...state,
        projects: state.projects.map((p) => (p.name === project.name ? project : p)),
      };
    }
    case "project_deleted": {
      const { name } = envelope.payload as { name: string };
      return { ...state, projects: state.projects.filter((p) => p.name !== name) };
    }
    case "task_created": {
      const task = envelope.payload as Task;
      return { ...state, tasks: [task, ...state.tasks] };
    }
    case "task_updated": {
      const task = envelope.payload as Task;
      return {
        ...state,
        tasks: state.tasks.map((t) => (t.id === task.id ? task : t)),
      };
    }
    case "task_deleted": {
      const { id } = envelope.payload as { id: number };
      const { [id]: _dropped, ...spawnProgress } = state.spawnProgress;
      return { ...state, tasks: state.tasks.filter((t) => t.id !== id), spawnProgress };
    }
    case "spawn_step": {
      const step = envelope.payload as SpawnStepPayload;
      const existing = state.spawnProgress[step.task_id] ?? [];
      return {
        ...state,
        spawnProgress: { ...state.spawnProgress, [step.task_id]: [...existing, step] },
      };
    }
    default:
      return state;
  }
}

/** A task whose pipeline hasn't finished presents as "spawning" on cards. */
export function isSpawning(task: Task): boolean {
  return task.state === "created" && task.spawn_completed_at === null;
}
