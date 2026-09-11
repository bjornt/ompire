"""Daemon-run host-side delivery: draft → sign → push → pull request, with the
operator choosing how far it goes.

Architecture: ADR-0011
(docs/adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md);
content binding and reconciliation: ADR-0032
(docs/adr/0032-bind-trusted-delivery-to-retained-candidates.md)

Three independently admitted trusted operations live here — a local signed
commit, a push to the task's accepted destination, and a pull request — plus a
small coordinator that runs only the prefix the operator's selected ending
authorizes. There is no second publisher: the Ship flow calls these same
services, and so does any other authenticated caller.

Nothing here is transient any more. The authorization, the candidate it names,
every action attempt with the exact refs it intended to write, and every
reconciliation decision are rows behind `registry/ships.py`. That is what makes
"the response was lost" answerable: a restart reads what was attempted and goes
looking for that specific result, instead of choosing between assuming success
and signing again.

Signing happens against the protected candidate in its own repository, never
against a freshly staged live workspace. The signed result is then installed
into the task clone under a compare-and-swap against the HEAD the candidate was
captured at, so a workspace that moved on blocks the install rather than
silently absorbing or discarding new work.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.delivery import (
    DeliveryWorkspaceError,
    EmptyCandidateError,
    ProtectedPathError,
    assert_range_unprotected,
    assert_tree_unprotected,
    candidate_identity,
    capture_candidate,
    protected_destinations,
    remove_candidate_storage,
    store_has_objects,
    workspace_tree_id,
)
from ompire_daemon.events import EventHub
from ompire_daemon.gh import (
    GitHubProbe,
    GitHubStatus,
    GitHubTargetStatus,
    parse_github_owner,
)
from ompire_daemon.gpg import STATE_READY, gpg_signing_refusal
from ompire_daemon.isolation import (
    WorkspaceBlockedError,
    WorkspaceBusyError,
    WorkspaceGuard,
)
from ompire_daemon.oversight.tasks import task_payload
from ompire_daemon.platform.git import GitCommandError, git_out, run_git, safe_git
from ompire_daemon.registry.reviews import get_review
from ompire_daemon.registry.ships import (
    ENDING_ACTIONS,
    ActionRecord,
    CandidateRecord,
    DeliveryConflictError,
    DeliveryRecord,
    append_decision,
    authorize_delivery,
    clear_candidate_storage,
    complete_action,
    extend_delivery,
    fail_action,
    flag_action_unresolved,
    get_action,
    get_candidate,
    get_delivery,
    get_latest_delivery,
    list_deliveries,
    list_task_candidates,
    list_unresolved_deliveries,
    mark_action_executing,
    open_delivery,
    prepare_action,
    reauthorize_delivery,
    record_action_progress,
    resolve_action,
    resume_delivery,
    save_draft,
    set_disposition,
    task_version,
)
from ompire_daemon.registry.workflows import DeliveryAuthorization, latest_step_record
from ompire_daemon.runauthority import (
    SOURCE_LEGACY_CONTINUATION,
    SOURCE_WORKFLOW_ACTION,
    SOURCE_WORKFLOW_GATE,
    RunAuthority,
    resolve_authority,
)
from ompire_daemon.sessions import wait_for_idle
from ompire_daemon.work.inputs import TaskExecutionInputs
from ompire_daemon.work.tasks import (
    Task,
    get_task,
    list_tasks,
    mark_pr_url,
    require_task_inputs,
)
from ompire_daemon.workflow_definitions import DeliveryStep

if TYPE_CHECKING:
    from ompire_daemon.agent import AgentSupervisor
    from ompire_daemon.gpg import GpgProbe, GpgStatus
    from ompire_daemon.sessions import SessionTracker

logger = logging.getLogger(__name__)

# The superseded reset-dance marker. New deliveries never write it — signing
# happens outside the clone — but a clone parked by an older daemon still has
# to be recognized and restored.
_SHIP_GIT_REF = "refs/ompire/ship-orig"

# The per-task clone is agent-writable, so nothing it says about signing is
# trusted (ADR-0011: identity selection stays in the control plane). A
# clone-local `gpg.program` is the sharpest edge: `git commit -S` would
# execute it on the host, outside the sandbox, as the operator.
_SIGNING_FORMAT = "openpgp"
_DEFAULT_SIGNING_PROGRAM = "gpg"

_PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")

# Field separator for the verification format. Git emits it from `%x1f`, and a
# space-separated format could not tell an empty parent list from a missing
# signer.
_FIELD = "\x1f"
# Git's own escape for it: an argument vector carries the escape, not the byte.
_FIELD_ESC = "%x1f"

# The marker Ompire adds to a pull-request body, and looks for when a create
# call's response was lost. It is shown in the preview, so the body the
# operator authorizes is the body that gets written.
_MARKER_PREFIX = "ompire-delivery"
_MARKER_RE = re.compile(rf"{_MARKER_PREFIX}:\s*([0-9a-f]{{8,64}})")

# How many pull requests a correlated lookup will page through before it
# reports the search as incomplete rather than as "not found".
_PR_LOOKUP_LIMIT = 100

_DRAFT_PROMPT = """You are helping the operator ship this task.

Write the publication text for the work in this workspace. Reply with exactly
these three marker-delimited sections and nothing else:

<<<COMMIT_MESSAGE>>>
a conventional-commit subject line, then a blank line, then the body
<<<PR_TITLE>>>
one line
<<<PR_BODY>>>
a short markdown summary of what changed and why
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ShipDraft:
    commit_message: str
    pr_title: str
    pr_body: str
    source: str = "agent"


# --- errors ----------------------------------------------------------------


class ShipError(Exception):
    """Base for delivery errors surfaced as delivery outcomes."""


class GpgNotReadyError(ShipError):
    """Signing is refused. Carries the state so callers can explain which one."""

    def __init__(self, status: GpgStatus) -> None:
        super().__init__(gpg_signing_refusal(status))
        self.status = status


class GitHubPreflightError(ShipError):
    """A safe, structured refusal before a delivery can mutate anything."""

    def __init__(self, status: GitHubStatus, target: GitHubTargetStatus) -> None:
        self.status = status
        self.target = target
        identity = status.identity
        if identity.state != "ready":
            source = (
                f" via {identity.credential_source}"
                if identity.credential_source
                else ""
            )
            detail = f": {identity.detail}" if identity.detail else ""
            super().__init__(
                f"GitHub preflight blocked shipping: GitHub CLI is {identity.state} "
                f"for {identity.host}{source}{detail}"
            )
            return

        account = (
            f"@{identity.login}" if identity.login else "the selected GitHub account"
        )
        target_name = (
            target.target.canonical
            if target.target is not None
            else "the registered upstream"
        )
        detail = f": {target.detail}" if target.detail else ""
        super().__init__(
            f"GitHub preflight blocked shipping: {account} cannot use {target_name} "
            f"({target.state}){detail}"
        )


class PushError(ShipError):
    """A Git transport failure while pushing an authorized signed result."""


class SshAuthenticationError(PushError):
    """The narrow SSH public-key authentication subset of a push failure."""


class PushConflictError(PushError):
    """The destination ref does not hold what the authorization leased."""


class PullRequestError(ShipError):
    """A sanitized pull-request preflight or creation failure."""


class NoLiveAgentError(ShipError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} has no live agent")
        self.task_id = task_id


class SessionNotIdleError(ShipError):
    def __init__(self, task_id: int, session: str, current_status: str | None) -> None:
        current = current_status or "untracked"
        super().__init__(
            f"task {task_id} session {session!r} is not idle (current status: {current})"
        )
        self.task_id = task_id
        self.session = session
        self.current_status = current_status


class DraftNotDeclaredError(ShipError):
    """This workflow does not draft publication text through an agent turn.

    Not a failure to draft: a refusal to start a turn nobody declared. The
    definition's own gate metadata supplies the starting text, and the
    operator edits it.
    """

    def __init__(self, task_id: int) -> None:
        super().__init__(
            f"task {task_id} runs a workflow that declares its own publication "
            "text; edit the fields directly rather than asking an agent for a "
            "draft"
        )
        self.task_id = task_id


class ShipAlreadyPublishedError(ShipError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} is already published or archived")
        self.task_id = task_id


class ShipInProgressError(ShipError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} already has a delivery action running")
        self.task_id = task_id


class DeliveryBlockedError(ShipError):
    """Admission refused. `blockers` names every reason, not just the first."""

    def __init__(self, blockers: list[Blocker]) -> None:
        super().__init__("; ".join(b.message for b in blockers) or "delivery is blocked")
        self.blockers = blockers


class PreviewMismatchError(ShipError):
    """The confirmation does not match anything Ompire offered."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnresolvedEffectError(ShipError):
    """A previous effect's outcome is unknown; nothing dependent may run."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# --- preview ---------------------------------------------------------------


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str


@dataclass
class DeliveryPreview:
    """A read-only resolution of one requested ending. Authorizes nothing."""

    task_id: int
    delivery_id: int
    version: int
    ending: str
    mode: str
    request_id: str
    # How this delivery would be authorized: an unanswered workflow gate, an
    # already-authorized action the run is at, or an authorization made before
    # workflows owned publication. Never absent — a preview with no source is
    # refused rather than shown.
    source: str
    # The exact question and answer, when a gate is what would authorize it.
    gate_seq: int | None
    choice_id: str | None
    # The review attempt the grant is bound to, and its recorded verdict.
    review_seq: int | None
    candidate_id: str | None
    candidate: dict[str, Any] | None
    review: dict[str, Any]
    completed_actions: list[str]
    remaining_actions: list[str]
    routing: dict[str, Any]
    identity: dict[str, Any]
    commit_message: str
    pr_title: str
    pr_body: str
    marker: str
    blockers: list[Blocker]
    fingerprint: str

    @property
    def deliverable(self) -> bool:
        return not self.blockers

    def payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "delivery_id": self.delivery_id,
            "version": self.version,
            "ending": self.ending,
            "mode": self.mode,
            "request_id": self.request_id,
            "source": self.source,
            "gate_seq": self.gate_seq,
            "choice_id": self.choice_id,
            "review_seq": self.review_seq,
            "actions": list(ENDING_ACTIONS[self.ending]),
            "candidate_id": self.candidate_id,
            "candidate": self.candidate,
            "review": self.review,
            "completed_actions": self.completed_actions,
            "remaining_actions": self.remaining_actions,
            "routing": self.routing,
            "identity": self.identity,
            "commit_message": self.commit_message,
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
            "marker": self.marker,
            "blockers": [asdict(b) for b in self.blockers],
            "deliverable": self.deliverable,
            "preview_token": self.fingerprint,
        }


def correlation_marker(task_id: int, request_id: str) -> str:
    """The delivery/action correlation marker, derived before the preview is
    fingerprinted so the body shown is the body authorized."""
    digest = hashlib.sha256(f"{task_id}:{request_id}".encode()).hexdigest()
    return digest[:32]


def body_with_marker(body: str, marker: str) -> str:
    """The exact pull-request body Ompire will write."""
    return f"{body.rstrip()}\n\n<!-- {_MARKER_PREFIX}: {marker} -->\n"


# How a workflow-owned action lands its result: the runner supplies this so
# the journal write and the run's own step transition are one commit.
Settle = Callable[[int, dict[str, Any], dict[str, Any] | None, str | None], None]


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


# --- manager ---------------------------------------------------------------


