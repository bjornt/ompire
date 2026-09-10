"""Handoff inputs: the rules a pinned result attachment must satisfy (ADR-0035).

Three boundaries are exercised here, and nothing else: the purely syntactic
destination rules, the classification of a target base reading, and the
filesystem materialization. Launch resolution and the publication refusal have
their own tests — they stand on these, and a rule that is wrong here would be
wrong in every one of them.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from ompire_daemon.handoff import (
    HandoffError,
    MaterializationError,
    TreeEntry,
    classify_target_entries,
    compare_bases,
    install_attachments,
    observation_paths,
    parse_batch_types,
    payload_key,
    plan_destinations,
)
from ompire_daemon.work.inputs import (
    BASE_COMPARISON_DIFFERENT,
    BASE_COMPARISON_MATCH,
    BASE_COMPARISON_UNKNOWN,
    HANDOFF_CLASSIFICATION,
    AttachedFile,
    ResultAttachment,
)

# The bodies each attachment was built from, so a payload map matches the
# manifest it is checked against.
BODIES: dict[str, bytes] = {}


def payloads(*attachments: ResultAttachment) -> dict[str, bytes]:
    return {
        payload_key(item.result_id, entry.path): BODIES[
            payload_key(item.result_id, entry.path)
        ]
        for item in attachments
        for entry in item.files
    }


def attachment(result_id: str, *files: tuple[str, bytes]) -> ResultAttachment:
    for path, body in files:
        BODIES[payload_key(result_id, path)] = body
    return ResultAttachment(
        result_id=result_id,
        producer_task_id=1,
        manifest_id=f"m-{result_id}",
        content_id=None,
        accepted_at="2026-09-09T00:00:00Z",
        manifest_project_name="demo",
        files=tuple(
            AttachedFile(
                path=path,
                length=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                media_type="text/markdown",
            )
            for path, body in files
        ),
        provenance={},
        classification=HANDOFF_CLASSIFICATION,
    )


# --- Destination rules ------------------------------------------------------


def test_two_bundles_claiming_one_path_are_refused_naming_both() -> None:
    """Only one file can exist at a path, so this is a choice the operator has
    to make. There is no remapping in this change, so Ompire cannot invent a
    second destination to resolve it."""
    with pytest.raises(HandoffError) as exc:
        plan_destinations(
            [
                attachment("a", ("epics/x/PLAN.md", b"one")),
                attachment("b", ("epics/x/PLAN.md", b"two")),
            ]
        )
    assert exc.value.reason == "destination-collision"
    assert "a" in exc.value.detail and "b" in exc.value.detail


def test_a_file_in_one_bundle_cannot_be_a_directory_in_another() -> None:
    """The subtler overlap: `epics/x` as a file and as a parent directory.
    Checking only for equal paths would let both through and fail at write
    time, after the launch was already accepted."""
    with pytest.raises(HandoffError) as exc:
        plan_destinations(
            [
                attachment("a", ("epics/x", b"file")),
                attachment("b", ("epics/x/PLAN.md", b"nested")),
            ]
        )
    assert exc.value.reason == "destination-collision"
    assert exc.value.path == "epics/x"


@pytest.mark.parametrize("name", ["workshop.yaml", "workshop.my.yaml"])
def test_launcher_configuration_can_never_be_an_attachment_destination(name: str) -> None:
    """my-workshop reads these from the clone root before an agent exists.
    Retained text landing there would change how the container itself is
    built — from a file the producing task wrote."""
    with pytest.raises(HandoffError) as exc:
        plan_destinations([attachment("a", (name, b"image: evil"))])
    assert exc.value.reason == "reserved-destination"


def test_the_same_revision_cannot_be_attached_twice() -> None:
    with pytest.raises(HandoffError) as exc:
        plan_destinations(
            [attachment("a", ("p.md", b"x")), attachment("a", ("q.md", b"y"))]
        )
    assert exc.value.reason == "duplicate-revision"


def test_combined_bundles_are_bounded_by_the_retained_result_limits() -> None:
    """The bounds are over the whole selection, not per bundle: two bundles
    that each fit must not add up to something that does not."""
    with pytest.raises(HandoffError) as exc:
        plan_destinations(
            [
                attachment("a", *[(f"a/f{i}.md", b"x") for i in range(100)]),
                attachment("b", *[(f"b/f{i}.md", b"x") for i in range(100)]),
            ]
        )
    assert exc.value.reason == "too-many-files"


def test_a_clean_selection_reports_its_ordered_destinations() -> None:
    plan = plan_destinations(
        [
            attachment("a", ("epics/x/PLAN.md", b"abc")),
            attachment("b", ("epics/x/SPEC.md", b"de")),
        ]
    )
    assert plan.paths == ("epics/x/PLAN.md", "epics/x/SPEC.md")
    assert plan.file_count == 2
    assert plan.total_bytes == 5


# --- Target base classification ---------------------------------------------


def test_observation_asks_about_every_ancestor_directory() -> None:
    """A blob at `epics` makes `epics/x/PLAN.md` uninstallable, and the reading
    can only notice that if it asked about `epics`."""
    assert observation_paths(("epics/x/PLAN.md",)) == [
        "epics",
        "epics/x",
        "epics/x/PLAN.md",
    ]


def test_a_tracked_destination_is_refused_even_when_it_would_be_identical() -> None:
    """Publication protection is a destination contract. A tracked path is one
    the recipient's ordinary Git result carries, so installing a handoff there
    would make the handoff publishable — identical bytes or not."""
    conflicts = classify_target_entries(
        ("epics/x/PLAN.md",),
        [TreeEntry(mode="100644", kind="blob", path="epics/x/PLAN.md")],
    )
    assert [c.reason for c in conflicts] == ["destination-tracked"]


@pytest.mark.parametrize(
    ("mode", "kind", "reason"),
    [
        ("040000", "tree", "destination-is-directory"),
        ("120000", "blob", "destination-symlink"),
        ("160000", "commit", "destination-submodule"),
    ],
)
def test_a_destination_that_is_not_an_ordinary_free_path_is_refused(
    mode: str, kind: str, reason: str
) -> None:
    conflicts = classify_target_entries(
        ("epics/x/PLAN.md",),
        [TreeEntry(mode=mode, kind=kind, path="epics/x/PLAN.md")],
    )
    assert [c.reason for c in conflicts] == [reason]


@pytest.mark.parametrize(
    ("mode", "kind", "reason"),
    [
        ("100644", "blob", "ancestor-not-a-directory"),
        ("120000", "blob", "ancestor-symlink"),
        ("160000", "commit", "ancestor-submodule"),
    ],
)
def test_an_ancestor_that_is_not_a_directory_is_refused(
    mode: str, kind: str, reason: str
) -> None:
    conflicts = classify_target_entries(
        ("epics/x/PLAN.md",),
        [TreeEntry(mode=mode, kind=kind, path="epics")],
    )
    assert [c.reason for c in conflicts] == [reason]
    assert conflicts[0].path == "epics/x/PLAN.md"


def test_a_free_destination_under_ordinary_directories_is_accepted() -> None:
    assert (
        classify_target_entries(
            ("epics/x/PLAN.md",),
            [
                TreeEntry(mode="040000", kind="tree", path="epics"),
                TreeEntry(mode="040000", kind="tree", path="epics/x"),
            ],
        )
        == ()
    )


def test_batch_check_output_is_read_positionally() -> None:
    """`cat-file --batch-check` answers one line per input line, in order, and
    does not echo the path for a found object. Captured paths carry no control
    characters, which is what makes the positional reading unambiguous."""
    found = parse_batch_types(
        ["epics", "epics/x", "epics/x/PLAN.md"],
        "1c51f14 tree 90\nHEAD:epics/x missing\n45b983b blob 3\n",
    )
    assert found == {"epics": "tree", "epics/x/PLAN.md": "blob"}


# --- Base comparison --------------------------------------------------------


def test_an_unrecorded_producer_base_is_unknown_not_a_match() -> None:
    """A capture that recorded no base observation cannot be compared. Reading
    that absence as agreement would vouch for a plan nobody checked."""
    comparison = compare_bases(
        result_id="r", target_commit="abc", producer_observation=None, comparable=False
    )
    assert comparison.state == BASE_COMPARISON_UNKNOWN
    assert comparison.needs_acknowledgement
    assert comparison.detail


def test_an_equal_observation_matches_and_needs_no_acknowledgement() -> None:
    comparison = compare_bases(
        result_id="r", target_commit="abc", producer_observation="abc", comparable=False
    )
    assert comparison.state == BASE_COMPARISON_MATCH
    assert not comparison.needs_acknowledgement


def test_a_producer_base_this_checkout_lacks_is_different_with_a_named_gap() -> None:
    """Different, and honestly incomparable: nothing fetches the producer's
    objects, so the difference is named as unlistable rather than reported as
    empty."""
    comparison = compare_bases(
        result_id="r", target_commit="abc", producer_observation="def", comparable=False
    )
    assert comparison.state == BASE_COMPARISON_DIFFERENT
    assert comparison.changed_paths == ()
    assert "not present in this checkout" in (comparison.detail or "")


def test_a_truncated_comparison_says_so() -> None:
    comparison = compare_bases(
        result_id="r",
        target_commit="abc",
        producer_observation="def",
        comparable=True,
        changed_paths=("a.py",),
        truncated=True,
    )
    assert comparison.truncated
    assert comparison.needs_acknowledgement


# --- Materialization --------------------------------------------------------


def test_files_are_installed_as_ordinary_owner_only_regular_files(tmp_path: Path) -> None:
    item = attachment("a", ("epics/x/PLAN.md", b"12345"))
    clone = tmp_path / "clone"
    clone.mkdir()
    installed = install_attachments(str(clone), [item], payloads(item))
    target = clone / "epics" / "x" / "PLAN.md"
    assert installed == ("epics/x/PLAN.md",)
    assert target.read_bytes() == b"12345"
    mode = target.stat().st_mode
    assert oct(mode & 0o777) == "0o600"
    assert not os.access(target, os.X_OK)


def test_an_existing_entry_is_never_replaced(tmp_path: Path) -> None:
    """Exclusive creation, always. Overwriting would destroy work Ompire does
    not own, and the operator never approved a replacement."""
    item = attachment("a", ("PLAN.md", b"new"))
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "PLAN.md").write_text("mine")
    with pytest.raises(MaterializationError) as exc:
        install_attachments(str(clone), [item], payloads(item))
    assert exc.value.path == "PLAN.md"
    assert (clone / "PLAN.md").read_text() == "mine"


def test_a_symlinked_ancestor_cannot_redirect_a_write_outside_the_clone(
    tmp_path: Path,
) -> None:
    """The reason every component is opened with `O_NOFOLLOW`: a resolved
    string path plus an ordinary open can be redirected between the two."""
    clone = tmp_path / "clone"
    clone.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (clone / "epics").symlink_to(outside)
    item = attachment("a", ("epics/PLAN.md", b"leak"))
    with pytest.raises(MaterializationError) as exc:
        install_attachments(str(clone), [item], payloads(item))
    assert exc.value.path == "epics"
    assert list(outside.iterdir()) == []


def test_a_component_on_another_filesystem_refuses(tmp_path: Path) -> None:
    """A bind mount is not a symlink, so `O_NOFOLLOW` says nothing about it.
    Without a device check, a mount planted inside the workspace would be a
    place a handoff could be written that is not part of the workspace."""
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "epics").mkdir()
    item = attachment("a", ("epics/PLAN.md", b"x"))

    real_fstat = os.fstat
    root_dev = os.stat(clone).st_dev

    def fake_fstat(fd: int):
        stats = real_fstat(fd)
        # Report the nested directory as living on another device.
        if stat.S_ISDIR(stats.st_mode) and stats.st_ino != os.stat(clone).st_ino:
            return os.stat_result(
                (stats.st_mode, stats.st_ino, root_dev + 1, *tuple(stats)[3:])
            )
        return stats

    with mock.patch.object(os, "fstat", fake_fstat), pytest.raises(
        MaterializationError
    ) as exc:
        install_attachments(str(clone), [item], payloads(item))
    assert "different filesystem" in exc.value.detail
    assert not (clone / "epics" / "PLAN.md").exists()


def test_a_file_where_a_directory_is_needed_refuses(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "epics").write_text("not a directory")
    item = attachment("a", ("epics/PLAN.md", b"x"))
    with pytest.raises(MaterializationError):
        install_attachments(str(clone), [item], payloads(item))


def test_installed_bytes_are_verified_against_the_accepted_manifest(
    tmp_path: Path,
) -> None:
    """The check that makes "prepared" mean something. A payload that does not
    hash to what was accepted fails the step rather than starting a task on
    inputs nobody verified."""
    item = attachment("a", ("PLAN.md", b"12345"))
    clone = tmp_path / "clone"
    clone.mkdir()
    wrong = {payload_key("a", "PLAN.md"): b"54321"}
    # Same length, different bytes: only the checksum catches this.
    with pytest.raises(MaterializationError) as exc:
        install_attachments(str(clone), [item], wrong)
    assert "does not match the accepted revision" in exc.value.detail


def test_a_failed_installation_leaves_what_it_wrote_for_inspection(
    tmp_path: Path,
) -> None:
    """No rollback, deliberately: deleting on the way out risks removing work
    that was already there. A failed clone stays failed and inspectable until
    ordinary cleanup removes the whole thing."""
    good = attachment("a", ("a/first.md", b"one"))
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "b").mkdir()
    (clone / "b" / "second.md").write_text("mine")
    bad = attachment("b", ("b/second.md", b"two"))
    with pytest.raises(MaterializationError):
        install_attachments(str(clone), [good, bad], payloads(good, bad))
    assert (clone / "a" / "first.md").read_bytes() == b"one"
    assert (clone / "b" / "second.md").read_text() == "mine"
