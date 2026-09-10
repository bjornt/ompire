"""Model-value vocabulary shared by every consumer of omp model settings.

The thinking vocabulary, the abstract role names, and the pure role-binding
value (`RoleBinding` and its validation) are what the profile registry,
launch resolution, and the pinned task inputs all agree on, so they live here
rather than inside any one of them. A profile role binding requires an
explicit thinking level: since the template retirement (ADR-0026) there is
no consumer left that may leave thinking unset and fall back to omp's own
default.

Model identifiers are validated here only as structural, provider-qualified
grammar. Nothing in this module calls a provider or model endpoint, and no
persistence or profile-identity concern (names, lookup, reference guards)
belongs here — those stay with the profile registry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# omp's `--thinking` vocabulary, verified against omp v17.2.12 (`omp --help`)
# and re-confirmed against the installed omp v18.1.10 role-flag probes.
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")


class InvalidThinkingLevelError(ValueError):
    def __init__(self, thinking: str) -> None:
        super().__init__(
            f"invalid thinking level {thinking!r}: must be one of {', '.join(THINKING_LEVELS)}"
        )
        self.thinking = thinking


def validate_thinking(thinking: str) -> None:
    if thinking not in THINKING_LEVELS:
        raise InvalidThinkingLevelError(thinking)


# The four abstract model roles, in presentation order. A model profile binds
# exactly these; a workflow's agent step names one of them. The vocabulary
# lives here because profiles, workflow definitions, and the native argv
# builder all have to agree on it without importing each other.
MODEL_ROLES = ("default", "smol", "slow", "plan")

# The role the engine-reserved LLM judge consumes (ADR-0026). It is a fixed
# binding of the task's profile, not a separate configurable model.
JUDGE_ROLE = "slow"


class InvalidModelRoleError(ValueError):
    def __init__(self, role: str) -> None:
        super().__init__(
            f"invalid model role {role!r}: must be one of {', '.join(MODEL_ROLES)}"
        )
        self.role = role


def validate_model_role(role: str) -> None:
    if role not in MODEL_ROLES:
        raise InvalidModelRoleError(role)


# The provider segment before the first slash: letters, digits, dots,
# underscores and hyphens, opening with a letter or digit.
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Characters that would make an identifier a path, a URL, a glob, or a shell
# argument rather than a model name.
_REJECTED_CHARS = ("\\", "*", "?", "#")


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


@dataclass(frozen=True)
class RoleBinding:
    """One role's concrete pair. Neither field is ever null: a profile that
    cannot say which model and how much reasoning is not a profile."""

    model: str
    thinking: str


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
