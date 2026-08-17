export interface Project {
  name: string;
  title: string;
  upstream_url: string;
  fork_url: string | null;
  checkout_path: string;
}

/** Thinking levels omp accepts (`--thinking`, verified against omp
 * v17.2.12); null on a template or unset on a spawn override means the omp
 * default. */
export type ThinkingLevel = "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "auto";

/** SPEC Decision 6 setup template: everything spawn needs. Checkout path and
 * remotes come from the referenced project (Decision 9), never stored here. */
export interface Template {
  name: string;
  project_name: string;
  base_branch: string;
  branch_pattern: string;
  workflow: string;
  workshop_additions: "project" | "global";
  model: string | null;
  thinking: ThinkingLevel | null;
  preamble: string;
  created_at: string;
  updated_at: string;
}

export type TaskState = "created" | "failed" | "archived";

/** Durable polled PR state (merge-poll capability): null until the first
 * successful poll; terminal states are never polled again. */
export type PrState = "open" | "merged" | "closed";

export interface Task {
  id: number;
  project_name: string;
  /** Template this task was spawned from; null for tasks that predate
   * templates (templates capability). */
  template_name: string | null;
  slug: string;
  branch: string;
  clone_path: string;
  state: TaskState;
  prompt: string;
  error: string | null;
  workshop_id: string | null;
  spawn_completed_at: string | null;
  pr_url: string | null;
  pr_state: PrState | null;
  pr_merged_at: string | null;
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

/** SPEC Decision 4: core subset plus the `ask-approvals` waiting states,
 * the notifications/attention chunk's `stalled`/`retrying`, and the review
 * chunk's `reviewing`. */
export type SessionStatus =
  | "starting"
  | "working"
  | "idle"
  | "failed"
  | "waiting-input"
  | "waiting-approval"
  | "stalled"
  | "retrying"
  | "reviewing";

/** SPEC Decision 4 attention tier, owned by the daemon's notifier
 * (attention-notifications capability): `notify`/`interrupt` are the only
 * tiers that ever appear in an `attention` entry — `silent`/`badge` sessions
 * never get one. */
export type AttentionTier = "silent" | "badge" | "notify" | "interrupt";

/** An active daemon attention entry (attention-notifications capability):
 * present for a task while its session is in the `notify`/`interrupt` tier,
 * driving the "N need you" count, tab-title badge, and favicon badge. */
export interface AttentionEntry {
  tier: AttentionTier;
  status: SessionStatus;
  reason: string;
}

export interface AttentionPayload extends AttentionEntry {
  task_id: number;
}

export interface AttentionClearedPayload {
  task_id: number;
}

/** GET .../agent/stats-shaped `stats` event (session-advisories capability):
 * throttled per task at each turn boundary. */
export interface StatsPayload {
  task_id: number;
  context_pct: number | null;
  tokens: { input?: number; output?: number } | null;
  cost: number | null;
}

export type AdvisoryKind = "context-high" | "maybe-waiting";

/** An advisory decoration (session-advisories capability): never a session
 * state, never contributes to the attention tier. `context_pct` is present
 * only for `context-high`. */
export interface AdvisoryPayload {
  task_id: number;
  kind: AdvisoryKind;
  context_pct?: number;
}

export interface AdvisoryClearedPayload {
  task_id: number;
  kind: AdvisoryKind;
}

/** Normalized pending-question payload (ask-approvals capability, design
 * D-4): `kind` distinguishes an `ask` question from an approval gate; only
 * `ask` questions carry structured `questions`. */
export type PendingQuestionKind = "ask" | "approval";

export interface PendingOption {
  value: string;
  label: string;
  description: string | null;
}

export interface PendingAskQuestion {
  prompt: string;
  options: PendingOption[];
  multi: boolean;
  recommended: string | null;
  allowsOther: boolean;
}

export interface PendingQuestion {
  id: string;
  kind: PendingQuestionKind;
  questions: PendingAskQuestion[];
}

export interface SessionInfo {
  status: SessionStatus;
  reason: string;
  since: string;
  /** Present while the session is `waiting-input` / `waiting-approval`. */
  question?: PendingQuestion;
}

export interface QuestionPostedPayload {
  task_id: number;
  question: PendingQuestion;
}

export interface QuestionResolvedPayload {
  task_id: number;
  question_id: string;
}

/** GET /api/tasks/:id/agent/state — the agent's `get_state` `data`, passed
 * through untouched by the daemon. Field names beyond isStreaming/queued are
 * read defensively (this change's open SPEC question); unknown keys tolerated. */
export interface AgentStateData {
  isStreaming?: boolean;
  queuedMessageCount?: number;
  todos?: unknown;
  model?: string;
  modelId?: string;
  [key: string]: unknown;
}

/** GET /api/tasks/:id/agent/stats — the agent's `get_session_stats` `data`. */
export interface AgentStatsData {
  inputTokens?: number;
  outputTokens?: number;
  totalCostUsd?: number;
  [key: string]: unknown;
}

export interface StatusChangedPayload {
  task_id: number;
  from: SessionStatus | null;
  to: SessionStatus;
  reason: string;
}

export interface ReviewIteration {
  outcome: "approved" | "comments" | "aborted" | "error";
  comment_count: number | null;
  stderr: string | null;
  recorded_at: string;
}

export interface ReviewState {
  status: "open" | "approved" | "aborted" | "error";
  url: string;
  port: number;
  iterations: ReviewIteration[];
}

export interface ReviewStartedPayload {
  task_id: number;
  url: string;
  port: number;
}

export interface ReviewIterationPayload {
  task_id: number;
  iteration: ReviewIteration;
}

export interface ReviewFinishedPayload {
  task_id: number;
  status: "approved" | "aborted" | "error";
}

export interface ShipDraft {
  commit_message: string;
  pr_title: string;
  pr_body: string;
  source: "agent" | "manual";
}

export type ShipStatus = "drafting" | "drafted" | "committing" | "pushing" | "shipped" | "error";

export interface ShipState {
  status: ShipStatus;
  mode?: "squash" | "retain";
  draft: ShipDraft | null;
  commit_sha: string | null;
  pr_url: string | null;
  error: string | null;
  updated_at: string;
  /** Augmented by the reducer from ship_step events for UI progress. */
  lastStep?: { step: ShipStepName; status: "ok" | "failed"; detail?: string } | null;
}

export type ShipStepName = "fetch" | "commit" | "push" | "pr";

export interface ShipDraftPayload {
  task_id: number;
  draft: ShipDraft;
}

export interface ShipStepPayload {
  task_id: number;
  step: ShipStepName;
  status: "ok" | "failed";
  detail?: string;
}

export interface ShipFinishedPayload {
  task_id: number;
  status: "shipped" | "error";
  pr_url?: string;
}

export interface GpgStatus {
  state: "cached" | "locked" | "unknown";
  key: string | null;
  keygrip: string | null;
  detail: string | null;
  checked_at: string;
  /** Seconds remaining in the gpg-agent cache, when reported. */
  ttl?: number | null;
}

export interface GpgStatusPayload {
  status: GpgStatus;
}

export interface SnapshotPayload {
  projects: Project[];
  templates: Template[];
  tasks: Task[];
  /** Keyed by task id (JSON object keys arrive as strings). */
  sessions: Record<string, SessionInfo>;
  /** Active attention entries, keyed by task id (JSON object keys arrive as
   * strings); absent from snapshots emitted before this chunk. */
  attention?: Record<string, AttentionEntry>;
  /** Live/completed reviews, keyed by task id (JSON object keys arrive as
   * strings); absent from snapshots emitted before the review chunk. */
  reviews?: Record<string, ReviewState>;
  /** Live/completed ship flows, keyed by task id (JSON object keys arrive as
   * strings); absent from snapshots emitted before the ship chunk. */
  ships?: Record<string, ShipState>;
  /** Current GPG signing-key cache state; absent from snapshots emitted before
   * the ship chunk. */
  gpg?: GpgStatus;
}

export interface Envelope<T = unknown> {
  seq: number;
  ts: string;
  type: string;
  payload: T;
}

export type ConnectionState = "connecting" | "connected" | "disconnected" | "reconnecting";
