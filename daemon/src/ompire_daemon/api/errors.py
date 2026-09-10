"""Shared error mapping for the work routers.

Domain errors from the work and application layers become HTTP responses
here — the one place transport decides what a refusal looks like. The
classifications are the published API contract and must not drift from the
pre-extraction routes.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from ompire_daemon.handoff import HandoffError
from ompire_daemon.oversight.tasks import resolution_payload
from ompire_daemon.work.launch import (
    LaunchInputError,
    PreviewChangedError,
    ProjectNotLaunchableError,
)


def launch_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LaunchInputError):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{exc.field}: {exc.detail}"
        )
    if isinstance(exc, ProjectNotLaunchableError):
        return HTTPException(status.HTTP_409_CONFLICT, exc.detail)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def preview_changed(exc: PreviewChangedError) -> HTTPException:
    """409 with a machine-readable reason and the current resolution, so the
    form can show the operator exactly what changed instead of retrying under
    settings they never reviewed."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "reason": exc.reason,
            "message": str(exc),
            "preview": (
                resolution_payload(exc.resolved) if exc.resolved is not None else None
            ),
        },
    )


def attachment_observation_error(exc: HandoffError) -> HTTPException:
    """A Git reading for an attachment launch refused the launch (ADR-0035)."""
    return HTTPException(
        status.HTTP_409_CONFLICT, f"result_attachments: {exc.detail}"
    )
