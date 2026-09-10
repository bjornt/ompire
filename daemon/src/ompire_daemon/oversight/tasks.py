"""The one wire shape of a task row and of a reviewed launch resolution.

Task storage answers "what was accepted"; this module answers "what a client
is told about it". REST responses, `task_updated`/`task_created` events, and
the WebSocket snapshot all serialize through here, so a client cannot see two
different shapes for the same row — and task persistence does not depend on
presentation or workflow-readiness resolution to store or read a record
(R6 of the work-boundary change).

The projection may resolve the task's pinned definition to describe it; that
readiness classification lives in `taskdefinition`, which is why this module
sits outside work persistence rather than inside it.

ADR-0026, ADR-0028.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.model_config import MODEL_ROLES
from ompire_daemon.taskdefinition import (
    resolve_task_definition,
    workflow_readiness,
)
from ompire_daemon.work.inputs import (
    AUXILIARY_CONSUMERS,
    ConsumerBinding,
    encode_binding,
    execution_inputs_payload,
)
from ompire_daemon.work.launch import ResolvedLaunch
from ompire_daemon.work.tasks import Task


def task_payload(task: Task, *, engine: Engine) -> dict:
    """The one wire shape of a task row, used by both REST responses and
    `task_updated` events so a client cannot see two different shapes for the
    same row.

    `execution_inputs` is the accepted decision itself — the same document
    execution reads — and `null` means the task predates pinned inputs, which
    `needs_configuration` states outright so a client does not have to infer
    a blocker from an absent field.

    The workflow fields describe the definition *this task* pinned, resolved
    through `taskdefinition` (ADR-0028). `workflow_primary_session` is null
    rather than a guess whenever the definition cannot be resolved: a client
    that substituted a plausible default would point review and shipping at a
    session this task may never have declared. A task whose revision is
    damaged or unsupported still serializes — it reports why, and stays
    readable, stoppable, and cleanable.
    """
    payload = asdict(task)
    payload["execution_inputs"] = (
        execution_inputs_payload(task.execution_inputs)
        if task.execution_inputs is not None
        else None
    )
    payload["needs_configuration"] = task.execution_inputs is None
    binding = (
        task.execution_inputs.workflow_binding
        if task.execution_inputs is not None
        else None
    )
    readiness = workflow_readiness(engine, task)
    payload["workflow_revision"] = binding.revision if binding else None
    payload["workflow_revision_source"] = binding.source if binding else None
    payload["workflow_ready"] = readiness.ready
    payload["workflow_readiness_reason"] = readiness.reason
    payload["workflow_readiness_detail"] = readiness.detail
    payload["workflow_primary_session"] = None
    payload["workflow_sessions"] = None
    if readiness.ready:
        definition = resolve_task_definition(engine, task).definition
        payload["workflow_primary_session"] = definition.primary
        payload["workflow_sessions"] = list(definition.sessions)
    return payload


def binding_payload(binding: ConsumerBinding) -> dict[str, Any]:
    """One consumer's wire shape.

    Byte-identical to what acceptance stores for that consumer, so the row
    the operator reviewed and the row the run executes are comparable
    without translation. The effective model and thinking level stay on the
    preview row itself rather than being duplicated in here.
    """
    return encode_binding(binding)


def resolution_payload(resolved: ResolvedLaunch) -> dict[str, Any]:
    """The preview's wire shape."""
    inputs = resolved.inputs
    return {
        "preview_token": resolved.fingerprint,
        "project_name": inputs.project_name,
        "workflow_name": inputs.workflow_name,
        # The exact procedure being accepted, not just its name. The UI shows
        # it beside the flow and offers the definition itself for reading.
        "workflow_revision": resolved.revision.revision,
        "workflow_format": resolved.revision.format,
        "workflow_primary_session": resolved.revision.definition.primary,
        "workflow_sessions": list(resolved.revision.definition.sessions),
        "model_profile": inputs.model_profile_name,
        "model_profile_source": inputs.model_profile_source,
        "project_default_model_profile": resolved.project_default_profile,
        "auxiliary_consumers": list(AUXILIARY_CONSUMERS),
        # The task-wide profile's own map: what a row inheriting both
        # dimensions resolves against.
        "roles": {
            role: {
                "model": resolved.task_roles[role].model,
                "thinking": resolved.task_roles[role].thinking,
            }
            for role in MODEL_ROLES
        },
        "workspace": {
            "base_branch": inputs.workspace.base_branch,
            "branch_pattern": inputs.workspace.branch_pattern,
            "workshop_additions": inputs.workspace.workshop_additions,
            "preamble": inputs.workspace.preamble,
        },
        "inherited_workspace": {
            "base_branch": resolved.inherited_workspace.base_branch,
            "branch_pattern": resolved.inherited_workspace.branch_pattern,
            "workshop_additions": resolved.inherited_workspace.workshop_additions,
            "preamble": resolved.inherited_workspace.preamble,
        },
        "workspace_overrides": list(inputs.workspace_overrides),
        "branch": inputs.branch,
        # The exact commit this launch was resolved against, null for an
        # ordinary launch. Named so the operator reviews an identity, not "the
        # tip of main, whenever the clone happens to run".
        "source_commit": inputs.source_commit,
        "needs_base_acknowledgement": resolved.needs_base_acknowledgement,
        "acknowledged_base_difference": inputs.acknowledged_base_difference,
        "result_attachments": [
            {
                "result_id": attachment.result_id,
                "producer_task_id": attachment.producer_task_id,
                "manifest_id": attachment.manifest_id,
                "content_id": attachment.content_id,
                "accepted_at": attachment.accepted_at,
                "manifest_project_name": attachment.manifest_project_name,
                # The fixed policy every destination carries. Rendered as a
                # label, never as an editable control: there is no
                # declassification.
                "classification": attachment.classification,
                "publishable": False,
                "files": [
                    {
                        "path": entry.path,
                        "length": entry.length,
                        "sha256": entry.sha256,
                        "media_type": entry.media_type,
                    }
                    for entry in attachment.files
                ],
                "destinations": list(attachment.destinations),
                "provenance": attachment.provenance,
            }
            for attachment in inputs.result_attachments
        ],
        "base_comparisons": [
            {
                "result_id": comparison.result_id,
                "state": comparison.state,
                "target_commit": comparison.target_commit,
                "producer_observation": comparison.producer_observation,
                "changed_paths": list(comparison.changed_paths),
                "truncated": comparison.truncated,
                "detail": comparison.detail,
            }
            for comparison in inputs.base_comparisons
        ],
        "steps": [
            {
                "step": row.step,
                "kind": row.kind,
                "session": row.session,
                "conditional": row.conditional,
                "declared_role": row.declared_role,
                # `None` for a command, decision, or gate: no binding, and no
                # override controls.
                "binding": binding_payload(row.binding) if row.binding else None,
                # Kept flat as well, because every existing consumer of this
                # payload reads the effective pair straight off the row.
                "role": row.role,
                "model": row.model,
                "thinking": row.thinking,
            }
            for row in resolved.rows
        ],
    }
