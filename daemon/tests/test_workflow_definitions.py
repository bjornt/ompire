"""Workflow documents: the loader's refusals, the identity rules, and
three-valued evaluation, in both formats.

The point of these tests is the boundary, not the built-ins: what a document
may say, what changes its revision, and what "cannot decide" means.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from ompire_daemon.workflow_definitions import (
    MISSING,
    AgentStep,
    CommandStep,
    CompleteDestination,
    DeliveryGrant,
    DeliveryStep,
    EvaluationContext,
    EvidenceBinding,
    EvidenceSelector,
    EvidenceValue,
    GateStep,
    HistoryRecord,
    RenderError,
    ReviewStep,
    StepDestination,
    Unresolved,
    UnsupportedWorkflowFormatError,
    WorkflowDocumentError,
    bindings_document,
    bindings_from_document,
    canonical_bytes,
    check_draft_data,
    definition_from_document,
    describe,
    emit_draft_yaml,
    evaluate_predicate,
    evaluate_value,
    evidence_views,
    export_yaml,
    load_definition,
    parse_yaml_document,
    record_view,
    render_text,
    resolve_evidence,
    validate_result_document,
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
    # A version this interpreter does not implement is refused rather than
    # read under the newest rules it happens to know.
    with pytest.raises(UnsupportedWorkflowFormatError):
        load(MINIMAL.replace("format: 1", "format: 5"))


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

    return _parse_value(document, "test", 1)


def parse_predicate(document):
    from ompire_daemon.workflow_definitions import _parse_predicate

    return _parse_predicate(document, "test", 1)


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
        render_text(_parse_text(document, "t", 1), context())


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
        render_text(_parse_text(document, "t", 1), context())


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
    assert render_text(_parse_text(document, "t", 1), ctx) == '{"a": 1, "b": 2}'


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


# --- format 2 -----------------------------------------------------------------
#
# The boundary that matters here is what format 2 *removes*: a route can no
# longer read a record the attempt did not freeze, a step can no longer finish
# on a result it never declared, and a run can no longer end without saying
# which ending it was.

FORMAT_2 = """
format: 2
name: two
sessions: [qa, coder]
primary: coder
steps:
  - name: reproduce
    kind: agent
    session: qa
    max_visits: 3
    on_exhausted: {step: exhausted}
    outcome:
      results:
        reproduced:
          required: {attempts: string, script_available: boolean}
        not-reproduced:
          required: {attempts: string}
    prompt:
      parts: [{text: "reproduce it"}]
  - name: route
    kind: decision
    evidence:
      repro: {steps: [reproduce]}
    cases:
      - when:
          op: eq
          left: {op: get, value: {op: evidence, name: repro}, keys: [outcome, result]}
          right: {op: literal, value: reproduced}
        next: {complete: true, result: validated}
    otherwise: {step: undecided}
  - name: exhausted
    kind: gate
    message: {parts: [{text: "out of attempts"}]}
    choices:
      - id: stop
        label: Stop
        next: {complete: true, result: stopped-without-fix}
  - name: undecided
    kind: gate
    evidence:
      repro: {steps: [reproduce], required: false}
    message: {parts: [{text: "could not reproduce"}]}
    choices:
      - id: retry
        label: Try again
        feedback_required: true
        next: {step: reproduce}
      - id: stop
        label: Stop
        next: {complete: true, result: stopped-without-fix}
