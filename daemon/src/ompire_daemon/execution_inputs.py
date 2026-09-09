"""The accepted execution inputs of one task: everything a run needs after
the operator reviewed and submitted it.

A task's inputs are decided once, at acceptance (or, for a task that predates
them, at explicit legacy confirmation), and never recomputed. Editing a
project default or a model profile afterwards changes what the *next* launch
resolves to; it does not reach into a task that is already running, waiting,
recovering, under review, or being shipped. That is the whole point of this
type: reusable defaults are inputs to a decision, not the decision.

Model policy is pinned *per consumer*, not once per task (ADR-0027). Every
declared agent step carries its own complete binding: which profile it came
from, how that profile was chosen, which abstract role it consumes, and the
whole four-role snapshot that profile bound at acceptance. A runtime lookup is
exact — a consumer with no stored binding is an error, never a fall back to
some task-wide default, because "the task's model" stopped being a single fact
the moment one step could differ from another.

The *workflow* is pinned the same way and for the same reason (ADR-0028).
`workflow_binding` names the exact definition revision this task executes, so
a later release cannot change an accepted task's prompts, routes, or sessions
by editing the definition that shares its name. It is nullable only for a task
that predates retained revisions: NULL is a real state meaning "no definition
was ever accepted here", and it is filled in by an explicit operator
confirmation, never by looking up today's catalog.

The document is stored as one version-tagged JSON blob on the task row,
following the registry's existing JSON-text convention (`model_profiles`).
Nothing queries a task by a nested binding, and a partial update would be a
different decision, so there is no field-level write API.

What is deliberately *not* here: credentials, signing keys, forge tokens, and
executable preferences. Those are read live, from the operator's own
configuration, at the moment they are used (ADR-0011, ADR-0015).

ADR-0026 (docs/adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
ADR-0027 (docs/adr/0027-hand-off-model-policy-between-turns.md)
ADR-0028 (docs/adr/0028-retain-declarative-workflow-revisions.md)
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ompire_daemon.model_config import MODEL_ROLES
from ompire_daemon.registry.model_profiles import RoleBinding

# Bumped when the stored shape changes in a way a reader must notice. A task
# written by a newer daemon is refused rather than half-understood. Version 2
# replaced the single task-wide role map with per-consumer bindings; version 3
# added the pinned workflow revision and retired the engine's auxiliary judge
# consumer; version 4 added pinned result attachments and the exact source
# commit an attachment launch was reviewed against.
EXECUTION_INPUTS_VERSION = 4

# The one classification an attached destination can carry. It is a constant,
# not a field an operator or an agent can set: there is no declassification,
# so a stored value other than this one is a damaged document rather than a
# different policy (ADR-0035).
HANDOFF_CLASSIFICATION = "handoff-input"

# How a reviewed launch compared the producer's recorded base observation with
# the commit this task will actually be built from.
BASE_COMPARISON_MATCH = "match"
BASE_COMPARISON_DIFFERENT = "different"
BASE_COMPARISON_UNKNOWN = "unknown"

# A comparison that is not `match` needs the operator to acknowledge it: the
# plan was not validated against this target.
BASE_COMPARISON_NEEDS_ACKNOWLEDGEMENT = (
    BASE_COMPARISON_DIFFERENT,
    BASE_COMPARISON_UNKNOWN,
)

# The workspace/prompt fields a project supplies as defaults and a single task
# may override. Order is presentation order.
WORKSPACE_FIELDS = ("base_branch", "branch_pattern", "workshop_additions", "preamble")

# How the pinned inputs came to exist.
PROVENANCE_ACCEPTED = "accepted"
PROVENANCE_LEGACY_CONFIRMED = "legacy-confirmed"

# Where the effective model profile came from. The first three are the launch
# precedence chain, narrowest first; the last is a confirmed legacy task.
PROFILE_SOURCE_STEP = "step"
PROFILE_SOURCE_TASK = "task"
PROFILE_SOURCE_PROJECT = "project"
PROFILE_SOURCE_LEGACY = "legacy-confirmed"

# Where a consumer's effective role came from.
ROLE_SOURCE_STEP = "step"
ROLE_SOURCE_WORKFLOW = "workflow"

# How a task came to be bound to a workflow revision.
WORKFLOW_SOURCE_ACCEPTED = "accepted"
WORKFLOW_SOURCE_LEGACY_CONFIRMED = "legacy-confirmed"

# The engine used to reserve one auxiliary model consumer, the implicit judge.
# Version 3 has none: no engine-reserved consumer executes, and a request that
# still names this one is refused rather than quietly dropped. The name
# survives only to label the retired binding kept as inert upgrade evidence and
# the legacy `judge` session still visible in old tasks' history.
RETIRED_AUXILIARY_JUDGE = "judge"
AUXILIARY_CONSUMERS: tuple[str, ...] = ()


class UnsupportedExecutionInputsVersionError(ValueError):
    def __init__(self, version: object) -> None:
        super().__init__(
            f"task execution inputs are version {version!r}; this daemon "
            f"understands version {EXECUTION_INPUTS_VERSION}"
        )
        self.version = version


class DamagedAttachmentPolicyError(ValueError):
    """A stored attachment does not carry the handoff policy this daemon
    implements.

    Refused rather than decoded as "no protection". A document whose
    classification cannot be read is a document whose publication restrictions
    cannot be enforced, and reading it permissively is the one failure mode the
    non-publishable contract cannot survive (ADR-0035).
    """

    def __init__(self, result_id: object, found: object) -> None:
        super().__init__(
            f"attachment {result_id!r} carries an unreadable handoff policy "
            f"({found!r}); this daemon enforces {HANDOFF_CLASSIFICATION!r} only"
        )
        self.result_id = result_id
        self.found = found


class MissingConsumerBindingError(LookupError):
    """A model consumer asked for its accepted policy and the task has none.

    Fails closed by design: the alternative — substituting a task-wide
    default — would run a turn under a policy nobody reviewed, which is the
    exact failure per-consumer pinning exists to prevent.
    """

    def __init__(self, kind: str, name: str) -> None:
        super().__init__(f"task has no accepted model binding for {kind} {name!r}")
        self.kind = kind
        self.name = name


@dataclass(frozen=True)
class WorkspaceInputs:
    """The effective workspace and prompt inputs, after inheritance and
    overrides were applied. `preamble` is the literal text — an empty string
    is a real answer ("no preamble"), not an absent one."""

    base_branch: str
    branch_pattern: str
    workshop_additions: str
    preamble: str


@dataclass(frozen=True)
class ConsumerBinding:
    """One model consumer's pinned policy, with its own attribution.

    `profile_name` is provenance, not a live foreign key: the profile may be
    renamed, edited, or (once nothing references it) deleted, and this
    consumer keeps running exactly as accepted, because `roles` is the
    snapshot that actually governs execution.

    `profile_source` and `role_source` are deliberately separate. An operator
    can override one dimension and inherit the other, and a display that
    collapsed them into one "overridden" flag would have to guess which.
    """

    profile_name: str
    profile_source: str
    role: str
    role_source: str
    # The full four-role snapshot of the effective profile. Every native
    # process gets all four, whichever one is active here.
    roles: dict[str, RoleBinding]

    @property
    def binding(self) -> RoleBinding:
        """The concrete pair this consumer's turns run under."""
        return self.roles[self.role]


