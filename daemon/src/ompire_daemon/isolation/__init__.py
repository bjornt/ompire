"""Isolation: where may untrusted work execute?

The explicitly declared public resource surface. One boundary owns the
disposable workspace and its container: preparing a confined clone from
resolved values, verifying a pinned base, launching and observing the
Workshop, running finite sandbox commands, starting piped container processes,
applying Git exclusions, guarding workspace writers, and destroying resources
safely.

Everything below the package is internal except what this surface names.
Cross-owner consumers import from `ompire_daemon.isolation` — never its
submodules — and the architecture checker rejects anything else. The package
imports no product module: no work registry, workflow, delivery, artifact
service, HTTP transport, or native agent RPC (ADR-0039). It receives resolved
values and returns resource observations, process handles, command results,
or classified failures; an owner identity is an opaque integer that needs no
task row.
"""

from ompire_daemon.isolation.additions import recover_pending
from ompire_daemon.isolation.excludes import (
    GIT_EXCLUDE_OMPIRE_ENTRY,
    ExcludeUpdateError,
    ensure_git_excludes,
    exclude_pattern_for,
)
from ompire_daemon.isolation.guard import (
    WorkspaceBlockedError,
    WorkspaceBusyError,
    WorkspaceGuard,
)
from ompire_daemon.isolation.workshop import (
    SandboxCommandError,
    SandboxCommandResult,
    WorkshopRemoveError,
    remove_workshop,
    run_sandbox_command,
    start_sandbox_process,
    workshop_status,
)
from ompire_daemon.isolation.workspace import (
    PhaseProgress,
    WorkspaceDeletionRefused,
    WorkspaceOperationError,
    WorkspaceRef,
    WorkspaceSpec,
    destroy_workspace,
    launch_workshop,
    prepare_clone,
    verify_pinned_source,
)

__all__ = [
    "GIT_EXCLUDE_OMPIRE_ENTRY",
    "ExcludeUpdateError",
    "PhaseProgress",
    "SandboxCommandError",
    "SandboxCommandResult",
    "WorkshopRemoveError",
    "WorkspaceBlockedError",
    "WorkspaceBusyError",
    "WorkspaceDeletionRefused",
    "WorkspaceGuard",
    "WorkspaceOperationError",
    "WorkspaceRef",
    "WorkspaceSpec",
    "destroy_workspace",
    "ensure_git_excludes",
    "exclude_pattern_for",
    "launch_workshop",
    "prepare_clone",
    "recover_pending",
    "remove_workshop",
    "run_sandbox_command",
    "start_sandbox_process",
    "verify_pinned_source",
    "workshop_status",
]