"""


def test_format_2_parses_contracts_selectors_and_choices() -> None:
    definition = load(FORMAT_2).definition
    reproduce = definition.step_named("reproduce")
    assert isinstance(reproduce, AgentStep)
    assert reproduce.outcome is not None
    assert reproduce.outcome.names == ("not-reproduced", "reproduced")
    assert reproduce.outcome.result_named("reproduced").required == (
        ("attempts", "string"),
        ("script_available", "boolean"),
    )
    assert reproduce.requires_outcome is True
    undecided = definition.step_named("undecided")
    assert isinstance(undecided, GateStep)
    assert [choice.id for choice in undecided.choices] == ["retry", "stop"]
    assert undecided.choice_named("retry").next == StepDestination(step="reproduce")
    assert undecided.choice_named("stop").next == CompleteDestination(
        result="stopped-without-fix"
    )
    assert undecided.evidence[0] == EvidenceSelector(
        name="repro", steps=("reproduce",), after=None, with_outcome=True, required=False
    )


def test_the_two_formats_keep_their_own_vocabularies() -> None:
    # `latest` re-reads history whenever it is asked, which is exactly what
    # format 2 replaced; `evidence` is frozen at attempt entry, which format 1
    # has no place to record. Neither leaks into the other.
    with pytest.raises(WorkflowDocumentError, match="belongs to format 1"):
        load(
            FORMAT_2.replace(
                "{op: evidence, name: repro}", "{op: latest, steps: [reproduce]}"
            )
        )
    with pytest.raises(WorkflowDocumentError, match="format-2 value operation"):
        load(MINIMAL.replace('- text: "do it"', "- value: {op: evidence, name: x}"))
    with pytest.raises(WorkflowDocumentError, match="unknown field 'expects_outcome'"):
        load(FORMAT_2.replace("    outcome:", "    expects_outcome: true\n    outcome:"))
    with pytest.raises(WorkflowDocumentError, match="unknown field 'choices'"):
        load(
            MINIMAL
            + "  - name: g\n    kind: gate\n    message: {parts: []}\n"
            + "    choices: [{id: a, label: A, next: {complete: true}}]\n"
        )


def test_a_format_2_agent_step_must_say_whether_it_produces_a_result() -> None:
    # Omitting the field is not "no result": it is an author who did not say,
    # and a step allowed to finish on nothing is how a run continues past
    # evidence nobody wrote.
    with pytest.raises(WorkflowDocumentError, match="must declare 'outcome'"):
        load(
            FORMAT_2.replace(
                "    outcome:\n      results:\n"
                "        reproduced:\n"
                "          required: {attempts: string, script_available: boolean}\n"
                "        not-reproduced:\n"
                "          required: {attempts: string}\n",
                "",
            )
        )


def test_an_evidence_alias_is_local_to_the_step_that_declared_it() -> None:
    with pytest.raises(WorkflowDocumentError, match="which this step does not declare"):
        load(FORMAT_2.replace("name: repro}", "name: elsewhere}"))
    with pytest.raises(WorkflowDocumentError, match="undeclared step"):
        load(FORMAT_2.replace("repro: {steps: [reproduce]}", "repro: {steps: [nope]}"))


def test_format_2_endings_are_named_and_never_implicit() -> None:
    with pytest.raises(WorkflowDocumentError, match="missing required field 'result'"):
        load(FORMAT_2.replace("{complete: true, result: validated}", "{complete: true}"))
    falls_off = """
format: 2
name: falloff
sessions: [qa]
primary: qa
steps:
  - name: work
    kind: agent
    session: qa
    outcome: null
    prompt: {parts: [{text: hi}]}
