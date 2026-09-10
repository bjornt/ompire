"""The launch application boundary: preview and acceptance as typed commands.

`LaunchService` is the one way a launch is resolved, reviewed, and accepted —
transport-independent, callable in process with a typed `LaunchRequest`. HTTP
routes convert their wire body to that request and map the domain errors this
module raises; they do not participate in the acceptance transaction, the
filesystem observation, or the supervision of preparation jobs.

The acceptance algorithm is unchanged from the route it was extracted from,
and its ordering is load-bearing:

1. everything slow — the Git reading for an attachment launch, mention
   validation, path checks — runs first, against a resolution made outside
   any lock;
2. the same rules run again on a `BEGIN IMMEDIATE` connection, the reviewed
   token is compared against what they now produce, and the task plus its
   pinned inputs *and* its result-consumer references are inserted on that
   one connection before the reservation is released;
3. nothing is published or scheduled until that transaction has committed, so
   a refused or stale submission leaves no task, reference, workspace, agent,
   or background job.

The service owns no FastAPI object, reads no `app.state`, and raises domain
errors (`LaunchInputError`, `PreviewChangedError`, `DuplicateTaskError`,
`ProjectFilesError`, `HandoffError`, ...) rather than HTTP responses. The
two post-commit effects — republishing producer result projections and
scheduling the spawn pipeline — are injected collaborators, because
scheduling and notification are real effect seams, not lookups.

ADR-0026, ADR-0028, ADR-0035.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy import Connection, Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.handoff import observe_base_difference, observe_target
from ompire_daemon.oversight.tasks import task_payload
from ompire_daemon.platform.transactions import reserved_write
from ompire_daemon.registry.results import (
    DamagedManifestError,
    insert_references_on,
    read_result_on,
)
from ompire_daemon.spawn import run_spawn_pipeline
from ompire_daemon.work.files import validate_mentions
from ompire_daemon.work.launch import (
    LaunchInputError,
    LaunchRequest,
    PreviewChangedError,
    ResolvedLaunch,
    TargetEvidence,
    resolve_launch,
    resolve_target_context,
)
from ompire_daemon.work.tasks import Task, clone_path_for, create_task

logger = logging.getLogger(__name__)


class LaunchMentionsRejectedError(ValueError):
    """A prompt `@file` mention does not resolve against the accepted base
    branch and checkout. Omp would drop it silently, so the launch is refused
    before anything is created."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"prompt file mention rejected — {detail}")
        self.detail = detail


