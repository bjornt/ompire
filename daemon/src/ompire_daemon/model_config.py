"""Model-value vocabulary shared by every consumer of omp model settings.

The thinking vocabulary and the abstract role names are what the profile
registry, launch resolution, and the pinned task inputs all agree on, so they
live here rather than inside any one of them. A profile role binding requires
an explicit thinking level: since the template retirement (ADR-0026) there is
no consumer left that may leave thinking unset and fall back to omp's own
default.

Model identifiers are deliberately *not* validated here. Model profiles
require a provider-qualified identifier and own that stricter grammar next to
the rest of their value validation.
"""

from __future__ import annotations

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