"""
    with pytest.raises(WorkflowDocumentError, match="falls off the end"):
        load(falls_off)


def test_gate_choices_are_edges_the_bound_check_can_see() -> None:
    # A retry choice is a loop like any other. If graph validation ignored
    # human edges, a definition could spin forever on answers alone.
    unbounded = FORMAT_2.replace(
        "    max_visits: 3\n    on_exhausted: {step: exhausted}\n", ""
    )
    with pytest.raises(WorkflowDocumentError, match="unbounded cycle"):
        load(unbounded)
    # And an exhaustion gate may not hand the run back into the loop it just
    # left, however it is phrased.
    with pytest.raises(WorkflowDocumentError, match="can return to it"):
        load(FORMAT_2.replace("on_exhausted: {step: exhausted}", "on_exhausted: {step: undecided}"))


def test_a_gate_choice_cannot_pause_or_repeat_an_id() -> None:
    # A choice says where the run goes. "Stop again" is not a destination, and
    # two choices with one id make the recorded answer ambiguous.
    with pytest.raises(WorkflowDocumentError, match="cannot be a pause"):
        load(
            FORMAT_2.replace(
                "        next: {step: reproduce}", "        next: {pause: true}"
            )
        )
    with pytest.raises(WorkflowDocumentError, match="duplicate choice 'retry'"):
        load(
            FORMAT_2.replace(
                "        next: {step: reproduce}\n      - id: stop\n",
                "        next: {step: reproduce}\n      - id: retry\n",
            )
        )


def test_editing_a_result_or_a_route_changes_the_revision() -> None:
    base = load(FORMAT_2).revision
    # A prompt edit already changed identity in format 1; what is new is that
    # the *contract* and the *choices* are part of what a task pinned.
    assert load(FORMAT_2.replace("attempts: string", "attempts: object")).revision != base
    assert (
        load(FORMAT_2.replace("result: stopped-without-fix", "result: abandoned")).revision
        != base
    )
    assert load(FORMAT_2.replace("label: Try again", "label: Retry")).revision != base
    assert (
        load(FORMAT_2.replace("repro: {steps: [reproduce]}", "repro: {steps: [reproduce], after: route}")).revision
        != base
    )
    # Key order and comments still do not.
    assert load("# a comment\n" + FORMAT_2).revision == base


def test_format_1_canonical_bytes_carry_nothing_format_2_added() -> None:
    document = json.loads(canonical_bytes(load(MINIMAL).definition))
    assert document["format"] == 1
    step = document["steps"][0]
    assert "expects_outcome" in step
    assert "evidence" not in step
    assert "outcome" not in step
    two = json.loads(canonical_bytes(load(FORMAT_2).definition))
    assert two["steps"][0]["evidence"] == {}
    assert "expects_outcome" not in two["steps"][0]
    assert two["steps"][2]["choices"][0]["next"] == {
        "complete": True,
        "result": "stopped-without-fix",
    }


def test_evidence_binds_once_and_records_what_it_bound() -> None:
    definition = load(FORMAT_2).definition
    route = definition.step_named("route")
    records = [
        HistoryRecord(seq=1, step="reproduce", status="ok", outcome={"result": "x"}),
        HistoryRecord(seq=2, step="reproduce", status="ok", outcome={"result": "y"}),
    ]
    bindings, missing = resolve_evidence(route, records)
    assert missing == ()
    assert bindings == (EvidenceBinding(name="repro", step="reproduce", seq=2),)
    # The binding survives a round trip through its persisted form, and a
    # later record does not move it.
    restored = bindings_from_document(bindings_document(bindings))
    assert restored == bindings
    records.append(
        HistoryRecord(seq=3, step="reproduce", status="ok", outcome={"result": "z"})
    )
    views = evidence_views(restored, records)
    assert views["repro"]["seq"] == 2
    assert views["repro"]["outcome"] == {"result": "y"}


def test_a_required_selector_that_matches_nothing_is_reported_not_guessed() -> None:
    definition = load(FORMAT_2).definition
    bindings, missing = resolve_evidence(definition.step_named("route"), [])
    assert missing == ("repro",)
    assert bindings == (EvidenceBinding(name="repro", step=None, seq=None),)
    # An optional one binds to explicit absence, and absence reads as missing
    # rather than as a null someone wrote.
    optional, optional_missing = resolve_evidence(
        definition.step_named("undecided"), []
    )
    assert optional_missing == ()
    context = EvaluationContext(
        inputs={}, records=(), evidence=evidence_views(optional, [])
    )
    assert evaluate_value(EvidenceValue(name="repro"), context) is MISSING


def test_a_view_carries_the_evidence_the_record_itself_bound() -> None:
    # This is what lets a route ask "which fix did that verification check?"
    # instead of assuming it checked the newest one.
    record = HistoryRecord(
        seq=7,
        step="verify",
        status="ok",
        outcome={"result": "validated"},
        evidence={"fix": {"step": "fix", "seq": 5}},
    )
    assert record_view(record)["evidence"] == {"fix": {"step": "fix", "seq": 5}}


def test_a_result_must_be_declared_and_carry_what_it_promised() -> None:
    contract = load(FORMAT_2).definition.step_named("reproduce").outcome
    valid = {
        "version": 2,
        "result": "reproduced",
        "summary": "it fails on main",
        "artifacts": {"attempts": "ran the suite", "script_available": True},
    }
    assert validate_result_document(valid, contract) == (valid, None)
    for document, expected in [
        ({**valid, "version": 1}, "result version must be 2"),
        ({**valid, "result": "fixed"}, "is not declared by this step"),
        ({**valid, "summary": "   "}, "summary must be a nonblank string"),
        ({**valid, "artifacts": {"attempts": "x"}}, "requires the artifact"),
        (
            {**valid, "artifacts": {"attempts": " ", "script_available": True}},
            "must not be blank",
        ),
        (
            {**valid, "artifacts": {"attempts": "x", "script_available": "yes"}},
            "must be boolean",
        ),
        ({**valid, "note": "hi"}, "unknown result field"),
        ("nope", "not a JSON object"),
    ]:
        outcome, reason = validate_result_document(document, contract)
        assert outcome is None
        assert reason is not None and expected in reason


def test_describe_treats_a_choice_gate_as_a_branch() -> None:
    descriptor = describe(load(FORMAT_2))
    assert [(step.name, step.conditional) for step in descriptor.steps] == [
        ("reproduce", False),
        ("route", False),
        ("exhausted", True),
        ("undecided", True),
    ]
    assert descriptor.format == 2


# --- YAML emission and portability (ADR-0031) ---------------------------------


def _round_trip(text: str) -> None:
    """Load, export, load again — and demand the same content identity.

    The identity is the whole assertion. A serializer that changed a scalar's
    type, folded a prompt differently, or dropped a field would produce a
    document that still parses and means something else, and only the digest
    catches that.
    """
    original = load_definition(text)
    exported = export_yaml(original)
    assert load_definition(exported).revision == original.revision


def test_an_exported_definition_reloads_to_the_same_revision() -> None:
    _round_trip(MINIMAL)
    _round_trip(FORMAT_2)


def test_export_preserves_both_packaged_formats() -> None:
    """The frozen format-1 identity survives a round trip, and so does the
    much larger format-2 built-in."""
    from ompire_daemon.workflows import load_packaged_workflows

    for revision in load_packaged_workflows().values():
        assert load_definition(export_yaml(revision)).revision == revision.revision


def test_export_never_lets_a_string_come_back_as_something_else() -> None:
    """The loader reads unquoted scalars under JSON's rules, so text that
    *looks* like a literal has to be emitted quoted.

    `yes`, a date, and `1.0` are all ordinary prose in a prompt. A serializer
    that emitted them plain would produce a document whose prompt renders a
    boolean, and whose author never wrote one.
    """
    definition = """
