"""Format-1 workflow documents: the loader's refusals, the identity rules, and
three-valued evaluation.

The point of these tests is the boundary, not the built-ins: what a document
may say, what changes its revision, and what "cannot decide" means.
"""

from __future__ import annotations

import json

import pytest

from ompire_daemon.workflow_definitions import (
    MISSING,
    AgentStep,
    CommandStep,
    EvaluationContext,
    HistoryRecord,
    RenderError,
    Unresolved,
    UnsupportedWorkflowFormatError,
    WorkflowDocumentError,
    canonical_bytes,
    describe,
    evaluate_predicate,
    evaluate_value,
    load_definition,
    parse_yaml_document,
    render_text,
)

MINIMAL = """
format: 1
name: minimal
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - text: "do it"
"""


def load(text: str):
    return load_definition(text)


def context(records=(), **inputs) -> EvaluationContext:
    base = {
        "task.prompt": "",
        "task.slug": "s",
        "task.branch": "b",
        "workspace.preamble": "",
    }
    base.update(inputs)
    return EvaluationContext(inputs=base, records=tuple(records))


def record(seq: int, step: str, outcome, status: str = "ok") -> HistoryRecord:
    return HistoryRecord(seq=seq, step=step, status=status, outcome=outcome)


# --- the bounded YAML subset --------------------------------------------------


def test_minimal_document_loads_with_defaults_made_explicit() -> None:
    revision = load(MINIMAL)
    step = revision.definition.steps[0]
    assert isinstance(step, AgentStep)
    assert (step.role, step.expects_outcome, step.max_visits) == ("default", False, None)
    assert revision.document["steps"][0]["role"] == "default"
    assert revision.revision.startswith("sha256:")


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("format: 1\nname: a\n<<: {}\n", "merge keys"),
        ("base: &a {x: 1}\nother: *a\n", "anchors"),
        ("format: 1\nformat: 2\n", "duplicate key"),
        ("a: 1\n---\nb: 2\n", "single YAML document"),
        ("x: !!python/object/apply:os.system ['id']\n", "not allowed"),
        ("1: two\n", "mapping keys must be strings"),
    ],
)
def test_unsafe_or_ambiguous_yaml_is_refused(document: str, expected: str) -> None:
    with pytest.raises(WorkflowDocumentError) as excinfo:
        parse_yaml_document(document)
    assert expected in str(excinfo.value)


def test_scalars_resolve_by_json_rules_not_yaml_1_1() -> None:
    data = parse_yaml_document(
        "yes_word: yes\noff_word: off\nstamp: 2026-09-06\n"
        "real_null: null\nreal_bool: true\nreal_int: 12\nquoted: '13'\n"
    )
    assert data == {
        "yes_word": "yes",
        "off_word": "off",
        "stamp": "2026-09-06",
        "real_null": None,
        "real_bool": True,
        "real_int": 12,
        "quoted": "13",
    }


def test_document_depth_is_bounded() -> None:
    deep = "".join(f"{' ' * i}a:\n" for i in range(40)) + f"{' ' * 40}b: 1\n"
    with pytest.raises(WorkflowDocumentError, match="nests deeper"):
        parse_yaml_document(deep)


def test_oversized_document_is_refused_before_parsing() -> None:
    with pytest.raises(WorkflowDocumentError, match="larger than"):
        parse_yaml_document("format: 1\n# " + "x" * (1024 * 1024))


def test_unsupported_format_is_refused_not_reinterpreted() -> None:
    with pytest.raises(UnsupportedWorkflowFormatError):
        load(MINIMAL.replace("format: 1", "format: 2"))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("session: main", "session: nope"),
        ("kind: agent", "kind: sorcery"),
        ("name: work", "name: judge"),
    ],
)
def test_invalid_step_fields_name_their_location(mutation: str, expected: str) -> None:
    with pytest.raises(WorkflowDocumentError) as excinfo:
        load(MINIMAL.replace(mutation, expected))
    assert excinfo.value.location.startswith("steps[0]")


def test_unknown_field_is_refused_rather_than_ignored() -> None:
    with pytest.raises(WorkflowDocumentError, match="unknown field 'retries'"):
        load(MINIMAL + "    retries: 3\n")


