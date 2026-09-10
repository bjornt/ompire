"""Model-profile registry: CRUD against the `model_profiles` table. No ORM —
Core queries only.

A profile is a global, reusable name for four model-role bindings. Each role
binds a concrete provider-qualified model *and* an explicit thinking level;
neither is inferred from the other, from omp's host configuration, or from
another role. Profiles carry no repository, workflow, or credential policy.

Validation here is structural only. It says the identifier is well formed —
never that the provider exists, that credentials are configured, that the
model is available, or that it supports the selected reasoning mode. Nothing
in this module calls a provider or model endpoint.

ADR-0025 (docs/adr/0025-store-global-model-profiles-separately-from-launch-policy.md)
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine

from ompire_daemon.db import model_profiles, projects
from ompire_daemon.model_config import (
    MODEL_ROLES,
    RoleBinding,
    validate_roles,
)
from ompire_daemon.platform.transactions import reserved_write

# `MODEL_ROLES`, `RoleBinding`, and role-map validation live in
# `model_config`: launch resolution and the pinned task inputs bind the same
# values, so no persistence module owns them. What stays here is the profile
# identity itself — names, lookup, CRUD, and the reference guards.

# Same lowercase alphanumeric-and-hyphen convention project names
# use; profiles report their own error rather than borrowing the project one.
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class InvalidModelProfileNameError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"invalid model profile name {name!r}: must be lowercase "
            "alphanumerics and hyphens"
        )
        self.name = name


class DuplicateModelProfileError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"model profile {name!r} already exists")
        self.name = name


class ModelProfileNotFoundError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"model profile {name!r} not found")
        self.name = name


class ModelProfileReferencedError(Exception):
    """409 detail for deletion: projects still name this profile as their
    default. No cascade — the operator clears or reassigns them."""

    def __init__(self, name: str, project_names: list[str]) -> None:
        super().__init__(
            f"model profile {name!r} is the default for "
            f"{', '.join(project_names)}; clear or reassign "
            "those project defaults first"
        )
        self.name = name
        self.project_names = project_names


class UnknownModelProfileReferenceError(ValueError):
    """422 detail for a project pointing at a profile that does not exist."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"unknown model profile {name!r}: create it under "
            "Settings → Model profiles first"
        )
        self.name = name


@dataclass(frozen=True)
class ModelProfile:
    name: str
    roles: dict[str, RoleBinding]
    created_at: str
    updated_at: str


def validate_profile_name(name: str) -> None:
    if not _SLUG_RE.match(name):
        raise InvalidModelProfileNameError(name)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _encode_roles(roles: Mapping[str, RoleBinding]) -> str:
    return json.dumps(
        {role: {"model": roles[role].model, "thinking": roles[role].thinking} for role in MODEL_ROLES}
    )


def _row_to_profile(row) -> ModelProfile:
    decoded = json.loads(row.roles_json)
    return ModelProfile(
        name=row.name,
        roles={
            role: RoleBinding(
                model=decoded[role]["model"], thinking=decoded[role]["thinking"]
            )
            for role in MODEL_ROLES
        },
        created_at=row.created_at,
        updated_at=row.updated_at,
    )

def require_profile_exists(conn: Connection, name: str) -> None:
    """Assert a profile reference inside an open write reservation."""
    row = conn.execute(
        model_profiles.select()
        .with_only_columns(model_profiles.c.name)
        .where(model_profiles.c.name == name)
    ).first()
    if row is None:
        raise UnknownModelProfileReferenceError(name)


def list_model_profiles(engine: Engine) -> list[ModelProfile]:
    with engine.connect() as conn:
        rows = conn.execute(
            model_profiles.select().order_by(model_profiles.c.name)
        ).all()
    return [_row_to_profile(row) for row in rows]


def get_model_profile(engine: Engine, name: str) -> ModelProfile:
    with engine.connect() as conn:
        row = conn.execute(
            model_profiles.select().where(model_profiles.c.name == name)
        ).first()
    if row is None:
        raise ModelProfileNotFoundError(name)
    return _row_to_profile(row)


def create_model_profile(
    engine: Engine, *, name: str, roles: Mapping[str, object]
) -> ModelProfile:
    validate_profile_name(name)
    validated = validate_roles(roles)
    now = _now_iso()
    # The existence check and the insert share one reservation, so a duplicate
    # is reported as a duplicate rather than as whatever constraint fired.
    with reserved_write(engine) as conn:
        clash = conn.execute(
            model_profiles.select()
            .with_only_columns(model_profiles.c.name)
            .where(model_profiles.c.name == name)
        ).first()
        if clash is not None:
            raise DuplicateModelProfileError(name)
        conn.execute(
            model_profiles.insert().values(
                name=name,
                roles_json=_encode_roles(validated),
                created_at=now,
                updated_at=now,
            )
        )
    return ModelProfile(name=name, roles=validated, created_at=now, updated_at=now)


def update_model_profile(
    engine: Engine, name: str, *, roles: Mapping[str, object]
) -> ModelProfile:
    """Replace all four bindings at once.

    The name is the stable identifier and never changes here. Validation runs
    before anything is written, so a refused update leaves the saved profile —
    every binding and its creation identity — exactly as it was.
    """
    validated = validate_roles(roles)
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            model_profiles.select().where(model_profiles.c.name == name)
        ).first()
        if row is None:
            raise ModelProfileNotFoundError(name)
        created_at = row.created_at
        conn.execute(
            model_profiles.update()
            .where(model_profiles.c.name == name)
            .values(roles_json=_encode_roles(validated), updated_at=now)
        )
    return ModelProfile(
        name=name, roles=validated, created_at=created_at, updated_at=now
    )


def referencing_project_names(conn: Connection, name: str) -> list[str]:
    rows = conn.execute(
        projects.select()
        .with_only_columns(projects.c.name)
        .where(projects.c.default_model_profile == name)
        .order_by(projects.c.name)
    ).all()
    return [row.name for row in rows]


def delete_model_profile(engine: Engine, name: str) -> None:
    """Remove a profile no project still points at.

    The reference scan and the delete share one write reservation: a project
    assignment committing in between would otherwise pass its own check and
    then be orphaned by this delete.
    """
    with reserved_write(engine) as conn:
        row = conn.execute(
            model_profiles.select()
            .with_only_columns(model_profiles.c.name)
            .where(model_profiles.c.name == name)
        ).first()
        if row is None:
            raise ModelProfileNotFoundError(name)
        referencing = referencing_project_names(conn, name)
        if referencing:
            raise ModelProfileReferencedError(name, referencing)
        conn.execute(model_profiles.delete().where(model_profiles.c.name == name))
