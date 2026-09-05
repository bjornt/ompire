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

import contextlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine

from ompire_daemon.db import model_profiles, projects
from ompire_daemon.model_config import (
    THINKING_LEVELS,
    InvalidThinkingLevelError,
    validate_thinking,
)

# The four fixed roles, in presentation order. This tuple is the contract:
# a profile has exactly these, no more and no fewer.
MODEL_ROLES = ("default", "smol", "slow", "plan")

# Same lowercase alphanumeric-and-hyphen convention project and template names
# use; profiles report their own error rather than borrowing the project one.
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# The provider segment before the first slash: letters, digits, dots,
# underscores and hyphens, opening with a letter or digit.
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Characters that would make an identifier a path, a URL, a glob, or a shell
# argument rather than a model name.
_REJECTED_CHARS = ("\\", "*", "?", "#")


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


class InvalidRoleSetError(ValueError):
    """The submitted role map is not exactly the four required roles."""

    def __init__(self, missing: list[str], unknown: list[str]) -> None:
        parts: list[str] = []
        if missing:
            parts.append(f"missing roles: {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown roles: {', '.join(unknown)}")
        super().__init__(
            f"model profile roles must be exactly {', '.join(MODEL_ROLES)}"
            + (f" ({'; '.join(parts)})" if parts else "")
        )
        self.missing = missing
        self.unknown = unknown


class InvalidRoleBindingError(ValueError):
    """One role's `model` or `thinking` value is unusable. Carries the role and
    field so the editor can point at the row the operator has to fix."""

    def __init__(self, role: str, field: str, detail: str) -> None:
        super().__init__(f"role {role!r} field {field!r}: {detail}")
        self.role = role
        self.field = field
        self.detail = detail


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
            "Templates & settings → Model profiles first"
        )
        self.name = name


@dataclass(frozen=True)
class RoleBinding:
    """One role's concrete pair. Neither field is ever null: a profile that
    cannot say which model and how much reasoning is not a profile."""

    model: str
    thinking: str


@dataclass(frozen=True)
class ModelProfile:
    name: str
    roles: dict[str, RoleBinding]
    created_at: str
    updated_at: str


def validate_profile_name(name: str) -> None:
    if not _SLUG_RE.match(name):
        raise InvalidModelProfileNameError(name)


def validate_model_identifier(role: str, model: object) -> str:
    """Return the trimmed provider-qualified identifier, or refuse it.

    Structural only. The split is at the *first* slash — later slashes are
    part of the model id, which is how nested provider catalogs name models.
    """
    if not isinstance(model, str):
        raise InvalidRoleBindingError(role, "model", "must be a string")
    value = model.strip()
    if not value:
        raise InvalidRoleBindingError(
            role, "model", "required; use a provider-qualified id such as 'openai/o3'"
        )
    if any(char.isspace() for char in value):
        raise InvalidRoleBindingError(
            role, "model", "must not contain whitespace inside the identifier"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise InvalidRoleBindingError(role, "model", "must not contain control characters")
    for char in _REJECTED_CHARS:
        if char in value:
            raise InvalidRoleBindingError(
                role, "model", f"must not contain {char!r}"
            )
    if "://" in value:
        raise InvalidRoleBindingError(
            role, "model", "must be a model identifier, not a URL"
        )
    provider, slash, model_id = value.partition("/")
    if not slash:
        raise InvalidRoleBindingError(
            role,
            "model",
            f"must be provider-qualified as 'provider/model-id' (got {value!r}); "
            "a bare model name is not a profile binding",
        )
    if not provider:
        raise InvalidRoleBindingError(role, "model", "provider segment is empty")
    if not model_id:
        raise InvalidRoleBindingError(role, "model", "model-id segment is empty")
    if not _PROVIDER_RE.match(provider):
        raise InvalidRoleBindingError(
            role,
            "model",
            f"provider {provider!r} must be letters, digits, dots, underscores "
            "or hyphens, starting with a letter or digit",
        )
    # `openai/o3:high` is the native argv encoding, not a model id. Taking it
    # would hide a second thinking level inside the model field and let the two
    # disagree; other suffixes (`:free`, dated ids) are the model's own and stay.
    suffix = model_id.rpartition(":")[2]
    if ":" in model_id and suffix in THINKING_LEVELS:
        raise InvalidRoleBindingError(
            role,
            "model",
            f"remove the trailing {':' + suffix!r} and set the thinking level "
            "in this role's thinking field instead",
        )
    return value


def validate_role_thinking(role: str, thinking: object) -> str:
    if not isinstance(thinking, str):
        raise InvalidRoleBindingError(role, "thinking", "must be a string")
    if not thinking:
        raise InvalidRoleBindingError(
            role, "thinking", f"required; must be one of {', '.join(THINKING_LEVELS)}"
        )
    try:
        validate_thinking(thinking)
    except InvalidThinkingLevelError as exc:
        raise InvalidRoleBindingError(role, "thinking", str(exc)) from exc
    return thinking


def validate_roles(roles: Mapping[str, object]) -> dict[str, RoleBinding]:
    """Validate the whole role map, or raise. Nothing partial is returned:
    an update commits four good bindings or none of them."""
    missing = [role for role in MODEL_ROLES if role not in roles]
    unknown = sorted(role for role in roles if role not in MODEL_ROLES)
    if missing or unknown:
        raise InvalidRoleSetError(missing, unknown)
    validated: dict[str, RoleBinding] = {}
    for role in MODEL_ROLES:
        binding = roles[role]
        if isinstance(binding, RoleBinding):
            model, thinking = binding.model, binding.thinking
        elif isinstance(binding, Mapping):
            extra = sorted(set(binding) - {"model", "thinking"})
            if extra:
                raise InvalidRoleBindingError(
                    role, "roles", f"unknown binding fields: {', '.join(extra)}"
                )
            model = binding.get("model")  # type: ignore[assignment]
            thinking = binding.get("thinking")  # type: ignore[assignment]
        else:
            raise InvalidRoleBindingError(
                role, "roles", "must be an object with 'model' and 'thinking'"
            )
        validated[role] = RoleBinding(
            model=validate_model_identifier(role, model),
            thinking=validate_role_thinking(role, thinking),
        )
    return validated


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


@contextlib.contextmanager
def reserved_write(engine: Engine) -> Iterator[Connection]:
    """Open a SQLite write reservation, then commit or roll back.

    `BEGIN IMMEDIATE` takes the database's write lock up front, so a read made
    inside this block cannot go stale before the matching write commits. That
    is what keeps profile deletion and project assignment from racing into a
    project whose default names a profile that no longer exists: whichever
    transaction starts first finishes first, and the other sees its result.

    A plain `engine.begin()` is not enough — pysqlite defers `BEGIN` until the
    first DML statement, so a preflight `SELECT` would run outside the
    reservation. The driver's implicit transaction handling is switched off for
    the duration and restored before the connection returns to the pool.

    Deliberately narrow: only the reference check and its write belong inside.
    Filesystem work, git, setup scheduling, and event publication stay outside,
    and this does not turn on SQLite foreign-key enforcement for other tables.
    """
    with engine.connect() as conn:
        dbapi = conn.connection.dbapi_connection
        assert dbapi is not None  # a live Connection always has one
        prior = dbapi.isolation_level
        dbapi.isolation_level = None
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.exec_driver_sql("ROLLBACK")
                raise
            else:
                conn.exec_driver_sql("COMMIT")
        finally:
            dbapi.isolation_level = prior


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