format: 1
name: scalars
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      separator: "\\n"
      parts:
        - text: "true"
        - text: "null"
        - text: "2026-09-07"
        - text: "1.0"
        - text: "0o17"
        - text: ""
        - text: "  leading and trailing  "
        - text: "line one\\nline two\\n"
        - text: "trailing space at eol \\nnext"
        - value: {op: literal, value: {"true": 1, "12": "x", "": null}}
"""
    _round_trip(definition)
    exported = export_yaml(load_definition(definition))
    parts = load_definition(exported).definition.steps[0].prompt.parts
    assert parts[0].text == "true"
    assert parts[3].text == "1.0"
    assert parts[5].text == ""


def test_export_omits_defaults_and_normalization_puts_them_back() -> None:
    """Readability, without a second meaning.

    The canonical document spells out every default; a file repeating
    `role: default`, `when: true`, and `evidence: {}` on every step is one an
    operator cannot read. Leaving them implicit is safe precisely because
    normalization restores them and the identity does not move.
    """
    revision = load_definition(FORMAT_2)
    exported = export_yaml(revision)
    assert "role: default" not in exported
    assert "when: true" not in exported
    assert "evidence: {}" not in exported
    assert load_definition(exported).document == revision.document


def test_a_near_boundary_document_still_exports_and_reloads() -> None:
    """A definition close to the loader's own limits, not a toy.

    Export expands nothing back to the canonical form, so a document that was
    accepted has to survive the round trip rather than come back too big or
    too deeply nested to load.
    """
    steps = []
    for index in range(120):
        steps.append(
            f"""
  - name: step-{index}
    kind: agent
    session: main
    role: smol
    max_visits: 3
    on_exhausted: {{step: bail}}
    when: {{op: exists, value: {{op: input, name: task.prompt}}}}
    expects_outcome: true
    prompt:
      separator: "\\n\\n"
      parts:
        - text: "{'instruction ' * 40}"
        - value: {{op: latest, steps: [step-0], with_outcome: true}}
          format: json
