import type {
  AdvisoryClearedPayload,
  AdvisoryKind,
  AdvisoryPayload,
  AttentionClearedPayload,
  AttentionEntry,
  AttentionPayload,
  ConnectionState,
  Envelope,
  GpgStatus,
  GpgStatusPayload,
  Project,
  QuestionPostedPayload,
  QuestionResolvedPayload,
  ReviewFinishedPayload,
  ReviewIterationPayload,
  ReviewStartedPayload,
  ReviewState,
  SessionInfo,
  ShipDraftPayload,
  ShipFinishedPayload,
  ShipState,
  ShipStepPayload,
  SnapshotPayload,
  SpawnStepPayload,
  StatsPayload,
  StatusChangedPayload,
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
  /** Live session status per task id: loaded from the snapshot, upserted by
   * `status_changed`, dropped with the task. */
  sessions: Record<number, SessionInfo>;
  /** Active daemon attention entries per task id (attention-notifications
   * capability): loaded from the snapshot, upserted by `attention`, dropped
   * by `attention_cleared`/the task's own deletion. The single seam
   * `countNeedsAttention` reads (design D-7). */
  attention: Record<number, AttentionEntry>;
  /** Latest `stats` sample per task id (session-advisories capability):
   * transient, never part of the snapshot — a reconnect waits for the next
   * turn boundary. */
  stats: Record<number, StatsPayload>;
  /** Active advisory decorations per task id, keyed by kind
   * (session-advisories capability): transient, never part of the snapshot. */
  advisories: Record<number, Partial<Record<AdvisoryKind, AdvisoryPayload>>>;
  /** Live/completed reviews per task id (review capability): loaded from the
   * snapshot, upserted by review events, dropped by review_finished and by
   * task deletion/cleanup. */
  reviews: Record<number, ReviewState>;
  /** Live/completed ship flows per task id (ship capability): loaded from the
   * snapshot, upserted by ship events, dropped by task deletion/cleanup. */
  ships: Record<number, ShipState>;
  /** Current GPG signing-key cache state (ship capability): loaded from the
   * snapshot, upserted by gpg_status events. */
  gpg: GpgStatus | null;
}

export const initialDaemonState: DaemonState = {
  connectionState: "connecting",
  projects: [],
  tasks: [],
  spawnProgress: {},
  sessions: {},
  attention: {},
  stats: {},
  advisories: {},
  reviews: {},
  ships: {},
  gpg: null,
};

/** Applies one envelope from the daemon's WebSocket. `snapshot` is a full
 * state replacement; every other `type` is an incremental delta. Unknown
 * types are ignored so the frontend forward-compatibly tolerates event
 * types added by later ROADMAP chunks. */
