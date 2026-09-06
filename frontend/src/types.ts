/** `checkout_mode` says who owns the base checkout: `adopted` is the
 * operator's own, `cloned` was created by Ompire (ADR-0022). `fetch_remote` is
 * the remote spawn fetches in *that* checkout — unrelated to the per-task
 * clone's `origin`. */
export interface Project {
  name: string;
  title: string;
  upstream_url: string;
  fork_url: string | null;
  checkout_path: string;
  checkout_mode: "adopted" | "cloned";
  fetch_remote: string;
  setup_state: "ready" | "cloning" | "failed";
  setup_error: string | null;
  /** The global model profile this project selects as its default, or null
   * (ADR-0025). A launch inherits it unless the operator selects a task
   * profile; what a task then runs is the snapshot pinned at acceptance. */
  default_model_profile: string | null;
  /** Workspace and prompt defaults a launch inherits (ADR-0026). Each is
   * independently overridable for one task. */
  base_branch: string;
  branch_pattern: string;
  workshop_additions: WorkshopAdditionsSource;
  preamble: string;
  /** Whether the configuration carried over from templates still needs the
   * operator's decision. Independent of `setup_state` — a ready checkout can
   * still be unlaunchable, and both are reported so the UI can say which. */
  launch_config_state: "reconciled" | "needs-reconciliation";
}

/** Which additions file a task's clone gets. Exclusive: the project's own or
 * the operator's global one, never an implicit fallback to the other. */
export type WorkshopAdditionsSource = "project" | "global";

/** Read-only look at a candidate checkout, used by the create form to
 * prefill and to explain a refusal before submission. */
export interface CheckoutInspection {
  ok: boolean;
  reason: string;
  detail: string;
  remotes: { name: string; url: string }[];
  suggested_upstream: string | null;
  suggested_fork: string | null;
}

/** Live progress of a clone-mode project's setup. Ephemeral: the durable
 * outcome is the project's own `setup_state`/`setup_error`. */
export interface ProjectSetupStep {
  project: string;
  step: string;
  status: "started" | "ok" | "failed";
  stderr?: string;
}

/** Repository-relative paths for the Spawn prompt's `@` mentions. Names
 * only — the daemon never returns file contents (add-spawn-file-mentions). */
export interface ProjectFiles {
  paths: string[];
  truncated: boolean;
}

/** Thinking levels omp accepts (`--thinking`, verified against omp v18.1.10).
 * Every binding carries one explicitly: `off` and `auto` are policies, not
 * absence. omp may resolve `auto`/`max` to a model-specific level at run
 * time; that resolved state is reported separately from the policy. */
export type ThinkingLevel = "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "auto";

/** The four fixed roles a model profile binds (ADR-0025), in presentation
 * order. A profile has exactly these — no more, no fewer, no custom aliases. */
export type ModelRole = "default" | "smol" | "slow" | "plan";

/** One role's concrete pair. Neither field is ever null: the model is a
 * provider-qualified identifier and the thinking level is explicit, so a
 * binding never falls back to omp's host configuration. */
export interface ModelRoleBinding {
  model: string;
  thinking: ThinkingLevel;
}

/** A global, reusable name for the four role bindings (ADR-0025). Contains
 * identifiers only — no repository, workflow, or credential policy. The name
 * is the stable identifier and cannot be renamed. */
export interface ModelProfile {
  name: string;
  roles: Record<ModelRole, ModelRoleBinding>;
  created_at: string;
  updated_at: string;
}

/** One declared step of a workflow, as the daemon describes it (ADR-0026).
 * `role` is set only for agent steps — a command, decision, or gate has no
 * model and is never shown with one. `conditional` says a decision declared
 * earlier can route past this step, so it may not execute. */
export interface WorkflowStepDescriptor {
  name: string;
  kind: "agent" | "command" | "decision" | "gate";
  session: string | null;
  role: ModelRole | null;
  /** A declared route can pass this step by, or its own condition can hold
   * it back, so it may not run. */
  conditional: boolean;
}

/** An installed workflow. Definitions ship with the daemon (ADR-0028), so
 * this catalog arrives in the snapshot and never changes while the daemon
 * runs — there is no CRUD and no change event.
 *
 * `revision` is the content identity a *new* launch of this name would pin.
 * A task's own revision is on the task, and the two can differ: that is the
 * whole point of pinning. */
export interface WorkflowDescriptor {
  name: string;
  revision: string;
  format: number;
  primary_session: string;
  sessions: string[];
  steps: WorkflowStepDescriptor[];
}