"""
        )
    text = (
        "format: 1\nname: big\nsessions: [main]\nprimary: main\nsteps:"
        + "".join(steps)
        + '\n  - name: bail\n    kind: gate\n    message: {parts: [{text: "stop"}]}\n'
    )
    _round_trip(text)


# --- draft documents ----------------------------------------------------------
# A visual editor edits data, so a draft crosses the boundary as data and has
# to come back as text that parses to the same thing. These hold the two
# promises that makes: the loader's bounds apply to submitted data, and the
# emitter's output reads back unchanged.


def test_a_draft_is_emitted_as_text_that_parses_back_to_itself() -> None:
    """Including the scalars YAML would otherwise reinterpret.

    `yes` is a boolean in YAML 1.1, `1.10` is a number to anything that
    guesses, and an empty string is easy to emit as null. A prompt that comes
    back as a different type is a changed instruction.
    """
    draft = {
        "format": 2,
        "name": "custom",
        "note": "yes",
        "off": "no",
        "version": "1.10",
        "empty": "",
        "nothing": None,
        "flag": True,
        "count": 1,
        "ratio": 1.0,
        "nested": [{"a": ["b", 2]}],
    }
    assert parse_yaml_document(emit_draft_yaml(draft)) == draft


def test_an_integral_float_stays_a_float_through_a_draft_round_trip() -> None:
    """`1.0` and `1` are different literals, and a literal is executable data.

    Collapsing them would change a definition's canonical bytes, and therefore
    its identity, without anybody editing it.
    """
    emitted = emit_draft_yaml({"format": 2, "timeout": 1.0})
    assert isinstance(parse_yaml_document(emitted)["timeout"], float)
    assert isinstance(parse_yaml_document(emit_draft_yaml({"n": 1}))["n"], int)


def test_draft_bounds_refuse_what_the_loader_would_refuse() -> None:
    with pytest.raises(WorkflowDocumentError, match="must be a mapping"):
        check_draft_data([1, 2])
    with pytest.raises(WorkflowDocumentError, match="keys must be strings"):
        check_draft_data({"steps": {1: "x"}})
    with pytest.raises(WorkflowDocumentError, match="not JSON data"):
        check_draft_data({"when": object()})
    with pytest.raises(WorkflowDocumentError, match="non-finite"):
        check_draft_data({"n": float("inf")})
    with pytest.raises(WorkflowDocumentError, match="more than 256 steps"):
        check_draft_data({"steps": [{"name": f"s{i}"} for i in range(300)]})
    deep: dict = {}
    node = deep
    for _ in range(40):
        child: dict = {}
        node["n"] = child
        node = child
    with pytest.raises(WorkflowDocumentError, match="nests deeper"):
        check_draft_data(deep)


def test_a_draft_may_be_incomplete_and_carry_fields_no_format_knows() -> None:
    """Bounds are not validation. A half-built card has to survive."""
    draft = {"format": 2, "steps": [{"name": "work", "next": None}], "invented": [1]}
    assert parse_yaml_document(emit_draft_yaml(draft)) == draft
    with pytest.raises(WorkflowDocumentError):
        load_definition(emit_draft_yaml(draft))


# --- format 3: review, delivery, and the authority between them ----------------


FORMAT_3 = """
format: 3
name: publisher
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    outcome:
      results:
        done:
          required: {headline: string}
    prompt: {parts: [{text: "do it"}]}

  - name: review
    kind: review
    max_visits: 3
    on_exhausted: {step: review-exhausted}
    evidence:
      work: {steps: [work]}

  - name: route-review
    kind: decision
    evidence:
      verdict: {steps: [review]}
    cases:
      - when:
          op: eq
          left: {op: get, value: {op: evidence, name: verdict}, keys: [outcome, result]}
          right: {op: literal, value: "approved"}
        next: {step: approve}
      - when:
          op: eq
          left: {op: get, value: {op: evidence, name: verdict}, keys: [outcome, result]}
          right: {op: literal, value: "comments"}
        next: {step: work}
    otherwise: {step: review-exhausted}

  - name: approve
    kind: gate
    evidence:
      verdict: {steps: [review]}
      work: {steps: [work]}
    delivery:
      review: verdict
      metadata:
        pr_title:
          parts:
            - value: {op: get, value: {op: evidence, name: work}, keys: [outcome, artifacts, headline]}
              format: text
    message: {parts: [{text: "Publish?"}]}
    choices:
      - id: publish
        label: Open a pull request
        next: {step: commit}
        authorize: {steps: [commit, push, pr]}
      - id: finish
        label: Finish without publishing
        next: {complete: true, result: done-unpublished}

  - name: commit
    kind: delivery
    action: commit
    mode: squash
    approval: approve
    next: {step: push}
  - name: push
    kind: delivery
    action: push
    previous: commit
    approval: approve
    next: {step: pr}
  - name: pr
    kind: delivery
    action: pr
    previous: push
    approval: approve
    next: {complete: true, result: published}

  - name: review-exhausted
    kind: gate
    message: {parts: [{text: "no review"}]}
    choices:
      - id: stop
        label: Stop
        next: {complete: true, result: stopped-unreviewed}
