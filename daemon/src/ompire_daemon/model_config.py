"""Model-value vocabulary shared by every consumer of omp model settings.

The thinking vocabulary is the one piece templates, per-spawn overrides, and
global model profiles all agree on, so it lives here rather than inside any
one registry. What each consumer *permits* still differs: a template or spawn
override may leave thinking unset (omp's own default), while a model profile
role binding requires an explicit level.

Model identifiers are deliberately *not* validated here. Templates accept
omp's fuzzy names; model profiles require a provider-qualified identifier and
own that stricter grammar next to the rest of their value validation.
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