class ShipManager:
    def __init__(
        self,
        config: Config,
        engine: Engine,
        hub: EventHub,
        sessions: SessionTracker,
        agents: AgentSupervisor,
        gpg: GpgProbe,
        gh: GitHubProbe,
        guard: WorkspaceGuard,
    ) -> None:
        self._config = config
        self._engine = engine
        self._hub = hub
        self._sessions = sessions
        self._agents = agents
        self._gpg = gpg
        self._gh = gh
        self._guard = guard
        self._backgrounds: dict[int, asyncio.Task] = {}

    # --- projection --------------------------------------------------------

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Every task's durable delivery projection, for the WebSocket snapshot.

        A task appears once it has a delivery record or a recorded publication.
        A legacy `pr_url` with no delivery rows is a known fact and is shown as
        one, without inventing the authorization that produced it.
        """
        payload: dict[int, dict[str, Any]] = {}
        for task in list_tasks(self._engine):
            projection = self.projection(task)
            if projection is not None:
                payload[task.id] = projection
        return payload

    def empty_projection(self, task_id: int) -> dict[str, Any]:
        """The shape a task with no delivery answers with.

        A read surface that returned nothing would make every caller special-case
        "not started yet"; this says it in the same vocabulary as every other
        state, at version 0.
        """
        return {
            "task_id": task_id,
            "version": 0,
            "delivery_id": None,
            "disposition": None,
            "ending": None,
            "mode": None,
            "candidate_id": None,
            "review_candidate_id": None,
            "draft": None,
            "blocked_reason": None,
            "workspace_owner": self._guard.owner(task_id),
            "completed_actions": [],
            "remaining_actions": [],
            "results": {},
            "pr_url": None,
            "legacy_publication": False,
            "authority": {
                "format": None,
                "source": None,
                "declared_actions": [],
                "declares_review": False,
                "gate_seq": None,
                "gate_step": None,
                "review_seq": None,
                "review_outcome": None,
                "choices": [],
                "suggested": {},
                "action_seq": None,
                "action_step": None,
                "action_kind": None,
                "awaiting_continuation": False,
                "review_step_seq": None,
                "refusal_code": None,
                "refusal": None,
            },
            "actions": [],
            "decisions": [],
            "history": [],
        }

    def authority_payload(self, task: Task) -> dict[str, Any]:
        """What this run's own procedure currently permits, for every reader.

        The same resolution the service admits against, projected. That is the
        point: a page cannot offer a button the service would refuse, or hide
        one it would accept, because both are reading the same answer.
        """
        authority = resolve_authority(self._engine, task)
        refusal = authority.refusal()
        approval = authority.approval
        action = authority.action
        return {
            "format": authority.format,
            "source": authority.source,
            "declared_actions": list(authority.declared_actions),
            "declares_review": authority.declares_review,
            "gate_seq": approval.seq if approval is not None else None,
            "gate_step": approval.step.name if approval is not None else None,
            "review_seq": (
                approval.review_step_seq if approval is not None else None
            ),
            "review_outcome": (
                approval.review.outcome
                if approval is not None and approval.review is not None
                else None
            ),
            "choices": (
                [
                    {
                        "id": choice.id,
                        "label": choice.label,
                        "feedback_required": choice.feedback_required,
                        "authorizes": (
                            None
                            if choice.authorize is None
                            else list(choice.authorize.steps)
                        ),
                    }
                    for choice in approval.step.choices
                ]
                if approval is not None
                else []
            ),
            "suggested": (
                (approval.snapshot.get("delivery") or {}).get("suggested") or {}
                if approval is not None
                else {}
            ),
            "action_seq": action.seq if action is not None else None,
            "action_step": action.step.name if action is not None else None,
            "action_kind": action.step.action if action is not None else None,
            "awaiting_continuation": (
                action.awaiting_continuation if action is not None else False
            ),
            "review_step_seq": authority.review_seq,
            "refusal_code": refusal[0] if refusal is not None else None,
            "refusal": refusal[1] if refusal is not None else None,
        }

    def projection(self, task: Task) -> dict[str, Any] | None:
        deliveries = list_deliveries(self._engine, task.id)
        if not deliveries and task.pr_url is None:
            # A task with no delivery record still has an *authority*: the
            # question its run is at, or the reason there is none. Without it
            # a page would have to guess whether an approval exists.
            authority = self.authority_payload(task)
            if authority["source"] is None:
                return None
            return {**self.empty_projection(task.id), "authority": authority}
        current = deliveries[-1] if deliveries else None
        results = self._results(current) if current is not None else {}
        pr_url = (results.get("pr") or {}).get("url") or task.pr_url
        return {
            "task_id": task.id,
            "version": current.version if current is not None else 0,
            "delivery_id": current.id if current is not None else None,
            "disposition": current.disposition if current is not None else None,
            "ending": current.ending if current is not None else None,
            "mode": current.mode if current is not None else None,
            "candidate_id": current.candidate_id if current is not None else None,
            "review_candidate_id": (
                current.review_candidate_id if current is not None else None
            ),
            "draft": current.draft if current is not None else None,
            "blocked_reason": current.blocked_reason if current is not None else None,
            "workspace_owner": self._guard.owner(task.id),
            "authority": self.authority_payload(task),
            "completed_actions": (
                [k for k in ("commit", "push", "pr") if current.succeeded(k)]
                if current is not None
                else []
            ),
            "remaining_actions": (
                list(current.remaining_actions) if current is not None else []
            ),
            "results": results,
            "pr_url": pr_url,
            "legacy_publication": bool(task.pr_url) and not results.get("pr"),
            "actions": [
                self._action_payload(action)
                for action in (current.actions if current is not None else [])
            ],
            "decisions": [
                {
                    "kind": d.kind,
                    "action_id": d.action_id,
                    "detail": d.detail,
                    "note": d.note,
                    "decided_at": d.decided_at,
                }
                for d in (current.decisions if current is not None else [])
            ],
            "history": [
                {
                    "delivery_id": record.id,
                    "ending": record.ending,
                    "mode": record.mode,
                    "disposition": record.disposition,
                    "candidate_id": record.candidate_id,
                    "authorized_at": record.authorized_at,
                    "authorized_by": record.authorized_by,
                    "results": self._results(record),
                    "updated_at": record.updated_at,
                }
                for record in deliveries
            ],
        }

    @staticmethod
    def _action_payload(action) -> dict[str, Any]:
        return {
            "id": action.id,
            "kind": action.kind,
            "attempt": action.attempt,
            "phase": action.phase,
            "expected": action.expected,
            "progress": action.progress,
            "identity": action.identity,
            "result": action.result,
            "error": action.error,
            "updated_at": action.updated_at,
        }

    @staticmethod
    def _results(delivery: DeliveryRecord) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for kind in ("commit", "push", "pr"):
            action = delivery.succeeded(kind)
            if action is not None:
                results[kind] = action.result
        return results

    def publish(self, task: Task) -> dict[str, Any] | None:
        """Broadcast the task's committed delivery projection.

        Published *after* the write commits and always as the whole versioned
        projection, so a client that misses one update or receives two out of
        order still converges on what the daemon actually stored.
        """
        projection = self.projection(task)
        if projection is not None:
            self._hub.publish("ship_updated", projection)
        return projection

    def _refresh(self, task_id: int) -> dict[str, Any] | None:
        try:
            task = get_task(self._engine, task_id)
        except Exception:  # noqa: BLE001 — purged mid-flight
            return None
        return self.publish(task)

    # --- lifecycle ---------------------------------------------------------

    def drop_ship(self, task_id: int) -> None:
        """Drop transient delivery state (cleanup/purge path).

        The journal is deliberately retained: a cleaned-up task keeps the record
        of what it published and under whose authorization. Only purge deletes
        it, and only purge removes the candidate staging repositories.
        """
        job = self._backgrounds.pop(task_id, None)
        if job is not None:
            job.cancel()

    async def cancel_and_drop(self, task_id: int) -> None:
        job = self._backgrounds.get(task_id)
        if job is not None and not job.done():
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job
        self.drop_ship(task_id)

    def purge_candidate_storage(self, paths: list[str]) -> None:
        for path in paths:
            remove_candidate_storage(path)

    def release_candidate_storage(self, task_id: int) -> None:
        """Remove staging repositories no active or unresolved work still needs.

        A terminal delivery's evidence has served its purpose; an unresolved one
        keeps its objects, because they may be the only proof of what a
        signature covers.
        """
        deliveries = list_deliveries(self._engine, task_id)
        keep: set[str] = set()
        for record in deliveries:
            if record.candidate_id and record.disposition in (
                "open",
                "authorized",
                "blocked",
                "unresolved",
            ):
                keep.add(record.candidate_id)
        for candidate in list_task_candidates(self._engine, task_id):
            if candidate.candidate_id in keep or candidate.storage_path is None:
                continue
            remove_candidate_storage(candidate.storage_path)
            clear_candidate_storage(self._engine, candidate.candidate_id)

    def unresolved_reason(self, task_id: int) -> str | None:
        """Why this task cannot be cleaned up or written to, if it cannot."""
        return self._guard.blocked_reason(task_id)

    # --- routing and probes ------------------------------------------------

    def _routing(self, task: Task) -> TaskExecutionInputs:
        """Where this task publishes, as accepted (ADR-0026).

        Upstream and fork URLs are read off the task rather than the project
        row: editing a project must not silently redirect a pull request for
        work that was already approved against a different target. The
        credential and identity checks around this are unchanged and still
        live (ADR-0011/0017) — only the destination is pinned."""
        return require_task_inputs(task)

    def _base_branch(self, task: Task) -> str:
        """The base branch the task was accepted with (ADR-0026).

        There is no `main` fallback. A task without confirmed inputs is refused
        by the readiness guard before any delivery step runs, because signing
        against a guessed base is a publishing action taken on invented
        information."""
        return require_task_inputs(task).workspace.base_branch

    def _destination(self, task: Task) -> dict[str, Any]:
        routing = self._routing(task)
        branch = task.branch
        if routing.fork_url:
            remote_url = routing.fork_url
            head = f"{parse_github_owner(routing.fork_url)}:{branch}"
        else:
            # Task clones are hardlink-cloned from the local checkout, so their
            # `origin` is a *local path* — pushing to `origin` never reaches
            # GitHub (found via dogfooding). Push to the upstream URL instead.
            remote_url = routing.upstream_url
            head = branch
        return {
            "remote_url": remote_url,
            "branch": branch,
            "ref": f"refs/heads/{branch}",
            "head": head,
            "base_branch": routing.workspace.base_branch,
            "upstream_url": routing.upstream_url,
        }

    async def preflight(self, task: Task) -> GitHubStatus:
        """Freshly prove the trusted task upstream is safe before shipping."""
        status, _target = await self._preflight_target(task)
        return status

    async def _preflight_target(
        self, task: Task
    ) -> tuple[GitHubStatus, GitHubTargetStatus]:
        routing = self._routing(task)
        status, target = await self._gh.probe_target(routing.upstream_url)
        if status.identity.state != "ready" or target.state != "allowed":
            raise GitHubPreflightError(status, target)
        return status, target

    # --- draft -------------------------------------------------------------

    async def draft(self, task: Task, *, replace: bool = False) -> dict[str, Any]:
        """Ask the primary session's live, idle agent for publication text.

        Only for a definition written before publication was declarable. A
        format-3 workflow gets its publication text from the gate's own
        declared metadata over frozen evidence, or from the operator typing
        it: a page that could start an agent turn during an approval wait
        would change the very content the approval is about, and would be a
        turn nobody declared. Editing the text by hand stays available in
        both, because that is inert.
        """
        from ompire_daemon.taskdefinition import task_primary_session

        if task.state == "archived":
            raise ShipAlreadyPublishedError(task.id)
        authority = resolve_authority(self._engine, task)
        if authority.declares_delivery:
            raise DraftNotDeclaredError(task.id)

        delivery = open_delivery(
            self._engine, task.id, workflow_revision=_task_revision(task)
        )
        existing = delivery.draft or {}
        if existing.get("state") == "drafting":
            raise ShipInProgressError(task.id)
        if not replace and existing.get("state") == "ready":
            projection = self.projection(task)
            assert projection is not None
            return projection
        if delivery.authorized_at is not None:
            raise ShipInProgressError(task.id)

        # The primary session *this task's pinned definition* declares
        # (ADR-0028), not what the workflow name resolves to today.
        primary = task_primary_session(self._engine, task)
        async with self._guard.hold(task.id, "ship-draft"):
            handle = await self._agents.acquire(task.id, primary)
            if handle is None:
                raise NoLiveAgentError(task.id)
            session = self._sessions.get(task.id, primary)
            if session is None or session.status != "idle":
                raise SessionNotIdleError(
                    task.id, primary, session.status if session is not None else None
                )

            save_draft(
                self._engine,
                delivery.id,
                {**existing, "state": "drafting", "error": None},
            )
            self._refresh(task.id)
            try:
                # Drafting continues the primary session's conversation on the
                # policy it last applied (ADR-0027).
                await handle.prompt(_DRAFT_PROMPT)
                await wait_for_idle(
                    self._hub, task.id, primary, timeout=self._config.spawn_step_timeout
                )
                try:
                    response = await handle.request("get_last_assistant_text")
                except Exception as exc:
                    raise ShipError(f"agent request failed: {exc}") from exc

                # Live omp wraps the text: {"data": {"text": ...}} — the same
                # shape advisories.py reads (found via dogfooding: reading data
                # as a bare string made every draft fail against real omp).
                data = response.get("data") if isinstance(response, dict) else None
                text = data.get("text") if isinstance(data, dict) else None
                if not isinstance(text, str):
                    raise ShipError("agent did not return text for draft")

                parsed = _parse_draft(text)
                if parsed is None:
                    raise ShipError("could not parse draft markers from agent reply")
                save_draft(
                    self._engine,
                    delivery.id,
                    {**asdict(parsed), "state": "ready", "error": None},
                )
            except TimeoutError:
                self._fail_draft(delivery.id, "timed out waiting for agent draft")
            except Exception as exc:  # noqa: BLE001
                self._fail_draft(delivery.id, f"draft failed: {exc}")
        projection = self.projection(task)
        assert projection is not None
        self._hub.publish("ship_updated", projection)
        return projection

    def _fail_draft(self, delivery_id: int, message: str) -> None:
        logger.warning("delivery draft failed for delivery %d: %s", delivery_id, message)
        record = get_delivery(self._engine, delivery_id)
        existing = (record.draft if record is not None else None) or {}
        save_draft(
            self._engine,
            delivery_id,
            {**existing, "state": "failed", "error": message},
        )

    def save_manual_draft(self, task: Task, draft: dict[str, Any]) -> dict[str, Any]:
        """Persist operator-entered publication text."""
        delivery = open_delivery(
            self._engine, task.id, workflow_revision=_task_revision(task)
        )
        save_draft(
            self._engine,
            delivery.id,
            {
                "commit_message": draft.get("commit_message", ""),
                "pr_title": draft.get("pr_title", ""),
                "pr_body": draft.get("pr_body", ""),
                "source": "operator",
                "state": "ready",
                "error": None,
            },
        )
        projection = self.publish(task)
        assert projection is not None
        return projection

    # --- preview -----------------------------------------------------------

    async def preview(
        self,
        task: Task,
        *,
        ending: str | None = None,
        mode: str | None = None,
        commit_message: str,
        pr_title: str,
        pr_body: str,
        request_id: str,
        delivery_id: int | None = None,
        gate_seq: int | None = None,
        choice_id: str | None = None,
    ) -> DeliveryPreview:
        """Resolve the delivery this run's procedure currently permits.

        Read-only in every sense that matters: it captures nothing, writes no
        Git state, and grants no authority. What it produces is a description of
        the remaining actions plus a fingerprint over the exact inputs, so a
        later confirmation can be checked against something the operator
        actually saw.

        The ending and mode are *derived*, not requested. They come from the
        pinned definition's own chain, so a caller cannot widen a workflow's
        declared ending by asking for a longer one; a caller that names a
        different ending is told it disagrees rather than quietly overridden.
        """
        authority, resolution = self._authority_for_preview(task, gate_seq, choice_id)
        derived_ending, derived_mode, delivery = resolution
        if ending is not None and ending != derived_ending:
            raise PreviewMismatchError(
                f"this run authorizes a delivery ending at {derived_ending!r}, "
                f"not {ending!r}; the workflow's own chain decides how far it "
                "goes"
            )
        if mode is not None and mode != derived_mode:
            raise PreviewMismatchError(
                f"this delivery commits in {derived_mode!r} mode, not {mode!r}"
            )
        ending, mode = derived_ending, derived_mode
        if delivery_id is not None and delivery_id != delivery.id:
            raise PreviewMismatchError(
                f"delivery {delivery_id} is not the one this run is at"
            )

        blockers: list[Blocker] = []
        completed = [k for k in ("commit", "push", "pr") if delivery.succeeded(k)]
        remaining = [k for k in ENDING_ACTIONS[ending] if k not in completed]

        if not remaining:
            blockers.append(
                Blocker(
                    "already-delivered",
                    f"this delivery already completed every action {ending} "
                    "authorizes",
                )
            )
        if task.state == "archived":
            blockers.append(Blocker("archived", "this task is archived"))
        busy = self._guard.owner(task.id)
        if busy is not None:
            blockers.append(
                Blocker("workspace-busy", f"the task workspace is in use by {busy}")
            )
        unresolved = self._guard.blocked_reason(task.id)
        if unresolved is not None:
            blockers.append(Blocker("unresolved-effect", unresolved))

        candidate, review_info, candidate_blockers = await self._resolve_candidate(
            task, delivery, remaining
        )
        blockers.extend(candidate_blockers)
        blockers.extend(self._question_blockers(authority, review_info))

        destination = self._destination(task)
        identity: dict[str, Any] = {}
        if "commit" in remaining:
            gpg_status = await self._gpg.probe()
            if gpg_status.state != STATE_READY or gpg_status.selected is None:
                blockers.append(
                    Blocker("signing-unavailable", gpg_signing_refusal(gpg_status))
                )
            else:
                identity["signing"] = {
                    "fingerprint": gpg_status.selected.fingerprint,
                    "uid": gpg_status.selected.uid,
                    "source": gpg_status.selected.source,
                }
            if mode == "retain" and candidate is not None:
                blockers.extend(self._retain_blockers(candidate))
                blockers.extend(
                    await self._retain_protected_blockers(task, candidate)
                )
        if candidate is not None:
            identity["git"] = await self._git_identity(task.clone_path)

        # A local-only ending needs no forge availability at all. Anything that
        # pushes keeps the existing trusted-target and eligibility preflight —
        # which is about the GitHub API, and is deliberately not represented as
        # proof of the Git transport identity.
        if "push" in remaining or "pr" in remaining:
            try:
                status, target = await self._preflight_target(task)
            except GitHubPreflightError as exc:
                blockers.append(Blocker("github-unavailable", str(exc)))
            else:
                assert target.target is not None
                destination["slug"] = target.target.slug
                identity["github"] = {
                    "host": status.identity.host,
                    "login": status.identity.login,
                    "credential_source": status.identity.credential_source,
                }
        identity["git_transport"] = {
            "state": "unattributed",
            "detail": (
                "Ompire uses ambient Git credentials for the push; it cannot "
                "observe which principal they authenticate as."
            ),
        }

        if "pr" in remaining and not pr_title.strip():
            blockers.append(
                Blocker("pr-title-missing", "a pull-request ending needs a PR title")
            )
        if "commit" in remaining and mode == "squash" and not commit_message.strip():
            blockers.append(
                Blocker("commit-message-missing", "a squash commit needs a message")
            )

        marker = correlation_marker(task.id, request_id)
        final_body = body_with_marker(pr_body, marker) if "pr" in remaining else pr_body
        candidate_payload = (
            {
                "candidate_id": candidate.candidate_id,
                "base_branch": candidate.base_branch,
                "base_commit": candidate.base_commit,
                "original_head": candidate.original_head,
                "tree_id": candidate.tree_id,
                "commit_count": candidate.commit_count,
                "dirty": candidate.dirty,
            }
            if candidate is not None
            else None
        )
        version = task_version(self._engine, task.id)
        review_seq = (
            authority.approval.review_step_seq
            if authority.approval is not None
            else delivery.review_seq
        )
        # The token covers exactly the inputs this delivery will use. A commit
        # message means nothing once the commit is done, and pull-request text
        # means nothing for an ending that opens none — including them would
        # invalidate a continuation over a field it cannot act on.
        #
        # It also covers *which decision* this is: the question, the answer,
        # and the review attempt the grant is bound to. Without those, a
        # confirmation prepared against one question could be replayed against
        # the next one with identical content.
        fingerprint = _fingerprint(
            {
                "task_id": task.id,
                "delivery_id": delivery.id,
                "version": version,
                "ending": ending,
                "mode": mode,
                "source": authority.source,
                "gate_seq": gate_seq,
                "choice_id": choice_id,
                "review_seq": review_seq,
                "candidate_id": candidate.candidate_id if candidate else None,
                "review_candidate_id": review_info.get("approved_candidate_id"),
                "commit_message": (
                    commit_message
                    if mode == "squash" and "commit" in remaining
                    else ""
                ),
                "pr_title": pr_title if "pr" in remaining else "",
                "pr_body": final_body if "pr" in remaining else "",
                "marker": marker,
                "destination": destination,
                "remaining": remaining,
                "request_id": request_id,
            }
        )
        assert authority.source is not None
        return DeliveryPreview(
            task_id=task.id,
            delivery_id=delivery.id,
            version=version,
            ending=ending,
            mode=mode,
            request_id=request_id,
            source=authority.source,
            gate_seq=gate_seq,
            choice_id=choice_id,
            review_seq=review_seq,
            candidate_id=candidate.candidate_id if candidate else None,
            candidate=candidate_payload,
            review=review_info,
            completed_actions=completed,
            remaining_actions=remaining,
            routing=destination,
            identity=identity,
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body=final_body,
            marker=marker,
            blockers=blockers,
            fingerprint=fingerprint,
        )

    def _authority_for_preview(
        self, task: Task, gate_seq: int | None, choice_id: str | None
    ) -> tuple[RunAuthority, tuple[str, str, DeliveryRecord]]:
        """What this run permits, and the ending, mode, and delivery it means.

        Everything privileged goes through here, whether it arrived as a REST
        request, a Ship-flow confirmation, or a direct service call. That is
        the point: there is no second door where a caller supplies its own
        ending and a delivery follows it.
        """
        authority = resolve_authority(self._engine, task)
        approval = authority.approval
        if approval is not None:
            if gate_seq is None or choice_id is None:
                raise PreviewMismatchError(
                    "this run is waiting at an approval; a delivery preview "
                    "must name the question ('gate_seq') and the answer "
                    "('choice_id') it is about"
                )
            if gate_seq != approval.seq:
                raise PreviewMismatchError(
                    f"this run is waiting on attempt {approval.seq}, not "
                    f"{gate_seq}; reload the question before answering it"
                )
            choice = approval.step.choice_named(choice_id)
            if choice is None:
                raise PreviewMismatchError(
                    f"{choice_id!r} is not one of this question's answers"
                )
            grant = choice.authorize
            if grant is None:
                raise PreviewMismatchError(
                    f"the answer {choice_id!r} authorizes no publication; it "
                    "needs no delivery preview and grants nothing"
                )
            definition = authority.revision.definition  # type: ignore[union-attr]
            chain = [definition.step_named(name) for name in grant.steps]
            first, last = chain[0], chain[-1]
            assert isinstance(first, DeliveryStep) and isinstance(last, DeliveryStep)
            delivery = open_delivery(
                self._engine, task.id, workflow_revision=_task_revision(task)
            )
            assert first.mode is not None
            return authority, (last.action, first.mode, delivery)

        action = authority.action
        if action is not None:
            delivery = action.delivery
            assert delivery.ending is not None and delivery.mode is not None
            return authority, (delivery.ending, delivery.mode, delivery)

        legacy = authority.legacy
        if legacy is not None:
            assert legacy.ending is not None and legacy.mode is not None
            return authority, (legacy.ending, legacy.mode, legacy)

        refusal = authority.refusal()
        assert refusal is not None
        raise DeliveryBlockedError([Blocker(refusal[0], refusal[1])])

    def _delivery_for_preview(
        self, task: Task, delivery_id: int | None
    ) -> DeliveryRecord:
        if delivery_id is not None:
            record = get_delivery(self._engine, delivery_id)
            if record is None or record.task_id != task.id:
                raise PreviewMismatchError(
                    f"delivery {delivery_id} does not belong to task {task.id}"
                )
            return record
        latest = get_latest_delivery(self._engine, task.id)
        if latest is not None and latest.disposition in (
            "open",
            "authorized",
            "blocked",
            "unresolved",
        ):
            return latest
        if latest is not None and latest.disposition == "completed":
            # A completed prefix an operator may still extend.
            return latest
        return open_delivery(
            self._engine, task.id, workflow_revision=_task_revision(task)
        )

    async def _resolve_candidate(
        self, task: Task, delivery: DeliveryRecord, remaining: list[str]
    ) -> tuple[CandidateRecord | None, dict[str, Any], list[Blocker]]:
        """What would be delivered, and whether the approval still covers it."""
        blockers: list[Blocker] = []
        review = get_review(self._engine, task.id)
        approved = review.approved_candidate_id if review is not None else None
        info: dict[str, Any] = {
            "status": review.status if review is not None else None,
            "approved_candidate_id": approved,
            "content_bound": approved is not None,
        }

        if delivery.candidate_id is not None and "commit" not in remaining:
            # Continuing after a completed commit: the content is the signed
            # result, which is fixed. Nothing about today's workspace can
            # change what a later push or pull request delivers.
            candidate = get_candidate(self._engine, delivery.candidate_id)
            info["current_candidate_id"] = delivery.candidate_id
            info["stale"] = False
            return candidate, info, blockers

        base_branch = self._base_branch(task)
        try:
            current = await candidate_identity(
                self._config, task, base_branch=base_branch, fetch=True
            )
        except EmptyCandidateError as exc:
            # Refused, not manufactured: Ompire does not invent a commit to
            # reach a selected ending.
            blockers.append(Blocker("empty-candidate", str(exc)))
            info["current_candidate_id"] = None
            info["stale"] = None
            return None, info, blockers
        except (DeliveryWorkspaceError, GitCommandError) as exc:
            blockers.append(
                Blocker(
                    "candidate-unavailable",
                    f"Ompire could not resolve what this task would publish: {exc}",
                )
            )
            info["current_candidate_id"] = None
            info["stale"] = None
            return None, info, blockers
        info["current_candidate_id"] = current

        if review is None or review.status != "approved":
            blockers.append(
                Blocker(
                    "review-missing",
                    "an approved review is required before delivering this task",
                )
            )
        elif approved is None:
            blockers.append(
                Blocker(
                    "review-unbound",
                    "this approval predates content-bound review; it is kept as "
                    "history and a fresh review is required to deliver",
                )
            )
        elif approved != current:
            blockers.append(
                Blocker(
                    "review-stale",
                    "the task content changed after it was approved; review the "
                    "current content before delivering it",
                )
            )
        info["stale"] = approved is not None and approved != current

        candidate = get_candidate(self._engine, current)
        if candidate is None:
            # The identity matches nothing retained — the review that captured
            # it was purged, or this content was never reviewed.
            info["retained"] = False
            return None, info, blockers
        info["retained"] = True
        return candidate, info, blockers

    @staticmethod
    def _question_blockers(
        authority: RunAuthority, review_info: dict[str, Any]
    ) -> list[Blocker]:
        """The grant must rest on the review *this question* is asking about.

        The task-level approval check above says "this task has an approval
        that covers the current content". That is necessary and not
        sufficient: a review that finished after the question was asked would
        satisfy it while answering a different question. So the frozen binding
        is checked too — the exact attempt, its verdict, and the candidate it
        actually graded.
        """
        approval = authority.approval
        if approval is None:
            return []
        review_info["question_review_seq"] = approval.review_step_seq
        iteration = approval.review
        if iteration is None:
            return [
                Blocker(
                    "review-unrecorded",
                    (
                        "the approval this run is waiting at names a review "
                        "attempt with no recorded verdict; nothing can be "
                        "authorized against a review that did not happen"
                    ),
                )
            ]
        review_info["question_review_outcome"] = iteration.outcome
        review_info["question_review_candidate_id"] = iteration.candidate_id
        if iteration.outcome != "approved":
            return [
                Blocker(
                    "review-not-approved",
                    (
                        f"the review this approval is about ended "
                        f"{iteration.outcome!r}, not approved"
                    ),
                )
            ]
        current = review_info.get("current_candidate_id")
        if current is not None and iteration.candidate_id != current:
            return [
                Blocker(
                    "review-stale",
                    (
                        "the content changed after the review this approval is "
                        "about; review the current content before publishing it"
                    ),
                )
            ]
        return []

    async def _assert_publishable(
        self, task: Task, delivery: DeliveryRecord, tip: str
    ) -> str | None:
        """Refuse a push or pull request whose commits carry a handoff input.

        Every trusted admission asks again, against the objects that exist now
        (ADR-0035). The commit gate already checked the signed result, but a
        push may be a continuation, a restart reconciliation, or a direct
        service call reached without replaying the earlier step — so none of
        them is allowed to inherit an earlier answer. Returns the refusal, or
        None when the range is clean.
        """
        protected = protected_destinations(task)
        if not protected:
            return None
        candidate = (
            get_candidate(self._engine, delivery.candidate_id)
            if delivery.candidate_id
            else None
        )
        if candidate is None:
            return (
                "this task has handoff inputs that may never be published, and "
                "the candidate that would prove what is being published is no "
                "longer retained; review the current content again"
            )
        try:
            await assert_range_unprotected(
                task.clone_path,
                candidate.base_commit,
                tip,
                protected,
                self._config.spawn_step_timeout,
            )
        except ProtectedPathError as exc:
            return str(exc)
        except (DeliveryWorkspaceError, GitCommandError) as exc:
            return (
                "Ompire could not check what this delivery would publish for "
                f"handoff inputs, so it will not publish it: {exc}"
            )
        return None

    async def _retain_protected_blockers(
        self, task: Task, candidate: CandidateRecord
    ) -> list[Blocker]:
        """Retain-mode contamination: any commit that would be published.

        Mode-specific on purpose (ADR-0035). The candidate's *final* tree was
        already checked when it was captured, mode-neutrally, so squash is
        settled by then. Retain publishes the commits themselves, so a handoff
        file that was added in a checkpoint and deleted before HEAD is still
        published — and is refused here, naming the commit, rather than being
        hidden behind a clean final tree.

        This blocks the delivery; it never rewrites, resets, or drops a commit
        to make the range publishable.
        """
        protected = protected_destinations(task)
        if not protected:
            return []
        for commit in candidate.source_commits:
            try:
                await assert_tree_unprotected(
                    task.clone_path,
                    commit.tree_id,
                    protected,
                    self._config.spawn_step_timeout,
                    commit=commit.commit_id,
                )
            except ProtectedPathError as exc:
                return [Blocker("retain-protected-paths", str(exc))]
            except (DeliveryWorkspaceError, GitCommandError) as exc:
                return [
                    Blocker(
                        "retain-protected-unreadable",
                        "Ompire could not check every commit this delivery "
                        f"would publish for handoff inputs: {exc}",
                    )
                ]
        return []

    def _retain_blockers(self, candidate: CandidateRecord) -> list[Blocker]:
        blockers: list[Blocker] = []
        if candidate.dirty:
            blockers.append(
                Blocker(
                    "retain-dirty",
                    "retain mode publishes existing commits; this task has "
                    "uncommitted changes. Use squash, or commit them first.",
                )
            )
        if not candidate.source_commits:
            blockers.append(
                Blocker("retain-empty", "there are no commits to retain")
            )
        if any(len(c.parent_ids) > 1 for c in candidate.source_commits):
            blockers.append(
                Blocker(
                    "retain-merges",
                    "the range contains merge commits; use squash mode",
                )
            )
        return blockers

    async def _git_identity(self, clone_path: str) -> dict[str, Any]:
        return {
            "name": await self._git_config(clone_path, "user.name"),
            "email": await self._git_config(clone_path, "user.email"),
        }

    # --- delivery ----------------------------------------------------------

    async def confirm(
        self,
        task: Task,
        resolved: DeliveryPreview,
        *,
        runner: Any,
        note: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Commit one operator authorization for a resolved, unblocked preview.

        The single confirmation operation. Task detail's approval and Ship
        flow's confirm button are two front doors onto it, and a direct
        service call is a third — none of them can authorize anything this
        does not.

        When a workflow gate is what authorizes the delivery, the decision and
        the grant become durable in the *same* transaction as the run's move to
        its first action. Nothing privileged has happened when this returns:
        what exists is a record of what the operator confirmed, which every
        action then re-reads rather than taking from its caller.
        """
        if resolved.blockers:
            raise DeliveryBlockedError(resolved.blockers)
        if resolved.source == SOURCE_WORKFLOW_ACTION:
            # An interrupted chain the operator is continuing. The grant is
            # unchanged — this refreshes what it was confirmed against and
            # hands the *same* attempt back to the run, so the journal link
            # that stops a repeated effect survives the continuation.
            delivery_id, projection = await self.authorize(
                task, resolved, expected_version=expected_version
            )
            from ompire_daemon.taskdefinition import resolve_task_definition

            waiting = latest_step_record(self._engine, task.id)
            if waiting is not None and waiting.status == "waiting":
                runner.continue_delivery(
                    task,
                    resolve_task_definition(self._engine, task),
                    expected_seq=waiting.seq,
                )
            return delivery_id, projection
        if resolved.source != SOURCE_WORKFLOW_GATE:
            return await self.authorize(
                task, resolved, expected_version=expected_version
            )
        if resolved.candidate_id is None:
            raise PreviewMismatchError(
                "there is no retained content to deliver for this task"
            )
        assert resolved.gate_seq is not None and resolved.choice_id is not None
        from ompire_daemon.taskdefinition import resolve_task_definition

        authorization = DeliveryAuthorization(
            delivery_id=resolved.delivery_id,
            expected_version=resolved.version,
            candidate_id=resolved.candidate_id,
            review_candidate_id=resolved.review.get("approved_candidate_id"),
            review_seq=resolved.review_seq,
            mode=resolved.mode,
            ending=resolved.ending,
            actions=ENDING_ACTIONS[resolved.ending],
            commit_message=resolved.commit_message,
            pr_title=resolved.pr_title,
            pr_body=resolved.pr_body,
            routing=resolved.routing,
            identity=resolved.identity,
            request_key=resolved.request_id,
            input_fingerprint=resolved.fingerprint,
        )
        updated = runner.answer_gate(
            task,
            resolve_task_definition(self._engine, task),
            expected_seq=resolved.gate_seq,
            choice_id=resolved.choice_id,
            note=note,
            authorization=authorization,
        )
        published = self.publish(updated)
        assert published is not None
        return resolved.delivery_id, published

    async def deliver(
        self,
        task: Task,
        *,
        commit_message: str,
        pr_title: str,
        pr_body: str,
        request_id: str,
        preview_token: str,
        runner: Any,
        note: str | None = None,
        gate_seq: int | None = None,
        choice_id: str | None = None,
        ending: str | None = None,
        mode: str | None = None,
        delivery_id: int | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Preview, authorize, and run one delivery to completion.

        The direct service entry point, and as authoritative as REST: admission
        is re-resolved here rather than trusted from the caller. The REST layer
        parses and authenticates, then calls `preview` and `confirm` itself so
        it can background the execution.
        """
        resolved = await self.preview(
            task,
            ending=ending,
            mode=mode,
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body=pr_body,
            request_id=request_id,
            delivery_id=delivery_id,
            gate_seq=gate_seq,
            choice_id=choice_id,
        )
        if resolved.fingerprint != preview_token:
            raise PreviewMismatchError(
                "the delivery changed since it was previewed; review the new "
                "preview before confirming"
            )
        delivery_id, _projection = await self.confirm(
            task, resolved, runner=runner, note=note, expected_version=expected_version
        )
        if resolved.source == SOURCE_LEGACY_CONTINUATION:
            # A pre-upgrade grant has no run to drive it, so its remaining
            # prefix is executed here.
            await self._run_prefix(task, delivery_id, request_id)
        projection = self.projection(task)
        assert projection is not None
        return projection

    async def authorize(
        self,
        task: Task,
        resolved: DeliveryPreview,
        *,
        expected_version: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Commit one operator authorization for a resolved, unblocked preview.

        Nothing privileged has happened yet when this returns. What it produces
        is the durable record of what the operator confirmed — the ending, the
        exact inputs, the candidate, and the review identity — which every
        action then re-reads rather than taking from its caller.
        """
        if resolved.blockers:
            raise DeliveryBlockedError(resolved.blockers)
        if resolved.candidate_id is None:
            raise PreviewMismatchError(
                "there is no retained content to deliver for this task"
            )
        if expected_version is not None and resolved.version != expected_version:
            raise PreviewMismatchError(
                f"delivery changed since it was previewed (version "
                f"{resolved.version}, expected {expected_version})"
            )
        record = get_delivery(self._engine, resolved.delivery_id)
        if record is None:
            raise PreviewMismatchError(
                f"delivery {resolved.delivery_id} no longer exists"
            )
        if record.authorized_at is not None and record.ending is not None:
            further = len(ENDING_ACTIONS[resolved.ending]) > len(
                ENDING_ACTIONS[record.ending]
            )
            completed = any(a.phase == "succeeded" for a in record.actions)
            if further:
                # Extending a completed prefix: the original authorization is
                # left exactly as it was, and this appends authority for the
                # rest.
                delivery = extend_delivery(
                    self._engine,
                    record.id,
                    expected_version=resolved.version,
                    ending=resolved.ending,
                    pr_title=resolved.pr_title,
                    pr_body=resolved.pr_body,
                    request_key=resolved.request_id,
                    input_fingerprint=resolved.fingerprint,
                    identity=resolved.identity,
                )
            elif completed:
                # A completed prefix stands and the next action was refused
                # safely. The operator looked at the refusal and asked for the
                # rest again; nothing already authorized is rewritten.
                delivery = resume_delivery(
                    self._engine,
                    record.id,
                    expected_version=resolved.version,
                    request_key=resolved.request_id,
                    input_fingerprint=resolved.fingerprint,
                )
            else:
                # Nothing succeeded, so there is no terminal prefix to protect:
                # a corrected confirmation replaces the refused one.
                delivery = reauthorize_delivery(
                    self._engine,
                    record.id,
                    expected_version=resolved.version,
                    candidate_id=resolved.candidate_id,
                    review_candidate_id=resolved.review.get("approved_candidate_id"),
                    mode=resolved.mode,
                    ending=resolved.ending,
                    commit_message=resolved.commit_message,
                    pr_title=resolved.pr_title,
                    pr_body=resolved.pr_body,
                    routing=resolved.routing,
                    identity=resolved.identity,
                    request_key=resolved.request_id,
                    input_fingerprint=resolved.fingerprint,
                )
        else:
            delivery = authorize_delivery(
                self._engine,
                record.id,
                expected_version=resolved.version,
                candidate_id=resolved.candidate_id,
                review_candidate_id=resolved.review.get("approved_candidate_id"),
                mode=resolved.mode,
                ending=resolved.ending,
                commit_message=resolved.commit_message,
                pr_title=resolved.pr_title,
                pr_body=resolved.pr_body,
                routing=resolved.routing,
                identity=resolved.identity,
                request_key=resolved.request_id,
                input_fingerprint=resolved.fingerprint,
            )
        projection = self.publish(task)
        assert projection is not None
        return delivery.id, projection

    def start_delivery(
        self, task: Task, delivery_id: int, request_id: str, jobs: set[asyncio.Task]
    ) -> None:
        """Run an authorized prefix in the background, tracked by the app."""
        job = asyncio.create_task(self._run_prefix(task, delivery_id, request_id))
        self._backgrounds[task.id] = job
        jobs.add(job)
        job.add_done_callback(jobs.discard)
        job.add_done_callback(
            lambda _t: self._backgrounds.pop(task.id, None)
        )

    def _land(
        self,
        action_id: int,
        *,
        result: dict[str, Any],
        identity: dict[str, Any] | None = None,
        disposition: str | None = None,
        settle: Settle | None = None,
    ) -> None:
        """Record one action's verified success.

        `settle` is how a workflow-owned action lands: the runner supplies it,
        and it commits the journal result *and* the step's transition in one
        write. Without it — a continuation, a pre-upgrade grant — the journal
        result stands alone, exactly as before.
        """
        if settle is None:
            complete_action(
                self._engine,
                action_id,
                result=result,
                identity=identity,
                disposition=disposition,
            )
            return
        settle(action_id, result, identity, disposition)

    def workflow_action(self, task_id: int, workflow_seq: int) -> ActionRecord | None:
        """The action attempt this workflow step opened, if it opened one.

        What recovery asks before it does anything: an effect that is already
        on record is adopted, and one that is not is left for an explicit
        continuation. Neither answer involves performing it again.
        """
        delivery = get_latest_delivery(self._engine, task_id)
        if delivery is None:
            return None
        for action in delivery.actions:
            if action.workflow_seq == workflow_seq:
                return action
        return None

    async def perform_action(
        self,
        task: Task,
        *,
        action: str,
        workflow_seq: int,
        request_id: str,
        settle: Settle,
    ) -> bool:
        """Perform the one effect this run's current delivery step declares.

        The runner decides that the step is next; this decides whether it is
        *permitted*, and performs it through the same admitted operation an
        operator's confirmation would. Authority is re-resolved here rather
        than trusted from the caller: a step is not a grant, and the run being
        at one proves only where the run is.
        """
        authority = resolve_authority(self._engine, task)
        pending = authority.action
        if pending is None or pending.seq != workflow_seq:
            raise DeliveryBlockedError(
                [
                    Blocker(
                        "not-at-action",
                        (
                            f"this run has no authorized action outstanding at "
                            f"attempt {workflow_seq}"
                        ),
                    )
                ]
            )
        if pending.step.action != action:
            raise DeliveryBlockedError(
                [
                    Blocker(
                        "action-mismatch",
                        (
                            f"attempt {workflow_seq} is a {pending.step.action!r} "
                            f"action, not {action!r}"
                        ),
                    )
                ]
            )
        delivery = pending.delivery
        if action not in delivery.remaining_actions:
            raise DeliveryBlockedError(
                [
                    Blocker(
                        "action-not-granted",
                        (
                            f"the authorization for this run does not have a "
                            f"remaining {action} action"
                        ),
                    )
                ]
            )
        if delivery.remaining_actions[0] != action:
            # The predecessor is consumed, not assumed: an action never runs
            # because the one before it was skipped.
            raise DeliveryBlockedError(
                [
                    Blocker(
                        "predecessor-missing",
                        (
                            f"{delivery.remaining_actions[0]} has not completed, "
                            f"so {action} cannot run yet"
                        ),
                    )
                ]
            )
        runners = {"commit": self._run_commit, "push": self._run_push, "pr": self._run_pr}
        async with self._guard.hold(task.id, f"workflow-{action}"):
            ok = await runners[action](
                task, delivery, request_id, workflow_seq=workflow_seq, settle=settle
            )
        if ok:
            landed = get_delivery(self._engine, delivery.id)
            if landed is not None and not landed.remaining_actions:
                # The chain performed every effect it was granted. Its
                # candidate staging repository has served its purpose; an
                # unresolved one is deliberately kept.
                set_disposition(self._engine, delivery.id, "completed")
                self.release_candidate_storage(task.id)
        self._refresh(task.id)
        return ok

    async def _run_prefix(self, task: Task, delivery_id: int, request_id: str) -> None:
        """Execute the remaining authorized actions, in order, stopping at the
        first that does not verifiably succeed."""
        runners = {
            "commit": self._run_commit,
            "push": self._run_push,
            "pr": self._run_pr,
        }
        try:
            async with self._guard.hold(task.id, "ship-delivery"):
                while True:
                    delivery = get_delivery(self._engine, delivery_id)
                    if delivery is None or delivery.disposition in (
                        "unresolved",
                        "blocked",
                        "abandoned",
                    ):
                        return
                    remaining = delivery.remaining_actions
                    if not remaining:
                        set_disposition(self._engine, delivery_id, "completed")
                        self._refresh(task.id)
                        self.release_candidate_storage(task.id)
                        return
                    kind = remaining[0]
                    ok = await runners[kind](task, delivery, request_id)
                    self._refresh(task.id)
                    if not ok:
                        return
        except (WorkspaceBusyError, WorkspaceBlockedError) as exc:
            logger.warning("delivery for task %d was not admitted: %s", task.id, exc)
            set_disposition(
                self._engine, delivery_id, "blocked", blocked_reason=str(exc)
            )
            self._refresh(task.id)

    # --- commit ------------------------------------------------------------

    async def _run_commit(
        self,
        task: Task,
        delivery: DeliveryRecord,
        request_id: str,
        *,
        workflow_seq: int | None = None,
        settle: Settle | None = None,
    ) -> bool:
        assert delivery.candidate_id is not None
        candidate = get_candidate(self._engine, delivery.candidate_id)
        if candidate is not None and not await store_has_objects(
            Path(candidate.storage_path or "/nonexistent"),
            [candidate.tree_id, candidate.base_commit],
            self._config.spawn_step_timeout,
        ):
            # The staging repository was removed since the review. Re-capturing
            # is safe precisely because identity is content: if the workspace
            # still holds the reviewed content it captures to the same id, and
            # if it does not, the mismatch blocks the delivery.
            candidate = await self._recapture(task, candidate)
        if candidate is None or candidate.storage_path is None:
            set_disposition(
                self._engine,
                delivery.id,
                "blocked",
                blocked_reason=(
                    "the reviewed content is no longer available in its "
                    "protected store; review the task again"
                ),
            )
            return False

        gpg_status = await self._gpg.probe()
        if gpg_status.state != STATE_READY or gpg_status.selected is None:
            set_disposition(
                self._engine,
                delivery.id,
                "blocked",
                blocked_reason=gpg_signing_refusal(gpg_status),
            )
            return False
        signing_key = gpg_status.selected.fingerprint
        store = Path(candidate.storage_path)
        timeout = self._config.spawn_step_timeout

        expected = {
            "candidate_id": candidate.candidate_id,
            "mode": delivery.mode,
            "base_commit": candidate.base_commit,
            "tree_id": candidate.tree_id,
            "original_head": candidate.original_head,
            "signing_key": signing_key,
            "commit_count": (
                1 if delivery.mode == "squash" else candidate.commit_count
            ),
            "store": str(store),
            "signed_ref": None,
        }
        try:
            action = prepare_action(
                self._engine,
                delivery.id,
                kind="commit",
                request_key=f"{request_id}:commit",
                input_fingerprint=_fingerprint(expected),
                expected=expected,
                identity=delivery.identity,
                workflow_seq=workflow_seq,
            )
        except DeliveryConflictError as exc:
            set_disposition(
                self._engine, delivery.id, "blocked", blocked_reason=str(exc)
            )
            return False

        signed_ref = f"refs/ompire/signed/{action.id}"
        expected["signed_ref"] = signed_ref
        mark_action_executing(self._engine, action.id, expected=expected)
        self._refresh(task.id)

        try:
            tip, signed_commits = await self._sign(
                store,
                candidate,
                mode=delivery.mode or "squash",
                message=delivery.commit_message or "",
                signing_key=signing_key,
                identity=delivery.identity or {},
                action_id=action.id,
                signed_ref=signed_ref,
                timeout=timeout,
            )
            await self._verify_signed(
                store,
                tip,
                candidate,
                delivery.mode or "squash",
                signing_key,
                timeout,
                protected_destinations(task),
            )
            installed, install_note = await self._install_signed(
                task, store, tip, candidate, signed_ref, timeout
            )
        except (DeliveryWorkspaceError, GitCommandError) as exc:
            # Signing either produced nothing or left evidence under the
            # action's own ref. `_sign` records what it produced before each
            # step, so classification is a lookup, not a guess.
            return await self._classify_commit_failure(
                task, action.id, store, str(exc)
            )
        except Exception as exc:  # noqa: BLE001
            return await self._classify_commit_failure(
                task, action.id, store, str(exc)
            )

        result = {
            "signed_tip": tip,
            "signed_commits": signed_commits,
            "commit_count": len(signed_commits),
            "mode": delivery.mode,
            "candidate_id": candidate.candidate_id,
            "base_commit": candidate.base_commit,
            "tree_id": candidate.tree_id,
            "signing_key": signing_key,
            "signed_ref": signed_ref,
            "installed": installed,
            "installed_over": candidate.original_head,
            "note": install_note,
        }
        self._land(
            action.id, result=result, identity=delivery.identity, settle=settle
        )
        if not installed:
            set_disposition(
                self._engine,
                delivery.id,
                "blocked",
                blocked_reason=install_note
                or "the signed result could not be installed into the task clone",
            )
            return False
        return True

    async def _recapture(
        self, task: Task, candidate: CandidateRecord
    ) -> CandidateRecord | None:
        """Rebuild a candidate's protected storage from an unchanged workspace."""
        try:
            fresh = await capture_candidate(
                self._config,
                self._engine,
                task,
                base_branch=candidate.base_branch,
                fetch=False,
            )
        except (DeliveryWorkspaceError, GitCommandError) as exc:
            logger.warning(
                "could not re-capture candidate %s for task %d: %s",
                candidate.candidate_id,
                task.id,
                exc,
            )
            return None
        if fresh.candidate_id != candidate.candidate_id:
            return None
        return fresh

    async def _sign(
        self,
        store: Path,
        candidate: CandidateRecord,
        *,
        mode: str,
        message: str,
        signing_key: str,
        identity: dict[str, Any],
        action_id: int,
        signed_ref: str,
        timeout: int,
    ) -> tuple[str, list[dict[str, str]]]:
        """Build the signed result inside the candidate's own repository.

        Never `git add --all` in the live workspace: the tree is the one the
        review graded, taken from the protected store. Retain replays the
        captured range commit by commit onto the same base, preserving each
        message and tree and rewriting only the identity and signature —
        recording each signature as it lands, so an interruption leaves
        inspectable progress rather than an opaque half-state.
        """
        git_identity = identity.get("git") or {}
        env = _identity_env(git_identity)
        signed: list[dict[str, str]] = []
        parent = candidate.base_commit

        plan: list[tuple[str, str, str | None]]
        if mode == "squash":
            plan = [(candidate.tree_id, message, None)]
        else:
            plan = [
                (source.tree_id, source.message, source.commit_id)
                for source in candidate.source_commits
            ]

        for tree_id, commit_message, source_id in plan:
            tip = await self._commit_tree(
                store,
                tree_id=tree_id,
                parent=parent,
                message=commit_message,
                signing_key=signing_key,
                env=env,
                timeout=timeout,
            )
            signed.append({"source": source_id or "", "signed": tip})
            parent = tip
            # The ref moves with every signature, so the objects are reachable
            # and a lost response can find exactly what was produced.
            await run_git(
                ["git", "-C", str(store), "update-ref", signed_ref, tip],
                cwd=store,
                timeout=timeout,
                step="delivery-sign-ref",
            )
            record_action_progress(
                self._engine,
                action_id,
                {"signed": signed, "planned": len(plan), "tip": tip},
            )
        return parent, signed

    async def _commit_tree(
        self,
        store: Path,
        *,
        tree_id: str,
        parent: str,
        message: str,
        signing_key: str,
        env: dict[str, str],
        timeout: int,
    ) -> str:
        def write_message() -> str:
            fd, path = tempfile.mkstemp(prefix="ompire-delivery-msg-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(message)
            return path

        msg_path = await asyncio.to_thread(write_message)
        try:
            argv = [
                "git",
                "-C",
                str(store),
                *await self._signing_config(),
                "commit-tree",
                tree_id,
                "-p",
                parent,
                "-F",
                msg_path,
                f"-S{signing_key}",
            ]
            stdout, _stderr, _code = await run_git(
                argv, cwd=store, timeout=timeout, step="delivery-commit-tree", env=env
            )
        finally:
            await asyncio.to_thread(Path(msg_path).unlink)
        tip = stdout.strip()
        if not tip:
            raise DeliveryWorkspaceError("signing produced no commit id")
        return tip

    async def _verify_signed(
        self,
        store: Path,
        tip: str,
        candidate: CandidateRecord,
        mode: str,
        signing_key: str,
        timeout: int,
        protected: Sequence[str] = (),
    ) -> None:
        """Prove the result is exactly what was authorized, before it is
        installed or published.

        `%G?` alone only says a signature verified; `%GF` says *whose* it is.
        Checking both is what makes the explicit `-S<key>` verifiable rather
        than merely requested. Verification runs under the operator-owned
        signing configuration for the same reason the signature does.

        `protected` is re-checked here against the *actual signed commits*, not
        against the candidate record they came from (ADR-0035). This is the
        last gate before anything is installed or pushed, and it asks the
        object store what it really holds rather than trusting an earlier
        reading of a workspace.
        """
        expected_count = 1 if mode == "squash" else candidate.commit_count
        stdout = await self._store_log(store, tip, candidate.base_commit, timeout)
        lines = [line for line in stdout.strip().splitlines() if line.strip()]
        if len(lines) != expected_count:
            raise DeliveryWorkspaceError(
                f"signed range has {len(lines)} commits, expected {expected_count}"
            )
        # Newest first, as `git log` walks. Each entry is checked against the
        # tree it must carry and the parent it must have, so the result is
        # verified as a *structure* rather than as a bag of signed commits.
        expected_trees = (
            [candidate.tree_id]
            if mode == "squash"
            else [c.tree_id for c in reversed(candidate.source_commits)]
        )
        wanted = signing_key.upper()
        expected_child: str | None = None
        for line, expected_tree in zip(lines, expected_trees, strict=True):
            parts = line.split(_FIELD)
            if len(parts) < 4:
                raise DeliveryWorkspaceError(
                    "could not read signature status for a signed commit"
                )
            sha, tree, parents_raw, status_field = (
                parts[0].strip(),
                parts[1].strip(),
                parts[2].strip(),
                parts[3].split(),
            )
            status = status_field[0] if status_field else ""
            signer = status_field[1].upper() if len(status_field) > 1 else ""
            if expected_child is not None and sha != expected_child:
                raise DeliveryWorkspaceError(
                    f"signed commit {sha[:12]} is not the parent of the commit "
                    "above it in the range"
                )
            parents = parents_raw.split()
            if len(parents) != 1:
                raise DeliveryWorkspaceError(
                    f"signed commit {sha[:12]} has {len(parents)} parents; the "
                    "published range must be linear"
                )
            expected_child = parents[0]
            if tree != expected_tree:
                raise DeliveryWorkspaceError(
                    f"signed commit {sha[:12]} does not carry the reviewed tree"
                )
            if status not in ("G", "U"):
                raise DeliveryWorkspaceError(
                    f"commit {sha[:12]} has no good signature (status {status!r})"
                )
            # `%GF` names the key that actually signed. The probe selects the
            # signing subkey itself, so this is an exact identity check.
            if signer != wanted:
                raise DeliveryWorkspaceError(
                    f"commit {sha[:12]} was signed by {signer or 'an unknown key'}, "
                    f"not the selected signing key {wanted}"
                )
        # The oldest signed commit must sit directly on the reviewed base.
        if expected_child != candidate.base_commit:
            raise DeliveryWorkspaceError(
                "the signed range does not start from the reviewed base "
                f"({candidate.base_commit[:12]})"
            )
        # Every tree that would actually be published, read out of the store
        # that holds the signed objects.
        await assert_range_unprotected(
            store, candidate.base_commit, tip, protected, timeout
        )

    async def _store_log(
        self, store: Path, tip: str, base: str, timeout: int
    ) -> str:
        argv = [
            "git",
            "-C",
            str(store),
            *await self._signing_config(),
            "log",
            f"--format=%H{_FIELD_ESC}%T{_FIELD_ESC}%P{_FIELD_ESC}%G? %GF",
            f"{base}..{tip}",
        ]
        stdout, _stderr, _code = await run_git(
            argv, cwd=store, timeout=timeout, step="delivery-verify-signatures"
        )
        return stdout

    async def _install_signed(
        self,
        task: Task,
        store: Path,
        tip: str,
        candidate: CandidateRecord,
        signed_ref: str,
        timeout: int,
    ) -> tuple[bool, str | None]:
        """Publish the signed result into the task's own object store and move
        its branch, under a compare-and-swap against the captured HEAD.

        The index is synchronized to the signed tree only after re-checking
        what the workspace holds, so a successful commit never leaves a false
        staged reverse diff — and never resets over work that arrived after the
        candidate was captured.
        """
        clone = task.clone_path
        await run_git(
            safe_git(clone, "fetch", "--quiet", str(store), f"+{signed_ref}:{signed_ref}"),
            cwd=clone,
            timeout=timeout,
            step="delivery-fetch-signed",
        )
        head_ref = (
            await git_out(
                clone,
                ["symbolic-ref", "--quiet", "HEAD"],
                timeout=timeout,
                step="delivery-head-ref",
            )
        ).strip()
        if not head_ref:
            return False, (
                "the task clone has a detached HEAD; Ompire will not move a "
                "branch it cannot identify"
            )
        current_head = (
            await git_out(
                clone, ["rev-parse", "HEAD"], timeout=timeout, step="delivery-head"
            )
        ).strip()
        if current_head == tip:
            return True, None
        if current_head != candidate.original_head:
            return False, (
                f"the task branch moved to {current_head[:12]} after the "
                f"content was captured at {candidate.original_head[:12]}; the "
                "signed result is retained but was not installed"
            )
        _out, stderr, code = await run_git(
            safe_git(
                clone,
                "update-ref",
                "-m",
                "ompire: signed delivery",
                head_ref,
                tip,
                candidate.original_head,
            ),
            cwd=clone,
            timeout=timeout,
            step="delivery-install",
            check=False,
        )
        if code != 0:
            return False, (
                "the task branch changed while the signed result was being "
                f"installed: {stderr.strip()}"
            )
        # Only now, and only if the working files still hold what was signed,
        # does the index move. The comparison is the *tree*, not the candidate
        # identity: installing the signed result deliberately changes the
        # commits, and Ompire's own rewrite must not read as the operator's
        # workspace having moved.
        try:
            tree_now: str | None = await workspace_tree_id(self._config, task)
        except (DeliveryWorkspaceError, GitCommandError):
            tree_now = None
        await run_git(
            safe_git(clone, "reset", "--mixed", "--quiet", tip),
            cwd=clone,
            timeout=timeout,
            step="delivery-sync-index",
            check=False,
        )
        if tree_now is not None and tree_now != candidate.tree_id:
            return True, (
                "the workspace changed after the content was captured; the "
                "signed commit is the reviewed content, and the extra changes "
                "remain uncommitted in the clone"
            )
        return True, None

    async def _classify_commit_failure(
        self, task: Task, action_id: int, store: Path, message: str
    ) -> bool:
        """Decide whether a failed signing attempt produced anything.

        Non-execution is established by looking for the attempt's own signed
        ref. Finding nothing there is proof that no signature landed, because
        the ref is written before the attempt can return. Finding something is
        not a failure at all — it is an unresolved result for an operator to
        adopt or discard, never an invitation to sign again.
        """
        action = get_action(self._engine, action_id)
        assert action is not None
        signed_ref = (action.expected or {}).get("signed_ref")
        produced = False
        if signed_ref:
            _out, _err, code = await run_git(
                ["git", "-C", str(store), "rev-parse", "--verify", "--quiet", signed_ref],
                cwd=store,
                timeout=self._config.spawn_step_timeout,
                step="delivery-signed-probe",
                check=False,
            )
            produced = code == 0
        if produced:
            flag_action_unresolved(
                self._engine,
                action_id,
                error=(
                    f"signing did not complete cleanly ({message}), but signatures "
                    "were produced. Ompire will not sign again until this is resolved."
                ),
                evidence={
                    "state": "unknown",
                    "signed_ref": signed_ref,
                    "detail": (
                        "signatures exist under the attempt's protected ref but "
                        "the attempt did not finish"
                    ),
                },
            )
            self._guard.block(
                task.id, "a signing attempt produced an unverified result"
            )
        else:
            fail_action(self._engine, action_id, error=f"commit failed: {message}")
        return False

    # --- push --------------------------------------------------------------

    async def _run_push(
        self,
        task: Task,
        delivery: DeliveryRecord,
        request_id: str,
        *,
        workflow_seq: int | None = None,
        settle: Settle | None = None,
    ) -> bool:
        commit = delivery.succeeded("commit")
        if commit is None or not (commit.result or {}).get("signed_tip"):
            set_disposition(
                self._engine,
                delivery.id,
                "blocked",
                blocked_reason="there is no verified signed result to push",
            )
            return False
        tip = (commit.result or {})["signed_tip"]
        refusal = await self._assert_publishable(task, delivery, tip)
        if refusal is not None:
            set_disposition(
                self._engine, delivery.id, "blocked", blocked_reason=refusal
            )
            return False
        destination = delivery.routing or self._destination(task)
        clone = task.clone_path
        timeout = self._config.spawn_step_timeout

        # The exact destination ref, and what it holds right now. Both are
        # recorded before the write, because after a lost response the observed
        # pre-push value is the only thing that can distinguish "never ran"
        # from "ran and someone else moved it".
        observed = await self._remote_head(
            clone, destination["remote_url"], destination["ref"], timeout
        )
        expected = {
            "remote_url": destination["remote_url"],
            "ref": destination["ref"],
            "branch": destination["branch"],
            "signed_tip": tip,
            "pre_push_oid": observed,
        }
        try:
            action = prepare_action(
                self._engine,
                delivery.id,
                kind="push",
                request_key=f"{request_id}:push",
                input_fingerprint=_fingerprint(expected),
                expected=expected,
                identity=delivery.identity,
                workflow_seq=workflow_seq,
            )
        except DeliveryConflictError as exc:
            set_disposition(
                self._engine, delivery.id, "blocked", blocked_reason=str(exc)
            )
            return False

        if observed == tip:
            # Already at the authorized head: the push is a completed fact,
            # not something to repeat.
            self._land(
                action.id,
                result={**expected, "head": tip, "adopted": True},
                settle=settle,
            )
            return True

        mark_action_executing(self._engine, action.id)
        self._refresh(task.id)
        try:
            await self._push(clone, destination, tip, observed, timeout)
        except PushConflictError as exc:
            fail_action(self._engine, action.id, error=str(exc))
            return False
        except PushError as exc:
            return await self._classify_push_failure(
                task, action.id, clone, destination, tip, str(exc)
            )
        except Exception as exc:  # noqa: BLE001
            return await self._classify_push_failure(
                task, action.id, clone, destination, tip, str(exc)
            )

        head = await self._remote_head(
            clone, destination["remote_url"], destination["ref"], timeout
        )
        if head != tip:
            flag_action_unresolved(
                self._engine,
                action.id,
                error=(
                    "the push reported success but the destination does not hold "
                    "the authorized head"
                ),
                evidence={
                    "state": "conflict",
                    "observed_head": head,
                    "expected_head": tip,
                },
            )
            self._guard.block(task.id, "a push landed on an unexpected remote head")
            return False
        self._land(
            action.id,
            result={**expected, "head": tip, "adopted": False},
            settle=settle,
        )
        return True

    async def _remote_head(
        self, clone: str, remote_url: str, ref: str, timeout: int
    ) -> str | None:
        """The destination ref's current object id, or None when it is absent.

        A transport failure raises rather than returning None: "cannot see the
        ref" and "the ref does not exist" have to stay different answers.
        """
        stdout, stderr, code = await run_git(
            safe_git(clone, "ls-remote", remote_url, ref),
            cwd=clone,
            timeout=timeout,
            step="delivery-ls-remote",
            check=False,
        )
        if code != 0:
            raise PushError(
                f"could not read the destination branch: {stderr.strip() or stdout.strip()}"
            )
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
        return None

    async def _push(
        self,
        clone: str,
        destination: dict[str, Any],
        tip: str,
        observed: str | None,
        timeout: int,
    ) -> None:
        """Write the exact authorized object to the exact authorized ref.

        The lease is the object id observed just now and recorded in the
        attempt, never a remote-tracking ref refreshed behind the operator's
        back. An absent destination is pushed without force at all, so it can
        only ever create the branch or fast-forward it.
        """
        argv = [
            "push",
            destination["remote_url"],
            f"{tip}:{destination['ref']}",
        ]
        if observed is not None:
            argv.append(f"--force-with-lease={destination['ref']}:{observed}")
        stdout, stderr, code = await run_git(
            safe_git(clone, *argv),
            cwd=clone,
            timeout=timeout,
            step="delivery-push",
            check=False,
        )
        if code == 0:
            return
        detail = (stderr.strip() or stdout.strip() or f"git push exited {code}")
        if (
            str(destination["remote_url"]).startswith("git@github.com:")
            and "Permission denied (publickey)" in detail
        ):
            raise SshAuthenticationError(detail)
        if "stale info" in detail or "non-fast-forward" in detail or "fetch first" in detail:
            raise PushConflictError(
                "the destination branch holds something Ompire did not "
                f"authorize overwriting: {detail}"
            )
        raise PushError(detail)

    async def _classify_push_failure(
        self,
        task: Task,
        action_id: int,
        clone: str,
        destination: dict[str, Any],
        tip: str,
        message: str,
    ) -> bool:
        """A failed push is only a failure when the destination proves it.

        Returns whether the action ended up succeeding, so the coordinator can
        carry on with the rest of the authorized prefix: a push whose reply was
        lost still pushed, and a delivery that stops there would leave an
        already-published branch looking unfinished.
        """
        try:
            head = await self._remote_head(
                clone,
                destination["remote_url"],
                destination["ref"],
                self._config.spawn_step_timeout,
            )
        except PushError:
            flag_action_unresolved(
                self._engine,
                action_id,
                error=(
                    f"the push failed ({message}) and the destination could not "
                    "be read, so Ompire cannot tell whether it landed"
                ),
                evidence={
                    "state": "unknown",
                    "expected_head": tip,
                    "detail": "the destination ref could not be read",
                },
            )
            self._guard.block(task.id, "a push has an unknown outcome")
            return False
        expected = (get_action(self._engine, action_id).expected or {})  # type: ignore[union-attr]
        if head == tip:
            complete_action(
                self._engine,
                action_id,
                result={**expected, "head": tip, "adopted": True},
            )
            return True
        if head == expected.get("pre_push_oid"):
            fail_action(self._engine, action_id, error=f"push failed: {message}")
            return False
        flag_action_unresolved(
            self._engine,
            action_id,
            error=(
                f"the push failed ({message}) and the destination now holds "
                f"{(head or 'nothing')[:12]}, which is neither the authorized head "
                "nor what was there before"
            ),
            evidence={
                "state": "conflict",
                "observed_head": head,
                "expected_head": tip,
                "detail": (
                    "the destination holds neither the authorized head nor what "
                    "was there before the push"
                ),
            },
        )
        self._guard.block(task.id, "a push landed on an unexpected remote head")
        return False

    # --- pull request ------------------------------------------------------

    async def _run_pr(
        self,
        task: Task,
        delivery: DeliveryRecord,
        request_id: str,
        *,
        workflow_seq: int | None = None,
        settle: Settle | None = None,
    ) -> bool:
        push = delivery.succeeded("push")
        if push is None:
            set_disposition(
                self._engine,
                delivery.id,
                "blocked",
                blocked_reason="there is no verified pushed result to open a "
                "pull request for",
            )
            return False
        # A pull request publishes the pushed range to reviewers, so it asks
        # the same question the push did rather than inheriting its answer.
        refusal = await self._assert_publishable(
            task, delivery, str((push.result or {}).get("signed_tip") or "")
        )
        if refusal is not None:
            set_disposition(
                self._engine, delivery.id, "blocked", blocked_reason=refusal
            )
            return False
        destination = delivery.routing or self._destination(task)
        # The ambient account can change while Git is signing and pushing. A
        # fresh read-only check is the last point at which a changed identity
        # can stop the external forge write.
        try:
            _status, target = await self._preflight_target(task)
        except GitHubPreflightError as exc:
            set_disposition(
                self._engine, delivery.id, "blocked", blocked_reason=str(exc)
            )
            return False
        assert target.target is not None
        slug = target.target.slug
        marker = _extract_marker(delivery.pr_body or "")
        expected = {
            "slug": slug,
            "base": destination["base_branch"],
            "head": destination["head"],
            "marker": marker,
            "title": delivery.pr_title,
        }
        try:
            action = prepare_action(
                self._engine,
                delivery.id,
                kind="pr",
                request_key=f"{request_id}:pr",
                input_fingerprint=_fingerprint(expected),
                expected=expected,
                identity=delivery.identity,
                workflow_seq=workflow_seq,
            )
        except DeliveryConflictError as exc:
            set_disposition(
                self._engine, delivery.id, "blocked", blocked_reason=str(exc)
            )
            return False

        mark_action_executing(self._engine, action.id)
        self._refresh(task.id)
        try:
            url = await self._create_pr(
                slug,
                destination["base_branch"],
                destination["head"],
                delivery.pr_title or "",
                delivery.pr_body or "",
            )
        except PullRequestError as exc:
            return await self._classify_pr_failure(
                task, action.id, expected, str(exc)
            )
        except Exception as exc:  # noqa: BLE001
            return await self._classify_pr_failure(
                task, action.id, expected, str(exc)
            )

        self._attach_pr(
            task, action.id, {**expected, "url": url, "adopted": False}, settle=settle
        )
        return True

    def _attach_pr(
        self,
        task: Task,
        action_id: int,
        result: dict[str, Any],
        *,
        settle: Settle | None = None,
    ) -> None:
        """Land the PR identity on the task and the action together.

        One transaction is not available across two registries, so the ordering
        is: the action's verified result first, then the task's `pr_url`. A
        crash between them leaves a recorded successful PR whose URL the next
        startup reattaches, never a polled task with no evidence behind it.
        """
        self._land(action_id, result=result, settle=settle)
        updated = mark_pr_url(self._engine, task.id, result["url"])
        self._hub.publish("task_updated", task_payload(updated, engine=self._engine))

    async def _create_pr(
        self, slug: str, base_branch: str, head: str, title: str, body: str
    ) -> str:
        def write_body() -> str:
            # Outside the clone: a body file inside it would show up as an
            # untracked file and could be captured into a later candidate.
            fd, path = tempfile.mkstemp(prefix="ompire-pr-body-", suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            return path

        body_path = await asyncio.to_thread(write_body)
        try:
            result = await self._gh.run(
                [
                    "pr",
                    "create",
                    "--repo",
                    slug,
                    "--base",
                    base_branch,
                    "--head",
                    head,
                    "--title",
                    title,
                    "--body-file",
                    body_path,
                ],
                str(self._config.data_dir),
                self._config.spawn_step_timeout,
            )
        finally:
            await asyncio.to_thread(Path(body_path).unlink)

        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}"
            url = _find_pr_url(combined)
            if url:
                return url
            raise PullRequestError(
                result.stderr.strip()
                or result.stdout.strip()
                or f"gh exited {result.returncode}"
            )
        url = _find_pr_url(result.stdout)
        if url:
            return url
        raise PullRequestError("gh pr create succeeded but printed no PR URL")

    async def _classify_pr_failure(
        self, task: Task, action_id: int, expected: dict[str, Any], message: str
    ) -> bool:
        """Look for the exact correlated pull request before calling it a
        failure. Absence after an uncertain write is not proof of absence.

        Returns whether the action ended up succeeding: a create whose reply was
        lost still created the pull request, and adopting it is the delivery
        completing rather than a recovery from a failure.
        """
        found, complete = await self._find_correlated_pr(expected)
        if found is not None:
            self._attach_pr(task, action_id, {**expected, **found, "adopted": True})
            return True
        if complete:
            fail_action(
                self._engine, action_id, error=f"pull request failed: {message}"
            )
            return False
        flag_action_unresolved(
            self._engine,
            action_id,
            error=(
                f"the pull request could not be created ({message}) and the "
                "forge could not be searched completely, so Ompire cannot tell "
                "whether one was opened"
            ),
            evidence={
                "state": "unknown",
                "marker": expected.get("marker"),
                "detail": (
                    "the correlated search of every pull-request state could "
                    "not be completed"
                ),
            },
        )
        self._guard.block(task.id, "a pull-request creation has an unknown outcome")
        return False

    async def _find_correlated_pr(
        self, expected: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        """`(match, search_was_complete)` for the authorized correlation marker.

        Every state is searched, because a pull request that was created and
        immediately closed is still a pull request Ompire created. Exactly one
        verified match is adoptable; zero with a complete search is a real
        absence; anything else — an ambiguous match, an unavailable forge, a
        truncated page — leaves the outcome unknown.
        """
        marker = expected.get("marker")
        if not marker:
            return None, False
        result = await self._gh.run(
            [
                "pr",
                "list",
                "--repo",
                str(expected["slug"]),
                "--state",
                "all",
                "--base",
                str(expected["base"]),
                "--head",
                str(expected["head"]),
                "--limit",
                str(_PR_LOOKUP_LIMIT),
                "--json",
                "number,url,body,state,headRefName,baseRefName",
            ],
            str(self._config.data_dir),
            self._config.spawn_step_timeout,
        )
        if result.returncode != 0:
            return None, False
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None, False
        if not isinstance(entries, list):
            return None, False
        if len(entries) >= _PR_LOOKUP_LIMIT:
            # The page was full: a match could be on the next one.
            return None, False
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and _extract_marker(str(entry.get("body") or "")) == marker
        ]
        if len(matches) == 1:
            match = matches[0]
            return (
                {
                    "url": match.get("url"),
                    "number": match.get("number"),
                    "state": match.get("state"),
                },
                True,
            )
        if len(matches) > 1:
            return None, False
        return None, True

    # --- reconciliation ----------------------------------------------------

    async def reconcile(
        self,
        task: Task,
        *,
        delivery_id: int,
        action_id: int,
        expected_version: int,
        decision: str,
        note: str | None = None,
        adopt_reference: str | None = None,
    ) -> dict[str, Any]:
        """Apply one explicit operator decision to one unresolved attempt.

        None of these write anything privileged. `recheck` observes,
        `adopt` verifies a result the daemon can prove, `retry` only makes a
        proven-not-executed action eligible for a fresh preview and
        confirmation, and `abandon` records that no further authority was
        granted while leaving an unknown effect exactly as unknown.
        """
        delivery = get_delivery(self._engine, delivery_id)
        if delivery is None or delivery.task_id != task.id:
            raise PreviewMismatchError(
                f"delivery {delivery_id} does not belong to task {task.id}"
            )
        if delivery.version != expected_version:
            raise PreviewMismatchError(
                f"delivery changed since it was displayed (version "
                f"{delivery.version}, expected {expected_version})"
            )
        action = next((a for a in delivery.actions if a.id == action_id), None)
        if action is None:
            raise PreviewMismatchError(
                f"delivery action {action_id} does not belong to delivery {delivery_id}"
            )
        if action.phase not in ("needs_reconciliation", "executing"):
            raise PreviewMismatchError(
                f"delivery action {action_id} is {action.phase} and needs no decision"
            )

        observation = await self._observe(task, delivery, action)
        if decision == "recheck":
            if observation["state"] == "completed":
                self._adopt(task, delivery, action, observation, note)
            else:
                append_decision(
                    self._engine,
                    delivery.id,
                    kind="recheck",
                    action_id=action.id,
                    detail=observation,
                    note=note,
                )
        elif decision == "adopt":
            if adopt_reference and observation.get("reference") != adopt_reference:
                observation = await self._observe(
                    task, delivery, action, reference=adopt_reference
                )
            if observation["state"] != "completed":
                raise UnresolvedEffectError(
                    "Ompire could not verify that result, so it will not adopt "
                    f"it: {observation.get('detail') or observation['state']}"
                )
            self._adopt(task, delivery, action, observation, note)
        elif decision == "retry":
            if observation["state"] != "not-executed":
                raise UnresolvedEffectError(
                    "this action cannot be retried until Ompire can prove it did "
                    f"not happen: {observation.get('detail') or observation['state']}"
                )
            resolve_action(
                self._engine,
                action.id,
                phase="failed",
                error=action.error,
                disposition="blocked",
                blocked_reason=(
                    "the interrupted action is proven not to have happened; "
                    "confirm a fresh preview to try again"
                ),
                decision="retry",
                note=note,
                detail=observation,
            )
            self._guard.unblock(task.id)
        elif decision == "abandon":
            still_unknown = observation["state"] not in ("completed", "not-executed")
            resolve_action(
                self._engine,
                action.id,
                phase="needs_reconciliation" if still_unknown else "failed",
                error=action.error,
                disposition="abandoned" if not still_unknown else "unresolved",
                blocked_reason=action.error if still_unknown else None,
                decision="abandon",
                note=note,
                detail=observation,
            )
            if not still_unknown:
                self._guard.unblock(task.id)
        else:
            raise PreviewMismatchError(f"unknown reconciliation decision {decision!r}")

        projection = self.publish(task)
        assert projection is not None
        return projection

    def _adopt(
        self,
        task: Task,
        delivery: DeliveryRecord,
        action,
        observation: dict[str, Any],
        note: str | None,
    ) -> None:
        result = {**(action.expected or {}), **(observation.get("result") or {})}
        if action.kind == "pr" and result.get("url"):
            resolve_action(
                self._engine,
                action.id,
                phase="succeeded",
                result={**result, "adopted": True},
                error=None,
                disposition="authorized",
                decision="adopt",
                note=note,
                detail=observation,
            )
            updated = mark_pr_url(self._engine, task.id, result["url"])
            self._hub.publish(
                "task_updated", task_payload(updated, engine=self._engine)
            )
        else:
            resolve_action(
                self._engine,
                action.id,
                phase="succeeded",
                result={**result, "adopted": True},
                error=None,
                disposition="authorized",
                decision="adopt",
                note=note,
                detail=observation,
            )
        self._guard.unblock(task.id)
        self._settle_after_adoption(delivery.id)

    def _settle_after_adoption(self, delivery_id: int) -> None:
        """Close out a delivery whose unresolved action turned out to have
        succeeded.

        If the adopted result completes the selected ending, the delivery is
        finished. If actions remain, they are *not* resumed here: recovering
        from an unknown effect is not authorization to keep writing, so the
        delivery waits for a fresh preview and confirmation.
        """
        record = get_delivery(self._engine, delivery_id)
        if record is None or record.disposition != "authorized":
            return
        if record.remaining_actions:
            set_disposition(
                self._engine,
                delivery_id,
                "blocked",
                blocked_reason=(
                    "the interrupted action is resolved; confirm the remaining "
                    "actions to continue"
                ),
            )
        else:
            set_disposition(self._engine, delivery_id, "completed")
            self.release_candidate_storage(record.task_id)

    async def _observe(
        self,
        task: Task,
        delivery: DeliveryRecord,
        action,
        reference: str | None = None,
    ) -> dict[str, Any]:
        """Look for the specific result this attempt intended to produce.

        Read-only against every external system. Each action kind has its own
        evidence, and every one of them can answer "unknown" — which is a
        result, not a failure to reach one.
        """
        expected = action.expected or {}
        timeout = self._config.spawn_step_timeout
        if action.kind == "commit":
            return await self._observe_commit(task, delivery, expected, timeout)
        if action.kind == "push":
            try:
                head = await self._remote_head(
                    task.clone_path,
                    str(expected.get("remote_url")),
                    str(expected.get("ref")),
                    timeout,
                )
            except PushError as exc:
                return {"state": "unknown", "detail": str(exc)}
            if head == expected.get("signed_tip"):
                return {
                    "state": "completed",
                    "reference": head,
                    "result": {"head": head},
                }
            if head == expected.get("pre_push_oid"):
                return {
                    "state": "not-executed",
                    "reference": head,
                    "detail": (
                        "the destination still holds what it held before the "
                        "push was attempted"
                    ),
                }
            return {
                "state": "conflict",
                "reference": head,
                "detail": (
                    f"the destination holds {(head or 'nothing')[:12]}, which "
                    "Ompire did not authorize overwriting"
                ),
            }
        if action.kind == "pr":
            lookup = dict(expected)
            if reference:
                lookup["marker"] = reference
            found, complete = await self._find_correlated_pr(lookup)
            if found is not None:
                return {
                    "state": "completed",
                    "reference": found.get("url"),
                    "result": found,
                }
            if complete:
                return {
                    "state": "not-executed",
                    "detail": (
                        "no pull request carrying this delivery's marker exists "
                        "on the authorized repository, base, and head"
                    ),
                }
            return {
                "state": "unknown",
                "detail": (
                    "the forge could not be searched completely, so Ompire "
                    "cannot tell whether a pull request was created"
                ),
            }
        return {"state": "unknown", "detail": f"unknown action {action.kind!r}"}

    async def _observe_commit(
        self,
        task: Task,
        delivery: DeliveryRecord,
        expected: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        store = Path(str(expected.get("store") or ""))
        signed_ref = expected.get("signed_ref")
        candidate_id = expected.get("candidate_id")
        candidate = (
            get_candidate(self._engine, str(candidate_id)) if candidate_id else None
        )
        if candidate is None or not signed_ref or not (store / "HEAD").exists():
            return {
                "state": "unknown",
                "detail": (
                    "the protected candidate storage for this attempt is no "
                    "longer available, so its result cannot be verified"
                ),
            }
        stdout, _err, code = await run_git(
            ["git", "-C", str(store), "rev-parse", "--verify", "--quiet", str(signed_ref)],
            cwd=store,
            timeout=timeout,
            step="delivery-observe-signed",
            check=False,
        )
        if code != 0:
            return {
                "state": "not-executed",
                "detail": (
                    "no signature was produced: the attempt's protected ref does "
                    "not exist, and it is written before any signature can return"
                ),
            }
        tip = stdout.strip()
        try:
            await self._verify_signed(
                store,
                tip,
                candidate,
                str(expected.get("mode") or delivery.mode or "squash"),
                str(expected.get("signing_key")),
                timeout,
                # Reconciliation re-derives the policy from the task, so a
                # restart mid-delivery cannot resume under a weaker one.
                protected_destinations(task),
            )
        except (DeliveryWorkspaceError, GitCommandError) as exc:
            return {
                "state": "partial",
                "reference": tip,
                "detail": (
                    f"a partial signed result exists at {tip[:12]} but does not "
                    f"verify as the authorized content: {exc}"
                ),
            }
        installed = False
        try:
            current = (
                await git_out(
                    task.clone_path,
                    ["rev-parse", "HEAD"],
                    timeout=timeout,
                    step="delivery-observe-head",
                )
            ).strip()
            installed = current == tip
        except (DeliveryWorkspaceError, GitCommandError):
            current = None
        return {
            "state": "completed",
            "reference": tip,
            "result": {
                "signed_tip": tip,
                "installed": installed,
                "clone_head": current,
            },
            "detail": (
                "a complete, verified signed result exists"
                + ("" if installed else "; it is not installed in the task clone")
            ),
        }

    # --- startup -----------------------------------------------------------

    async def restore(self) -> list[int]:
        """Reconcile interrupted deliveries before the task is writable again.

        Performs no signing, no push, and no forge write. It reads what was
        attempted, does bounded local Git observation, and either records a
        proven result or marks the task blocked with the evidence attached. Any
        remaining work needs an explicit continuation afterwards.
        """
        blocked: list[int] = []
        for delivery in list_unresolved_deliveries(self._engine):
            try:
                task = get_task(self._engine, delivery.task_id)
            except Exception:  # noqa: BLE001 — purged while the daemon was down
                logger.info(
                    "delivery %d has no task; skipping restore", delivery.id
                )
                continue
            for action in delivery.unresolved_actions:
                reason = await self._restore_action(task, delivery, action)
                if reason is not None:
                    self._guard.block(task.id, reason)
                    blocked.append(task.id)
            record = get_delivery(self._engine, delivery.id)
            if record is not None and record.disposition == "authorized":
                if record.remaining_actions:
                    # Authorized work that never finished. It is not resumed on
                    # the operator's behalf: a fresh preview and confirmation
                    # decide whether the rest still applies.
                    set_disposition(
                        self._engine,
                        delivery.id,
                        "blocked",
                        blocked_reason=(
                            "the daemon restarted before this delivery finished; "
                            "confirm the remaining actions to continue"
                        ),
                    )
                else:
                    set_disposition(self._engine, delivery.id, "completed")
                    self.release_candidate_storage(task.id)
            # A draft interrupted mid-turn is a retryable interruption, never a
            # new agent turn started on the operator's behalf.
            if record is not None and (record.draft or {}).get("state") == "drafting":
                save_draft(
                    self._engine,
                    record.id,
                    {
                        **(record.draft or {}),
                        "state": "interrupted",
                        "error": "the daemon restarted while the agent was drafting",
                    },
                )
            # Broadcast what reconciliation concluded, so a client connected
            # through the restart converges without waiting for a new snapshot.
            self.publish(task)
        return blocked

    async def _restore_action(
        self, task: Task, delivery: DeliveryRecord, action
    ) -> str | None:
        """Classify one interrupted attempt. Returns a block reason, or None."""
        observation = await self._observe(task, delivery, action)
        state = observation["state"]
        if state == "completed":
            resolve_action(
                self._engine,
                action.id,
                phase="succeeded",
                result={
                    **(action.expected or {}),
                    **(observation.get("result") or {}),
                    "adopted": True,
                },
                error=None,
                disposition="authorized",
                decision="recheck",
                note=None,
                detail=observation,
            )
            if action.kind == "pr":
                url = (observation.get("result") or {}).get("url")
                if url:
                    updated = mark_pr_url(self._engine, task.id, url)
                    self._hub.publish(
                        "task_updated", task_payload(updated, engine=self._engine)
                    )
            return None
        if state == "not-executed":
            resolve_action(
                self._engine,
                action.id,
                phase="failed",
                error=(
                    "interrupted by a daemon restart; the effect is proven not "
                    "to have happened"
                ),
                disposition="blocked",
                blocked_reason=(
                    "the daemon restarted before this action ran; confirm a "
                    "fresh preview to try again"
                ),
                decision="recheck",
                note=None,
                detail=observation,
            )
            return None
        detail = observation.get("detail") or "the outcome could not be established"
        flag_action_unresolved(
            self._engine,
            action.id,
            error=f"interrupted by a daemon restart: {detail}",
            evidence=observation,
        )
        return f"an interrupted {action.kind} action has an unknown outcome: {detail}"

    # --- git configuration helpers -----------------------------------------

    async def _git_config(self, clone_path: str, key: str) -> str | None:
        stdout, _stderr, code = await run_git(
            safe_git(clone_path, "config", "--get", key),
            cwd=clone_path,
            timeout=10,
            step=f"git-config-{key.replace('.', '-')}",
            check=False,
        )
        value = stdout.strip()
        return value if code == 0 and value else None

    async def _signing_config(self) -> list[str]:
        """Command-local signing settings taken from operator-owned config.

        Read from the operator's global/system Git configuration rather than
        the clone's, so the agent cannot choose the signing program, and pass
        the result explicitly so any local value is overridden.
        """
        program = (
            await self._operator_git_config("gpg.program") or _DEFAULT_SIGNING_PROGRAM
        )
        return [
            "-c",
            f"gpg.format={_SIGNING_FORMAT}",
            "-c",
            f"gpg.program={program}",
        ]

    async def _operator_git_config(self, key: str) -> str | None:
        """Read `key` from operator-owned Git config only, never a clone's."""
        for scope in ("--global", "--system"):
            stdout, _stderr, code = await run_git(
                ["git", "config", scope, "--get", key],
                cwd=tempfile.gettempdir(),
                timeout=10,
                step="operator-git-config",
                check=False,
            )
            if code == 0 and stdout.strip():
                return stdout.strip()
        return None

    # --- legacy clone recovery ---------------------------------------------

    @staticmethod
    async def restore_parked_clone(clone_path: str, timeout: int) -> str:
        """Restore a clone parked by the superseded reset dance.

        Returns `absent` when there is nothing to restore, `restored` when the
        clone verifiably came back to its parked head, and `unsafe` when a ref
        survives that could not be restored. The three are deliberately
        distinct: "no legacy ref" and "a legacy ref Ompire could not honour"
        look identical from a boolean, and only the second is a reason to stop
        working on the task.

        New deliveries never park the clone — signing happens in the
        candidate's own repository — but a clone left by an older daemon still
        carries `refs/ompire/ship-orig`, and its work is only recoverable
        through it. The marker is removed only once restoration verifies.
        """
        _out, _err, code = await run_git(
            safe_git(clone_path, "rev-parse", "--verify", "--quiet", _SHIP_GIT_REF),
            cwd=clone_path,
            timeout=timeout,
            step="ship-legacy-ref-check",
            check=False,
        )
        if code != 0:
            return "absent"
        parked = _out.strip()
        _out2, _err2, reset_code = await run_git(
            safe_git(clone_path, "reset", "--soft", _SHIP_GIT_REF),
            cwd=clone_path,
            timeout=timeout,
            step="ship-legacy-restore",
            check=False,
        )
        if reset_code != 0:
            logger.warning(
                "clone %s carries a legacy ship-orig ref that could not be "
                "restored; leaving it in place",
                clone_path,
            )
            return "unsafe"
        head, _err3, head_code = await run_git(
            safe_git(clone_path, "rev-parse", "HEAD"),
            cwd=clone_path,
            timeout=timeout,
            step="ship-legacy-verify",
            check=False,
        )
        if head_code != 0 or head.strip() != parked:
            logger.warning(
                "clone %s did not restore to its parked head; leaving the "
                "legacy ref in place",
                clone_path,
            )
            return "unsafe"
        await run_git(
            safe_git(clone_path, "update-ref", "-d", _SHIP_GIT_REF),
            cwd=clone_path,
            timeout=timeout,
            step="ship-legacy-delete-ref",
            check=False,
        )
        return "restored"


# --- module-level helpers --------------------------------------------------


def _task_revision(task: Task) -> str | None:
    """The workflow revision this task was accepted under (ADR-0028).

    Attribution, not policy: a delivery says which pinned procedure produced the
    work it published, and that stays true after the library moves on. None for
    a task whose launch configuration was never confirmed.
    """
    inputs = task.execution_inputs
    binding = inputs.workflow_binding if inputs is not None else None
    return binding.revision if binding is not None else None


def _identity_env(git_identity: dict[str, Any]) -> dict[str, str]:
    """Author and committer for a signed result.

    Ompire signs as the operator, so both roles carry the same configured
    identity — which is what makes a retain rewrite's `%GF` check meaningful
    rather than merely preserving whoever the agent claimed to be.
    """
    env: dict[str, str] = {}
    name = git_identity.get("name")
    email = git_identity.get("email")
    if name:
        env["GIT_AUTHOR_NAME"] = str(name)
        env["GIT_COMMITTER_NAME"] = str(name)
    if email:
        env["GIT_AUTHOR_EMAIL"] = str(email)
        env["GIT_COMMITTER_EMAIL"] = str(email)
    return env


def _extract_marker(body: str) -> str | None:
    match = _MARKER_RE.search(body)
    return match.group(1) if match else None


def _parse_draft(text: str) -> ShipDraft | None:
    markers = ["<<<COMMIT_MESSAGE>>>", "<<<PR_TITLE>>>", "<<<PR_BODY>>>"]
    sections: dict[str, str] = {}
    for marker in markers:
        idx = text.find(marker)
        if idx == -1:
            return None
        start = idx + len(marker)
        next_marker = len(text)
        for other in markers:
            nxt = text.find(other, start)
            if nxt != -1 and nxt < next_marker:
                next_marker = nxt
        sections[marker] = text[start:next_marker].strip()

    return ShipDraft(
        commit_message=sections["<<<COMMIT_MESSAGE>>>"],
        pr_title=sections["<<<PR_TITLE>>>"],
        pr_body=sections["<<<PR_BODY>>>"],
    )


def _find_pr_url(text: str) -> str | None:
    match = _PR_URL_RE.search(text)
    return match.group(0) if match else None


def new_request_id() -> str:
    return uuid.uuid4().hex