@dataclass(frozen=True)
class AttachedFile:
    """One attached file, exactly as the producer's manifest describes it.

    Copied into the launch document rather than referenced, so the recipient's
    inputs still say what it was accepted with after the producing revision is
    unreadable. The retained manifest is re-checked against these values at
    materialization: a disagreement is an integrity failure, never a repair.
    """

    path: str
    length: int
    sha256: str
    media_type: str


@dataclass(frozen=True)
class ResultAttachment:
    """One accepted result revision pinned as an input to this task.

    `manifest_id` is the whole point: it names an *exact immutable revision*,
    so a successor capture or a later acceptance on the producing task cannot
    reach a consumer that was already accepted. `manifest_project_name` is the
    label the producer's manifest recorded and is provenance only — project
    membership is decided through current task/project records, so renaming a
    project is not mistaken for a cross-project transfer.

    Every destination is non-publishable, and that is not stored per file:
    there is one classification for the whole contract, checked on read.
    """

    result_id: str
    producer_task_id: int
    manifest_id: str
    content_id: str | None
    accepted_at: str
    manifest_project_name: str
    files: tuple[AttachedFile, ...]
    # The producer's recorded provenance, copied verbatim. Observations, not
    # claims: `capture_merge_base` is what Git said at capture time, never the
    # commit the producing task was launched from.
    provenance: dict[str, Any]
    classification: str = HANDOFF_CLASSIFICATION

    @property
    def destinations(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.files)