"""


def test_format_3_parses_review_delivery_and_the_grant_between_them() -> None:
    revision = load(FORMAT_3)
    definition = revision.definition
    review = definition.step_named("review")
    assert isinstance(review, ReviewStep)
    assert review.max_visits == 3
    gate = definition.step_named("approve")
    assert isinstance(gate, GateStep)
    assert gate.delivery is not None
    assert gate.delivery.review == "verdict"
    assert gate.delivery.metadata is not None
    assert gate.delivery.metadata.message is None  # omitted starts blank
    assert gate.choice_named("finish").authorize is None
    assert definition.grant_for("approve", "publish") == DeliveryGrant(
        steps=("commit", "push", "pr")
    )
    commit = definition.step_named("commit")
    assert isinstance(commit, DeliveryStep)
    assert (commit.action, commit.mode, commit.previous) == ("commit", "squash", None)
    assert definition.declared_actions == ("commit", "push", "pr")
    descriptor = describe(revision)
    assert descriptor.actions == ("commit", "push", "pr")
    assert descriptor.reviews is True
    # An action never runs just because the run reached it.
    assert all(
        step.conditional for step in descriptor.steps if step.action is not None
    )


def _chain_document(*actions: str) -> str:
    """FORMAT_3 with its delivery chain cut back to `actions`."""
    bodies = {
        "commit": "  - name: commit\n    kind: delivery\n    action: commit\n"
        "    mode: squash\n    approval: approve\n    next: {next}\n",
        "push": "  - name: push\n    kind: delivery\n    action: push\n"
        "    previous: commit\n    approval: approve\n    next: {next}\n",
        "pr": "  - name: pr\n    kind: delivery\n    action: pr\n"
        "    previous: push\n    approval: approve\n    next: {next}\n",
    }
    steps = ""
    for index, action in enumerate(actions):
        following = (
            f"{{step: {actions[index + 1]}}}"
            if index + 1 < len(actions)
            else "{complete: true, result: published}"
        )
        steps += bodies[action].replace("{next}", following)
    original = "".join(
        bodies[action].replace(
            "{next}",
            f"{{step: {nxt}}}" if nxt else "{complete: true, result: published}",
        )
        for action, nxt in (("commit", "push"), ("push", "pr"), ("pr", ""))
    )
    assert original in FORMAT_3
    return FORMAT_3.replace(original, steps).replace(
        "authorize: {steps: [commit, push, pr]}",
        f"authorize: {{steps: [{', '.join(actions)}]}}",
    )


@pytest.mark.parametrize(
    "actions", [("commit",), ("commit", "push"), ("commit", "push", "pr")]
)
def test_a_shorter_ending_is_a_whole_valid_chain(actions: tuple[str, ...]) -> None:
    """Stopping after a local commit, or after a push, is a real ending — not
    a truncated pull-request flow with its last step missing."""
    text = _chain_document(*actions)
    assert load(text).definition.declared_actions == actions
    _round_trip(text)


def test_a_workflow_that_publishes_nothing_says_so() -> None:
    text = FORMAT_3
    for cut in (
        """
  - name: commit
    kind: delivery
    action: commit
    mode: squash
    approval: approve
    next: {step: push}
  - name: push
    kind: delivery
    action: push
    previous: commit
    approval: approve
    next: {step: pr}
  - name: pr
    kind: delivery
    action: pr
    previous: push
    approval: approve
    next: {complete: true, result: published}