class SpawnScheduler:
    """Owns the lifetime of accepted tasks' asynchronous preparation jobs.

    Holds strong references so a job cannot be garbage-collected mid-pipeline
    (an `asyncio.Task` nobody references can be), removes each one on
    completion, and cancels every live job at shutdown. It admits and tracks
    jobs; it never constructs pipelines or knows what they do — the caller
    hands it the coroutine to run, which is what keeps scheduling a narrow
    effect seam rather than a dependency on the spawn pipeline.

    The same scheduler serves delivery jobs: they need the identical
    lifetime guarantee (strong reference, completion removal, shutdown
    cancellation) and share the one shutdown path in the app lifespan.
    """

    def __init__(self) -> None:
        self.jobs: set[asyncio.Task[None]] = set()

    def submit(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        job = asyncio.create_task(coro)
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return job

    async def shutdown(self) -> None:
        """Cancel every live job and wait for it to finish cancelling."""
        live = list(self.jobs)
        for job in live:
            job.cancel()
        if live:
            await asyncio.gather(*live, return_exceptions=True)


class LaunchService:
    """Preview and accept launches against the shared resolution rules."""

    def __init__(
        self,
        engine: Engine,
        config: Config,
        *,
        events: EventHub,
        scheduler: SpawnScheduler,
        workflow_runner: Any,
        result_notifier: Callable[[int], None],
    ) -> None:
        self._engine = engine
        self._config = config
        self._events = events
        self._scheduler = scheduler
        self._workflow_runner = workflow_runner
        # Republishing a producer's result projection after a consumer's
        # references move (`ResultManager.publish`). Injected because it is
        # the retained-results owner's operation, not launch's.
        self._notify_result_projection = result_notifier

    async def preview(self, request: LaunchRequest) -> ResolvedLaunch:
        """Resolve the operator's selections without creating anything.

        Same rules, same module, and same output as acceptance — that identity
        is what makes reviewing a preview worth anything. Preview acquires no
        acceptance-only checks and schedules nothing.
        """
        evidence = await self._target_evidence(request)
        with self._engine.connect() as conn:
            return self._resolve(conn, request, evidence)

    async def accept(self, request: LaunchRequest, *, preview_token: str) -> Task:
        """Accept one reviewed launch: record the task and its pinned inputs,
        then schedule preparation.

        Nothing observable — event, clone, container, agent, or job — exists
        until the acceptance transaction has committed. A refusal at any
        earlier point leaves nothing behind.
        """
        evidence = await self._target_evidence(request)

        # First resolution: validation only. Its results are what the Git work
        # below is done against; the authoritative one is taken again under
        # the write reservation.
        with self._engine.connect() as conn:
            resolved = self._resolve(conn, request, evidence)
        if resolved.fingerprint != preview_token:
            raise PreviewChangedError(resolved)

        # Mentions are validated before anything is created: Omp drops one it
        # cannot resolve without a word, so a mention that will not survive
        # into the clone must be refused here, not discovered after the
        # workspace is built. Against the *accepted* base branch and checkout,
        # not today's project defaults.
        rejections = await validate_mentions(
            request.prompt,
            checkout_path=resolved.inputs.checkout_path,
            base_branch=resolved.inputs.workspace.base_branch,
            timeout=self._config.spawn_step_timeout,
            # An attached destination is a file the clone *will* contain by
            # the time the agent runs, so a mention naming one resolves
            # (ADR-0035). Everything else keeps its existing refusal.
            extra_paths=resolved.inputs.protected_destinations,
        )
        if rejections:
            raise LaunchMentionsRejectedError(
                "; ".join(rejection.message() for rejection in rejections)
            )

        clone_path = clone_path_for(
            self._config.task_dir_root, resolved.inputs.project_name, request.slug
        )

        # The consistency boundary. `BEGIN IMMEDIATE` up front means the
        # re-read cannot go stale before the insert commits, and nothing
        # awaited, spawned, or published happens inside it.
        with reserved_write(self._engine) as conn:
            final = self._resolve(conn, request, evidence)
            if final.fingerprint != preview_token:
                raise PreviewChangedError(final)
            task = create_task(
                self._engine,
                project_name=final.inputs.project_name,
                slug=request.slug,
                branch=final.inputs.branch,
                clone_path=str(clone_path),
                prompt=request.prompt,
                execution_inputs=final.inputs,
                workflow_name=final.inputs.workflow_name,
                conn=conn,
            )
            # Same transaction as the task and its pinned inputs (ADR-0035).
            # Accepting the launch and reserving the bytes it depends on are
            # one durable operation: a purge racing this either loses the
            # reservation and is refused, or wins and leaves no consumer
            # behind.
            pinned_producers = insert_references_on(
                conn,
                consumer_task_id=task.id,
                references=[
                    (
                        attachment.result_id,
                        attachment.producer_task_id,
                        attachment.manifest_id,
                    )
                    for attachment in final.inputs.result_attachments
                ],
            )

        # After the commit: a producer's reverse-dependency list moved, and a
        # purge dialog open elsewhere has to converge without a refresh.
        for producer_task_id in pinned_producers:
            self._notify_result_projection(producer_task_id)

        self._events.publish("task_created", task_payload(task, engine=self._engine))
        self._scheduler.submit(
            run_spawn_pipeline(
                self._engine,
                self._events,
                self._config,
                task.id,
                self._workflow_runner,
            )
        )
        return task

    def _resolve(
        self,
        conn: Connection,
        request: LaunchRequest,
        evidence: TargetEvidence | None,
    ) -> ResolvedLaunch:
        """Resolve on an open connection, refusing inadmissible selections.

        Applies to every caller alike — an in-process command cannot skip the
        selection, profile, or workflow checks the HTTP path runs.
        """
        self._refuse_empty_profile_overrides(request)
        return resolve_launch(conn, request, evidence=evidence)

    @staticmethod
    def _refuse_empty_profile_overrides(request: LaunchRequest) -> None:
        """An empty profile name is refused rather than read as a reset — a
        reset is expressed by omitting the override. The same rule the wire
        adapter applies to submitted rows applies to direct callers."""
        for field, overrides in (
            ("step_overrides", request.step_overrides),
            ("auxiliary_overrides", request.auxiliary_overrides),
        ):
            for name, override in overrides.items():
                if override.model_profile is not None and not override.model_profile.strip():
                    raise LaunchInputError(
                        f"{field}.{name}.model_profile",
                        "a model profile name must not be empty; omit the "
                        "field to inherit",
                    )

    async def _target_evidence(
        self, request: LaunchRequest
    ) -> TargetEvidence | None:
        """Read the target base for an attachment launch, outside any lock.

        Returns None when nothing is attached, which is what keeps an ordinary
        launch on exactly its previous path: no commit is resolved, no tree is
        read, and nothing new can refuse it.

        A revision that cannot be read here contributes no destinations. It is
        not refused *here* — resolution owns that refusal, and it has to be the
        same refusal in the preview and in the acceptance.
        """
        if not request.result_attachments:
            return None
        with self._engine.connect() as conn:
            checkout_path, base_branch = resolve_target_context(conn, request)
            observations: dict[str, str | None] = {}
            destinations: set[str] = set()
            for selection in request.result_attachments:
                result = read_result_on(conn, selection.result_id)
                if result is None or result.manifest is None:
                    continue
                try:
                    paths = [entry.path for entry in result.files]
                except DamagedManifestError:
                    continue
                destinations.update(paths)
                provenance = result.manifest.get("provenance") or {}
                observations[selection.result_id] = provenance.get(
                    "capture_merge_base"
                )

        timeout = self._config.spawn_step_timeout
        observation = await observe_target(
            checkout_path=checkout_path,
            base_branch=base_branch,
            destinations=tuple(sorted(destinations)),
            timeout=timeout,
        )
        comparisons = {
            result_id: await observe_base_difference(
                result_id=result_id,
                checkout_path=checkout_path,
                target_commit=observation.commit,
                producer_observation=producer_observation,
                timeout=timeout,
            )
            for result_id, producer_observation in observations.items()
        }
        return TargetEvidence(
            commit=observation.commit,
            conflicts=tuple(
                (conflict.reason, conflict.detail, conflict.path)
                for conflict in observation.conflicts
            ),
            comparisons=comparisons,
        )