export function applyEnvelope(state: DaemonState, envelope: Envelope): DaemonState {
  switch (envelope.type) {
    case "snapshot": {
      const payload = envelope.payload as SnapshotPayload;
      const sessions: Record<number, SessionInfo> = {};
      for (const [taskId, info] of Object.entries(payload.sessions ?? {})) {
        sessions[Number(taskId)] = info;
      }
      const attention: Record<number, AttentionEntry> = {};
      for (const [taskId, entry] of Object.entries(payload.attention ?? {})) {
        attention[Number(taskId)] = entry;
      }
      const reviews: Record<number, ReviewState> = {};
      for (const [taskId, review] of Object.entries(payload.reviews ?? {})) {
        reviews[Number(taskId)] = review;
      }
      const ships: Record<number, ShipState> = {};
      for (const [taskId, ship] of Object.entries(payload.ships ?? {})) {
        ships[Number(taskId)] = ship;
      }
      return {
        ...state,
        projects: payload.projects,
        tasks: payload.tasks,
        spawnProgress: {},
        sessions,
        attention,
        stats: {},
        advisories: {},
        reviews,
        ships,
        gpg: payload.gpg ?? null,
      };
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
      const { [id]: _droppedSession, ...sessions } = state.sessions;
      const { [id]: _droppedAttention, ...attention } = state.attention;
      const { [id]: _droppedStats, ...stats } = state.stats;
      const { [id]: _droppedAdvisories, ...advisories } = state.advisories;
      const { [id]: _droppedReview, ...reviews } = state.reviews;
      const { [id]: _droppedShip, ...ships } = state.ships;
      return {
        ...state,
        tasks: state.tasks.filter((t) => t.id !== id),
        spawnProgress,
        sessions,
        attention,
        stats,
        advisories,
        reviews,
        ships,
      };
    }
    case "spawn_step": {
      const step = envelope.payload as SpawnStepPayload;
      const existing = state.spawnProgress[step.task_id] ?? [];
      return {
        ...state,
        spawnProgress: { ...state.spawnProgress, [step.task_id]: [...existing, step] },
      };
    }
    case "status_changed": {
      const { task_id, to, reason } = envelope.payload as StatusChangedPayload;
      return {
        ...state,
        sessions: {
          ...state.sessions,
          [task_id]: { status: to, reason, since: envelope.ts },
        },
      };
    }
    case "question_posted": {
      const { task_id, question } = envelope.payload as QuestionPostedPayload;
      const existing = state.sessions[task_id];
      if (!existing) return state;
      return {
        ...state,
        sessions: { ...state.sessions, [task_id]: { ...existing, question } },
      };
    }
    case "question_resolved": {
      const { task_id } = envelope.payload as QuestionResolvedPayload;
      const existing = state.sessions[task_id];
      if (!existing?.question) return state;
      const { question: _dropped, ...rest } = existing;
      return { ...state, sessions: { ...state.sessions, [task_id]: rest } };
    }
    case "attention": {
      const { task_id, tier, status, reason } = envelope.payload as AttentionPayload;
      return {
        ...state,
        attention: { ...state.attention, [task_id]: { tier, status, reason } },
      };
    }
    case "attention_cleared": {
      const { task_id } = envelope.payload as AttentionClearedPayload;
      if (!(task_id in state.attention)) return state;
      const { [task_id]: _dropped, ...attention } = state.attention;
      return { ...state, attention };
    }
    case "stats": {
      const payload = envelope.payload as StatsPayload;
      return { ...state, stats: { ...state.stats, [payload.task_id]: payload } };
    }
    case "advisory": {
      const payload = envelope.payload as AdvisoryPayload;
      const existing = state.advisories[payload.task_id] ?? {};
      return {
        ...state,
        advisories: {
          ...state.advisories,
          [payload.task_id]: { ...existing, [payload.kind]: payload },
        },
      };
    }
    case "advisory_cleared": {
      const { task_id, kind } = envelope.payload as AdvisoryClearedPayload;
      const existing = state.advisories[task_id];
      if (!existing?.[kind]) return state;
      const { [kind]: _dropped, ...rest } = existing;
      return { ...state, advisories: { ...state.advisories, [task_id]: rest } };
    }
    case "review_started": {
      const { task_id, url, port } = envelope.payload as ReviewStartedPayload;
      const existing = state.reviews[task_id];
      return {
        ...state,
        reviews: {
          ...state.reviews,
          [task_id]: {
            status: "open",
            url,
            port,
            iterations: existing?.iterations ?? [],
          },
        },
      };
    }
    case "review_iteration": {
      const { task_id, iteration } = envelope.payload as ReviewIterationPayload;
      const existing = state.reviews[task_id];
      if (!existing) return state;
      return {
        ...state,
        reviews: {
          ...state.reviews,
          [task_id]: { ...existing, iterations: [...existing.iterations, iteration] },
        },
      };
    }
    case "review_finished": {
      const { task_id, status } = envelope.payload as ReviewFinishedPayload;
      const existing = state.reviews[task_id];
      if (!existing) return state;
      return {
        ...state,
        reviews: {
          ...state.reviews,
          [task_id]: { ...existing, status },
        },
      };
    }
    case "ship_draft": {
      const { task_id, draft } = envelope.payload as ShipDraftPayload;
      const existing = state.ships[task_id];
      return {
        ...state,
        ships: {
          ...state.ships,
          [task_id]: {
            ...(existing ?? {
              status: "drafted",
              draft: null,
              commit_sha: null,
              pr_url: null,
              error: null,
              updated_at: envelope.ts,
            }),
            status: "drafted",
            draft,
            updated_at: envelope.ts,
          } as ShipState,
        },
      };
    }
    case "ship_step": {
      const { task_id, step, status, detail } = envelope.payload as ShipStepPayload;
      const existing = state.ships[task_id];
      if (!existing) return state;
      const nextStatus: ShipState["status"] =
        step === "commit" ? "committing" : step === "push" || step === "pr" ? "pushing" : existing.status;
      return {
        ...state,
        ships: {
          ...state.ships,
          [task_id]: {
            ...existing,
            status: status === "failed" && existing.status !== "shipped" ? "error" : nextStatus,
            error: status === "failed" ? detail ?? null : existing.error,
            lastStep: { step, status, detail },
            updated_at: envelope.ts,
          },
        },
      };
    }
    case "ship_finished": {
      const { task_id, status, pr_url } = envelope.payload as ShipFinishedPayload;
      const existing = state.ships[task_id];
      if (!existing) return state;
      return {
        ...state,
        ships: {
          ...state.ships,
          [task_id]: {
            ...existing,
            status,
            pr_url: pr_url ?? existing.pr_url,
            updated_at: envelope.ts,
          },
        },
      };
    }
    case "gpg_status": {
      const { status } = envelope.payload as GpgStatusPayload;
      return { ...state, gpg: status };
    }
    default:
      return state;
  }
}

/** A task whose pipeline hasn't finished presents as "spawning" on cards. */
export function isSpawning(task: Task): boolean {
  return task.state === "created" && task.spawn_completed_at === null;
}
