"""SQLite engine and Core table metadata. No ORM: queries are built against
`Table` objects directly. This `metadata` is the schema source of truth;
Alembic migrations under `daemon/alembic/` are generated from it.

Architecture: ADR-0005
(docs/adr/0005-persist-local-state-with-sqlite-core-and-alembic.md)
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    text,
)

metadata = MetaData()

# `checkout_mode`/`setup_state` carry the onboarding facts a bare row could
# not: whether Ompire created the base checkout or adopted the operator's, and
# whether it is usable yet (ADR-0022). `fetch_remote` is the remote spawn
# fetches in *that* checkout — the per-task clone's own `origin` is unrelated
# and unchanged.
projects = Table(
    "projects",
    metadata,
    Column("name", String, primary_key=True),
    Column("title", String, nullable=False),
    Column("upstream_url", String, nullable=False),
    Column("fork_url", String, nullable=True),
    Column("checkout_path", String, nullable=False),
    Column("checkout_mode", String, nullable=False, server_default="adopted"),
    Column("fetch_remote", String, nullable=False, server_default="origin"),
    Column("setup_state", String, nullable=False, server_default="ready"),
    Column("setup_error", Text, nullable=True),
    # Optional global model profile (ADR-0025). NULL means no default; the
    # named, non-cascading FK is schema metadata — the runtime guarantee is the
    # write reservation in `registry/model_profiles.reserved_write`, because
    # this connection does not enable `PRAGMA foreign_keys`.
    Column(
        "default_model_profile",
        String,
        ForeignKey("model_profiles.name", name="fk_projects_default_model_profile"),
        nullable=True,
    ),
    # Workspace and prompt defaults a launch inherits and one task may
    # override (ADR-0026). They moved here from templates: they describe the
    # project, not a saved launch preset. `preamble` is not nullable — an
    # empty string means "no preamble", which is a real answer.
    Column("base_branch", String, nullable=False, server_default="main"),
    Column("branch_pattern", String, nullable=False, server_default="ompire/<slug>"),
    Column("workshop_additions", String, nullable=False, server_default="project"),
    Column("preamble", Text, nullable=False, server_default=""),
    # Whether this project's *launch configuration* still needs the operator's
    # decision after the template upgrade. Deliberately separate from
    # `setup_state`: a checkout can be perfectly ready while the migration
    # found two templates disagreeing about the base branch.
    Column(
        "launch_config_state", String, nullable=False, server_default="reconciled"
    ),
    Index("ix_projects_default_model_profile", "default_model_profile"),
)

# Global model profiles (ADR-0025): a reusable name for the four model-role
# bindings. The role map is one small complete document — there is no
# role-level update API and nothing queries profiles by a nested model — so it
# follows the existing JSON-text convention rather than eight fixed columns.
model_profiles = Table(
    "model_profiles",
    metadata,
    Column("name", String, primary_key=True),
    Column("roles_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

# Inert upgrade history from the template retirement (ADR-0026).
#
# Every old template row, every task's template attribution, and any
# explicitly configured `judge_model` is copied here before the live storage
# is removed, including null and empty values. This table is *evidence*: it is
# read to show the operator what used to be configured and to detect a changed
# retired setting, and it is never read to execute anything. There is no CRUD,
# no launch selector, and no path from a row here to a running agent — that is
# the whole reason it can be kept indefinitely without becoming a second
# source of launch policy.
#
# `scope_kind`/`scope` say what the evidence is about (`project` + name,
# `task` + id, `daemon` + ""), `source` names where it came from (the template
# name, or `config.toml`), and `payload_json` is the original values verbatim.
launch_migration_evidence = Table(
    "launch_migration_evidence",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String, nullable=False),
    Column("scope_kind", String, nullable=False),
    Column("scope", String, nullable=False),
    Column("source", String, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("recorded_at", String, nullable=False),
    Index("ix_launch_migration_evidence_scope", "scope_kind", "scope"),
)

# Operator decisions that closed out a reconciliation, and the acknowledgement
# of a retired `judge_model` value. Keyed by scope like the evidence rows.
# `acknowledged_value` lets a *changed* retired setting reopen as new evidence
# while an unchanged one stays quiet across restarts.
launch_reconciliations = Table(
    "launch_reconciliations",
    metadata,
    Column("scope_kind", String, primary_key=True),
    Column("scope", String, primary_key=True),
    Column("kind", String, primary_key=True),
    Column("acknowledged_value", Text, nullable=True),
    Column("decided_at", String, nullable=False),
)

# Retained workflow definitions, keyed by the content identity of their
# canonical document (ADR-0028). A row is written when a definition first
# enters the catalog and is never updated or deleted: a task pins a revision,
# and that revision has to keep meaning what it meant for as long as the task
# is inspectable — including after the packaged definition changes, and after
# the workflow name disappears from a later release's catalog.
#
# `document_json` is the whole executable document, not a summary: the
# identifier alone would name a definition nobody could still read.
workflow_revisions = Table(
    "workflow_revisions",
    metadata,
    Column("revision", String, primary_key=True),
    Column("workflow_name", String, nullable=False),
    Column("format", Integer, nullable=False),
    Column("document_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Index("ix_workflow_revisions_workflow_name", "workflow_name"),
)

# The operator-owned workflow library (ADR-0031): which procedures exist, what
# each one's editable text says, and which retained revision a *new* launch of
# that name would pin. Deliberately a separate table from `workflow_revisions`
# above: revisions are append-only executable documents, and this is the
# mutable selection over them.
#
# `draft_yaml` is inert text — whatever the operator last saved in the editor,
# possibly invalid, possibly empty, and never executed. NULL for a built-in,
# whose text comes from the package.
#
# `current_revision` is the entry's executable choice, NULL for a draft-only
# entry. The named, non-cascading FK is schema metadata; the runtime guarantee
# is the write reservation, because this connection does not enable
# `PRAGMA foreign_keys`. Nothing here ever deletes a revision row.
#
# `version` is the *edit* version, advanced by every successful mutation
# including a comment-only draft save. It is what two tabs compare, and it is
# deliberately not the content revision: identical semantics under a changed
# comment is the same procedure but a different edit.
workflow_library = Table(
    "workflow_library",
    metadata,
    Column("name", String, primary_key=True),
    Column("origin", String, nullable=False),
    Column("draft_yaml", Text, nullable=True),
    Column(
        "current_revision",
        String,
        ForeignKey("workflow_revisions.revision", name="fk_workflow_library_revision"),
        nullable=True,
    ),
    Column("archived", Integer, nullable=False, server_default="0"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_workflow_library_current_revision", "current_revision"),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_name", String, ForeignKey("projects.name"), nullable=False),
    # The launch decision this task was accepted under (ADR-0026), as one
    # version-tagged JSON document. NULL means the task predates pinned
    # inputs: it keeps its history and stays readable, but anything that would
    # need a model, a base branch, or a preamble is blocked until the operator
    # confirms a continuation configuration. NULL is never quietly filled in
    # from today's project or profile settings.
    Column("execution_inputs_json", Text, nullable=True),
    Column("slug", String, nullable=False),
    Column("branch", String, nullable=False),
    Column("clone_path", String, nullable=False),
    Column("state", String, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("error", Text, nullable=True),
    Column("workshop_id", String, nullable=True),
    # Workflow run state (workflow-engine capability): the workflow chosen at
    # creation; status/step NULL for legacy rows and whenever no run is
    # active.
    Column("workflow_name", String, nullable=False, server_default="single-step"),
    Column("workflow_status", String, nullable=True),
    Column("workflow_step", String, nullable=True),
    # The declared ending a format-2 run reached (ADR-0029): `validated`,
    # `stopped-without-fix`, and so on. `workflow_status` says the run stopped;
    # only this says what stopping meant. NULL for a run still going, and for
    # every format-1 run — those have no name for their ending and none is
    # invented for them.
    Column("workflow_result", String, nullable=True),
    Column("pr_url", String, nullable=True),
    Column("pr_state", String, nullable=True),
    Column("pr_merged_at", String, nullable=True),
    Column("spawn_completed_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    # Monotonic version over this task's *durable result* projection
    # (ADR-0034), advanced in the same transaction as every observable result
    # mutation: a finished capture, an acceptance, a purge, a detected
    # integrity failure. It is what a reconnecting client compares a queued
    # `task_results_updated` delta against, and it is deliberately separate
    # from `updated_at` — capturing a result changes nothing about the task
    # row itself, and a task edit must not make a client think its result
    # projection moved.
    Column("results_version", Integer, nullable=False, server_default="0"),
    # A slug is reusable after archive; uniqueness applies to live rows only.
    Index(
        "uq_tasks_live_project_slug",
        "project_name",
        "slug",
        unique=True,
        sqlite_where=text("state != 'archived'"),
    ),
)


# Named omp sessions per task (workflow-engine capability): identity for
# `omp --resume` is per (task, session), not per task.
#
# `applied_policy_json` is mutable *execution state*, not an input: the
# complete model policy this session's child last verifiably ran under
# (ADR-0027). It is what a restart resumes on and what a follow-up keeps
# using, which is why it cannot be recomputed from the task document — two
# steps sharing a session can pin different policies, and only the session
# knows which one actually took effect. NULL until the first successful
# application.
task_sessions = Table(
    "task_sessions",
    metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("name", String, primary_key=True),
    Column("omp_session_id", String, nullable=True),
    Column("spawned_at", String, nullable=False),
    Column("applied_policy_json", Text, nullable=True),
)

# One row per executed workflow step; identity is (task_id, seq) because loops
# revisit step names. `prompted_at` marks an agent step's prompt as sent, so
# restart recovery can tell "never prompted" (send fresh) from "turn lost"
# (resume-nudge) — see the workflow-engine design's recovery rules.
workflow_step_records = Table(
    "workflow_step_records",
    metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("step", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("session", String, nullable=True),
    Column("status", String, nullable=False),
    Column("outcome_json", Text, nullable=True),
    Column("error", Text, nullable=True),
    # An uncertainty pause (ADR-0028), distinct from a declared gate's message
    # in `outcome_json`. The attempt keeps its own kind, its absent outcome,
    # and the parse or evaluation error that stopped it; this column adds why
    # the run is waiting and which step an operator retry re-enters. Replacing
    # the attempt's evidence with a synthetic success is exactly what this
    # column exists to avoid.
    Column("pause_json", Text, nullable=True),
    # The prior attempts this attempt bound when it opened (ADR-0029), alias →
    # `{step, seq}` or null for an optional selector that matched nothing.
    # Frozen: the prompt, the routing decision, the gate message, and recovery
    # all read these same records, so what a step was given cannot drift as
    # later attempts land. NULL means the attempt recorded none — a format-1
    # attempt, or one whose step declares no evidence — never an empty binding.
    Column("evidence_json", Text, nullable=True),
    Column("prompted_at", String, nullable=True),
    Column("started_at", String, nullable=False),
    Column("finished_at", String, nullable=True),
)

# Durable review history (ADR-0016's review slice). One row per task; the
# reviewer process itself is not durable, so its URL and port stay in
# `ReviewManager` memory. `process_started_at` is the write-ahead marker: it
# is stamped before llmvet is launched and cleared when the process is
# observed exiting, so startup can tell an interrupted reviewer from a review
# left `open` because its comments went back to the agent.
#
# `candidate_id` binds the review to the protected candidate it is grading
# (ADR-0032). NULL is the legacy shape: a review recorded before content
# binding existed. Such a review stays readable as history and is never
# backfilled from today's workspace, but it cannot authorize a new delivery.
reviews = Table(
    "reviews",
    metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("status", String, nullable=False),
    Column("process_started_at", String, nullable=True),
    Column("candidate_id", String, nullable=True),
    # The workflow attempt this review was launched for (format 3). Written
    # with the process marker, before llmvet starts, so a restart can resolve
    # the interrupted reviewer against the step that is waiting for it rather
    # than against whatever the run has moved on to. NULL is honest history:
    # a review an operator started by hand, or one recorded before workflows
    # owned review at all.
    Column("workflow_seq", Integer, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

# Ordered review iterations; identity is (task_id, seq) because re-review
# after comments appends to the same review's history, mirroring
# `workflow_step_records`.
# Each iteration also names the candidate it actually graded, so an approval
# says *what* was approved rather than only that something was.
review_iterations = Table(
    "review_iterations",
    metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("outcome", String, nullable=False),
    Column("comment_count", Integer, nullable=True),
    Column("stderr", Text, nullable=True),
    Column("candidate_id", String, nullable=True),
    # The workflow attempt this iteration answered, and the reviewer's actual
    # report. `findings_state` says what `findings` is: `complete` is the
    # whole report, `empty` is an approval with nothing to say, `truncated`
    # is a report too large to retain whole, and `unavailable` is one that
    # could not be captured. A correction may only run against `complete` —
    # a count is not a report, and a partial report must never be handed to
    # an agent as if it were the reviewer's whole opinion.
    Column("workflow_seq", Integer, nullable=True),
    Column("findings", Text, nullable=True),
    Column("findings_state", String, nullable=True),
    Column("recorded_at", String, nullable=False),
)

# The protected content a review graded and a delivery signs (ADR-0032). A
# candidate is the *whole* publishable task delta resolved once: the pinned
# base branch, the base commit the delta is measured from, the original HEAD it
# was captured at, the full candidate tree, and — for retain — the ordered
# source commits with their trees and messages.
#
# `candidate_id` is a hash of that normalized semantic data, not of rendered UI
# text, timestamps, or the agent's draft: re-capturing an unchanged workspace
# yields the same identity, and any change to what would be published yields a
# different one. That is the whole approval binding.
#
# `storage_path` names the owner-private bare Git repository holding the
# candidate's objects outside the task clone, so a task cannot mutate or
# garbage-collect what is under review. It is temporary operation evidence
# with its own lifecycle, not an artifact store.
delivery_candidates = Table(
    "delivery_candidates",
    metadata,
    Column("candidate_id", String, primary_key=True),
    Column("task_id", Integer, ForeignKey("tasks.id"), nullable=False),
    Column("base_branch", String, nullable=False),
    Column("base_commit", String, nullable=False),
    Column("original_head", String, nullable=False),
    Column("tree_id", String, nullable=False),
    Column("source_commits_json", Text, nullable=False),
    Column("dirty", Integer, nullable=False, server_default="0"),
    Column("storage_path", String, nullable=True),
    Column("created_at", String, nullable=False),
    Index("ix_delivery_candidates_task", "task_id"),
)

# One delivery: the operator's authorization to publish one candidate as far as
# one selected ending (ADR-0032). Rows are history — a task accumulates them —
# but at most one is non-terminal at a time, which is what makes "this task is
# already delivering" a durable fact rather than an in-memory flag.
#
# `version` is a per-task monotonic counter across the task's deliveries. It is
# what a client compares before confirming and what the projection reducer uses
# to drop a stale or duplicated update.
#
# `draft_json` is the durable publication draft — inert text, editable by hand,
# authorizing nothing. It lives here rather than in a separate table because a
# draft is the beginning of a delivery, and an interrupted agent draft has to be
# recoverable as *this* delivery's retryable interruption.
#
# `ending`, `mode`, and the final metadata are immutable once authorized: they
# are what the confirmation named. Extending a completed prefix (a later push,
# a later PR) appends a decision and new actions; it never rewrites them.
deliveries = Table(
    "deliveries",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", Integer, ForeignKey("tasks.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("workflow_revision", String, nullable=True),
    Column("candidate_id", String, nullable=True),
    Column("review_candidate_id", String, nullable=True),
    Column("mode", String, nullable=True),
    Column("ending", String, nullable=True),
    Column("commit_message", Text, nullable=True),
    Column("pr_title", Text, nullable=True),
    Column("pr_body", Text, nullable=True),
    Column("routing_json", Text, nullable=True),
    Column("identity_json", Text, nullable=True),
    Column("authorized_at", String, nullable=True),
    Column("authorized_by", String, nullable=True),
    Column("request_key", String, nullable=True),
    Column("input_fingerprint", String, nullable=True),
    Column("draft_json", Text, nullable=True),
    # Format 3: which decision, on which question, granted this authorization.
    # `workflow_gate_seq` is the answered gate attempt, `workflow_choice_id`
    # the choice, and `review_seq` the review iteration the grant is bound to.
    # All three are NULL for a delivery an operator authorized through the
    # manual Ship path before workflows owned that authority — and a NULL here
    # never means "authorized by the workflow", which is why the migration
    # boundary below exists.
    Column("workflow_gate_seq", Integer, nullable=True),
    Column("workflow_choice_id", String, nullable=True),
    Column("review_seq", Integer, nullable=True),
    Column("disposition", String, nullable=False),
    Column("blocked_reason", Text, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_deliveries_task", "task_id"),
    Index(
        "uq_deliveries_request_key",
        "task_id",
        "request_key",
        unique=True,
        sqlite_where=text("request_key IS NOT NULL"),
    ),
)

# One row per attempt at one privileged action. `phase` is the write-ahead
# marker: `prepared` is committed before anything runs, `executing` is
# committed before the effect is launched, and only an established outcome
# moves it to `succeeded`, `failed` (proven not to have happened, or verifiably
# rolled back) or `needs_reconciliation` (unknown).
#
# `expected_json` is what the attempt is allowed to do — destination ref, source
# object id, observed pre-write remote head, PR correlation marker — captured
# before the effect so recovery can look for exactly that result rather than
# guessing from today's state. `progress_json` records per-signature progress,
# so an interrupted retain rewrite is not an opaque boolean.
delivery_actions = Table(
    "delivery_actions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("delivery_id", Integer, ForeignKey("deliveries.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("kind", String, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("request_key", String, nullable=False),
    Column("input_fingerprint", String, nullable=False),
    Column("phase", String, nullable=False),
    Column("expected_json", Text, nullable=True),
    Column("progress_json", Text, nullable=True),
    Column("identity_json", Text, nullable=True),
    Column("result_json", Text, nullable=True),
    Column("error", Text, nullable=True),
    # The workflow delivery-step attempt this action belongs to (format 3).
    # Persisted with the write-ahead intent, before the effect, so recovery
    # attaches an observed result to the attempt that asked for it instead of
    # dispatching a second one.
    Column("workflow_seq", Integer, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_delivery_actions_delivery", "delivery_id", "seq"),
    # One live effect per workflow step. `failed` rows are excluded because
    # that phase means the effect is *proven* not to have happened — an
    # interrupted attempt an operator then continues opens a fresh one, and
    # the history of both stays readable. Everything else — prepared,
    # executing, succeeded, needs_reconciliation — is exactly what must never
    # exist twice for one step.
    Index(
        "uq_delivery_actions_workflow_seq",
        "delivery_id",
        "workflow_seq",
        unique=True,
        sqlite_where=text("workflow_seq IS NOT NULL AND phase != 'failed'"),
    ),
)

# Ordered, immutable authorization and reconciliation decisions: who authorized
# what, which observations a recheck made, what an adoption verified, and what
# an abandonment left unresolved. Appended, never rewritten — extending a
# delivery to a further ending adds a row rather than editing the first one.
delivery_decisions = Table(
    "delivery_decisions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("delivery_id", Integer, ForeignKey("deliveries.id"), nullable=False),
    Column("action_id", Integer, nullable=True),
    Column("kind", String, nullable=False),
    Column("detail_json", Text, nullable=True),
    Column("note", Text, nullable=True),
    Column("decided_at", String, nullable=False),
    Index("ix_delivery_decisions_delivery", "delivery_id", "id"),
)

# The one-time boundary between authority granted before workflows owned
# publication and everything created after. Exactly one row, written by the
# migration and never updated.
#
# It exists because a NULL workflow link is ambiguous on its own: it is what a
# genuine pre-upgrade authorization looks like, and it is also what a freshly
# inserted row looks like for the instant before its links are written. Rows at
# or below this boundary predate the upgrade and may be *continued* under their
# original grant; anything above it must carry its own workflow authority.
# Nothing can move the boundary, so no new row can ever look historical.
delivery_authority_boundary = Table(
    "delivery_authority_boundary",
    metadata,
    Column("id", Integer, primary_key=True),  # always 1
    Column("max_delivery_id", Integer, nullable=False),
    Column("max_action_id", Integer, nullable=False),
    Column("recorded_at", String, nullable=False),
)

# Durable task results (ADR-0034): the bytes a task produced, retained outside
# its disposable workspace so exploration and planning are complete outcomes
# without a commit, a push, or a pull request.
#
# `id` is an opaque capture identity, stable across restarts and independent of
# content: two captures of byte-identical files are two results with two
# provenances, because they were produced at different times by different work.
# `manifest_json` is the immutable, canonical description of exactly what was
# retained — every relative path, byte length, media type and SHA-256 — and
# `manifest_id` hashes that whole document. That hash is the revision binding:
# acceptance names it, and a client that acts on a stale one is refused rather
# than silently retargeted at newer files.
#
# `content_id` hashes only the sorted path/media/length/checksum entries, so an
# unchanged file set is *recognizable* without merging two captures' identities.
#
# `request_id` is the caller's replay key, unique per task: a lost response is
# recovered by repeating the request, never by capturing different bytes. It is
# paired with `selection_fingerprint` so a repeat under the same id that asks
# for a *different* selection is refused instead of answering about files
# nobody requested.
#
# `state` is `capturing`, `failed`, `ready`, or `purged`. Acceptance is a
# separate, independent decision: `accepted_at`/`accepted_by` record it, and a
# result that later becomes unreadable keeps them, because the operator really
# did accept it. `unavailable_reason` is that classified damage, set without
# discarding history and never repaired from today's workspace.
#
# `predecessor_id` is the most recent ready result at admission time, frozen
# then. A failed capture never becomes anyone's predecessor, and a purged one
# keeps its identity while losing its ability to supply a text diff.
task_results = Table(
    "task_results",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", Integer, ForeignKey("tasks.id"), nullable=False),
    Column("request_id", String, nullable=False),
    Column("selection_fingerprint", String, nullable=False),
    Column("selection_json", Text, nullable=False),
    Column("state", String, nullable=False),
    Column("error", Text, nullable=True),
    Column("unavailable_reason", Text, nullable=True),
    Column("manifest_json", Text, nullable=True),
    Column("manifest_id", String, nullable=True),
    Column("content_id", String, nullable=True),
    Column("predecessor_id", String, nullable=True),
    Column("started_at", String, nullable=False),
    Column("finished_at", String, nullable=True),
    Column("accepted_at", String, nullable=True),
    Column("accepted_by", String, nullable=True),
    Column("purged_at", String, nullable=True),
    Column("purged_by", String, nullable=True),
    Index("ix_task_results_task", "task_id", "started_at"),
    Index(
        "uq_task_results_request",
        "task_id",
        "request_id",
        unique=True,
    ),
)

# The retained bytes, one row per file. Deliberately carries *only* the bytes:
# length, checksum and media type live in the manifest, which is hashed into
# the revision identity, so there is no second independently mutable
# description of a file to disagree with the one acceptance was bound to.
#
# Bytes are exact. Line endings, final newlines and zero-length files are
# preserved as captured; nothing here normalizes text.
task_result_files = Table(
    "task_result_files",
    metadata,
    Column(
        "result_id",
        String,
        ForeignKey("task_results.id"),
        primary_key=True,
    ),
    Column("relative_path", String, primary_key=True),
    Column("content", LargeBinary, nullable=False),
)

# Which consumer task pinned which retained revision (ADR-0035).
#
# The immutable execution-inputs document remains the execution contract; this
# is the *index* over it, so "may these bytes be purged?" and "who is holding
# them?" are answerable without decoding every launch document in the database.
# Rows are inserted in the same reservation that creates the consumer task, and
# removed only when that consumer's own record is explicitly purged — a failed,
# completed, or archived consumer keeps its inputs, so it keeps its references.
#
# No foreign-key cascade is assumed: connections do not enable foreign-key
# enforcement, so both deletions are written explicitly.
task_result_references = Table(
    "task_result_references",
    metadata,
    Column(
        "consumer_task_id", Integer, ForeignKey("tasks.id"), primary_key=True
    ),
    Column(
        "result_id", String, ForeignKey("task_results.id"), primary_key=True
    ),
    # Denormalized from the result row so a purge refusal can name the
    # producing task without joining through bytes that may be gone.
    Column("producer_task_id", Integer, nullable=False),
    Column("manifest_id", String, nullable=False),
    Column("created_at", String, nullable=False),
    Index("ix_task_result_references_result", "result_id"),
)

# ADR-0013: UI-editable overrides are persisted as JSON-encoded scalar
# values and layered over operator-owned config.toml.
settings = Table(
    "settings",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),
)


def db_path_for(data_dir: Path) -> Path:
    return data_dir / "db" / "ompire.db"


def ensure_db_dir(db_path: Path) -> None:
    """Create the parent directory for the SQLite database, owner-only."""
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_wal_mode(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine
