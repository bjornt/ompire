"""The accepted execution inputs of one task: everything a run needs after
the operator reviewed and submitted it.

A task's inputs are decided once, at acceptance (or, for a task that predates
them, at explicit legacy confirmation), and never recomputed. Editing a
project default or a model profile afterwards changes what the *next* launch
resolves to; it does not reach into a task that is already running, waiting,
recovering, under review, or being shipped. That is the whole point of this
type: reusable defaults are inputs to a decision, not the decision.

Model policy is pinned *per consumer*, not once per task (ADR-0027). Every
declared agent step and the engine-reserved judge carries its own complete
binding: which profile it came from, how that profile was chosen, which
abstract role it consumes, and the whole four-role snapshot that profile
bound at acceptance. A runtime lookup is exact — a consumer with no stored
binding is an error, never a fall back to some task-wide default, because
"the task's model" stopped being a single fact the moment one step could
differ from another.

The document is stored as one version-tagged JSON blob on the task row,
following the registry's existing JSON-text convention (`model_profiles`).
Nothing queries a task by a nested binding, and a partial update would be a
different decision, so there is no field-level write API.

What is deliberately *not* here: credentials, signing keys, forge tokens, and
executable preferences. Those are read live, from the operator's own
configuration, at the moment they are used (ADR-0011, ADR-0015).

ADR-0026 (docs/adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
ADR-0027 (docs/adr/0027-hand-off-model-policy-between-turns.md)
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
# replaced the single task-wide role map with per-consumer bindings.
EXECUTION_INPUTS_VERSION = 2

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

# The engine-reserved auxiliary consumers, addressed by name. A separate
# namespace from step names on purpose: a decision step can never become an
# agent binding by sharing a name with one.
AUXILIARY_JUDGE = "judge"
AUXILIARY_CONSUMERS = (AUXILIARY_JUDGE,)


class UnsupportedExecutionInputsVersionError(ValueError):
    def __init__(self, version: object) -> None:
        super().__init__(
            f"task execution inputs are version {version!r}; this daemon "
            f"understands version {EXECUTION_INPUTS_VERSION}"
        )
        self.version = version


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
    model_profile_name: str | None
    model_profile_source: str
    # Agent step name → its complete binding. Populated for *every* declared
    # agent step at acceptance, so a later workflow edit cannot silently
    # rebind a step and a runtime lookup never has to invent one.
    step_bindings: dict[str, ConsumerBinding]
    # Engine-reserved consumer name → its complete binding (today: `judge`).
    auxiliary_bindings: dict[str, ConsumerBinding]
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

    @property
    def preamble(self) -> str:
        return self.workspace.preamble

    def binding_for_step(self, step: str) -> ConsumerBinding:
        try:
            return self.step_bindings[step]
        except KeyError:
            raise MissingConsumerBindingError("agent step", step) from None

    def binding_for_auxiliary(self, name: str) -> ConsumerBinding:
        try:
            return self.auxiliary_bindings[name]
        except KeyError:
            raise MissingConsumerBindingError("auxiliary consumer", name) from None


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


def execution_inputs_document(inputs: TaskExecutionInputs) -> dict[str, Any]:
    return {
        "version": EXECUTION_INPUTS_VERSION,
        "provenance": inputs.provenance,
        "accepted_at": inputs.accepted_at,
        "project_name": inputs.project_name,
        "workflow_name": inputs.workflow_name,
        "model_profile_name": inputs.model_profile_name,
        "model_profile_source": inputs.model_profile_source,
        "step_bindings": _encode_bindings(inputs.step_bindings),
        "auxiliary_bindings": _encode_bindings(inputs.auxiliary_bindings),
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
        model_profile_name=document["model_profile_name"],
        model_profile_source=document["model_profile_source"],
        step_bindings={
            name: decode_consumer_binding(entry)
            for name, entry in document["step_bindings"].items()
        },
        auxiliary_bindings={
            name: decode_consumer_binding(entry)
            for name, entry in document["auxiliary_bindings"].items()
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
    own auxiliary roles, passed on every process (including the judge's) so
    a `/switch smol` or an internal role use inside the container runs the
    model the operator chose rather than whatever the host defaults to.

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

    @classmethod
    def for_judge(cls, inputs: TaskExecutionInputs) -> ModelPolicy:
        return cls.from_binding(inputs.binding_for_auxiliary(AUXILIARY_JUDGE))

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