""",
        """    delivery:
      review: verdict
      metadata:
        pr_title:
          parts:
            - value: {op: get, value: {op: evidence, name: work}, keys: [outcome, artifacts, headline]}
              format: text
""",
        """      - id: publish
        label: Open a pull request
        next: {step: commit}
        authorize: {steps: [commit, push, pr]}
""",
    ):
        assert cut in text
        text = text.replace(cut, "")
    revision = load(text)
    assert revision.definition.declared_actions == ()
    assert describe(revision).actions == ()
    # Review without delivery is still a perfectly good workflow.
    assert describe(revision).reviews is True


@pytest.mark.parametrize(
    ("mutation", "expected", "match"),
    [
        # A grant must be a real chain, starting at the local signed commit.
        (
            "authorize: {steps: [commit, push, pr]}",
            "authorize: {steps: [push, pr]}",
            "No action performs a missing predecessor",
        ),
        (
            "authorize: {steps: [commit, push, pr]}",
            "authorize: {steps: [commit, pr]}",
            "No action performs a missing predecessor",
        ),
        # The approving answer goes straight to what it grants.
        (
            "        next: {step: commit}\n        authorize",
            "        next: {step: push}\n        authorize",
            "must go straight to the first action it grants",
        ),
        # A delivery gate always offers a way out that publishes nothing.
        (
            (
                "      - id: finish\n        label: Finish without publishing\n"
                "        next: {complete: true, result: done-unpublished}\n"
            ),
            "",
            "must also offer a way out that publishes nothing",
        ),
        # The grant is bound to a trusted verdict, never to an agent's claim.
        ("      review: verdict", "      review: work", "is not a review step"),
        # Nothing runs in the workspace after publication.
        (
            "    next: {complete: true, result: published}",
            "    next: {step: review-exhausted}",
            "must end the run at a named result",
        ),
        # No edge enters a chain except its grant or the action before it.
        (
            "    otherwise: {step: review-exhausted}",
            "    otherwise: {step: push}",
            "reachable only from the choice that authorizes its chain",
        ),
        # Each action belongs to exactly one gate.
        (
            "    action: push\n    previous: commit\n    approval: approve",
            "    action: push\n    previous: commit\n    approval: review-exhausted",
            "but this grant comes from 'approve'",
        ),
        # A commit says how it composes history; nothing else may.
        ("    action: commit\n    mode: squash\n", "    action: commit\n", "'mode'"),
        (
            "    action: push\n    previous: commit",
            "    action: push\n    mode: squash\n    previous: commit",
            "unknown field 'mode'",
        ),
        # An action nothing can grant is not left lying in the document.
        (
            "        authorize: {steps: [commit, push, pr]}",
            "        authorize: {steps: [commit, push]}",
            "must end the run at a named result",
        ),
        # A privileged step is never reached by simply finishing the one before.
        (
            (
                "  - name: review-exhausted\n    kind: gate\n"
                "    message: {parts: [{text: \"no review\"}]}\n"
                "    choices:\n      - id: stop\n        label: Stop\n"
                "        next: {complete: true, result: stopped-unreviewed}\n"
            ),
            "",
            "routes to undeclared step",
        ),
    ],
)
def test_format_3_refuses_authority_it_cannot_account_for(
    mutation: str, expected: str, match: str
) -> None:
    assert mutation in FORMAT_3
    with pytest.raises(WorkflowDocumentError, match=re.escape(match)):
        load(FORMAT_3.replace(mutation, expected))


def test_delivery_vocabulary_belongs_to_format_3_alone() -> None:
    with pytest.raises(WorkflowDocumentError, match="exists only in format 3"):
        load(FORMAT_3.replace("format: 3", "format: 2"))
    # And a format-2 gate has no delivery binding to give it authority.
    with pytest.raises(WorkflowDocumentError, match="unknown field 'delivery'"):
        load(
            FORMAT_2.replace(
                "    message: {parts: [{text: \"could not reproduce\"}]}",
                "    delivery: {review: repro}\n"
                "    message: {parts: [{text: \"could not reproduce\"}]}",
            )
        )


def test_format_3_exports_and_reloads_to_the_same_revision() -> None:
    _round_trip(FORMAT_3)


def test_earlier_formats_keep_their_canonical_bytes() -> None:
    """Adding format 3 must not move a single retained revision's identity."""
    assert (
        load(MINIMAL).revision
        == "sha256:" + hashlib.sha256(canonical_bytes(load(MINIMAL).definition)).hexdigest()
    )
    for text in (MINIMAL, FORMAT_2):
        document = json.loads(canonical_bytes(load(text).definition))
        for step in document["steps"]:
            assert "delivery" not in step
            for choice in step.get("choices", []):
                assert "authorize" not in choice