def test_command_step_must_declare_idempotence_and_literal_argv() -> None:
    base = """
format: 1
name: c
sessions: [main]
primary: main
steps:
  - name: run
    kind: command
    argv: ["bash", "x.sh"]
    idempotent: true
"""
    assert isinstance(load(base).definition.steps[0], CommandStep)
    with pytest.raises(WorkflowDocumentError, match="idempotent"):
        load(base.replace("    idempotent: true\n", ""))
    with pytest.raises(WorkflowDocumentError, match="literal string"):
        load(base.replace('argv: ["bash", "x.sh"]', "argv: [{op: input, name: task.prompt}]"))


def test_decision_needs_an_explicit_otherwise() -> None:
    document = """
format: 1
name: d
sessions: [main]
primary: main
steps:
  - name: choose
    kind: decision
    cases:
      - when: true
        next: {complete: true}
"""
    with pytest.raises(WorkflowDocumentError, match="explicit 'otherwise'"):
        load(document)


def test_unbounded_cycle_is_refused() -> None:
    document = """
format: 1
name: loop
sessions: [main]
primary: main
steps:
  - name: a
    kind: agent
    session: main
    prompt: {parts: []}
  - name: back
    kind: decision
    cases:
      - when: true
        next: {step: a}
    otherwise: {complete: true}
"""
    with pytest.raises(WorkflowDocumentError, match="unbounded cycle"):
        load(document)
    bounded = document.replace(
        "    kind: agent\n", "    kind: agent\n    max_visits: 2\n    on_exhausted: {step: stop}\n"
    ) + """  - name: stop
    kind: gate
    message: {parts: [{text: "stopped"}]}
"""
    assert load(bounded).definition.step_named("a").max_visits == 2


def test_exhaustion_target_must_be_a_gate_outside_the_cycle() -> None:
    document = """
format: 1
name: loop
sessions: [main]
primary: main
steps:
  - name: a
    kind: agent
    session: main
    max_visits: 2
    on_exhausted: {step: back}
    prompt: {parts: []}
  - name: back
    kind: decision
    cases:
      - when: true
        next: {step: a}
    otherwise: {complete: true}
"""
    with pytest.raises(WorkflowDocumentError, match="must name a declared gate"):
        load(document)


# --- identity -----------------------------------------------------------------


def test_comments_and_key_order_do_not_change_the_revision() -> None:
    reordered = """
# a comment the revision must ignore
name: minimal
steps:
  - kind: agent
    prompt:
      parts:
        - text: "do it"
    session: main
    name: work
primary: main
sessions: [main]
format: 1
"""
    assert load(reordered).revision == load(MINIMAL).revision


@pytest.mark.parametrize(
    ("original", "edited"),
    [
        ('text: "do it"', 'text: "do it now"'),
        ("session: main", "session: main\n    role: slow"),
        ("primary: main", "primary: main\n"),
    ],
)
def test_executable_content_changes_the_revision(original: str, edited: str) -> None:
    if original == edited.rstrip("\n"):
        pytest.skip("no change")
    before = load(MINIMAL)
    after = load(MINIMAL.replace(original, edited))
    assert (before.revision == after.revision) == (
        canonical_bytes(before.definition) == canonical_bytes(after.definition)
    )


def test_a_prompt_edit_changes_the_revision() -> None:
    assert load(MINIMAL).revision != load(MINIMAL.replace("do it", "do it now")).revision


def test_sequence_order_is_part_of_identity() -> None:
    two = MINIMAL + """  - name: second
    kind: agent
    session: main
    prompt: {parts: []}
"""
    swapped = """
format: 1
name: minimal
sessions: [main]
primary: main
steps:
  - name: second
    kind: agent
    session: main
    prompt: {parts: []}
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - text: "do it"
"""
    assert load(two).revision != load(swapped).revision


def test_canonical_document_round_trips_to_the_same_revision() -> None:
    revision = load(MINIMAL)
    from ompire_daemon.workflow_definitions import (
        load_canonical_document,
        make_revision,
    )

    again = make_revision(load_canonical_document(json.loads(json.dumps(revision.document))))
    assert again.revision == revision.revision


