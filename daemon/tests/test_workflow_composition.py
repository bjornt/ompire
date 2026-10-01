"""Consumer-visible contracts for frozen, bounded reusable workflow composition."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from ompire_daemon import workflow_composition as composition
from ompire_daemon.workflow_definitions import (
    MAX_STEPS,
    WorkflowDocumentError,
    definition_from_document,
    emit_draft_yaml,
    export_document,
    export_yaml,
    load_canonical_document,
    load_definition,
    make_revision,
)


def shared_definition(**changes):
    body = {
        "name": "finish-procedure",
        "label": "Finish",
        "description": "Select a declared ending.",
        "parameters": {
            "engine_name": {"type": "string"},
            "routes": {"type": "array", "default": [{"when": False, "next": {"complete": True, "result": "done"}}]},
            "ending": {"type": "object", "default": {"complete": True, "result": "done"}},
        },
        "steps": [{
            "name": {"param": "engine_name"},
            "kind": "decision",
            "cases": [{"spread": "routes"}],
            "otherwise": {"param": "ending"},
        }],
    }
    body.update(changes)
    return composition.normalize_shared_definition(body, require_revision=False)


def source_document(*, embedded=True, bindings=None, definition=None):
    definition = definition or shared_definition()
    document = {
        "format": 5,
        "name": "custom",
        "sessions": ["main"],
        "primary": "main",
        "steps": [{
            "name": "finish-phase",
            "use": definition["name"],
            "bindings": bindings if bindings is not None else {"engine_name": "finish"},
        }],
    }
    if embedded:
        document["definitions"] = {definition["name"]: definition}
    return document


def revision(document):
    return make_revision(definition_from_document(document))


def no_catalog():
    raise AssertionError("frozen documents must not consult a global catalog")


def test_source_invocations_have_the_same_ordinary_execution_semantics():
    document = source_document(bindings={
        "engine_name": "choose",
        "routes": [{"when": True, "next": {"complete": True, "result": "chosen"}}],
        "ending": {"complete": True, "result": "not-chosen"},
    })
    expanded = revision(document)
    ordinary = revision({
        "format": 4,
        "name": "custom",
        "sessions": ["main"],
        "primary": "main",
        "steps": [{
            "name": "choose",
            "kind": "decision",
            "cases": [{"when": True, "next": {"complete": True, "result": "chosen"}}],
            "otherwise": {"complete": True, "result": "not-chosen"},
        }],
    })
    assert expanded.definition.steps == ordinary.definition.steps
    assert expanded.definition.declared_actions == ()
    assert expanded.document["composition"]["stages"] == [{
        "name": "finish-phase", "label": "Finish",
        "description": "Select a declared ending.", "steps": ["choose"],
    }]


@pytest.mark.parametrize("bindings", [{}, {"engine_name": 3}, {"engine_name": "finish", "unknown": True}])
def test_missing_wrong_and_unknown_parameters_are_located(bindings):
    with pytest.raises(WorkflowDocumentError) as error:
        revision(source_document(bindings=bindings))
    assert error.value.location.startswith("steps[0].bindings")


@pytest.mark.parametrize(("kind", "wrong", "valid"), [
    ("boolean", 1, False),
    ("integer", True, 3),
    ("number", True, 3.5),
    ("string", None, ""),
    ("array", {}, []),
    ("object", [], {}),
])
def test_parameter_types_are_closed_json_types(kind, wrong, valid):
    body = shared_definition()
    body.pop("revision")
    body["parameters"]["typed"] = {"type": kind}
    shared = composition.normalize_shared_definition(body, require_revision=False)
    with pytest.raises(WorkflowDocumentError) as error:
        revision(source_document(definition=shared, bindings={"engine_name": "finish", "typed": wrong}))
    assert error.value.location == "steps[0].bindings.typed"
    accepted = revision(source_document(definition=shared, bindings={"engine_name": "finish", "typed": valid}))
    assert accepted.document["composition"]["steps"][0]["bindings"]["typed"] == valid


def test_defaults_normalize_to_the_same_revision_as_explicit_bindings():
    implicit = revision(source_document())
    explicit = revision(source_document(bindings={
        "engine_name": "finish", "routes": [{"when": False, "next": {"complete": True, "result": "done"}}], "ending": {"complete": True, "result": "done"},
    }))
    assert implicit.revision == explicit.revision


def test_prospective_catalog_changes_never_move_a_frozen_definition(monkeypatch):
    original = shared_definition()
    monkeypatch.setattr(composition, "shared_step_catalog", lambda: {original["name"]: original})
    document = source_document(embedded=False)
    frozen = revision(document)
    updated = shared_definition(label="Finish differently")
    monkeypatch.setattr(composition, "shared_step_catalog", lambda: {updated["name"]: updated})
    changed = revision(document)
    assert changed.revision != frozen.revision
    assert changed.definition.steps == frozen.definition.steps
    monkeypatch.setattr(composition, "shared_step_catalog", no_catalog)
    assert make_revision(load_canonical_document(frozen.document)).revision == frozen.revision
    assert revision(frozen.document).revision == frozen.revision
    assert load_definition(export_yaml(frozen)).revision == frozen.revision
    portable = export_document(frozen.document)
    assert portable["steps"][0]["use"] == "finish-procedure"
    assert revision(portable).revision == frozen.revision


@pytest.mark.parametrize("damage", ["snapshot", "rehash-snapshot", "expansion", "missing-snapshot", "missing-composition"])
def test_retained_corruption_is_refused_without_catalog_reads(monkeypatch, damage):
    saved = revision(source_document())
    document = copy.deepcopy(saved.document)
    if damage == "snapshot":
        document["composition"]["definitions"]["finish-procedure"]["label"] = "Modified"
    elif damage == "rehash-snapshot":
        shared = document["composition"]["definitions"]["finish-procedure"]
        shared.pop("revision")
        shared["steps"][0]["otherwise"] = {"complete": True, "result": "changed"}
        document["composition"]["definitions"]["finish-procedure"] = composition.normalize_shared_definition(shared, require_revision=False)
    elif damage == "expansion":
        document["steps"][0]["otherwise"]["result"] = "different"
    elif damage == "missing-snapshot":
        document["composition"]["definitions"] = {}
    else:
        del document["composition"]
    monkeypatch.setattr(composition, "shared_step_catalog", no_catalog)
    with pytest.raises(WorkflowDocumentError) as error:
        load_canonical_document(document)
    assert error.value.location
    if damage != "missing-composition":
        with pytest.raises(WorkflowDocumentError):
            definition_from_document(document)


def test_unknown_shared_reference_is_located(monkeypatch):
    monkeypatch.setattr(composition, "shared_step_catalog", dict)
    with pytest.raises(WorkflowDocumentError) as error:
        revision(source_document(embedded=False))
    assert error.value.location == "steps[0].use"


@pytest.mark.parametrize("template", [
    {"name": {"param": "unknown"}, "kind": "decision", "cases": [], "otherwise": {"complete": True, "result": "done"}},
    {"name": "finish", "kind": "decision", "cases": [], "otherwise": {"spread": "routes"}},
    {"name": "finish", "kind": "decision", "cases": [{"spread": "engine_name"}], "otherwise": {"complete": True, "result": "done"}},
    {"name": "nested", "use": "finish-procedure", "bindings": {}},
])
def test_templates_refuse_undeclared_placeholders_invalid_splices_and_nested_uses(template):
    with pytest.raises(WorkflowDocumentError) as error:
        shared_definition(steps=[template])
    assert "steps[0]" in error.value.location


def test_substituted_data_is_not_interpreted_as_another_placeholder():
    shared = shared_definition(
        parameters={"payload": {"type": "object"}},
        steps=[{
            "name": "finish", "kind": "decision",
            "cases": [{
                "when": {"op": "eq", "left": {"op": "literal", "value": {"param": "payload"}}, "right": {"op": "literal", "value": {"param": "payload"}}},
                "next": {"complete": True, "result": "done"},
            }],
            "otherwise": {"complete": True, "result": "other"},
        }],
    )
    payload = {"param": "not-a-template-parameter"}
    saved = revision(source_document(definition=shared, bindings={"payload": payload}))
    assert saved.document["steps"][0]["cases"][0]["when"]["left"]["value"] == payload


def test_expansion_cannot_introduce_privileged_actions_without_the_existing_grant():
    shared = shared_definition(
        parameters={},
        steps=[{"name": "commit", "kind": "delivery", "action": "commit", "mode": "squash", "approval": "approve", "next": {"complete": True, "result": "published"}}],
    )
    with pytest.raises(WorkflowDocumentError):
        revision(source_document(definition=shared, bindings={}))


def test_expansion_rechecks_ordinary_step_fields_and_routes():
    with pytest.raises(WorkflowDocumentError) as error:
        revision(source_document(bindings={"engine_name": "finish", "ending": {"step": "absent"}}))
    assert error.value.location.startswith("steps[0]")


def test_expanded_step_count_is_bounded_before_graph_validation():
    shared = shared_definition(steps=[
        {"name": "first", "kind": "decision", "cases": [{"when": True, "next": {"complete": True, "result": "done"}}], "otherwise": {"complete": True, "result": "done"}},
        {"name": "second", "kind": "decision", "cases": [{"when": True, "next": {"complete": True, "result": "done"}}], "otherwise": {"complete": True, "result": "done"}},
    ])
    document = source_document(definition=shared)
    document["steps"] = [{"name": f"phase-{index}", "use": shared["name"], "bindings": {"engine_name": "finish"}} for index in range(MAX_STEPS // 2 + 1)]
    with pytest.raises(WorkflowDocumentError) as error:
        revision(document)
    assert error.value.location.startswith("steps[")


def test_repeated_substitution_is_bounded_even_when_source_data_is_small():
    shared = shared_definition(
        parameters={"argv": {"type": "array"}},
        steps=[{"name": f"command-{index}", "kind": "command", "argv": {"param": "argv"}, "idempotent": True} for index in range(20)],
    )
    document = source_document(definition=shared, bindings={"argv": ["echo"] * 600})
    with pytest.raises(WorkflowDocumentError) as error:
        revision(document)
    assert error.value.location.startswith("steps[0].expansion")


@pytest.mark.parametrize("members", [["absent"], ["finish", "finish"], []])
def test_stages_refuse_unknown_duplicate_and_empty_members(members):
    document = source_document()
    document["stages"] = [{"name": "finish-phase", "label": "Finish", "description": "", "steps": members}]
    with pytest.raises(WorkflowDocumentError) as error:
        revision(document)
    assert error.value.location.startswith("stages")


def stage(name, members):
    return {"name": name, "label": name, "description": "", "steps": members}


def test_stages_may_group_later_exception_steps_without_reordering_execution():
    document = source_document()
    document["steps"] = [
        {"name": name, "kind": "decision", "cases": [{"when": True, "next": {"complete": True, "result": "done"}}], "otherwise": {"complete": True, "result": "done"}}
        for name in ("work", "review", "work-stop", "review-stop")
    ]
    document["stages"] = [stage("work-phase", ["work", "work-stop"]), stage("review-phase", ["review", "review-stop"])]
    saved = revision(document)
    assert [step.name for step in saved.definition.steps] == ["work", "review", "work-stop", "review-stop"]
    assert saved.document["composition"]["stages"] == document["stages"]
    for invalid in (
        [stage("work-phase", ["work"]), stage("review-phase", ["review", "review-stop"])],
        [stage("work-phase", ["work-stop", "work"]), stage("review-phase", ["review", "review-stop"])],
        list(reversed(document["stages"])),
        [stage("same", ["work", "work-stop"]), stage("same", ["review", "review-stop"])],
    ):
        document["stages"] = invalid
        with pytest.raises(WorkflowDocumentError) as error:
            revision(document)
        assert error.value.location.startswith("stages")


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_old_formats_keep_their_exact_canonical_identity(version):
    ending = {"complete": True} if version == 1 else {"complete": True, "result": "done"}
    step = {"name": "finish", "kind": "decision", "cases": [{"when": True, "next": ending}], "otherwise": ending}
    source = {"format": version, "name": "custom", "sessions": ["main"], "primary": "main", "steps": [step]}
    expected_step = {**step, "max_visits": None, "on_exhausted": None}
    if version >= 2:
        expected_step["evidence"] = {}
    expected = {**source, "steps": [expected_step]}
    expected_bytes = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    saved = revision(source)
    assert saved.document == expected
    assert saved.revision == "sha256:" + hashlib.sha256(expected_bytes).hexdigest()
    assert load_definition(export_yaml(saved)).revision == saved.revision


def test_packaged_catalog_is_usable_for_custom_source_authoring():
    document = {
        "format": 5, "name": "custom", "sessions": ["main"], "primary": "main",
        "steps": [
            {"name": "check", "kind": "review"},
            {"name": "stop", "use": "review-stop", "bindings": {
                "gate_name": "finish", "review_name": "check", "result": "stopped",
                "message": {"parts": [{"text": "Nothing is published."}]},
            }},
        ],
    }
    catalog = composition.shared_step_catalog()
    # This catalog contract is for authoring; no reference grants authority.
    saved = load_definition(emit_draft_yaml(document))
    assert saved.definition.declared_actions == ()
    assert saved.definition.steps[1].name == "finish"
    assert saved.document["composition"]["definitions"]["review-stop"] == catalog["review-stop"]


def test_invalid_parameter_defaults_and_embedded_revisions_are_refused():
    body = shared_definition()
    body.pop("revision")
    body["parameters"]["engine_name"]["default"] = False
    with pytest.raises(WorkflowDocumentError) as error:
        composition.normalize_shared_definition(body, require_revision=False)
    assert error.value.location.endswith("parameters.engine_name.default")
    document = source_document()
    document["definitions"]["finish-procedure"]["description"] = "Unreviewed content"
    with pytest.raises(WorkflowDocumentError) as error:
        revision(document)
    assert error.value.location == "definitions.finish-procedure.revision"


def test_embedded_snapshots_override_current_catalog_and_keep_source_references(monkeypatch):
    document = source_document()
    frozen = revision(document)
    monkeypatch.setattr(composition, "shared_step_catalog", no_catalog)
    assert revision(document).revision == frozen.revision
    portable = export_document(frozen.document)
    assert portable["steps"][0]["use"] == document["steps"][0]["use"]
    assert portable["stages"] == frozen.document["composition"]["stages"]
    assert revision(portable).revision == frozen.revision