/** One retained definition, read by content identity
 * (`GET /api/workflows/revisions/:revision`). `definition` is the normalized
 * document itself — the same bytes the revision is taken over — so what the
 * operator reads is literally what executes. */
export interface WorkflowRevisionDetail {
  revision: string;
  name: string;
  format: number;
  primary_session: string;
  sessions: string[];
  definition: Record<string, unknown>;
}

/** The workspace and prompt inputs one task actually runs under. */
export interface WorkspaceInputs {
  base_branch: string;
  branch_pattern: string;
  workshop_additions: WorkshopAdditionsSource;
  preamble: string;
}

/** The launch decision a task was accepted under (ADR-0026), pinned at
 * acceptance and never recomputed. `model_profile_name` is provenance, not a
 * live reference: the profile may be edited, renamed, or deleted and this
 * task keeps running exactly as accepted. */
/** Where one consumer's effective model profile came from. `step` means the
 * operator overrode this row; the rest are inherited (ADR-0027). */
export type ProfileSource = "step" | "task" | "project" | "legacy-confirmed";

/** Where one consumer's effective role came from: an explicit row override,
 * or the role the workflow declares. */
export type RoleSource = "step" | "workflow";

/** One model consumer's pinned policy (ADR-0027).
 *
 * `roles` is the complete native map that consumer's process carries, not
 * only its active pair: `smol`, `slow`, and `plan` reach the container too.
 * The two `*_source` fields are separate because the operator can override
 * one dimension and inherit the other. */
export interface ConsumerBinding {
  profile_name: string;
  profile_source: ProfileSource;
  role: ModelRole;
  role_source: RoleSource;
  roles: Record<ModelRole, ModelRoleBinding>;
}

/** The definition a task is pinned to, and the honest boundary of its
 * authority. Everything up to `legacy_through_seq` happened under a
 * definition nobody retained; `interrupted_legacy_seq` names the one attempt
 * that spans the boundary. Both are 0/null for a normal acceptance. */
export interface WorkflowBinding {
  revision: string;
  source: "accepted" | "legacy-confirmed";
  bound_at: string;
  legacy_through_seq: number;
  interrupted_legacy_seq: number | null;
}

export interface TaskExecutionInputs {
  version: number;
  provenance: "accepted" | "legacy-confirmed";
  accepted_at: string;
  project_name: string;
  workflow_name: string;
  /** The task-wide decision an unoverridden consumer inherited. What runs is
   * always a binding below. */
  model_profile_name: string | null;
  model_profile_source: "task" | "project" | "legacy-confirmed";
  /** The exact definition this task executes (ADR-0028), or null for a task
   * accepted before revisions were retained. Null is a real state: it is
   * filled in by an explicit operator confirmation, never by looking up
   * what the workflow name means today. */
  workflow_binding: WorkflowBinding | null;
  /** Every declared agent step, keyed by step name. Every model consumer is a
   * declared step: the engine reserves none. */
  step_bindings: Record<string, ConsumerBinding>;
  workspace: WorkspaceInputs;
  /** Which workspace fields this task overrode rather than inheriting. */
  workspace_overrides: string[];
  branch: string;
  checkout_path: string;
  fetch_remote: string;
  upstream_url: string;
  fork_url: string | null;
  /** Historical inputs a legacy task could not recover. Empty for anything
   * accepted through the normal launch path. */
  unknown_inputs: string[];
}

export type TaskState = "created" | "failed" | "archived";

/** Durable polled PR state (merge-poll capability): null until the first
 * successful poll; terminal states are never polled again. */
export type PrState = "open" | "merged" | "closed";

export interface Task {
  id: number;
  project_name: string;
  /** The decision this task runs under, or null for a task created before
   * pinned inputs existed (ADR-0026). */
  execution_inputs: TaskExecutionInputs | null;
  /** True exactly when `execution_inputs` is null: the task keeps its
   * history and stays readable, but anything needing a model or a base
   * branch is blocked until the operator confirms a continuation. */
  needs_configuration: boolean;
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
  /** The workflow chosen at creation (workflow-engine capability). */
  workflow_name: string;
  /** The pinned definition's content identity, or null for a task that
   * predates retained revisions (ADR-0028). */
  workflow_revision: string | null;
  workflow_revision_source: "accepted" | "legacy-confirmed" | null;
  /** Whether that definition can currently be resolved. A task whose
   * definition is missing, damaged, or written for a newer format stays
   * listed and readable and says why. */
  workflow_ready: boolean;
  workflow_readiness_reason: WorkflowReadinessReason | null;
  workflow_readiness_detail: string | null;
  /** From the *pinned* definition. Null rather than a guess whenever the
   * definition cannot be resolved: substituting a plausible default would
   * point review and shipping at a session this task may never declare. */
  workflow_primary_session: string | null;
  workflow_sessions: string[] | null;
  /** Run status; null for tasks whose run hasn't started (or that predate
   * workflows). */
  workflow_status: WorkflowRunStatus | null;
  /** Current step name; null when the run is not in flight. */
  workflow_step: string | null;
  /** The declared ending a finished format-2 run reached — `validated`,
   * `stopped-without-fix`, and so on. Null while the run is going, and for
   * every format-1 run: those have no name for their ending and none is
   * invented for them. */
  workflow_result: string | null;
  created_at: string;
  updated_at: string;
}

