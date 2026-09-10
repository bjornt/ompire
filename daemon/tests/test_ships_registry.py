"""Tests for `ompire_daemon.registry.ships`: the durable delivery journal.

These are the invariants the recovery and admission rules stand on — one
delivery at a time, replay is not a second authorization, a result and the
eligibility it grants land together, and history is never rewritten. They are
exercised against the registry directly, because every one of them has to hold
for a direct service caller and not only for a REST request.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ompire_daemon.db import db_path_for, ensure_db_dir, make_engine
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.ships import (
    AuthorityBoundary,
    DeliveryConflictError,
    SourceCommit,
    append_decision,
    authority_boundary,
    authorize_delivery,
    complete_action,
    delete_task_deliveries,
    extend_delivery,
    fail_action,
    flag_action_unresolved,
    get_active_delivery,
    get_candidate,
    get_delivery,
    get_latest_delivery,
    is_pre_upgrade_grant,
    list_deliveries,
    list_unresolved_deliveries,
    mark_action_executing,
    open_delivery,
    prepare_action,
    reauthorize_delivery,
    record_action_progress,
    record_candidate,
    resolve_action,
    task_version,
)
from ompire_daemon.work.projects import create_project
from ompire_daemon.work.tasks import create_task, mark_archived
from tests.conftest import make_execution_inputs


@pytest.fixture
def engine(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(data_dir)
    ensure_db_dir(db_path)
    upgrade_head(db_path)
    return make_engine(db_path)


@pytest.fixture
def task(engine, tmp_path: Path):
    checkout = tmp_path / "proj" / "demo"
    checkout.mkdir(parents=True, exist_ok=True)
    create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://github.com/owner/repo",
        fork_url=None,
        checkout_path=str(checkout),
        default_checkout_root=tmp_path / "proj",
    )
    return create_task(
        engine,
        project_name="demo",
        slug="task-1",
        branch="ompire/task-1",
        clone_path=str(tmp_path / "tasks" / "demo" / "task-1"),
        prompt="do the thing",
        execution_inputs=make_execution_inputs(
            checkout_path=str(checkout),
            project_name="demo",
            branch="ompire/task-1",
        ),
    )


def _candidate(engine, task, candidate_id: str = "cand-1"):
    return record_candidate(
        engine,
        candidate_id=candidate_id,
        task_id=task.id,
        base_branch="main",
        base_commit="b" * 40,
        original_head="h" * 40,
        tree_id="t" * 40,
        source_commits=(
            SourceCommit(
                commit_id="c" * 40,
                tree_id="t" * 40,
                message="first",
                parent_ids=("b" * 40,),
            ),
        ),
        dirty=False,
        storage_path="/tmp/ompire-candidate.git",
    )


def _authorize(engine, task, delivery, *, ending="pr", request_key="req-1"):
    return authorize_delivery(
        engine,
        delivery.id,
        expected_version=delivery.version,
        candidate_id="cand-1",
        review_candidate_id="cand-1",
        mode="squash",
        ending=ending,
        commit_message="ship: it",
        pr_title="It",
        pr_body="why",
        routing={"ref": "refs/heads/ompire/task-1"},
        identity={"signing": {"fingerprint": "F" * 40}},
        request_key=request_key,
        input_fingerprint="fp-1",
    )


def test_a_candidate_is_idempotent_by_content_identity(engine, task) -> None:
    first = _candidate(engine, task)
    again = _candidate(engine, task)
    assert again.candidate_id == first.candidate_id
    assert again.created_at == first.created_at
    stored = get_candidate(engine, first.candidate_id)
    assert stored is not None
    assert stored.commit_count == 1
    assert stored.source_commits[0].message == "first"


def test_only_one_delivery_is_non_terminal_per_task(engine, task) -> None:
    first = open_delivery(engine, task.id)
    again = open_delivery(engine, task.id)
    assert again.id == first.id
    assert get_active_delivery(engine, task.id).id == first.id

    _candidate(engine, task)
    authorized = _authorize(engine, task, first, ending="commit")
    action = prepare_action(
        engine,
        authorized.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={"signed_ref": "refs/ompire/signed/1"},
    )
    complete_action(
        engine, action.id, result={"signed_tip": "s" * 40}, disposition="completed"
    )

    # A completed delivery is terminal, so the next one is genuinely new.
    fresh = open_delivery(engine, task.id)
    assert fresh.id != first.id
    assert fresh.version > authorized.version
    assert [record.id for record in list_deliveries(engine, task.id)] == [
        first.id,
        fresh.id,
    ]


def test_an_exact_replay_returns_the_same_authorization(engine, task) -> None:
    _candidate(engine, task)
    delivery = open_delivery(engine, task.id)
    first = _authorize(engine, task, delivery)
    replay = authorize_delivery(
        engine,
        delivery.id,
        expected_version=first.version,
        candidate_id="cand-1",
        review_candidate_id="cand-1",
        mode="squash",
        ending="pr",
        commit_message="ship: it",
        pr_title="It",
        pr_body="why",
        routing={"ref": "refs/heads/ompire/task-1"},
        identity={},
        request_key="req-1",
        input_fingerprint="fp-1",
    )
    assert replay.version == first.version
    assert replay.authorized_at == first.authorized_at
    assert [d.kind for d in replay.decisions].count("authorize") == 1


def test_a_conflicting_reuse_of_a_request_key_is_refused(engine, task) -> None:
    _candidate(engine, task)
    delivery = open_delivery(engine, task.id)
    _authorize(engine, task, delivery)
    with pytest.raises(DeliveryConflictError):
        authorize_delivery(
            engine,
            delivery.id,
            expected_version=None,
            candidate_id="cand-1",
            review_candidate_id="cand-1",
            mode="retain",
            ending="pr",
            commit_message="something else entirely",
            pr_title="It",
            pr_body="why",
            routing={},
            identity={},
            request_key="req-1",
            input_fingerprint="a-different-fingerprint",
        )


def test_a_stale_version_cannot_authorize(engine, task) -> None:
    _candidate(engine, task)
    delivery = open_delivery(engine, task.id)
    stale_version = delivery.version
    append_decision(engine, delivery.id, kind="recheck", detail={"note": "moved on"})
    with pytest.raises(DeliveryConflictError):
        authorize_delivery(
            engine,
            delivery.id,
            expected_version=stale_version,
            candidate_id="cand-1",
            review_candidate_id="cand-1",
            mode="squash",
            ending="commit",
            commit_message="m",
            pr_title="",
            pr_body="",
            routing={},
            identity={},
            request_key="req-9",
            input_fingerprint="fp",
        )


def test_a_second_attempt_is_refused_while_the_first_is_unresolved(
    engine, task
) -> None:
    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="push")
    action = prepare_action(
        engine,
        delivery.id,
        kind="push",
        request_key="req-1:push",
        input_fingerprint="fp",
        expected={"ref": "refs/heads/ompire/task-1"},
    )
    mark_action_executing(engine, action.id)
    flag_action_unresolved(
        engine, action.id, error="the response was lost", evidence={"head": None}
    )
    with pytest.raises(DeliveryConflictError, match="unresolved outcome"):
        prepare_action(
            engine,
            delivery.id,
            kind="push",
            request_key="req-2:push",
            input_fingerprint="fp2",
            expected={},
        )
    assert get_delivery(engine, delivery.id).disposition == "unresolved"
    assert [d.id for d in list_unresolved_deliveries(engine)] == [delivery.id]


def test_a_completed_action_is_not_attempted_again(engine, task) -> None:
    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="pr")
    action = prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={},
    )
    updated = complete_action(engine, action.id, result={"signed_tip": "s" * 40})
    assert updated.remaining_actions == ("push", "pr")
    with pytest.raises(DeliveryConflictError, match="already completed"):
        prepare_action(
            engine,
            delivery.id,
            kind="commit",
            request_key="req-2:commit",
            input_fingerprint="fp2",
            expected={},
        )


def test_a_finished_prefix_survives_a_later_failure(engine, task) -> None:
    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="pr")
    commit = prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={},
    )
    complete_action(engine, commit.id, result={"signed_tip": "s" * 40})
    push = prepare_action(
        engine,
        delivery.id,
        kind="push",
        request_key="req-1:push",
        input_fingerprint="fp",
        expected={},
    )
    mark_action_executing(engine, push.id)
    record = fail_action(engine, push.id, error="the remote refused it")

    assert record.succeeded("commit").result["signed_tip"] == "s" * 40
    assert record.action("push").phase == "failed"
    assert record.disposition == "blocked"
    assert record.remaining_actions == ("push", "pr")


def test_partial_signing_progress_is_recorded_rather_than_a_boolean(
    engine, task
) -> None:
    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="commit")
    action = prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={},
    )
    mark_action_executing(engine, action.id)
    record_action_progress(
        engine,
        action.id,
        {"signed": [{"source": "c" * 40, "signed": "s" * 40}], "planned": 3},
    )
    flag_action_unresolved(engine, action.id, error="interrupted")
    stored = get_delivery(engine, delivery.id).action("commit")
    assert stored.progress["planned"] == 3
    assert len(stored.progress["signed"]) == 1


def test_extending_appends_authority_and_never_rewrites_the_first_grant(
    engine, task
) -> None:
    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="commit")
    original_authorized_at = delivery.authorized_at
    commit = prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={},
    )
    delivery = complete_action(
        engine, commit.id, result={"signed_tip": "s" * 40}, disposition="completed"
    )

    extended = extend_delivery(
        engine,
        delivery.id,
        expected_version=delivery.version,
        ending="push",
        pr_title=None,
        pr_body=None,
        request_key="req-2",
        input_fingerprint="fp-2",
        identity={},
    )
    assert extended.ending == "push"
    assert extended.disposition == "authorized"
    assert extended.authorized_at == original_authorized_at
    assert extended.remaining_actions == ("push",)
    kinds = [d.kind for d in extended.decisions]
    assert kinds.count("authorize") == 1
    assert kinds.count("extend") == 1

    # Replaying the extension is not a second grant.
    replay = extend_delivery(
        engine,
        delivery.id,
        expected_version=delivery.version,
        ending="push",
        pr_title=None,
        pr_body=None,
        request_key="req-2",
        input_fingerprint="fp-2",
        identity={},
    )
    assert [d.kind for d in replay.decisions].count("extend") == 1

    # And a narrower ending is not an extension at all.
    with pytest.raises(DeliveryConflictError):
        extend_delivery(
            engine,
            delivery.id,
            expected_version=extended.version,
            ending="commit",
            pr_title=None,
            pr_body=None,
            request_key="req-3",
            input_fingerprint="fp-3",
            identity={},
        )


def test_reconciliation_decisions_are_immutable_history(engine, task) -> None:
    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="push")
    action = prepare_action(
        engine,
        delivery.id,
        kind="push",
        request_key="req-1:push",
        input_fingerprint="fp",
        expected={},
    )
    mark_action_executing(engine, action.id)
    flag_action_unresolved(engine, action.id, error="lost response")
    resolve_action(
        engine,
        action.id,
        phase="succeeded",
        result={"head": "s" * 40},
        error=None,
        disposition="authorized",
        decision="adopt",
        note="verified by hand",
        detail={"observed": "s" * 40},
    )
    record = get_delivery(engine, delivery.id)
    kinds = [d.kind for d in record.decisions]
    assert kinds == ["authorize", "adopt"]
    assert record.decisions[-1].note == "verified by hand"
    assert record.succeeded("push").result["head"] == "s" * 40


def test_the_task_version_is_monotonic_across_deliveries(engine, task) -> None:
    assert task_version(engine, task.id) == 0
    first = open_delivery(engine, task.id)
    _candidate(engine, task)
    authorized = _authorize(engine, task, first, ending="commit")
    action = prepare_action(
        engine,
        authorized.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={},
    )
    complete_action(engine, action.id, result={}, disposition="completed")
    second = open_delivery(engine, task.id)
    versions = [record.version for record in list_deliveries(engine, task.id)]
    assert versions == sorted(versions)
    assert task_version(engine, task.id) == second.version
    assert get_latest_delivery(engine, task.id).id == second.id


def test_purge_deletes_the_journal_and_names_the_storage_to_remove(
    engine, task
) -> None:
    """Cleanup retains all of this; only purge deletes it, and it has to say
    which candidate repositories are now unreferenced — nothing else knows."""
    from ompire_daemon.work.tasks import purge_task

    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="commit")
    action = prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={},
    )
    complete_action(engine, action.id, result={}, disposition="completed")

    mark_archived(engine, task.id)
    # Archival alone keeps the evidence.
    assert list_deliveries(engine, task.id)

    paths, released = purge_task(engine, task.id)
    assert paths == ["/tmp/ompire-candidate.git"]
    # This task pinned no result revisions, so it releases none.
    assert released == []
    assert list_deliveries(engine, task.id) == []
    assert get_candidate(engine, "cand-1") is None


def test_delete_task_deliveries_removes_children_explicitly(engine, task) -> None:
    """Foreign-key cascades cannot be assumed on this connection, so the
    children have to go by name."""
    from sqlalchemy import select

    from ompire_daemon.db import delivery_actions, delivery_decisions

    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="commit")
    prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={},
    )
    delete_task_deliveries(engine, task.id)
    with engine.connect() as conn:
        assert conn.execute(select(delivery_actions)).all() == []
        assert conn.execute(select(delivery_decisions)).all() == []


def test_an_executing_or_unresolved_action_freezes_the_authorization(
    engine, task
) -> None:
    """R4: the selected ending and recorded inputs cannot change in place under
    an executing action. That is what a running effect and its recovery read."""
    _candidate(engine, task)
    delivery = _authorize(engine, task, open_delivery(engine, task.id), ending="commit")
    action = prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-1:commit",
        input_fingerprint="fp",
        expected={},
    )
    mark_action_executing(engine, action.id)

    def replace(version: int):
        return reauthorize_delivery(
            engine,
            delivery.id,
            expected_version=version,
            candidate_id="cand-1",
            review_candidate_id="cand-1",
            mode="retain",
            ending="pr",
            commit_message="something else",
            pr_title="t",
            pr_body="b",
            routing={},
            identity={},
            request_key="req-2",
            input_fingerprint="fp-2",
        )

    current = get_delivery(engine, delivery.id)
    with pytest.raises(DeliveryConflictError, match="executing"):
        replace(current.version)

    flag_action_unresolved(engine, action.id, error="the response was lost")
    current = get_delivery(engine, delivery.id)
    with pytest.raises(DeliveryConflictError, match="needs_reconciliation"):
        replace(current.version)

    # The recorded authorization is exactly as it was.
    unchanged = get_delivery(engine, delivery.id)
    assert unchanged.ending == "commit"
    assert unchanged.mode == "squash"
    assert unchanged.commit_message == "ship: it"


# --- workflow-scoped authority (format 3) ------------------------------------


def test_a_step_performs_its_effect_once_even_under_a_retry(engine, task) -> None:
    """Two prepares for the same delivery step are one attempt, not two.

    The re-driven step is the ordinary case: a restart, a retried dispatch, a
    duplicated call. The uniqueness is enforced under the same reservation as
    the insert, because "check then insert" is exactly the race that produces
    a second signature.
    """
    _candidate(engine, task)
    delivery = open_delivery(engine, task.id)
    _authorize(engine, task, delivery)
    first = prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-a",
        input_fingerprint="fp",
        expected={"ref": "refs/heads/x"},
        workflow_seq=9,
    )
    again = prepare_action(
        engine,
        delivery.id,
        kind="commit",
        request_key="req-a",
        input_fingerprint="fp",
        expected={"ref": "refs/heads/x"},
        workflow_seq=9,
    )
    assert again.id == first.id
    assert first.workflow_seq == 9

    mark_action_executing(engine, first.id)
    complete_action(engine, first.id, result={"commit_id": "c" * 40})
    # A different request identity for the same step is refused rather than
    # allowed to open a second effect for one authorization.
    with pytest.raises(DeliveryConflictError, match="performs its effect once"):
        prepare_action(
            engine,
            delivery.id,
            kind="push",
            request_key="req-b",
            input_fingerprint="fp",
            expected={"ref": "refs/heads/x"},
            workflow_seq=9,
        )


def test_an_authorization_records_the_decision_that_granted_it(engine, task) -> None:
    _candidate(engine, task)
    delivery = open_delivery(engine, task.id)
    granted = authorize_delivery(
        engine,
        delivery.id,
        expected_version=delivery.version,
        candidate_id="cand-1",
        review_candidate_id="cand-1",
        mode="squash",
        ending="pr",
        commit_message="ship: it",
        pr_title="It",
        pr_body="why",
        routing={"ref": "refs/heads/ompire/task-1"},
        identity={},
        request_key="req-w",
        input_fingerprint="fp-w",
        workflow_gate_seq=4,
        workflow_choice_id="publish",
        review_seq=2,
    )
    assert granted.workflow_authorized is True
    assert (granted.workflow_gate_seq, granted.workflow_choice_id) == (4, "publish")
    assert granted.review_seq == 2
    detail = next(d for d in granted.decisions if d.kind == "authorize").detail
    assert detail["workflow_choice_id"] == "publish"


def test_a_manual_grant_is_never_mistaken_for_a_workflow_one(engine, task) -> None:
    _candidate(engine, task)
    delivery = open_delivery(engine, task.id)
    granted = _authorize(engine, task, delivery)
    assert granted.workflow_authorized is False
    assert granted.workflow_gate_seq is None


def test_the_upgrade_boundary_classifies_only_what_predates_it(engine, task) -> None:
    """A new row cannot pass itself off as historical by leaving links unset.

    On a fresh database the boundary is zero, so *nothing* is pre-upgrade —
    which is the correct answer for a daemon that never had legacy grants.
    """
    _candidate(engine, task)
    boundary = authority_boundary(engine)
    assert boundary is not None
    delivery = open_delivery(engine, task.id)
    granted = _authorize(engine, task, delivery)
    assert granted.id > boundary.max_delivery_id
    assert is_pre_upgrade_grant(boundary, granted) is False
    # Pretend this row predates the upgrade: an unauthorized one still is not
    # a grant, and only a genuine authorization below the line counts.
    wide = AuthorityBoundary(
        max_delivery_id=granted.id,
        max_action_id=0,
        recorded_at=boundary.recorded_at,
    )
    assert is_pre_upgrade_grant(wide, granted) is True
    # And a row below the line that was never authorized is not a grant.
    unauthorized = replace(granted, authorized_at=None, ending=None)
    assert is_pre_upgrade_grant(wide, unauthorized) is False