@dataclass(frozen=True)
class BaseComparison:
    """How one attachment's producer base observation relates to the commit
    this task is actually built from.

    Per attachment, not per task: two bundles can have been captured against
    different bases, and collapsing them into one verdict would let a matching
    bundle vouch for a stale one.

    `state` is one of match/different/unknown. `changed_paths` is a bounded
    name-status summary offered only when both objects were locally readable;
    `truncated` says so out loud rather than presenting a partial list as the
    whole difference. `detail` names the gap when no comparison was possible.
    """

    result_id: str
    state: str
    target_commit: str
    producer_observation: str | None
    changed_paths: tuple[str, ...] = ()
    truncated: bool = False
    detail: str | None = None

    @property
    def needs_acknowledgement(self) -> bool:
        return self.state in BASE_COMPARISON_NEEDS_ACKNOWLEDGEMENT


@dataclass(frozen=True)
class WorkflowBinding:
    """The exact workflow revision this task executes, and how it got it.

    `legacy_through_seq` is the honest boundary: every step record up to and
    including that sequence happened under a definition nobody retained, and
    the projection says so rather than attributing old prompts to the newly
    confirmed revision. `interrupted_legacy_seq` names the one attempt that
    spans the boundary — it was opened before confirmation and finishes after
    it — because pretending it belongs wholly to either side would be a claim
    about a turn whose beginning is unknown.

    Both are zero/None for a normal acceptance: there is no history to
    disclaim when the definition was pinned before the first step ran.
    """

    revision: str
    source: str
    bound_at: str
    legacy_through_seq: int = 0
    interrupted_legacy_seq: int | None = None


@dataclass(frozen=True)
class TaskExecutionInputs:
    """One task's pinned launch decision.

    `model_profile_name` records the *task-wide* decision — what an
    unoverridden consumer inherited — and stays for display and attribution.
    What executes is always a `ConsumerBinding`.
    """

    provenance: str
    accepted_at: str
    project_name: str
    workflow_name: str
    # The pinned definition, or None for a task accepted before revisions were
    # retained. Execution, recovery, session admission, and presentation all
    # resolve through this — never through `workflow_name` in today's catalog.
    workflow_binding: WorkflowBinding | None
    model_profile_name: str | None
    model_profile_source: str
    # Agent step name → its complete binding. Populated for *every* declared
    # agent step at acceptance, so a later workflow edit cannot silently
    # rebind a step and a runtime lookup never has to invent one.
    step_bindings: dict[str, ConsumerBinding]
    workspace: WorkspaceInputs
    # Which workspace fields the operator overrode for this task rather than
    # inheriting. Kept so task detail can say *why* a value is what it is.
    workspace_overrides: tuple[str, ...]
    branch: str
    # Project-derived checkout and routing facts the later stages need. Copied
    # so a project edit cannot repoint an accepted task's fetches or PRs.
    checkout_path: str
    fetch_remote: str
    upstream_url: str
    fork_url: str | None
    # Historical inputs that could not be recovered for a legacy task. Empty
    # for anything accepted through the normal launch path.
    unknown_inputs: tuple[str, ...] = ()
    # The accepted result revisions this task materializes before its first
    # step runs (ADR-0035). Empty is the ordinary case and is not an error.
    result_attachments: tuple[ResultAttachment, ...] = ()
    # The exact commit an attachment launch was reviewed against. `None` for a
    # launch with no attachments, which pins a branch exactly as before — and
    # for every task written before version 4, where no observation was made
    # and none is invented.
    source_commit: str | None = None
    # One comparison per attachment, in attachment order. Empty for a launch
    # with none.
    base_comparisons: tuple[BaseComparison, ...] = ()
    # Whether the operator acknowledged that the plan was not validated against
    # this target. Meaningful only when the comparison asked for it.
    acknowledged_base_difference: bool = False

    @property
    def preamble(self) -> str:
        return self.workspace.preamble

    @property
    def protected_destinations(self) -> tuple[str, ...]:
        """Every repository-relative path this task may not publish.

        Derived from the stored attachments and nothing else: not from an
        ignore file the agent can edit, not from a pattern like `epics/`, and
        not from anything the task itself supplies. Empty for an ordinary task,
        which is what keeps its candidate identity unchanged.
        """
        paths: set[str] = set()
        for attachment in self.result_attachments:
            paths.update(attachment.destinations)
        return tuple(sorted(paths))

    @property
    def has_attachments(self) -> bool:
        return bool(self.result_attachments)

    def binding_for_step(self, step: str) -> ConsumerBinding:
        try:
            return self.step_bindings[step]
        except KeyError:
            raise MissingConsumerBindingError("agent step", step) from None

    @property
    def workflow_revision(self) -> str | None:
        return self.workflow_binding.revision if self.workflow_binding else None


