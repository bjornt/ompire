"""Work configuration commands: projects, profiles, and reconciliation.

The transport-independent bodies behind the work routers. Each function takes
its dependencies explicitly — engine, config, event hub, the setup manager —
and raises the owning module's domain errors; HTTP never appears here. Simple
reads are not wrapped: callers use the public work queries directly.

What each command adds over the registry call beneath it is the part that is
not storage: filesystem observation before admitting a checkout, the
clone-mode destination derivation, omission-versus-null update semantics,
result events, and setup scheduling after a commit.

ADR-0022 (clone/adopt), ADR-0025 (profiles), ADR-0026 (reconciliation).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.settings import SettingsStore, effective_checkout_root
from ompire_daemon.work.checkout import inspect_checkout, inspection_message
from ompire_daemon.work.profiles import (
    ModelProfile,
    create_model_profile,
    delete_model_profile,
    update_model_profile,
)
from ompire_daemon.work.projects import (
    UNSUPPLIED,
    Project,
    ProjectSetupBusyError,
    create_project,
    delete_project,
    get_project,
    update_project,
)
from ompire_daemon.work.reconciliation import (
    ProjectDecision,
    confirm_project_reconciliation,
)
from ompire_daemon.work.setup import (
    CLONE_FETCH_REMOTE,
    DestinationExistsError,
    ProjectSetupManager,
    clone_target,
)


class CheckoutPathSuppliedInCloneModeError(ValueError):
    """Clone mode derives its own destination; a supplied path is refused
    rather than ignored, because the one place Ompire creates a repository
    outside its task root must stay bounded (ADR-0022/0023)."""


class UnusableCheckoutError(ValueError):
    """An adopted checkout Ompire cannot clone a task workspace from. Carries
    the read-only inspection's explanation (ADR-0022)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ImmutableCheckoutPathError(Exception):
    """A cloned project's checkout path cannot be repointed: the checkout is
    Ompire's own derived path, and changing it would silently orphan what was
    created (ADR-0022)."""


@dataclass(frozen=True)
class ProjectRegistration:
    """One project registration, after wire validation, before admission.

    `checkout_path` is the caller-supplied adoption candidate (or None in
    clone mode); the clone-mode destination is derived inside the command so
    the derivation cannot be bypassed by any caller.
    """

    name: str
    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str | None
    checkout_mode: str
    fetch_remote: str
    default_model_profile: str | None
    base_branch: str
    branch_pattern: str | None
    workshop_additions: str
    preamble: str


@dataclass(frozen=True)
class ProjectChanges:
    """One project update's editable fields, with the three-valued profile
    reference expressed as `profile_supplied`/`profile` and only the
    workspace defaults the caller actually mentioned."""

    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str
    fetch_remote: str
    new_name: str | None
    profile_supplied: bool
    profile: str | None
    supplied_workspace_defaults: Mapping[str, Any]


async def _require_usable_checkout(
    checkout_path: str, fetch_remote: str, timeout: int
) -> None:
    """Refuse an adopted checkout Ompire cannot clone a task workspace from.

    Read-only: this only looks (ADR-0022).
    """
    inspection = await inspect_checkout(
        checkout_path, fetch_remote=fetch_remote, timeout=timeout
    )
    if not inspection.ok:
        raise UnusableCheckoutError(inspection_message(inspection, fetch_remote))


async def register_project(
    engine: Engine,
    config: Config,
    events: EventHub,
    settings: SettingsStore,
    setup: ProjectSetupManager,
    registration: ProjectRegistration,
) -> Project:
    """Register a project, adopting an existing checkout or creating one.

    Adoption is answered here: validation is a handful of local git reads, so
    the caller gets ready-or-why. Clone mode returns a `cloning` project
    immediately and continues in the background.
    """
    if registration.checkout_mode == "clone":
        if registration.checkout_path:
            raise CheckoutPathSuppliedInCloneModeError(
                "checkout_path cannot be supplied in clone mode; the "
                "destination is derived from the effective checkout root"
            )
        # Derived, never supplied — that is what bounds the one place Ompire
        # creates a repository outside its task root (ADR-0022/0023).
        target = clone_target(
            effective_checkout_root(settings.effective()), registration.name
        )
        if target.destination.exists():
            raise DestinationExistsError(target.destination)
        checkout_path: str | None = str(target.destination)
        fetch_remote = CLONE_FETCH_REMOTE
        checkout_mode, setup_state = "cloned", "cloning"
    else:
        checkout_path = registration.checkout_path or str(
            effective_checkout_root(settings.effective()) / registration.name
        )
        fetch_remote = registration.fetch_remote
        await _require_usable_checkout(
            checkout_path, fetch_remote, config.spawn_step_timeout
        )
        checkout_mode, setup_state = "adopted", "ready"

    # Both modes converge here, so an unknown profile is refused before any
    # row exists — and, in clone mode, before a clone job is scheduled.
    project = create_project(
        engine,
        name=registration.name,
        title=registration.title,
        upstream_url=registration.upstream_url,
        fork_url=registration.fork_url,
        checkout_path=checkout_path,
        default_checkout_root=config.checkout_root,
        checkout_mode=checkout_mode,
        fetch_remote=fetch_remote,
        setup_state=setup_state,
        default_model_profile=registration.default_model_profile,
        base_branch=registration.base_branch,
        branch_pattern=registration.branch_pattern
        or config.default_branch_pattern,
        workshop_additions=registration.workshop_additions,
        preamble=registration.preamble,
    )
    events.publish("project_created", asdict(project))
    if project.setup_state == "cloning":
        setup.start(project)
    return project


