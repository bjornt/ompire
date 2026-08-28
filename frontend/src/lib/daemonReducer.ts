import type {
  AdvisoryClearedPayload,
  AdvisoryKind,
  AdvisoryPayload,
  AttentionClearedPayload,
  AttentionEntry,
  AttentionPayload,
  ConnectionState,
  DaemonSettings,
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
  SettingsChangedPayload,
  ShipDraftPayload,
  ShipFinishedPayload,
  ShipState,
  ShipStepPayload,
  SnapshotPayload,
  SpawnStepPayload,
  StatsPayload,
  StatusChangedPayload,
  StepRecord,
  Task,
  Template,
  WorkflowState,
  WorkflowStepPayload,
} from "../types";

export interface DaemonState {
  connectionState: ConnectionState;
  /** A route may make an absence decision only after the current main socket
   * has delivered its authoritative replacement snapshot. Connection open
   * alone is insufficient: it precedes that first message. */
  snapshotReady: boolean;
  projects: Project[];
  /** Template registry (templates capability), keyed by name like projects:
   * loaded from the snapshot, upserted by `template_created`/
   * `template_updated`, dropped by `template_deleted`. */
  templates: Template[];
  tasks: Task[];
  /** Transient per-task spawn pipeline progress, keyed by task id. Fed by
   * `spawn_step` events, never part of the snapshot — a reconnect drops it,
   * and the persisted task state is authoritative from then on. */
  spawnProgress: Record<number, SpawnStepPayload[]>;
  /** Live session statuses per task id, then per session name
   * (workflow-engine design D-7): loaded from the snapshot, upserted by the
   * session-carrying `status_changed`, dropped with the task. */
  sessions: Record<number, Record<string, SessionInfo>>;
  /** Per-task workflow run state (workflow-engine capability): loaded from
   * the snapshot, advanced by `workflow_step` events, and synced from the
   * persisted workflow fields on `task_created`/`task_updated`. */
  workflows: Record<number, WorkflowState>;
  /** Active daemon attention entries per task id (attention-notifications
   * capability): loaded from the snapshot, upserted by `attention`, dropped
   * by `attention_cleared`/the task's own deletion. The single seam
   * `countNeedsAttention` reads (design D-7). */
  attention: Record<number, AttentionEntry>;
  /** Latest `stats` sample per task id and session name
   * (session-advisories capability): transient, never part of the snapshot —
   * a reconnect waits for the next turn boundary. */
  stats: Record<number, Record<string, StatsPayload>>;
  /** Active advisory decorations per task id, then per session name and kind
   * (session-advisories capability): transient, never part of the snapshot. */
  advisories: Record<number, Record<string, Partial<Record<AdvisoryKind, AdvisoryPayload>>>>;
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
  /** Effective daemon settings (daemon-settings capability): loaded from the
   * snapshot, replaced by `settings_changed` events. */
  settings: DaemonSettings;
}

export const initialDaemonState: DaemonState = {
  connectionState: "connecting",
  snapshotReady: false,
  projects: [],
  templates: [],
  tasks: [],
  spawnProgress: {},
  sessions: {},
  workflows: {},
  attention: {},
  stats: {},
  advisories: {},
  reviews: {},
  ships: {},
  gpg: null,
  settings: {},
};


// Architecture: ADR-0004 (docs/adr/0004-use-rest-and-websocket-snapshot-deltas.md)
/** Applies one envelope from the daemon's WebSocket. `snapshot` is a full
 * state replacement; every other `type` is an incremental delta. Unknown
 * types are ignored so the frontend forward-compatibly tolerates event
 * types added by later ROADMAP chunks. */