def _encode_roles(roles: Mapping[str, RoleBinding]) -> dict[str, dict[str, str]]:
    return {
        role: {"model": roles[role].model, "thinking": roles[role].thinking}
        for role in MODEL_ROLES
    }


def decode_roles(document: Mapping[str, Any]) -> dict[str, RoleBinding]:
    """Decode a stored four-role map. Shared with launch resolution and the
    migration, so one shape is understood in exactly one way."""
    return {
        role: RoleBinding(
            model=document[role]["model"], thinking=document[role]["thinking"]
        )
        for role in MODEL_ROLES
    }


def encode_binding(binding: ConsumerBinding) -> dict[str, Any]:
    return {
        "profile_name": binding.profile_name,
        "profile_source": binding.profile_source,
        "role": binding.role,
        "role_source": binding.role_source,
        "roles": _encode_roles(binding.roles),
    }


def decode_consumer_binding(document: Mapping[str, Any]) -> ConsumerBinding:
    return ConsumerBinding(
        profile_name=document["profile_name"],
        profile_source=document["profile_source"],
        role=document["role"],
        role_source=document["role_source"],
        roles=decode_roles(document["roles"]),
    )


def _encode_bindings(
    bindings: Mapping[str, ConsumerBinding],
) -> dict[str, dict[str, Any]]:
    return {name: encode_binding(bindings[name]) for name in sorted(bindings)}


def encode_workflow_binding(binding: WorkflowBinding) -> dict[str, Any]:
    return {
        "revision": binding.revision,
        "source": binding.source,
        "bound_at": binding.bound_at,
        "legacy_through_seq": binding.legacy_through_seq,
        "interrupted_legacy_seq": binding.interrupted_legacy_seq,
    }


def decode_workflow_binding(document: Mapping[str, Any]) -> WorkflowBinding:
    return WorkflowBinding(
        revision=document["revision"],
        source=document["source"],
        bound_at=document["bound_at"],
        legacy_through_seq=document.get("legacy_through_seq", 0),
        interrupted_legacy_seq=document.get("interrupted_legacy_seq"),
    )


def encode_attachment(attachment: ResultAttachment) -> dict[str, Any]:
    return {
        "result_id": attachment.result_id,
        "producer_task_id": attachment.producer_task_id,
        "manifest_id": attachment.manifest_id,
        "content_id": attachment.content_id,
        "accepted_at": attachment.accepted_at,
        "manifest_project_name": attachment.manifest_project_name,
        "classification": attachment.classification,
        "files": [
            {
                "path": entry.path,
                "length": entry.length,
                "sha256": entry.sha256,
                "media_type": entry.media_type,
            }
            for entry in attachment.files
        ],
        "provenance": dict(attachment.provenance),
    }