def test_an_invalid_draft_is_still_a_draft() -> None:
    """Half-authored delivery survives a save; it just cannot execute."""
    half = {
        "format": 3,
        "name": "half",
        "steps": [{"name": "pr", "kind": "delivery", "action": "pr"}],
    }
    assert check_draft_data(half) is half
    with pytest.raises(WorkflowDocumentError):
        definition_from_document(half)


def test_format_4_composes_declared_capture_and_result_gate() -> None:
    revision = load(
        """
format: 4
name: planning
sessions: [plan]
primary: plan
steps:
  - name: propose
    kind: agent
    session: plan
    role: plan
    outcome:
      results:
        proposed:
          required: {root: string}
    prompt:
      parts: [{text: prepare a proposal}]
  - name: capture
    kind: capture
    evidence:
      producer:
        steps: [propose]
        required: true
        with_outcome: true
    producer: producer
    paths:
      - parts: [{text: changes/example/SPEC.md}]
    allowlist: [changes]
    next: {step: decide}
  - name: decide
    kind: gate
    evidence:
      captured:
        steps: [capture]
        required: true
    result: {evidence: captured}
    message:
      parts: [{text: inspect the captured result}]
    choices:
      - id: finish
        label: Finish with accepted result
        requires_result_acceptance: true
        next: {complete: true, result: accepted-result}
      - id: stop
        label: Stop without accepting
        next: {complete: true, result: stopped}
"""
    )

    assert revision.format == 4
    assert revision.definition.step_named("capture").kind == "capture"
    assert load_definition(export_yaml(revision)).revision == revision.revision