export function applyEnvelope(state: DaemonState, envelope: Envelope): DaemonState {
  switch (envelope.type) {
    case "snapshot": {
      const payload = envelope.payload as SnapshotPayload;
      const sessions: Record<number, Record<string, SessionInfo>> = {};
      for (const [taskId, perSession] of Object.entries(payload.sessions ?? {})) {
        sessions[Number(taskId)] = perSession;
      }
      const workflows: Record<number, WorkflowState> = {};
      for (const [taskId, workflow] of Object.entries(payload.workflows ?? {})) {
        workflows[Number(taskId)] = workflow;
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
        snapshotReady: true,
        projects: payload.projects,
        templates: payload.templates ?? [],
        tasks: payload.tasks,
        spawnProgress: {},
        sessions,
        workflows,
        attention,
        stats: {},
        advisories: {},
        reviews,
        ships,
        gpg: payload.gpg ?? null,
        settings: payload.settings ?? {},
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
    case "project_renamed": {
      // Renames change the key `project_updated` matches on, so they carry
      // the old name alongside the new payload (projects capability).
      const { old_name, project } = envelope.payload as { old_name: string; project: Project };
      return {
        ...state,
        projects: state.projects.map((p) => (p.name === old_name ? project : p)),
      };
    }
    case "project_deleted": {
      const { name } = envelope.payload as { name: string };
      return { ...state, projects: state.projects.filter((p) => p.name !== name) };
    }
    case "template_created": {
      const template = envelope.payload as Template;
      return { ...state, templates: [...state.templates, template] };
    }
    case "template_updated": {
      const template = envelope.payload as Template;
      return {
        ...state,
        templates: state.templates.map((t) => (t.name === template.name ? template : t)),
      };
    }
    case "template_deleted": {
      const { name } = envelope.payload as { name: string };
      return { ...state, templates: state.templates.filter((t) => t.name !== name) };
    }
    case "task_created": {
      const task = envelope.payload as Task;
      return { ...state, tasks: [task, ...state.tasks], workflows: syncWorkflow(state, task) };
    }
    case "task_updated": {
      const task = envelope.payload as Task;
      return {
        ...state,
        tasks: state.tasks.map((t) => (t.id === task.id ? task : t)),
        workflows: syncWorkflow(state, task),
      };
    }
    case "task_deleted": {
      const { id } = envelope.payload as { id: number };
      const { [id]: _dropped, ...spawnProgress } = state.spawnProgress;
      const { [id]: _droppedSession, ...sessions } = state.sessions;
      const { [id]: _droppedWorkflow, ...workflows } = state.workflows;
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
        workflows,
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
      const { task_id, session, to, reason } = envelope.payload as StatusChangedPayload;
      const perSession = state.sessions[task_id] ?? {};
      return {
        ...state,
        sessions: {
          ...state.sessions,
          [task_id]: {
            ...perSession,
            [session]: { status: to, reason, since: envelope.ts },
          },
        },
      };
    }
    case "question_posted": {
      const { task_id, session, question } = envelope.payload as QuestionPostedPayload;
      const perSession = state.sessions[task_id];
      const existing = perSession?.[session];
      if (!perSession || !existing) return state;
      return {
        ...state,
        sessions: {
          ...state.sessions,
          [task_id]: { ...perSession, [session]: { ...existing, question } },
        },
      };
    }
    case "question_resolved": {
      const { task_id, session } = envelope.payload as QuestionResolvedPayload;
      const perSession = state.sessions[task_id];
      const existing = perSession?.[session];
      if (!perSession || !existing?.question) return state;
      const { question: _dropped, ...rest } = existing;
      return {
        ...state,
        sessions: { ...state.sessions, [task_id]: { ...perSession, [session]: rest } },
      };
    }
    case "attention": {
      const { task_id, tier, status, reason, session } = envelope.payload as AttentionPayload;
      return {
        ...state,
        attention: { ...state.attention, [task_id]: { tier, status, reason, session } },
      };
    }
    case "attention_cleared": {
      const { task_id } = envelope.payload as AttentionClearedPayload;
      if (!(task_id in state.attention)) return state;
      const { [task_id]: _dropped, ...attention } = state.attention;
      return { ...state, attention };
    }
    case "settings_changed": {
      const { settings } = envelope.payload as SettingsChangedPayload;
      return { ...state, settings };
    }
    case "stats": {
      const payload = envelope.payload as StatsPayload;
      const perSession = state.stats[payload.task_id] ?? {};
      return {
        ...state,
        stats: {
          ...state.stats,
          [payload.task_id]: { ...perSession, [payload.session]: payload },
        },
      };
    }
    case "advisory": {
      const payload = envelope.payload as AdvisoryPayload;
      const perSession = state.advisories[payload.task_id] ?? {};
      const existing = perSession[payload.session] ?? {};
      return {
        ...state,
        advisories: {
          ...state.advisories,
          [payload.task_id]: {
            ...perSession,
            [payload.session]: { ...existing, [payload.kind]: payload },
          },
        },
      };
    }
    case "advisory_cleared": {
      const { task_id, session, kind } = envelope.payload as AdvisoryClearedPayload;
      const perSession = state.advisories[task_id];
      const existing = perSession?.[session];
      if (!perSession || !existing?.[kind]) return state;
      const { [kind]: _dropped, ...rest } = existing;
      return {
        ...state,
        advisories: { ...state.advisories, [task_id]: { ...perSession, [session]: rest } },
      };
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
      const existing: ShipState = state.ships[task_id] ?? {
        status: "drafted",
        mode: "squash",
        draft: null,
        commit_sha: null,
        pr_url: null,
        error: null,
        updated_at: envelope.ts,
        last_step: null,
      };
      return {
        ...state,
        ships: {
          ...state.ships,
          [task_id]: {
            ...existing,
            status: "drafted",
            draft,
            error: null,
            last_step: { step: "draft", status: "ok" },
            updated_at: envelope.ts,
          },
        },
      };
    }
    case "ship_step": {
      const { task_id, step, status, detail } = envelope.payload as ShipStepPayload;
      const existing: ShipState = state.ships[task_id] ?? {
        status: "drafting",
        mode: "squash",
        draft: null,
        commit_sha: null,
        pr_url: null,
        error: null,
        updated_at: envelope.ts,
        last_step: null,
      };
      let nextStatus = existing.status;
      if (status === "failed" && existing.status !== "shipped") {
        nextStatus = "error";
      } else if (step === "draft") {
        if (status === "started") nextStatus = "drafting";
        else if (existing.draft !== null) nextStatus = "drafted";
      } else if (step === "commit") {
        nextStatus = "committing";
      } else if (step === "push" || step === "pr") {
        nextStatus = "pushing";
      }
      const lastStep = {
        step,
        status,
        ...(detail === undefined ? {} : { detail }),
      };
      return {
        ...state,
        ships: {
          ...state.ships,
          [task_id]: {
            ...existing,
            status: nextStatus,
            error:
              status === "failed"
                ? typeof detail === "string"
                  ? detail
                  : "Ship step failed"
                : status === "started"
                  ? null
                  : existing.error,
            last_step: lastStep,
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
    case "workflow_step": {
      const p = envelope.payload as WorkflowStepPayload;
      const existing = state.workflows[p.task_id];
      const steps = [...(existing?.steps ?? [])];
      const nextSeq = steps.reduce((max, record) => Math.max(max, record.seq), 0) + 1;
      if (p.status === "started") {
        // Each execution appends a record daemon-side (append_step_record,
        // seq = max+1) — a looped/retried step gets one record per run.
        steps.push({
          task_id: p.task_id,
          seq: nextSeq,
          step: p.step,
          kind: p.kind,
          session: p.session,
          status: "running",
          outcome: null,
          error: null,
          prompted_at: null,
          started_at: envelope.ts,
          finished_at: null,
        });
      } else {
        // ok/failed/waiting close out the newest record for this step
        // name+kind (a decision-escalation gate shares the decision's name,
        // so kind disambiguates). Tolerate a terminal event whose `started`
        // we never saw (e.g. joined mid-stream).
        const idx = steps.findLastIndex((s) => s.step === p.step && s.kind === p.kind);
        const outcome =
          p.status === "waiting" && p.message !== undefined ? { message: p.message } : null;
        const updated: StepRecord = {
          task_id: p.task_id,
          seq: idx >= 0 ? steps[idx].seq : nextSeq,
          step: p.step,
          kind: p.kind,
          session: p.session,
          status: p.status,
          outcome: outcome ?? (idx >= 0 ? steps[idx].outcome : null),
          error: p.status === "failed" ? (p.error ?? null) : idx >= 0 ? steps[idx].error : null,
          prompted_at: idx >= 0 ? steps[idx].prompted_at : null,
          started_at: idx >= 0 ? steps[idx].started_at : envelope.ts,
          finished_at: p.status === "waiting" ? null : envelope.ts,
        };
        if (idx >= 0) steps[idx] = updated;
        else steps.push(updated);
      }
      const runStatus =
        p.status === "started"
          ? "running"
          : p.status === "waiting"
            ? "waiting"
            : p.status === "failed"
              ? "failed"
              : // An `ok` keeps the run's last known status; the paired
                // task_updated lands the authoritative value (complete, or
                // the next step's running).
                (existing?.status ?? "running");
      return {
        ...state,
        workflows: {
          ...state.workflows,
          [p.task_id]: {
            name:
              existing?.name ??
              state.tasks.find((t) => t.id === p.task_id)?.workflow_name ??
              "",
            status: runStatus,
            step: p.step,
            steps,
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

/** Syncs the workflows slice from a task payload's persisted workflow fields
 * (workflow-engine design D-9), keeping any step records already accumulated
 * from the snapshot/`workflow_step` events. */
function syncWorkflow(state: DaemonState, task: Task): Record<number, WorkflowState> {
  const existing = state.workflows[task.id];
  // Before a run starts the row carries only the workflow name; the daemon's
  // snapshot still emits an entry per task, so mirror that here.
  return {
    ...state.workflows,
    [task.id]: {
      name: task.workflow_name,
      status: task.workflow_status,
      step: task.workflow_step,
      steps: existing?.steps ?? [],
    },
  };
}

/** A run is in flight while `running` or parked `waiting` at a gate. */
export function workflowActive(workflow: WorkflowState | undefined): boolean {
  return workflow?.status === "running" || workflow?.status === "waiting";
}

/** Ordered session names for a task's tabs and cards (workflow-engine design
 * D-9): workflow step order first, then any tracker-only sessions, defaulting
 * to the single-step workflow's lone `main` session. The daemon doesn't expose
 * a workflow's declared sessions before its steps run, so the list grows as
 * records land. */
export function taskSessionNames(
  taskSessions: Record<string, SessionInfo> | undefined,
  workflow: WorkflowState | undefined,
): string[] {
  const names: string[] = [];
  for (const record of workflow?.steps ?? []) {
    if (record.session !== null && !names.includes(record.session)) names.push(record.session);
  }
  for (const name of Object.keys(taskSessions ?? {})) {
    if (!names.includes(name)) names.push(name);
  }
  if (names.length === 0) names.push("main");
  return names;
}

/** The newest executed record for the run's current step, if any. */
export function currentStepRecord(workflow: WorkflowState | undefined): StepRecord | undefined {
  if (!workflow?.step) return undefined;
  return [...workflow.steps].reverse().find((record) => record.step === workflow.step);
}

/** Built-in workflow primaries are not part of the current WebSocket payload.
 * Keep the task-scoped selector explicit for those workflows; unknown or
 * legacy workflows retain the first known session fallback. */
const PRIMARY_SESSION_BY_WORKFLOW: Record<string, string> = {
  "single-step": "main",
  bugfix: "coder",
};

/** The workflow-declared primary session for task-scoped operations such as
 * review and publishing. It deliberately ignores the current workflow step
 * and any UI tab selection. */
export function primarySessionName(
  taskSessions: Record<string, SessionInfo> | undefined,
  workflow: WorkflowState | undefined,
): string {
  const configured = workflow === undefined ? undefined : PRIMARY_SESSION_BY_WORKFLOW[workflow.name];
  return configured ?? taskSessionNames(taskSessions, workflow)[0];
}

/** The session a task's surfaces focus by default (workflow-engine design
 * D-9): the current step's session while the run is in flight, else the
 * primary session. */
export function defaultSessionName(
  taskSessions: Record<string, SessionInfo> | undefined,
  workflow: WorkflowState | undefined,
): string {
  if (workflowActive(workflow)) {
    const current = currentStepRecord(workflow);
    if (current?.session) return current.session;
  }
  return primarySessionName(taskSessions, workflow);
}