def decode_attachment(document: Mapping[str, Any]) -> ResultAttachment:
    """Read one stored attachment, refusing a policy this daemon does not
    implement.

    A `classification` other than the single handoff value is a damaged
    document, not a second policy: decoding it as "no protection" is exactly
    the silent declassification this contract exists to prevent.
    """
    classification = document.get("classification")
    if classification != HANDOFF_CLASSIFICATION:
        raise DamagedAttachmentPolicyError(
            document.get("result_id"), classification
        )
    files = document.get("files")
    if not isinstance(files, list) or not files:
        raise DamagedAttachmentPolicyError(
            document.get("result_id"), "no attached file list"
        )
    return ResultAttachment(
        result_id=document["result_id"],
        producer_task_id=document["producer_task_id"],
        manifest_id=document["manifest_id"],
        content_id=document.get("content_id"),
        accepted_at=document["accepted_at"],
        manifest_project_name=document["manifest_project_name"],
        files=tuple(
            AttachedFile(
                path=entry["path"],
                length=entry["length"],
                sha256=entry["sha256"],
                media_type=entry["media_type"],
            )
            for entry in files
        ),
        provenance=dict(document.get("provenance") or {}),
        classification=classification,
    )


def encode_base_comparison(comparison: BaseComparison) -> dict[str, Any]:
    return {
        "result_id": comparison.result_id,
        "state": comparison.state,
        "target_commit": comparison.target_commit,
        "producer_observation": comparison.producer_observation,
        "changed_paths": list(comparison.changed_paths),
        "truncated": comparison.truncated,
        "detail": comparison.detail,
    }


def decode_base_comparison(document: Mapping[str, Any]) -> BaseComparison:
    return BaseComparison(
        result_id=document["result_id"],
        state=document["state"],
        target_commit=document["target_commit"],
        producer_observation=document.get("producer_observation"),
        changed_paths=tuple(document.get("changed_paths", ())),
        truncated=bool(document.get("truncated", False)),
        detail=document.get("detail"),
    )


def execution_inputs_document(inputs: TaskExecutionInputs) -> dict[str, Any]:
    return {
        "version": EXECUTION_INPUTS_VERSION,
        "provenance": inputs.provenance,
        "accepted_at": inputs.accepted_at,
        "project_name": inputs.project_name,
        "workflow_name": inputs.workflow_name,
        "workflow_binding": (
            encode_workflow_binding(inputs.workflow_binding)
            if inputs.workflow_binding is not None
            else None
        ),
        "model_profile_name": inputs.model_profile_name,
        "model_profile_source": inputs.model_profile_source,
        "step_bindings": _encode_bindings(inputs.step_bindings),
        "workspace": {
            "base_branch": inputs.workspace.base_branch,
            "branch_pattern": inputs.workspace.branch_pattern,
            "workshop_additions": inputs.workspace.workshop_additions,
            "preamble": inputs.workspace.preamble,
        },
        "workspace_overrides": list(inputs.workspace_overrides),
        "branch": inputs.branch,
        "checkout_path": inputs.checkout_path,
        "fetch_remote": inputs.fetch_remote,
        "upstream_url": inputs.upstream_url,
        "fork_url": inputs.fork_url,
        "unknown_inputs": list(inputs.unknown_inputs),
        "result_attachments": [
            encode_attachment(attachment) for attachment in inputs.result_attachments
        ],
        "source_commit": inputs.source_commit,
        "base_comparisons": [
            encode_base_comparison(comparison)
            for comparison in inputs.base_comparisons
        ],
        "acknowledged_base_difference": inputs.acknowledged_base_difference,
    }


def encode_execution_inputs(inputs: TaskExecutionInputs) -> str:
    return json.dumps(execution_inputs_document(inputs))