async def change_project(
    engine: Engine,
    config: Config,
    events: EventHub,
    name: str,
    changes: ProjectChanges,
) -> Project:
    """Update a project's editable fields.

    The checkout mode is fixed at registration; the busy and cloned-path
    refusals are admission rules of the command, not wire validation.
    """
    current = get_project(engine, name)
    if current.setup_state == "cloning":
        raise ProjectSetupBusyError(name)
    if current.checkout_mode == "cloned" and changes.checkout_path != current.checkout_path:
        raise ImmutableCheckoutPathError(
            f"project {name!r} uses a checkout Ompire created; its path cannot "
            "be changed"
        )
    checkout_changed = (
        changes.checkout_path != current.checkout_path
        or changes.fetch_remote != current.fetch_remote
    )
    if current.setup_state == "ready" and checkout_changed:
        await _require_usable_checkout(
            changes.checkout_path, changes.fetch_remote, config.spawn_step_timeout
        )
    project = update_project(
        engine,
        name,
        title=changes.title,
        upstream_url=changes.upstream_url,
        fork_url=changes.fork_url,
        checkout_path=changes.checkout_path,
        fetch_remote=changes.fetch_remote,
        new_name=changes.new_name,
        # Absent from the body means "leave it alone"; the registry resolves
        # that against the stored row inside its write transaction, not
        # against the `current` read above.
        default_model_profile=(
            changes.profile if changes.profile_supplied else UNSUPPLIED
        ),
        **changes.supplied_workspace_defaults,
    )
    renamed = changes.new_name is not None and changes.new_name != name
    if renamed:
        # Keyed-by-name consumers can't match a renamed payload via
        # `project_updated`; the rename event carries the old key.
        events.publish(
            "project_renamed", {"old_name": name, "project": asdict(project)}
        )
    else:
        events.publish("project_updated", asdict(project))
    return project


def remove_project(engine: Engine, events: EventHub, name: str) -> None:
    delete_project(engine, name)
    events.publish("project_deleted", {"name": name})


async def retry_project_setup(setup: ProjectSetupManager, name: str) -> Project:
    """Arm a retry for a failed clone. Must run on the event loop: `retry`
    schedules the clone job there."""
    return setup.retry(name)


def confirm_reconciliation(
    engine: Engine, events: EventHub, name: str, decision: ProjectDecision
) -> Project:
    """Record the operator's launch-configuration decision for one project."""
    project = confirm_project_reconciliation(engine, name, decision)
    events.publish("project_updated", asdict(project))
    return project


# --- Model profiles ---------------------------------------------------------
# Thin by design (ADR-0025): the registry owns validation and reference
# guards; the command adds the committed-change event.


def create_profile(
    engine: Engine, events: EventHub, *, name: str, roles: Mapping[str, object]
) -> ModelProfile:
    profile = create_model_profile(engine, name=name, roles=roles)
    events.publish("model_profile_created", asdict(profile))
    return profile


def update_profile(
    engine: Engine, events: EventHub, name: str, *, roles: Mapping[str, object]
) -> ModelProfile:
    profile = update_model_profile(engine, name, roles=roles)
    events.publish("model_profile_updated", asdict(profile))
    return profile


def delete_profile(engine: Engine, events: EventHub, name: str) -> None:
    delete_model_profile(engine, name)
    events.publish("model_profile_deleted", {"name": name})
