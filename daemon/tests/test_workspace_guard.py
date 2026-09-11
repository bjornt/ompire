"""The workspace writer guard's contention contract.

Focused coverage for the mechanism that moved behind the isolation boundary:
cleanup's reservation versus every other writer, in both directions, and the
execution-context inheritance that lets a holder's own nested calls in while
an unrelated context is still refused (ADR-0032, ADR-0039).
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from ompire_daemon.isolation import (
    WorkspaceBlockedError,
    WorkspaceBusyError,
    WorkspaceGuard,
)

OWNER = 4242  # arbitrary: the guard never consults a task registry


@pytest.mark.asyncio
async def test_cleanup_reservation_refuses_a_new_capture_and_vice_versa() -> None:
    """A started cleanup refuses a new writer from any other context, and a
    busy host writer refuses cleanup — the exclusion holds in both orders.
    The holder's own nested calls keep their inherited admission."""
    guard = WorkspaceGuard()

    async with guard.cleanup_hold(OWNER, "cleanup"):
        guard.assert_available(OWNER)  # nested: already the owner
        fresh = contextvars.Context()
        with pytest.raises(WorkspaceBusyError):
            fresh.run(guard.assert_available, OWNER)
        with pytest.raises(WorkspaceBusyError):
            fresh.run(guard.acquire, OWNER, "capture")

    # Free after teardown: the reservation is not a leak.
    guard.assert_available(OWNER)

    guard.acquire(OWNER, "review", kind=WorkspaceGuard.HOST)
    with pytest.raises(WorkspaceBusyError):
        async with guard.cleanup_hold(OWNER, "cleanup"):
            pass
    guard.release(OWNER, "review")


@pytest.mark.asyncio
async def test_cleanup_displaces_only_agent_work() -> None:
    """Cleanup legitimately abandons the task's own agent: it admits over an
    agent hold and takes ownership. A blocked workspace still refuses it."""
    guard = WorkspaceGuard()

    async with (
        guard.hold(OWNER, "workflow-step", kind=WorkspaceGuard.AGENT),
        guard.cleanup_hold(OWNER, "cleanup"),
    ):
        assert guard.owner(OWNER) == "cleanup"
        assert guard.owner_kind(OWNER) == WorkspaceGuard.HOST

    guard.block(OWNER, "an unresolved delivery effect")
    with pytest.raises(WorkspaceBlockedError):
        async with guard.cleanup_hold(OWNER, "cleanup"):
            pass
    guard.unblock(OWNER)


@pytest.mark.asyncio
async def test_cleanup_reservation_releases_on_failure() -> None:
    """An aborted cleanup must not leave the task permanently unwritable."""
    guard = WorkspaceGuard()

    with pytest.raises(RuntimeError):
        async with guard.cleanup_hold(OWNER, "cleanup"):
            raise RuntimeError("teardown exploded")

    guard.assert_available(OWNER)
    assert guard.owner(OWNER) is None


@pytest.mark.asyncio
async def test_nested_calls_inherit_admission_but_unrelated_contexts_do_not() -> None:
    """A holder's nested daemon calls pass their own admission, including a
    background job started inside the hold; an unrelated execution context is
    refused even while the hold is live. A nested hold keeps the original
    ownership kind rather than upgrading an agent hold."""
    guard = WorkspaceGuard()

    async with guard.hold(OWNER, "workflow-step", kind=WorkspaceGuard.AGENT):
        guard.assert_available(OWNER)  # nested: already the owner
        nested_kind_observed: list[str] = []

        async def child_job() -> None:
            guard.assert_available(OWNER)
            nested_kind_observed.append(guard.owner_kind(OWNER) or "")

        await asyncio.create_task(child_job())
        assert nested_kind_observed == [WorkspaceGuard.AGENT]

        # An unrelated execution context cannot gain admission by choosing
        # the same owner: a fresh context sees no hold and is refused.
        fresh = contextvars.Context()
        with pytest.raises(WorkspaceBusyError):
            fresh.run(guard.assert_available, OWNER)

    # Released at the boundary: a new writer is admitted again.
    guard.assert_available(OWNER)
