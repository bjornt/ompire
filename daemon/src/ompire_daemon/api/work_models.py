"""Shared wire schemas for the work routers (projects, profiles, tasks).

Pure request/response shapes: field sets, defaults, and the `extra="forbid"`
contracts that refuse stale callers. No route logic and no domain behavior
lives here. Class names are the OpenAPI component names and must not change.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ompire_daemon.model_config import (
    RoleBinding,  # noqa: F401 (re-export is not intended; removed below)
)
from ompire_daemon.work.profiles import validate_profile_name
from ompire_daemon.work.projects import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_FETCH_REMOTE,
    DEFAULT_WORKSHOP_ADDITIONS,
    validate_slug,
)
from ompire_daemon.work.tasks import validate_task_slug


class ProjectCreate(BaseModel):
    """`checkout_mode` defaults to `adopt`, which is what every registration
    before ADR-0022 meant: the operator supplies (or derives) a checkout that
    already exists. `clone` derives the destination from the effective
    checkout root and refuses a `checkout_path` of its own."""

    name: str
    title: str
    upstream_url: str
    fork_url: str | None = None
    checkout_path: str | None = None
    checkout_mode: str = "adopt"
    fetch_remote: str = DEFAULT_FETCH_REMOTE
    # Optional global model profile (ADR-0025). Omitted or null means no
    # default; nothing is inferred from credentials or the project name.
    default_model_profile: str | None = None
    # Workspace and prompt defaults a launch inherits (ADR-0026). The branch
    # pattern defaults to the daemon's `default_branch_pattern` *setting* at
    # registration time — a seed, not a value anything reads later.
    base_branch: str = DEFAULT_BASE_BRANCH
    branch_pattern: str | None = None
    workshop_additions: str = DEFAULT_WORKSHOP_ADDITIONS
    preamble: str = ""

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        validate_slug(value)
        return value

    @field_validator("checkout_mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        if value not in ("adopt", "clone"):
            raise ValueError("checkout_mode must be 'adopt' or 'clone'")
        return value


class ProjectUpdate(BaseModel):
    title: str
    upstream_url: str
    fork_url: str | None = None
    checkout_path: str
    fetch_remote: str = DEFAULT_FETCH_REMOTE
    new_name: str | None = None
    # Three-valued on update: absent from the body preserves the stored
    # reference, explicit null clears it, a name selects that profile. The
    # route reads `model_fields_set` to tell the first two apart, so a caller
    # written before profiles existed cannot clear one by not mentioning it.
    default_model_profile: str | None = None
    # Same omission rule for the workspace defaults, but they are never null:
    # an empty preamble is the value "no preamble".
    base_branch: str | None = None
    branch_pattern: str | None = None
    workshop_additions: str | None = None
    preamble: str | None = None

    @field_validator("new_name")
    @classmethod
    def _validate_new_name(cls, value: str | None) -> str | None:
        if value is not None:
            validate_slug(value)
        return value


class CheckoutInspectIn(BaseModel):
    checkout_path: str
    fetch_remote: str = DEFAULT_FETCH_REMOTE


class RemoteOut(BaseModel):
    name: str
    url: str


class CheckoutInspectOut(BaseModel):
    """What a read-only look at a candidate checkout found. Remote names and
    URLs only — never file contents, and nothing is written to the path."""

    ok: bool
    reason: str
    detail: str
    remotes: list[RemoteOut]
    suggested_upstream: str | None = None
    suggested_fork: str | None = None


class ProjectOut(BaseModel):
    name: str
    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str
    checkout_mode: str
    fetch_remote: str
    setup_state: str
    setup_error: str | None
    default_model_profile: str | None
    base_branch: str
    branch_pattern: str
    workshop_additions: str
    preamble: str
    # `reconciled` or `needs-reconciliation` — whether the carried-over
    # template configuration still needs a decision (ADR-0026). Independent of
    # `setup_state`, and reported alongside it so the UI can say which of the
    # two is blocking a launch.
    launch_config_state: str

    model_config = {"from_attributes": True}


class ProjectFilesOut(BaseModel):
    """Repository-relative path names only — never contents, sizes, or
    absolute paths (add-spawn-file-mentions)."""

    paths: list[str]
    truncated: bool


class RoleBindingIn(BaseModel):
    """One role's pair. Both fields are required and neither may be null: a
    binding that cannot say which model and how much reasoning is not one."""

    model_config = ConfigDict(extra="forbid")

    model: str
    thinking: str


class ModelProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    roles: dict[str, RoleBindingIn]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        validate_profile_name(value)
        return value


class ModelProfileUpdate(BaseModel):
    """The name is the stable identifier, so an update replaces only the
    bindings — and replaces all four of them together."""

    model_config = ConfigDict(extra="forbid")

    roles: dict[str, RoleBindingIn]


class RoleBindingOut(BaseModel):
    model: str
    thinking: str

    model_config = {"from_attributes": True}


class ModelProfileOut(BaseModel):
    name: str
    roles: dict[str, RoleBindingOut]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class WorkspaceOverridesIn(BaseModel):
    """Task-local overrides of the project's workspace defaults.

    Each field is genuinely optional: leaving a key out inherits the project
    value. An explicit empty `preamble` is an override to "no preamble", which
    is why the route distinguishes omitted from present via `model_fields_set`
    rather than treating empty as absent. The other three have no meaningful
    null — a task cannot run with no base branch — so an explicit null is
    refused instead of being read as a reset.
    """

    model_config = ConfigDict(extra="forbid")

    base_branch: str | None = None
    branch_pattern: str | None = None
    workshop_additions: str | None = None
    preamble: str | None = None


class ConsumerOverrideIn(BaseModel):
    """One model consumer's row-level selections (ADR-0027).

    Each dimension is independently optional: omitted or null means inherit,
    a value means the operator chose it. `extra="forbid"` keeps a typo like
    `model` or `thinking` from being silently ignored — those are profile
    settings, deliberately not a third override hierarchy.
    """

    model_config = ConfigDict(extra="forbid")

    model_profile: str | None = None
    role: str | None = None


class ResultAttachmentIn(BaseModel):
    """One selected revision.

    All three fields together, because two of them alone would not identify an
    immutable revision: the manifest id is what refuses a stale selection when
    a successor capture has landed, and the producing task is what the refusal
    can name.
    """

    model_config = ConfigDict(extra="forbid")

    producer_task_id: int
    result_id: str
    expected_manifest_id: str


class TaskCreate(BaseModel):
    """The one supported creation contract (ADR-0026, ADR-0027).

    `extra="forbid"` is load-bearing: `template_name`, and the old scalar
    `model`/`thinking` spawn overrides, must be refused rather than ignored,
    or a stale caller would silently get a launch it did not ask for.
    """

    model_config = ConfigDict(extra="forbid")

    project_name: str
    workflow_name: str
    slug: str
    prompt: str
    # Omitted or null means "inherit the project default". A name selects that
    # profile for this task and replaces the inheritance.
    model_profile: str | None = None
    workspace_overrides: WorkspaceOverridesIn | None = None
    # Per-consumer overrides, keyed by declared agent step name. Absent means
    # "everything inherits". `auxiliary_overrides` is kept only so a caller
    # still naming the retired judge is refused with a field-level error
    # rather than having its choice silently dropped (ADR-0028).
    step_overrides: dict[str, ConsumerOverrideIn] = Field(default_factory=dict)
    auxiliary_overrides: dict[str, ConsumerOverrideIn] = Field(default_factory=dict)
    # Accepted result revisions to install before the first step runs
    # (ADR-0035). Destinations are the manifest's own paths — deliberately not
    # a caller-supplied mapping, so a request cannot choose where retained
    # bytes land.
    result_attachments: list[ResultAttachmentIn] = Field(default_factory=list)
    # Explicit, and bound by the preview token to this exact attachment set and
    # target commit: an acknowledgement cannot be carried over to a different
    # selection or a base that moved.
    acknowledge_result_base_difference: bool = False

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        validate_task_slug(value)
        return value


class TaskAccept(TaskCreate):
    """Creation adds the reviewed resolution's token. It identifies *what was
    reviewed*, not who is asking: it authorizes nothing, and a mismatch is a
    409 asking for a fresh review rather than a permission error."""

    preview_token: str


class TaskOut(BaseModel):
    id: int
    project_name: str
    execution_inputs: dict[str, Any] | None
    needs_configuration: bool
    slug: str
    branch: str
    clone_path: str
    state: str
    prompt: str
    error: str | None
    workshop_id: str | None
    workflow_name: str
    # The pinned definition (ADR-0028). `workflow_revision` is null only for a
    # task that predates retained revisions; `workflow_primary_session` and
    # `workflow_sessions` are null whenever the definition cannot be resolved,
    # rather than a plausible-looking guess.
    workflow_revision: str | None
    workflow_revision_source: str | None
    workflow_ready: bool
    workflow_readiness_reason: str | None
    workflow_readiness_detail: str | None
    workflow_primary_session: str | None
    workflow_sessions: list[str] | None
    workflow_status: str | None
    workflow_step: str | None
    # The declared ending a finished format-2 run reached (ADR-0029). Null
    # while it runs, and for every format-1 run: those have no name for their
    # ending, and none is invented for them.
    workflow_result: str | None
    pr_url: str | None
    pr_state: str | None
    pr_merged_at: str | None
    spawn_completed_at: str | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class TaskDetailOut(TaskOut):
    # Derived on demand from the workshop CLI, never persisted (design D-3).
    workshop_status: str | None
    # The run's executed attempts, in order — the same records the snapshot
    # replays, from the same projection. A gate's question and its answer, an
    # attempt's frozen evidence, and an uncertainty pause all live here, so a
    # reader that is not holding a socket open still sees the whole history
    # rather than only the task row's summary.
    workflow_steps: list[dict[str, Any]]


class ProjectReconciliationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Names exactly the evidence the operator read. A decision made against a
    # stale reading is refused rather than applied.
    evidence_fingerprint: str
    base_branch: str
    branch_pattern: str
    workshop_additions: str
    preamble: str = ""
    # Explicit either way: a name, or `null` meaning "this project has no
    # default; a profile is selected at each launch".
    default_model_profile: str | None = None
    acknowledge_model_candidates: bool = False
    acknowledge_judge_model: bool = False


class TaskContinuationIn(BaseModel):
    """What the operator supplies to continue a task the upgrade left blocked.

    Every field is optional at the schema level because two different gaps
    share this route (ADR-0026, ADR-0028): a task that was never configured
    needs all of them, while a task that merely predates retained workflow
    definitions needs none — its model, branch, and preamble were reviewed
    once and are not re-decided. Which are actually required is decided
    against the task, and a missing one comes back as a field-level error.
    """

    model_config = ConfigDict(extra="forbid")

    model_profile: str | None = None
    base_branch: str | None = None
    workshop_additions: str = DEFAULT_WORKSHOP_ADDITIONS
    preamble: str = ""


class TaskContinuationConfirmIn(TaskContinuationIn):
    preview_token: str
    # The operator says, in as many words, that the original model, thinking
    # level, preamble, and overrides are unrecoverable. Confirmation pins what
    # happens next; it never claims the past turns used these values.
    acknowledge_unknown: bool = False
    # And, separately, that the exact procedure this task already ran was
    # never recorded. Two acknowledgements because they are two different
    # things the daemon cannot recover, and a task can need only one of them.
    acknowledge_workflow: bool = False