/** Why a task's pinned definition cannot be resolved. Only
 * `needs_workflow_confirmation` is something the operator can confirm away;
 * the rest describe a store or a daemon that cannot read it. */
export type WorkflowReadinessReason =
  | "needs_configuration"
  | "needs_workflow_confirmation"
  | "missing"
  | "unsupported_format"
  | "integrity"
  | "invalid";

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

/** Workflow run lifecycle (workflow-engine capability). */
export type WorkflowRunStatus = "running" | "waiting" | "complete" | "failed";

export type StepKind = "agent" | "command" | "decision" | "gate";

/** Persisted step-record status: the event stream's `started` lands as
 * `running` on the record. */
export type StepRecordStatus = "running" | "waiting" | "ok" | "failed";

/** Why the engine stopped rather than choosing (ADR-0028). Each names
 * evidence that is absent or unreadable — never a declared negative result,
 * which is data and follows the definition's own routes. */
export type PauseReason =
  | "missing_outcome"
  | "unresolved_decision"
  | "prompt_unrenderable"
  | "condition_unresolved"
  | "missing_evidence";

/** An uncertainty pause on one attempt. Distinct from a declared gate: the
 * attempt keeps its own kind, its absent outcome, and the error that stopped
 * it, and `retry_step` is what an operator retry re-enters — always the
 * blocked step, never the step after it. */
export interface StepPause {
  version: number;
  reason: PauseReason;
  message: string;
  step: string;
  retry_step: string;
  retry_kind: StepKind | null;
}

/** One prior attempt an attempt was bound to, as the daemon recorded it. */
export interface EvidenceBinding {
  step: string;
  seq: number;
}

/** What an attempt froze when it opened (ADR-0029): alias → the attempt it
 * selected, or null for an optional selector that matched nothing. Null on
 * the record itself means the attempt recorded none — a format-1 attempt, or
 * a step declaring no evidence — which is not the same as binding nothing. */
export interface StepEvidence {
  version: number;
  bindings: Record<string, EvidenceBinding | null>;
}

/** One answer a format-2 gate offers, and where it goes. Static: a choice
 * cannot compute a route, so what the operator picked is what happened. */
export interface GateChoice {
  id: string;
  label: string;
  feedback_required: boolean;
  next: Record<string, unknown>;
}

/** The operator's answer, once given. Recorded beside — never instead of —
 * the question, so a decision stays readable after the definition changes. */
export interface GateDecision {
  choice_id: string;
  label: string;
  feedback: string | null;
  next?: Record<string, unknown>;
  destination?: Record<string, unknown>;
  actor: string;
  decided_at: string;
}

/** A format-2 gate's persisted question, carried in the attempt's outcome.
 * Present without `decision` while it waits, and with one once answered. */
export interface GateSnapshot {
  version: number;
  message: string;
  choices: GateChoice[];
  evidence: Record<string, EvidenceBinding | null>;
  decision?: GateDecision | null;
}

/** One executed workflow step (workflow-engine capability), as persisted by
 * the daemon and replayed in the snapshot's `workflows` map. A waiting
 * format-1 gate carries its operator message in `outcome.message`; a
 * format-2 gate carries a whole `GateSnapshot` there; a waiting *pause*
 * carries `pause` instead. Outcome shapes are read defensively. */
export interface StepRecord {
  task_id: number;
  seq: number;
  step: string;
  kind: StepKind;
  session: string | null;
  status: StepRecordStatus;
  outcome: Record<string, unknown> | null;
  error: string | null;
  /** Set only while this attempt is the one the run is waiting on. */
  pause: StepPause | null;
  /** The attempts this one was handed, frozen when it opened. */
  evidence: StepEvidence | null;
  prompted_at: string | null;
  started_at: string;
  finished_at: string | null;
}

/** A task's workflow run state (workflow-engine capability): the task row's
 * persisted workflow fields plus its step-record history. */
