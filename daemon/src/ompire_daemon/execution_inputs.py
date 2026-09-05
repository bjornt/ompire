"""The accepted execution inputs of one task: everything a run needs after
the operator reviewed and submitted it.

A task's inputs are decided once, at acceptance (or, for a task that predates
them, at explicit legacy confirmation), and never recomputed. Editing a
project default or a model profile afterwards changes what the *next* launch
resolves to; it does not reach into a task that is already running, waiting,
recovering, under review, or being shipped. That is the whole point of this
type: reusable defaults are inputs to a decision, not the decision.

The document is stored as one version-tagged JSON blob on the task row,
following the registry's existing JSON-text convention (`model_profiles`).
Nothing queries a task by a nested binding, and a partial update would be a
different decision, so there is no field-level write API.

What is deliberately *not* here: credentials, signing keys, forge tokens, and
executable preferences. Those are read live, from the operator's own
configuration, at the moment they are used (ADR-0011, ADR-0015).

ADR-0026 (docs/adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ompire_daemon.model_config import JUDGE_ROLE, MODEL_ROLES
from ompire_daemon.registry.model_profiles import RoleBinding

# Bumped when the stored shape changes in a way a reader must notice. A task
# written by a newer daemon is refused rather than half-understood.
EXECUTION_INPUTS_VERSION = 1

# The workspace/prompt fields a project supplies as defaults and a single task
# may override. Order is presentation order.
WORKSPACE_FIELDS = ("base_branch", "branch_pattern", "workshop_additions", "preamble")

# How the pinned inputs came to exist.
PROVENANCE_ACCEPTED = "accepted"
PROVENANCE_LEGACY_CONFIRMED = "legacy-confirmed"

# Where the effective model profile came from.
PROFILE_SOURCE_TASK = "task"
PROFILE_SOURCE_PROJECT = "project"
PROFILE_SOURCE_LEGACY = "legacy-confirmed"


class UnsupportedExecutionInputsVersionError(ValueError):
    def __init__(self, version: object) -> None:
        super().__init__(
            f"task execution inputs are version {version!r}; this daemon "
            f"understands version {EXECUTION_INPUTS_VERSION}"
        )
        self.version = version


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
class TaskExecutionInputs:
    """One task's pinned launch decision.

    `model_profile_name` is provenance, not a live foreign key: the profile
    may be renamed, edited, or (once nothing references it) deleted, and this
    task keeps running exactly as accepted. `roles` is the snapshot that
    actually governs execution.
    """

    provenance: str
    accepted_at: str
    project_name: str
    workflow_name: str
    model_profile_name: str | None
    model_profile_source: str
    # The full four-role snapshot. Every native process gets all four.
    roles: dict[str, RoleBinding]
    # Agent step name → abstract role. Populated from the workflow descriptor
    # at acceptance so a later workflow edit cannot silently rebind a step.
    step_roles: dict[str, str]
    judge_role: str
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
    def active(self) -> RoleBinding:
        """The pair an ordinary agent step runs under in this change: every
        built-in step declares `default`. Per-step overrides are the next
        epic child; until then a single active pair is the honest summary."""
        return self.roles["default"]

    @property
    def preamble(self) -> str:
        return self.workspace.preamble

    def role_for_step(self, step: str) -> str:
        return self.step_roles.get(step, "default")

    def binding_for_step(self, step: str) -> RoleBinding:
        return self.roles[self.role_for_step(step)]


def _encode_roles(roles: Mapping[str, RoleBinding]) -> dict[str, dict[str, str]]:
    return {
        role: {"model": roles[role].model, "thinking": roles[role].thinking}
        for role in MODEL_ROLES
    }


def encode_execution_inputs(inputs: TaskExecutionInputs) -> str:
    return json.dumps(
        {
            "version": EXECUTION_INPUTS_VERSION,
            "provenance": inputs.provenance,
            "accepted_at": inputs.accepted_at,
            "project_name": inputs.project_name,
            "workflow_name": inputs.workflow_name,
            "model_profile_name": inputs.model_profile_name,
            "model_profile_source": inputs.model_profile_source,
            "roles": _encode_roles(inputs.roles),
            "step_roles": dict(inputs.step_roles),
            "judge_role": inputs.judge_role,
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
    )


def decode_execution_inputs(raw: str) -> TaskExecutionInputs:
    document = json.loads(raw)
    version = document.get("version")
    if version != EXECUTION_INPUTS_VERSION:
        raise UnsupportedExecutionInputsVersionError(version)
    roles_document = document["roles"]
    workspace = document["workspace"]
    return TaskExecutionInputs(
        provenance=document["provenance"],
        accepted_at=document["accepted_at"],
        project_name=document["project_name"],
        workflow_name=document["workflow_name"],
        model_profile_name=document["model_profile_name"],
        model_profile_source=document["model_profile_source"],
        roles={
            role: RoleBinding(
                model=roles_document[role]["model"],
                thinking=roles_document[role]["thinking"],
            )
            for role in MODEL_ROLES
        },
        step_roles=dict(document["step_roles"]),
        judge_role=document.get("judge_role", JUDGE_ROLE),
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
    """The API/event shape. Same document as storage, decoded — the wire
    contract is deliberately the stored one, so what the UI shows is what
    execution reads."""
    return json.loads(encode_execution_inputs(inputs))


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
    def for_step(cls, inputs: TaskExecutionInputs, step: str) -> ModelPolicy:
        return cls.from_roles(inputs.roles, active_role=inputs.role_for_step(step))

    @classmethod
    def for_judge(cls, inputs: TaskExecutionInputs) -> ModelPolicy:
        """The judge's active pair is the profile's `slow` binding; its
        auxiliary map is still the task's own profile."""
        return cls.from_roles(inputs.roles, active_role=inputs.judge_role)
