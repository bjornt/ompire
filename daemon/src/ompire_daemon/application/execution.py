"""The workflow command-execution adapter.

Application wiring between two owners that must not import each other: the
workflow engine declares a consumer-owned contract (`CommandExecutor`,
`CommandOutcome`, `CommandExecutionError` in `workflows.py`), and this adapter
implements it over the isolation boundary's public sandbox execution. The
engine gets its exit-code-and-output evidence without learning container
transport; isolation runs the command without learning what a workflow is
(ADR-0039).

Failure translation is the adapter's whole job apart from forwarding: a
resource-boundary failure to start the command or its deadline expiring
becomes the engine's one public execution error, which `_run_command_step`
already maps to its existing infrastructure-failure path. A completed
nonzero exit crosses untouched, as data.
"""

from __future__ import annotations

from collections.abc import Sequence

from ompire_daemon.isolation import SandboxCommandError, run_sandbox_command
from ompire_daemon.workflows import CommandExecutionError, CommandOutcome


class SandboxCommandExecutor:
    """Executes workflow commands through the isolation boundary.

    Stateless: the workspace path, argv, deadline, and output-tail bound all
    arrive per call, exactly as the engine's contract specifies. There is no
    host-execution fallback and no second default path.
    """

    async def __call__(
        self,
        clone_path: str,
        argv: list[str],
        *,
        timeout: float,
        output_tail: int,
    ) -> CommandOutcome:
        try:
            result = await run_sandbox_command(
                clone_path,
                _literal(argv),
                timeout=timeout,
                output_tail=output_tail,
            )
        except SandboxCommandError as exc:
            raise CommandExecutionError(str(exc)) from exc
        return CommandOutcome(exit_code=result.exit_code, output=result.output)


def _literal(argv: Sequence[str]) -> list[str]:
    """A mutable literal copy: the boundary owns what it executes end to end."""
    return list(argv)