export interface WorkflowState {
  name: string;
  status: WorkflowRunStatus | null;
  step: string | null;
  steps: StepRecord[];
}

/** `workflow_step` event on the main socket: one step transition of a run.
 * `message` is present on `waiting` (the gate's operator message), `error`
 * on `failed`. */
export interface WorkflowStepPayload {
  task_id: number;
  /** The attempt this transition belongs to. Sent so a client never has to
   * guess which iteration of a bounded step an event is about. */
  seq?: number;
  step: string;
  kind: StepKind;
  session: string | null;
  status: "started" | "ok" | "failed" | "waiting";
  error?: string;
  /** A declared gate's operator message. */
  message?: string;
  /** A format-2 gate's whole question — and, on `ok`, the answer it got.
   * Sent rather than looked up, so a client never renders choices from
   * today's catalog for a gate that asked something else. */
  gate?: GateSnapshot;
  /** An uncertainty pause, when the run stopped rather than guessing. */
  pause?: StepPause;
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
  /** The session that raised the entry; null for a workflow-gate wait
   * (workflow-engine design D-7: attention stays one entry per task). */
  session: string | null;
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
  session: string;
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
  session: string;
  kind: AdvisoryKind;
  context_pct?: number;
}

export interface AdvisoryClearedPayload {
  task_id: number;
  session: string;
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
  /** What this session's omp child reports it is actually running, recorded
   * once its model handshake succeeded (ADR-0026). Absent until then. */
  model?: NativeModelInfo;
}

/** The accepted policy beside the level omp resolved it to. `thinking` is
 * the operator's choice, spelled as they chose it; `resolved_thinking` is
 * what the model actually uses, which legitimately differs for `auto` and
 * `max`. Showing both keeps normalization from reading as a lost override. */
export interface NativeModelInfo {
  model: string;
  thinking: ThinkingLevel;
  resolved_thinking: string | null;
}

export interface SessionModelPayload extends NativeModelInfo {
  task_id: number;
  session: string;
}

export interface QuestionPostedPayload {
  task_id: number;
  session: string;
  question: PendingQuestion;
}

export interface QuestionResolvedPayload {
  task_id: number;
  session: string;
  question_id: string;
}

/** GET /api/tasks/:id/sessions/:name/agent/state — the agent's `get_state`
 * `data`, passed through untouched by the daemon. Field names beyond
 * isStreaming/queued are read defensively (this change's open SPEC
 * question); unknown keys tolerated. */
export interface AgentStateData {
  isStreaming?: boolean;
  queuedMessageCount?: number;
  todos?: unknown;
  model?: string;
  modelId?: string;
  [key: string]: unknown;
}

/** GET /api/tasks/:id/sessions/:name/agent/stats — the agent's
 * `get_session_stats` `data`. */
export interface AgentStatsData {
  inputTokens?: number;
  outputTokens?: number;
  totalCostUsd?: number;
  [key: string]: unknown;
}

export interface StatusChangedPayload {
  task_id: number;
  session: string;
  from: SessionStatus | null;
  to: SessionStatus;
  reason: string;
}

export interface ReviewIteration {
  /** `interrupted` is iteration-only: a daemon restart killed the reviewer.
   * The review itself lands `aborted`. */
  outcome: "approved" | "comments" | "aborted" | "error" | "interrupted";
  comment_count: number | null;
  stderr: string | null;
  recorded_at: string;
}