# --- evaluation ---------------------------------------------------------------


def test_latest_selects_the_newest_ok_attempt_and_says_which_step_answered() -> None:
    ctx = context(
        records=[
            record(1, "a", {"n": 1}),
            record(2, "b", {"n": 2}),
            record(3, "a", {"n": 3}, status="failed"),
        ]
    )
    value = evaluate_value(
        parse_value({"op": "latest", "steps": ["a", "b"]}), ctx
    )
    assert value["step"] == "b" and value["outcome"] == {"n": 2}


def parse_value(document):
    from ompire_daemon.workflow_definitions import _parse_value

    return _parse_value(document, "test")


def parse_predicate(document):
    from ompire_daemon.workflow_definitions import _parse_predicate

    return _parse_predicate(document, "test")


def test_after_excludes_attempts_older_than_the_anchor() -> None:
    records = [
        record(1, "check", {"round": 1}),
        record(2, "fix", {"ok": True}),
        record(3, "check", {"round": 2}),
    ]
    expression = parse_value({"op": "latest", "steps": ["check"], "after": "fix"})
    assert evaluate_value(expression, context(records=records))["outcome"] == {"round": 2}
    # An anchor that never ran has sequence zero, so nothing is excluded: the
    # reference is "newer than the anchor", not "only after the anchor ran".
    assert evaluate_value(expression, context(records=records[:1]))["outcome"] == {"round": 1}
    assert evaluate_value(expression, context()) is MISSING


def test_with_outcome_skips_a_deliberately_unprompted_attempt() -> None:
    records = [
        record(1, "script", {"exit_code": 1}),
        record(2, "agent-check", None),
    ]
    without = parse_value({"op": "latest", "steps": ["script", "agent-check"]})
    strict = parse_value(
        {"op": "latest", "steps": ["script", "agent-check"], "with_outcome": True}
    )
    assert evaluate_value(without, context(records=records))["step"] == "agent-check"
    assert evaluate_value(strict, context(records=records))["step"] == "script"


def test_missing_is_not_null_and_not_an_older_success() -> None:
    ctx = context(records=[record(1, "a", {"status": "ok"}), record(2, "a", None)])
    latest = parse_value({"op": "latest", "steps": ["a"]})
    assert evaluate_value(parse_value({"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "status"]}), ctx) is MISSING
    assert evaluate_value(latest, ctx)["outcome"] is None


def test_get_never_traverses_attributes() -> None:
    ctx = context(records=[record(1, "a", {"x": {"y": "z"}})])
    expression = {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "x", "y"]}
    assert evaluate_value(parse_value(expression), ctx) == "z"
    attribute = {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "__class__"]}
    assert evaluate_value(parse_value(attribute), ctx) is MISSING


def test_coalesce_keeps_an_empty_string_and_skips_missing() -> None:
    ctx = context(records=[record(1, "a", {"summary": ""})])
    expression = parse_value(
        {
            "op": "coalesce",
            "values": [
                {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "absent"]},
                {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "summary"]},
                {"op": "literal", "value": "fallback"},
            ],
        }
    )
    assert evaluate_value(expression, ctx) == ""


def test_count_counts_every_attempt_of_a_step() -> None:
    ctx = context(records=[record(1, "fix", None, status="failed"), record(2, "fix", {})])
    assert evaluate_value(parse_value({"op": "count", "step": "fix"}), ctx) == 2


def test_missing_operand_is_unresolved_never_false() -> None:
    predicate = parse_predicate(
        {
            "op": "eq",
            "left": {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "status"]},
            "right": {"op": "literal", "value": "success"},
        }
    )
    assert isinstance(evaluate_predicate(predicate, context()), Unresolved)