def decode_execution_inputs(raw: str) -> TaskExecutionInputs:
    document = json.loads(raw)
    version = document.get("version")
    if version != EXECUTION_INPUTS_VERSION:
        raise UnsupportedExecutionInputsVersionError(version)
    workspace = document["workspace"]
    return TaskExecutionInputs(
        provenance=document["provenance"],
        accepted_at=document["accepted_at"],
        project_name=document["project_name"],
        workflow_name=document["workflow_name"],
        workflow_binding=(
            decode_workflow_binding(document["workflow_binding"])
            if document.get("workflow_binding") is not None
            else None
        ),
        model_profile_name=document["model_profile_name"],
        model_profile_source=document["model_profile_source"],
        step_bindings={
            name: decode_consumer_binding(entry)
            for name, entry in document["step_bindings"].items()
        },
        workspace=WorkspaceInputs(
            base_branch=workspace["base_branch"],
            branch_pattern=workspace["branch_pattern"],
            workshop_additions=workspace["workshop_additions"],
            preamble=workspace["preamble"],
        ),
        workspace_overrides=tuple(document.get("workspace_overrides", ())),
        branch=document["branch"],
        checkout_path=document["checkout_path"],
        fetch_remote=document["fetch_remote"],
        upstream_url=document["upstream_url"],
        fork_url=document["fork_url"],
        unknown_inputs=tuple(document.get("unknown_inputs", ())),
        result_attachments=tuple(
            decode_attachment(entry)
            for entry in document.get("result_attachments", ())
        ),
        source_commit=document.get("source_commit"),
        base_comparisons=tuple(
            decode_base_comparison(entry)
            for entry in document.get("base_comparisons", ())
        ),
        acknowledged_base_difference=bool(
            document.get("acknowledged_base_difference", False)
        ),
    )


def execution_inputs_payload(inputs: TaskExecutionInputs) -> dict[str, Any]:
    """The API/event shape. Same document as storage — the wire contract is
    deliberately the stored one, so what the UI shows is what execution
    reads."""
    return execution_inputs_document(inputs)


def split_model_identifier(model: str) -> tuple[str, str]:
    """Split a provider-qualified identifier at the *first* slash.

    Later slashes belong to the model id — nested provider catalogs name
    models that way — so this must never be a plain `split("/")`.
    """
    provider, _, model_id = model.partition("/")
    return provider, model_id


@dataclass(frozen=True)
class ModelPolicy:
    """The complete native model configuration one omp process runs under.

    `active` is what `--model`/`--thinking` set; the other three are omp's
    own auxiliary roles, passed on every process so a `/switch smol` or an
    internal role use inside the container runs the model the operator chose
    rather than whatever the host defaults to.

    Every field carries its own thinking level. `off` and `auto` are explicit
    policies here, never "no value".

    Equality decides whether a live process can keep running for the next
    consumer, so all four pairs are part of it (ADR-0027): two steps that
    agree on the active model but disagree on `slow` are *not* running the
    same policy, and a `/switch slow` inside the container would prove it.
    """

    active: RoleBinding
    smol: RoleBinding
    slow: RoleBinding
    plan: RoleBinding

    @classmethod
    def from_roles(
        cls, roles: Mapping[str, RoleBinding], *, active_role: str = "default"
    ) -> ModelPolicy:
        return cls(
            active=roles[active_role],
            smol=roles["smol"],
            slow=roles["slow"],
            plan=roles["plan"],
        )

    @classmethod
    def from_binding(cls, binding: ConsumerBinding) -> ModelPolicy:
        return cls.from_roles(binding.roles, active_role=binding.role)

    @classmethod
    def for_step(cls, inputs: TaskExecutionInputs, step: str) -> ModelPolicy:
        return cls.from_binding(inputs.binding_for_step(step))

    def auxiliary_equals(self, other: ModelPolicy) -> bool:
        """True when at most the active pair differs.

        The native RPC offers `set_model`/`set_thinking_level` and no
        auxiliary-role setter (omp v18.1.10), so this is exactly the question
        "can this transition happen in place, or must the process be replaced
        and its session resumed?".
        """
        return (
            self.smol == other.smol
            and self.slow == other.slow
            and self.plan == other.plan
        )

    def payload(self) -> dict[str, dict[str, str]]:
        """The wire shape for the complete native map."""
        return {
            role: {"model": binding.model, "thinking": binding.thinking}
            for role, binding in (
                ("active", self.active),
                ("smol", self.smol),
                ("slow", self.slow),
                ("plan", self.plan),
            )
        }

    @classmethod
    def from_payload(cls, document: Mapping[str, Any]) -> ModelPolicy:
        return cls(
            **{
                role: RoleBinding(
                    model=document[role]["model"], thinking=document[role]["thinking"]
                )
                for role in ("active", "smol", "slow", "plan")
            }
        )