export interface ReviewState {
  status: "open" | "approved" | "aborted" | "error";
  /** Null whenever no reviewer process is live — including every review
   * restored across a daemon restart, whose llmvet process is gone. */
  url: string | null;
  port: number | null;
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

export type ShipStepName = "draft" | "fetch" | "commit" | "push" | "pr";
export type ShipStepStatus = "started" | "ok" | "failed";
export type ShipStepDetail = string | { sha: string; count: number };

export interface ShipStepState {
  step: ShipStepName;
  status: ShipStepStatus;
  detail?: ShipStepDetail | null;
}

export interface ShipState {
  status: ShipStatus;
  mode?: "squash" | "retain";
  draft: ShipDraft | null;
  commit_sha: string | null;
  pr_url: string | null;
  error: string | null;
  updated_at: string;
  /** Latest daemon-owned transient step, included in snapshots and deltas. */
  last_step?: ShipStepState | null;
}

export interface ShipDraftPayload {
  task_id: number;
  draft: ShipDraft;
}

export interface ShipStepPayload extends ShipStepState {
  task_id: number;
}

export interface ShipFinishedPayload {
  task_id: number;
  status: "shipped" | "error";
  pr_url?: string;
}

export type GpgState =
  /** No probe has completed yet. */
  | "unknown"
  /** The selected key can sign right now. */
  | "ready"
  /** Passphrase-protected key with a cold agent cache. */
  | "locked"
  /** Several usable signing keys and no selection. */
  | "ambiguous"
  /** No signing-capable secret key in the daemon's keyring. */
  | "no_key"
  /** gpg or gpg-connect-agent is not executable. */
  | "missing"
  /** The tools run but gpg-agent is unreachable. */
  | "agent_unavailable"
  /** Any other indeterminate result; always carries a detail. */
  | "error";

/** A signing-capable secret key. Public identifiers only — never key material. */
export interface GpgCandidate {
  fingerprint: string;
  key_id: string;
  uid: string | null;
  keygrip: string;
  created_at: string | null;
  expires_at: string | null;
  /** The primary key this one belongs to; itself for a signing primary. */
  primary_fingerprint: string;
}

/** The key the daemon will sign with, and where that choice came from. */
export interface GpgSelection {
  fingerprint: string;
  key_id: string;
  uid: string | null;
  keygrip: string;
  source: "override" | "config" | "git" | "auto";
  protection: "protected" | "unprotected" | null;
}

export interface GpgStatus {
  state: GpgState;
  selected: GpgSelection | null;
  candidates: GpgCandidate[];
  /** Seconds left in the agent cache, only when the agent reports one. */
  cache_ttl: number | null;
  detail: string | null;
  checked_at: string;
}

export interface GpgStatusPayload {
  status: GpgStatus;
}

export type GitHubIdentityState = "unknown" | "missing" | "unauthenticated" | "ready" | "error";
export type GitHubTargetState = "unchecked" | "allowed" | "denied" | "error";

/** Canonical GitHub pull-request target, derived by the trusted daemon. */
export interface GitHubTarget {
  host: string;
  owner: string;
  repository: string;
}

/** Safe ambient identity tuple which makes a target result current. */
export interface GitHubIdentityBinding {
  host: string;
  login: string;
  credential_source: string;
}

/** The daemon's current GitHub CLI observation. No credential value is exposed. */
export interface GitHubIdentityStatus {
  state: GitHubIdentityState;
  host: string;
  login: string | null;
  credential_source: string | null;
  executable_path: string | null;
  version: string | null;
  detail: string | null;
  checked_at: string | null;
}

/** A read-only repository eligibility result bound to its producing identity. */
export interface GitHubTargetStatus {
  state: GitHubTargetState;
  target: GitHubTarget | null;
  identity: GitHubIdentityBinding | null;
  detail: string | null;
  checked_at: string | null;
}

export interface GitHubStatus {
  identity: GitHubIdentityStatus;
  targets: Record<string, GitHubTargetStatus>;
}

export interface GitHubStatusPayload {
  gh: GitHubStatus;
}

/** Effective daemon settings map (daemon-settings capability). Values are
 * booleans for tier prefs or numbers for intervals/thresholds. */
export type DaemonSettings = Record<string, boolean | number | string | null>;

export interface SettingsChangedPayload {
  settings: DaemonSettings;
}

export interface DaemonInfo {
  bind: string;
  port: number;
  version: string;
  config_path: string;
  data_dir: string;
  audit_log_path: string | null;
}

export interface SnapshotPayload {
  projects: Project[];
  /** The complete sorted model-profile registry (ADR-0025); absent from
   * snapshots emitted before this capability. */
  model_profiles?: ModelProfile[];
  /** Every registered built-in workflow (ADR-0026). Snapshot-only: the
   * catalog is constant for the life of the daemon process. */
  workflow_catalog?: WorkflowDescriptor[];
  tasks: Task[];
  /** Nested task id → session name → info (workflow-engine design D-7; JSON
   * object keys arrive as strings). */
  sessions: Record<string, Record<string, SessionInfo>>;
  /** Per-task workflow run state, keyed by task id (JSON object keys arrive
   * as strings); absent from snapshots emitted before the workflow-engine
   * chunk. */
  workflows?: Record<string, WorkflowState>;
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
  /** Current daemon-owned GitHub CLI and target eligibility observation; absent
   * from snapshots emitted before the GitHub preflight capability. */
  gh?: GitHubStatus;
  /** Effective daemon settings (daemon-settings capability). */
  settings?: DaemonSettings;
}

export interface Envelope<T = unknown> {
  seq: number;
  ts: string;
  type: string;
  payload: T;
}

export type ConnectionState = "connecting" | "connected" | "disconnected" | "reconnecting";