def test_exists_is_false_for_missing_and_true_for_an_empty_string() -> None:
    ctx = context(records=[record(1, "a", {"summary": ""})])
    exists = lambda keys: parse_predicate(
        {"op": "exists", "value": {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": keys}}
    )
    assert evaluate_predicate(exists(["outcome", "summary"]), ctx) is True
    assert evaluate_predicate(exists(["outcome", "absent"]), ctx) is False


def test_is_type_never_calls_a_boolean_a_number() -> None:
    ctx = context(records=[record(1, "a", {"flag": True, "n": 1})])
    is_type = lambda key, json_type: parse_predicate(
        {
            "op": "is_type",
            "value": {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", key]},
            "type": json_type,
        }
    )
    assert evaluate_predicate(is_type("flag", "boolean"), ctx) is True
    assert evaluate_predicate(is_type("flag", "integer"), ctx) is False
    assert evaluate_predicate(is_type("n", "integer"), ctx) is True


def test_a_decisive_operand_wins_over_a_later_unresolved_one() -> None:
    missing = {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "x"]}
    guarded = parse_predicate(
        {
            "op": "all",
            "of": [
                {"op": "exists", "value": missing},
                {"op": "eq", "left": missing, "right": {"op": "literal", "value": 1}},
            ],
        }
    )
    assert evaluate_predicate(guarded, context()) is False
    unguarded = parse_predicate(
        {"op": "all", "of": [{"op": "eq", "left": missing, "right": {"op": "literal", "value": 1}}]}
    )
    assert isinstance(evaluate_predicate(unguarded, context()), Unresolved)


def test_type_mismatch_in_an_ordered_comparison_is_unresolved() -> None:
    predicate = parse_predicate(
        {"op": "lt", "left": {"op": "literal", "value": "a"}, "right": {"op": "literal", "value": 1}}
    )
    assert isinstance(evaluate_predicate(predicate, context()), Unresolved)


# --- rendering ----------------------------------------------------------------


def test_interpolated_text_is_data_and_is_never_parsed_again() -> None:
    revision = load(
        """
format: 1
name: echo
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - value: {op: input, name: task.prompt}
          format: text
"""
    )
    injected = "{op: input, name: task.branch}"
    rendered = render_text(revision.definition.steps[0].prompt, context(**{"task.prompt": injected}))
    assert rendered == injected


def test_a_missing_value_without_a_fallback_refuses_to_render() -> None:
    document = {
        "separator": "",
        "parts": [
            {
                "value": {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "x"]},
                "format": "text",
            }
        ],
    }
    from ompire_daemon.workflow_definitions import _parse_text

    with pytest.raises(RenderError, match="missing"):
        render_text(_parse_text(document, "t"), context())


def test_an_unresolved_conditional_refuses_rather_than_dropping_the_section() -> None:
    from ompire_daemon.workflow_definitions import _parse_text

    document = {
        "separator": "",
        "parts": [
            {
                "if": {
                    "op": "eq",
                    "left": {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome", "x"]},
                    "right": {"op": "literal", "value": 1},
                },
                "then": {"parts": [{"text": "yes"}]},
                "else": {"parts": [{"text": "no"}]},
            }
        ],
    }
    with pytest.raises(RenderError, match="cannot be resolved"):
        render_text(_parse_text(document, "t"), context())


def test_json_format_is_stable_and_sorted() -> None:
    from ompire_daemon.workflow_definitions import _parse_text

    document = {
        "separator": "",
        "parts": [
            {
                "value": {"op": "get", "value": {"op": "latest", "steps": ["a"]}, "keys": ["outcome"]},
                "format": "json",
            }
        ],
    }
    ctx = context(records=[record(1, "a", {"b": 2, "a": 1})])
    assert render_text(_parse_text(document, "t"), ctx) == '{"a": 1, "b": 2}'


# --- description --------------------------------------------------------------


def test_describe_marks_steps_a_route_or_a_when_can_skip() -> None:
    revision = load(
        """
format: 1
name: branchy
sessions: [main]
primary: main
steps:
  - name: first
    kind: agent
    session: main
    prompt: {parts: []}
  - name: pick
    kind: decision
    cases:
      - when: true
        next: {complete: true}
    otherwise: {step: later}
  - name: later
    kind: gate
    message: {parts: [{text: "hi"}]}
"""
    )
    descriptor = describe(revision)
    assert [(s.name, s.conditional) for s in descriptor.steps] == [
        ("first", False),
        ("pick", False),
        ("later", True),
    ]
    assert descriptor.revision == revision.revision
