"""The declarative workflow document: formats 1 and 2.

A workflow definition is *data*. It is authored as a bounded YAML subset,
normalized into one canonical JSON document, and identified by the SHA-256 of
those canonical bytes. Nothing in here imports a task, opens a connection,
touches the filesystem, or executes anything: this module answers "what does
this document mean", and `workflows.py` answers "how is that carried out".

Three properties are load-bearing, and each one is a rule below rather than a
convention:

*Bounded.* The loader inspects parser events before any object is built, so
depth, node count, and document size bound the parse itself and not just the
result. Anchors, aliases, merge keys, tags, duplicate keys, non-string mapping
keys, extra documents, and YAML 1.1's `yes`/`no`/timestamp scalars are all
refused. Scalars resolve by JSON's rules, so `no` is the string "no".

*Non-executable.* Prompts are ordered part lists, not templates: there is no
expression source code, no attribute traversal, no second interpolation pass,
and no way to name a file, an environment variable, or a Python object. A
value expression is a tagged data node (`op`), so the set of things a
definition can ask for is closed and enumerable.

*Identical means identical.* `canonical_document` fills in every default and
sorts every mapping key, so YAML comments and key order do not change a
revision, while executable strings and sequence order do. Two documents with
the same revision therefore mean the same thing to the same interpreter
version, which is what pinning a revision to a task is worth.

Uncertainty is explicit. Predicates are three-valued: a missing operand or a
type error is `Unresolved`, never `False`, so a decision that cannot be made
pauses for the operator instead of guessing a route.

Format 1's interpreter semantics are frozen. A change to what a retained
document *means* requires a new format version; a retained format-1 document
is always read under format-1 rules, and its canonical bytes — and therefore
its revision identity — are unchanged by anything format 2 adds.

*Format 2* adds the vocabulary a domain flow needs and takes away the two
places format 1 let meaning leak:

- An agent step declares an `outcome` contract: the named results it may
  produce and, per result, the artifact fields that result must carry. A
  result outside the contract, or missing a required field, is not a result.
- A step declares its `evidence`: named selectors over prior attempts,
  resolved *once* at attempt entry and then frozen. `{op: evidence, name: …}`
  reads one of those bindings, so what a prompt or a route saw is a recorded
  fact rather than whatever "latest" would mean when it is asked again.
  Format 2 therefore has no `latest` operation, and format 1 has no `evidence`.
- A gate declares named `choices` with static destinations, so answering it is
  choosing a declared route rather than pressing Resume.
- Completion is named: `{complete: true, result: <slug>}`. Falling off the end
  of the step list is rejected, because "the run ended" is not a work result.

ADR-0009, ADR-0028, ADR-0029, ADR-0030
(docs/adr/0029-declare-domain-outcomes-and-evidence-handoffs.md,
docs/adr/0030-commit-human-decisions-before-advancing.md)
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import yaml

from ompire_daemon.model_config import MODEL_ROLES

# The document format *and* the interpreter semantics. Bumping this is a
# statement that the meaning of a document changed, not that a field was
# added: a retained format-1 document keeps being read under format-1 rules
# forever, and an unsupported version is refused rather than reinterpreted.
FORMAT_VERSION = 2
SUPPORTED_FORMATS = (1, 2)

# Loader bounds. They protect the daemon from a hostile or accidental
# document; every packaged built-in is orders of magnitude below them.
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 10_000
MAX_STEPS = 256
MAX_RENDERED_BYTES = 1024 * 1024

# Names format 1 keeps for the engine. `judge` is reserved even though no
# implicit judge executes any more: a future declared judge step must not
# collide with the retired session name still present in legacy history.
RESERVED_NAMES = ("judge",)

STEP_KINDS = ("agent", "command", "decision", "gate")
VALUE_FORMATS = ("text", "json")
JSON_TYPES = ("null", "boolean", "integer", "number", "string", "array", "object")
COMPARISONS = ("eq", "ne", "lt", "lte", "gt", "gte")

DEFAULT_COMMAND_TIMEOUT = 600.0
DEFAULT_ROLE = "default"

# Format-2 bounds. Like the loader bounds above these exist so a definition
# cannot grow without limit; they are not a statement about what is useful.
MAX_RESULTS = 16
MAX_REQUIRED_FIELDS = 32
MAX_EVIDENCE = 16
MAX_CHOICES = 8

# What a required artifact field may be declared as. `null` is absent from the
# list on purpose: a required field whose accepted type is "nothing" would let
# an agent satisfy the contract by writing nothing, which is the whole failure
# mode the contract exists to close.
REQUIRED_TYPES = ("boolean", "integer", "number", "string", "array", "object")

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# JSON's own number grammar. YAML 1.1 octals, sexagesimals, `.inf`, and `.nan`
# are deliberately not here.
_INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?$")


class WorkflowDocumentError(ValueError):
    """A document is not a valid format-1 workflow.

    `location` addresses the offending place in the document the way the
    author wrote it (`steps[2].prompt.parts[0]`), so a refusal says where to
    look rather than only that something was wrong.
    """

    def __init__(self, location: str, reason: str) -> None:
        super().__init__(f"{location}: {reason}" if location else reason)
        self.location = location
        self.reason = reason


class UnsupportedWorkflowFormatError(WorkflowDocumentError):
    """The document declares a format this interpreter does not implement.

    Refused, never read under the newest rules: a retained revision written by
    a later daemon means something this one cannot promise to reproduce.
    """

    def __init__(self, version: object) -> None:
        super().__init__(
            "format",
            f"workflow format {version!r} is not supported; this daemon "
            f"understands {', '.join(str(v) for v in SUPPORTED_FORMATS)}",
        )
        self.version = version


# --- the safe loader ----------------------------------------------------------


class _Missing:
    """Absence, distinct from `null` and from an empty string.

    A step that never ran, an outcome key that was not written, and a record
    filtered out by `with_outcome` are all *missing*. `null` is a value the
    agent wrote. Conflating them is how "no evidence" silently becomes "a
    negative result", which is the failure this whole type exists to prevent.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def _resolve_scalar(node: yaml.ScalarNode) -> Any:
    """JSON's scalar rules, not YAML 1.1's.

    A quoted or block scalar is always a string. A plain scalar is a string
    unless it is exactly a JSON literal, so `yes`, `on`, `2026-09-06`, and
    `.inf` are all ordinary text — which is what an author writing prose in a
    prompt means by them.
    """
    if node.style is not None:
        return node.value
    raw = node.value
    if raw == "" or raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if _INT_RE.match(raw):
        return int(raw)
    if _FLOAT_RE.match(raw) and ("." in raw or "e" in raw or "E" in raw):
        value = float(raw)
        if not math.isfinite(value):
            raise WorkflowDocumentError("", f"non-finite number {raw!r}")
        return value
    return raw


def _check_events(text: str) -> None:
    """Bound the parse itself, before any node object exists.

    Depth and node count are checked over the event stream, so a document that
    would expand into something enormous is refused while it is still being
    parsed rather than after it has been built.
    """
    depth = 0
    nodes = 0
    documents = 0
    try:
        events = yaml.parse(text, Loader=yaml.SafeLoader)
        for event in events:
            if isinstance(event, yaml.AliasEvent):
                raise WorkflowDocumentError(
                    "", "YAML aliases are not allowed in a workflow document"
                )
            if isinstance(event, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise WorkflowDocumentError(
                        "", "a workflow document must be a single YAML document"
                    )
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                nodes += 1
                if depth > MAX_DEPTH:
                    raise WorkflowDocumentError(
                        "", f"document nests deeper than {MAX_DEPTH} levels"
                    )
                if event.anchor is not None or (
                    event.tag is not None and not event.implicit
                ):
                    raise WorkflowDocumentError(
                        "", "YAML anchors and explicit tags are not allowed"
                    )
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            elif isinstance(event, yaml.ScalarEvent):
                nodes += 1
                if event.anchor is not None:
                    raise WorkflowDocumentError(
                        "", "YAML anchors and explicit tags are not allowed"
                    )
                if event.tag is not None and event.tag not in (
                    "tag:yaml.org,2002:str",
                    "tag:yaml.org,2002:null",
                    "tag:yaml.org,2002:bool",
                    "tag:yaml.org,2002:int",
                    "tag:yaml.org,2002:float",
                ):
                    raise WorkflowDocumentError(
                        "", f"YAML tag {event.tag!r} is not allowed"
                    )
            if nodes > MAX_NODES:
                raise WorkflowDocumentError(
                    "", f"document has more than {MAX_NODES} nodes"
                )
    except yaml.YAMLError as exc:
        raise WorkflowDocumentError("", f"not valid YAML: {exc}") from exc


def _node_to_data(node: yaml.Node, location: str) -> Any:
    if isinstance(node, yaml.ScalarNode):
        try:
            return _resolve_scalar(node)
        except WorkflowDocumentError as exc:
            raise WorkflowDocumentError(location, exc.reason) from None
    if isinstance(node, yaml.SequenceNode):
        return [
            _node_to_data(child, f"{location}[{index}]")
            for index, child in enumerate(node.value)
        ]
    if isinstance(node, yaml.MappingNode):
        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or (
                key_node.style is None
                and _resolve_scalar(key_node) is not None
                and not isinstance(_resolve_scalar(key_node), str)
            ):
                raise WorkflowDocumentError(location, "mapping keys must be strings")
            key = key_node.value
            if key == "<<":
                raise WorkflowDocumentError(location, "YAML merge keys are not allowed")
            if key in result:
                raise WorkflowDocumentError(location, f"duplicate key {key!r}")
            result[key] = _node_to_data(
                value_node, f"{location}.{key}" if location else key
            )
        return result
    raise WorkflowDocumentError(location, "unsupported YAML node")  # pragma: no cover


def parse_yaml_document(text: str) -> dict[str, Any]:
    """The bounded YAML subset, as plain JSON data. No definition yet."""
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise WorkflowDocumentError(
            "", f"document is larger than {MAX_DOCUMENT_BYTES} bytes"
        )
    _check_events(text)
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:  # pragma: no cover - _check_events parses first
        raise WorkflowDocumentError("", f"not valid YAML: {exc}") from exc
    if node is None:
        raise WorkflowDocumentError("", "document is empty")
    data = _node_to_data(node, "")
    if not isinstance(data, dict):
        raise WorkflowDocumentError("", "document root must be a mapping")
    return data


# --- value expressions --------------------------------------------------------

# The task and workspace facts a prompt may read. Deliberately a closed list of
# accepted, already-pinned inputs: there is no lookup into today's project,
# profile, environment, or filesystem.
INPUT_NAMES = ("task.prompt", "task.slug", "task.branch", "workspace.preamble")


@dataclass(frozen=True)
class LiteralValue:
    value: Any


@dataclass(frozen=True)
class InputValue:
    name: str


@dataclass(frozen=True)
class LatestValue:
    """The newest finished `ok` attempt among `steps`.

    Yields a record view — `{step, seq, status, outcome}` — rather than the
    outcome alone, because "which step answered" is itself routing evidence:
    bugfix must distinguish a script exit code from an agent verdict, and a
    bare outcome could not say which it was holding.

    `after` selects only attempts newer than the latest attempt of that anchor
    step whatever its status, which is how one fix iteration's validation is
    kept from being answered by the previous iteration's. `with_outcome`
    excludes null-outcome attempts before selection, so a deliberately
    unprompted step does not mask the real evidence beneath it.
    """

    steps: tuple[str, ...]
    after: str | None
    with_outcome: bool


@dataclass(frozen=True)
class GetValue:
    """Literal key traversal into JSON data. Never a Python attribute."""

    value: ValueExpr
    keys: tuple[Any, ...]


@dataclass(frozen=True)
class CountValue:
    step: str


@dataclass(frozen=True)
class EvidenceValue:
    """One of *this step's* declared evidence bindings, by alias (format 2).

    The difference from `LatestValue` is when the question is asked. `latest`
    re-scans history every time it is evaluated, so a prompt and the decision
    that routes on it can disagree, and a restart can answer with a record
    that did not exist when the attempt opened. An evidence alias is resolved
    once, at attempt entry, and recorded on the attempt: every later read —
    prompt, predicate, gate message, recovery — sees that same record.
    """

    name: str


@dataclass(frozen=True)
class CoalesceValue:
    """First non-missing, non-null value. An empty string is a value."""

    values: tuple[ValueExpr, ...]


ValueExpr = (
    LiteralValue
    | InputValue
    | LatestValue
    | EvidenceValue
    | GetValue
    | CountValue
    | CoalesceValue
)


# --- predicates ---------------------------------------------------------------


@dataclass(frozen=True)
class Unresolved:
    """Neither true nor false: the evidence to decide is missing or malformed.

    Carries the reason so a pause can say what it was waiting on rather than
    only that it stopped.
    """

    reason: str


@dataclass(frozen=True)
class ComparePredicate:
    op: str
    left: ValueExpr
    right: ValueExpr


@dataclass(frozen=True)
class ExistsPredicate:
    value: ValueExpr


@dataclass(frozen=True)
class IsTypePredicate:
    value: ValueExpr
    type: str


@dataclass(frozen=True)
class JunctionPredicate:
    op: str  # "all" | "any"
    of: tuple[Predicate, ...]


@dataclass(frozen=True)
class NotPredicate:
    of: Predicate


@dataclass(frozen=True)
class LiteralPredicate:
    value: bool


Predicate = (
    ComparePredicate
    | ExistsPredicate
    | IsTypePredicate
    | JunctionPredicate
    | NotPredicate
    | LiteralPredicate
)


# --- text documents -----------------------------------------------------------


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ValuePart:
    value: ValueExpr
    format: str  # "text" | "json"


@dataclass(frozen=True)
class ConditionalPart:
    when: Predicate
    then: TextDocument
    otherwise: TextDocument


Part = TextPart | ValuePart | ConditionalPart


@dataclass(frozen=True)
class TextDocument:
    """Ordered parts joined by a literal separator.

    An empty document renders to the empty string, which is a real and
    deliberate answer: an agent step whose prompt renders empty spends no turn.
    """

    separator: str
    parts: tuple[Part, ...]


EMPTY_TEXT = TextDocument(separator="", parts=())


# --- destinations and steps ---------------------------------------------------


@dataclass(frozen=True)
class StepDestination:
    step: str


@dataclass(frozen=True)
class CompleteDestination:
    """The run ends here.

    In format 2 `result` names *which* ending this is, because "the workflow
    finished" and "the bug was fixed" are different facts and a run that
    conflates them cannot be read afterwards. Format 1 has no name for its
    ending, so `result` is None there and stays None.
    """

    result: str | None = None


@dataclass(frozen=True)
class PauseDestination:
    pass


Destination = StepDestination | CompleteDestination | PauseDestination


@dataclass(frozen=True)
class DecisionCase:
    when: Predicate
    next: Destination


# --- format 2: result contracts, evidence selectors, gate choices -------------


@dataclass(frozen=True)
class ResultContract:
    """One declared result name and the artifact fields it must carry.

    `required` is a sorted tuple of `(field, json type)` pairs rather than a
    dict so the contract stays hashable and frozen like everything else here.
    The types are JSON's, checked structurally: this says a `findings` field
    is a nonblank string, never that its content is true.
    """

    name: str
    required: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OutcomeContract:
    """What results an agent step may declare. Nonempty by construction."""

    results: tuple[ResultContract, ...]

    def result_named(self, name: str) -> ResultContract | None:
        for result in self.results:
            if result.name == name:
                return result
        return None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(result.name for result in self.results)


@dataclass(frozen=True)
class EvidenceSelector:
    """A named selection over prior attempts, resolved once at attempt entry.

    `steps`, `after`, and `with_outcome` mean exactly what they mean for
    format 1's `latest` — the newest finished `ok` attempt among `steps`,
    newer than the anchor step's own newest attempt, optionally requiring a
    recorded outcome. `required` is what happens when nothing matches: a
    required selector pauses the attempt before it prompts or routes, an
    optional one binds to explicit absence.
    """

    name: str
    steps: tuple[str, ...]
    after: str | None
    with_outcome: bool
    required: bool


@dataclass(frozen=True)
class GateChoice:
    """One answer a person may give, and where it goes.

    The destination is static: a choice cannot compute a route, and it cannot
    pause. Answering a gate is picking a declared edge, which is what makes
    the decision replayable from the record and refusable when stale.
    """

    id: str
    label: str
    feedback_required: bool
    next: Destination


@dataclass(frozen=True)
class AgentStep:
    name: str
    session: str
    role: str
    prompt: TextDocument
    expects_outcome: bool  # format 1's generic success/failed envelope
    when: Predicate
    max_visits: int | None
    on_exhausted: StepDestination | None
    outcome: OutcomeContract | None = None  # format 2's declared results
    evidence: tuple[EvidenceSelector, ...] = ()
    kind: str = "agent"

    @property
    def requires_outcome(self) -> bool:
        """Whether this step must produce a result document to finish."""
        return self.expects_outcome or self.outcome is not None


@dataclass(frozen=True)
class CommandStep:
    name: str
    argv: tuple[str, ...]
    timeout: float
    idempotent: bool
    max_visits: int | None
    on_exhausted: StepDestination | None
    evidence: tuple[EvidenceSelector, ...] = ()
    kind: str = "command"


@dataclass(frozen=True)
class DecisionStep:
    name: str
    cases: tuple[DecisionCase, ...]
    otherwise: Destination
    max_visits: int | None
    on_exhausted: StepDestination | None
    evidence: tuple[EvidenceSelector, ...] = ()
    kind: str = "decision"


@dataclass(frozen=True)
class GateStep:
    name: str
    message: TextDocument
    max_visits: int | None
    on_exhausted: StepDestination | None
    choices: tuple[GateChoice, ...] = ()  # format 2; empty means fall-through
    evidence: tuple[EvidenceSelector, ...] = ()
    kind: str = "gate"

    def choice_named(self, choice_id: str) -> GateChoice | None:
        for choice in self.choices:
            if choice.id == choice_id:
                return choice
        return None


Step = AgentStep | CommandStep | DecisionStep | GateStep


@dataclass(frozen=True)
class WorkflowDefinition:
    format: int
    name: str
    sessions: tuple[str, ...]
    primary: str
    steps: tuple[Step, ...]

    def step_named(self, name: str) -> Step | None:
        for step in self.steps:
            if step.name == name:
                return step
        return None

    def step_after(self, name: str) -> Step | None:
        """The fall-through target: the step declared after `name`."""
        names = [step.name for step in self.steps]
        try:
            index = names.index(name)
        except ValueError:
            return None
        return self.steps[index + 1] if index + 1 < len(self.steps) else None

    def agent_steps(self) -> tuple[AgentStep, ...]:
        return tuple(step for step in self.steps if isinstance(step, AgentStep))


@dataclass(frozen=True)
class WorkflowRevision:
    """One retained definition and the identity of its exact content."""

    revision: str
    definition: WorkflowDefinition
    document: dict[str, Any]

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def format(self) -> int:
        return self.definition.format


# --- parsing ------------------------------------------------------------------


def _require_mapping(data: Any, location: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise WorkflowDocumentError(location, "must be a mapping")
    return data


def _reject_unknown(data: Mapping[str, Any], allowed: Sequence[str], location: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise WorkflowDocumentError(
            location,
            f"unknown field{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(name) for name in unknown)}; "
            f"allowed: {', '.join(sorted(allowed))}",
        )


def _require_str(data: Mapping[str, Any], key: str, location: str) -> str:
    if key not in data:
        raise WorkflowDocumentError(location, f"missing required field {key!r}")
    value = data[key]
    if not isinstance(value, str):
        raise WorkflowDocumentError(f"{location}.{key}", "must be a string")
    return value


def _require_slug(data: Mapping[str, Any], key: str, location: str) -> str:
    value = _require_str(data, key, location)
    if not _SLUG_RE.match(value):
        raise WorkflowDocumentError(
            f"{location}.{key}", f"{value!r} is not slug-format (a-z, 0-9, hyphens)"
        )
    return value


def _is_json_data(value: Any, depth: int = 0) -> bool:
    if depth > MAX_DEPTH:
        return False
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_data(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_data(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _parse_value(data: Any, location: str, version: int) -> ValueExpr:
    data = _require_mapping(data, location)
    op = data.get("op")
    if not isinstance(op, str):
        raise WorkflowDocumentError(location, "value expression needs a string 'op'")
    if op == "latest" and version != 1:
        raise WorkflowDocumentError(
            location,
            "'latest' belongs to format 1; format 2 reads prior attempts "
            "through a step's declared 'evidence' selectors, which are "
            "resolved once when the attempt opens",
        )
    if op == "evidence" and version == 1:
        raise WorkflowDocumentError(
            location, "'evidence' is a format-2 value operation"
        )
    if op == "evidence":
        _reject_unknown(data, ("op", "name"), location)
        return EvidenceValue(name=_require_slug(data, "name", location))
    if op == "literal":
        _reject_unknown(data, ("op", "value"), location)
        if "value" not in data:
            raise WorkflowDocumentError(location, "missing required field 'value'")
        value = data["value"]
        if not _is_json_data(value):
            raise WorkflowDocumentError(f"{location}.value", "must be JSON data")
        return LiteralValue(value=value)
    if op == "input":
        _reject_unknown(data, ("op", "name"), location)
        name = _require_str(data, "name", location)
        if name not in INPUT_NAMES:
            raise WorkflowDocumentError(
                f"{location}.name",
                f"unknown input {name!r}; available: {', '.join(INPUT_NAMES)}",
            )
        return InputValue(name=name)
    if op == "latest":
        _reject_unknown(data, ("op", "steps", "after", "with_outcome"), location)
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise WorkflowDocumentError(
                f"{location}.steps", "must be a nonempty list of step names"
            )
        steps: list[str] = []
        for index, item in enumerate(raw_steps):
            if not isinstance(item, str):
                raise WorkflowDocumentError(
                    f"{location}.steps[{index}]", "must be a step name"
                )
            steps.append(item)
        after = data.get("after")
        if after is not None and not isinstance(after, str):
            raise WorkflowDocumentError(f"{location}.after", "must be a step name")
        with_outcome = data.get("with_outcome", False)
        if not isinstance(with_outcome, bool):
            raise WorkflowDocumentError(f"{location}.with_outcome", "must be a boolean")
        return LatestValue(
            steps=tuple(steps), after=after, with_outcome=with_outcome
        )
    if op == "get":
        _reject_unknown(data, ("op", "value", "keys"), location)
        if "value" not in data:
            raise WorkflowDocumentError(location, "missing required field 'value'")
        raw_keys = data.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise WorkflowDocumentError(
                f"{location}.keys", "must be a nonempty list of literal keys"
            )
        for index, key in enumerate(raw_keys):
            if not isinstance(key, (str, int)) or isinstance(key, bool):
                raise WorkflowDocumentError(
                    f"{location}.keys[{index}]",
                    "must be a string key or an integer index",
                )
        return GetValue(
            value=_parse_value(data["value"], f"{location}.value", version),
            keys=tuple(raw_keys),
        )
    if op == "count":
        _reject_unknown(data, ("op", "step"), location)
        return CountValue(step=_require_str(data, "step", location))
    if op == "coalesce":
        _reject_unknown(data, ("op", "values"), location)
        raw_values = data.get("values")
        if not isinstance(raw_values, list) or not raw_values:
            raise WorkflowDocumentError(
                f"{location}.values", "must be a nonempty list of value expressions"
            )
        return CoalesceValue(
            values=tuple(
                _parse_value(item, f"{location}.values[{index}]", version)
                for index, item in enumerate(raw_values)
            )
        )
    raise WorkflowDocumentError(location, f"unknown value operation {op!r}")


def _parse_predicate(data: Any, location: str, version: int) -> Predicate:
    if isinstance(data, bool):
        return LiteralPredicate(value=data)
    data = _require_mapping(data, location)
    op = data.get("op")
    if not isinstance(op, str):
        raise WorkflowDocumentError(location, "predicate needs a string 'op'")
    if op in COMPARISONS:
        _reject_unknown(data, ("op", "left", "right"), location)
        for key in ("left", "right"):
            if key not in data:
                raise WorkflowDocumentError(location, f"missing required field {key!r}")
        return ComparePredicate(
            op=op,
            left=_parse_value(data["left"], f"{location}.left", version),
            right=_parse_value(data["right"], f"{location}.right", version),
        )
    if op == "exists":
        _reject_unknown(data, ("op", "value"), location)
        if "value" not in data:
            raise WorkflowDocumentError(location, "missing required field 'value'")
        return ExistsPredicate(
            value=_parse_value(data["value"], f"{location}.value", version)
        )
    if op == "is_type":
        _reject_unknown(data, ("op", "value", "type"), location)
        if "value" not in data:
            raise WorkflowDocumentError(location, "missing required field 'value'")
        json_type = _require_str(data, "type", location)
        if json_type not in JSON_TYPES:
            raise WorkflowDocumentError(
                f"{location}.type",
                f"unknown JSON type {json_type!r}; one of {', '.join(JSON_TYPES)}",
            )
        return IsTypePredicate(
            value=_parse_value(data["value"], f"{location}.value", version),
            type=json_type,
        )
    if op in ("all", "any"):
        _reject_unknown(data, ("op", "of"), location)
        raw = data.get("of")
        if not isinstance(raw, list) or not raw:
            raise WorkflowDocumentError(
                f"{location}.of", "must be a nonempty list of predicates"
            )
        return JunctionPredicate(
            op=op,
            of=tuple(
                _parse_predicate(item, f"{location}.of[{index}]", version)
                for index, item in enumerate(raw)
            ),
        )
    if op == "not":
        _reject_unknown(data, ("op", "of"), location)
        if "of" not in data:
            raise WorkflowDocumentError(location, "missing required field 'of'")
        return NotPredicate(of=_parse_predicate(data["of"], f"{location}.of", version))
    raise WorkflowDocumentError(location, f"unknown predicate operation {op!r}")


def _parse_text(data: Any, location: str, version: int) -> TextDocument:
    data = _require_mapping(data, location)
    _reject_unknown(data, ("separator", "parts"), location)
    separator = data.get("separator", "")
    if not isinstance(separator, str):
        raise WorkflowDocumentError(f"{location}.separator", "must be a string")
    raw_parts = data.get("parts")
    if raw_parts is None:
        raise WorkflowDocumentError(location, "missing required field 'parts'")
    if not isinstance(raw_parts, list):
        raise WorkflowDocumentError(f"{location}.parts", "must be a list")
    parts: list[Part] = []
    for index, item in enumerate(raw_parts):
        parts.append(_parse_part(item, f"{location}.parts[{index}]", version))
    return TextDocument(separator=separator, parts=tuple(parts))


def _parse_part(data: Any, location: str, version: int) -> Part:
    data = _require_mapping(data, location)
    present = sorted({"text", "value", "if"} & set(data))
    if len(present) != 1:
        raise WorkflowDocumentError(
            location,
            "a text part is exactly one of 'text', 'value', or 'if'"
            + (f"; found {', '.join(present)}" if present else ""),
        )
    kind = present[0]
    if kind == "text":
        _reject_unknown(data, ("text",), location)
        value = data["text"]
        if not isinstance(value, str):
            raise WorkflowDocumentError(f"{location}.text", "must be a string")
        return TextPart(text=value)
    if kind == "value":
        _reject_unknown(data, ("value", "format"), location)
        value_format = data.get("format", "text")
        if value_format not in VALUE_FORMATS:
            raise WorkflowDocumentError(
                f"{location}.format",
                f"must be one of {', '.join(VALUE_FORMATS)}",
            )
        return ValuePart(
            value=_parse_value(data["value"], f"{location}.value", version),
            format=value_format,
        )
    _reject_unknown(data, ("if", "then", "else"), location)
    if "then" not in data:
        raise WorkflowDocumentError(location, "a conditional part needs 'then'")
    return ConditionalPart(
        when=_parse_predicate(data["if"], f"{location}.if", version),
        then=_parse_text(data["then"], f"{location}.then", version),
        otherwise=(
            _parse_text(data["else"], f"{location}.else", version)
            if "else" in data
            else EMPTY_TEXT
        ),
    )


def _parse_destination(
    data: Any, location: str, version: int, *, allow_pause: bool = True
) -> Destination:
    data = _require_mapping(data, location)
    present = sorted({"step", "complete", "pause"} & set(data))
    if len(present) != 1:
        raise WorkflowDocumentError(
            location,
            "a destination is exactly one of 'step', 'complete', or 'pause'"
            if allow_pause
            else "a destination is exactly one of 'step' or 'complete'",
        )
    if present[0] == "pause" and not allow_pause:
        raise WorkflowDocumentError(
            location,
            "this destination cannot be a pause: it must name where the run "
            "goes, not ask the engine to stop again",
        )
    _reject_unknown(
        data,
        ("complete", "result") if present[0] == "complete" and version >= 2 else (present[0],),
        location,
    )
    if present[0] == "step":
        value = data["step"]
        if not isinstance(value, str):
            raise WorkflowDocumentError(f"{location}.step", "must be a step name")
        return StepDestination(step=value)
    if data[present[0]] is not True:
        raise WorkflowDocumentError(
            f"{location}.{present[0]}", "must be literally true"
        )
    if present[0] == "pause":
        return PauseDestination()
    if version == 1:
        return CompleteDestination()
    # Format 2: an ending has a name. "The run stopped" is not a work result,
    # and a reader months later cannot tell a validated fix from an abandoned
    # investigation if both simply ended.
    return CompleteDestination(result=_require_slug(data, "result", location))


_COMMON_STEP_FIELDS = ("name", "kind", "max_visits", "on_exhausted")


def _parse_bound(
    data: Mapping[str, Any], location: str, version: int
) -> tuple[int | None, StepDestination | None]:
    max_visits = data.get("max_visits")
    if max_visits is not None and (
        not isinstance(max_visits, int)
        or isinstance(max_visits, bool)
        or max_visits < 1
    ):
        raise WorkflowDocumentError(
            f"{location}.max_visits", "must be a positive integer"
        )
    on_exhausted_raw = data.get("on_exhausted")
    on_exhausted: StepDestination | None = None
    if on_exhausted_raw is not None:
        destination = _parse_destination(
            on_exhausted_raw, f"{location}.on_exhausted", version, allow_pause=False
        )
        if not isinstance(destination, StepDestination):
            raise WorkflowDocumentError(
                f"{location}.on_exhausted",
                "must name a declared gate step; a bound cannot complete or "
                "pause the run implicitly",
            )
        on_exhausted = destination
    if (max_visits is None) != (on_exhausted is None):
        raise WorkflowDocumentError(
            location, "'max_visits' and 'on_exhausted' must be declared together"
        )
    return max_visits, on_exhausted


def _parse_outcome(data: Any, location: str) -> OutcomeContract | None:
    """An agent step's declared results (format 2).

    `null` is a real declaration — "this step is not asked for a result" — and
    is why the field is required rather than defaulted: a step that silently
    produced no contract would be indistinguishable from one whose author
    forgot, and only one of those should be allowed to finish on nothing.
    """
    if data is None:
        return None
    data = _require_mapping(data, location)
    _reject_unknown(data, ("results",), location)
    raw_results = data.get("results")
    if not isinstance(raw_results, dict) or not raw_results:
        raise WorkflowDocumentError(
            f"{location}.results",
            "must be a nonempty mapping from result name to its contract",
        )
    if len(raw_results) > MAX_RESULTS:
        raise WorkflowDocumentError(
            f"{location}.results", f"more than {MAX_RESULTS} declared results"
        )
    results: list[ResultContract] = []
    for result_name in sorted(raw_results):
        result_location = f"{location}.results.{result_name}"
        if not _SLUG_RE.match(result_name):
            raise WorkflowDocumentError(
                result_location,
                "a result name must be slug-format (a-z, 0-9, hyphens)",
            )
        contract = _require_mapping(raw_results[result_name], result_location)
        _reject_unknown(contract, ("required",), result_location)
        raw_required = contract.get("required", {})
        if not isinstance(raw_required, dict):
            raise WorkflowDocumentError(
                f"{result_location}.required",
                "must be a mapping from artifact field name to its JSON type",
            )
        if len(raw_required) > MAX_REQUIRED_FIELDS:
            raise WorkflowDocumentError(
                f"{result_location}.required",
                f"more than {MAX_REQUIRED_FIELDS} required fields",
            )
        required: list[tuple[str, str]] = []
        for field in sorted(raw_required):
            declared = raw_required[field]
            if not isinstance(declared, str) or declared not in REQUIRED_TYPES:
                raise WorkflowDocumentError(
                    f"{result_location}.required.{field}",
                    f"must be one of {', '.join(REQUIRED_TYPES)}",
                )
            required.append((field, declared))
        results.append(ResultContract(name=result_name, required=tuple(required)))
    return OutcomeContract(results=tuple(results))


def _parse_evidence(data: Any, location: str) -> tuple[EvidenceSelector, ...]:
    """A step's named evidence selectors (format 2)."""
    if data is None:
        return ()
    data = _require_mapping(data, location)
    if len(data) > MAX_EVIDENCE:
        raise WorkflowDocumentError(
            location, f"more than {MAX_EVIDENCE} evidence selectors"
        )
    selectors: list[EvidenceSelector] = []
    for alias in sorted(data):
        alias_location = f"{location}.{alias}"
        if not _SLUG_RE.match(alias):
            raise WorkflowDocumentError(
                alias_location, "an evidence alias must be slug-format"
            )
        selector = _require_mapping(data[alias], alias_location)
        _reject_unknown(
            selector, ("steps", "after", "with_outcome", "required"), alias_location
        )
        raw_steps = selector.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise WorkflowDocumentError(
                f"{alias_location}.steps", "must be a nonempty list of step names"
            )
        steps: list[str] = []
        for index, item in enumerate(raw_steps):
            if not isinstance(item, str):
                raise WorkflowDocumentError(
                    f"{alias_location}.steps[{index}]", "must be a step name"
                )
            steps.append(item)
        after = selector.get("after")
        if after is not None and not isinstance(after, str):
            raise WorkflowDocumentError(
                f"{alias_location}.after", "must be a step name"
            )
        with_outcome = selector.get("with_outcome", True)
        if not isinstance(with_outcome, bool):
            raise WorkflowDocumentError(
                f"{alias_location}.with_outcome", "must be a boolean"
            )
        required = selector.get("required", True)
        if not isinstance(required, bool):
            raise WorkflowDocumentError(
                f"{alias_location}.required", "must be a boolean"
            )
        selectors.append(
            EvidenceSelector(
                name=alias,
                steps=tuple(steps),
                after=after,
                with_outcome=with_outcome,
                required=required,
            )
        )
    return tuple(selectors)


def _parse_choices(data: Any, location: str, version: int) -> tuple[GateChoice, ...]:
    """A format-2 gate's declared answers, in the order they are offered."""
    if not isinstance(data, list) or not data:
        raise WorkflowDocumentError(
            location, "a format-2 gate needs a nonempty ordered list of 'choices'"
        )
    if len(data) > MAX_CHOICES:
        raise WorkflowDocumentError(location, f"more than {MAX_CHOICES} choices")
    choices: list[GateChoice] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        choice_location = f"{location}[{index}]"
        item = _require_mapping(item, choice_location)
        _reject_unknown(
            item, ("id", "label", "feedback_required", "next"), choice_location
        )
        choice_id = _require_slug(item, "id", choice_location)
        if choice_id in seen:
            raise WorkflowDocumentError(
                f"{choice_location}.id", f"duplicate choice {choice_id!r}"
            )
        seen.add(choice_id)
        label = _require_str(item, "label", choice_location)
        if not label.strip():
            raise WorkflowDocumentError(f"{choice_location}.label", "must not be blank")
        feedback_required = item.get("feedback_required", False)
        if not isinstance(feedback_required, bool):
            raise WorkflowDocumentError(
                f"{choice_location}.feedback_required", "must be a boolean"
            )
        if "next" not in item:
            raise WorkflowDocumentError(
                choice_location, "a choice needs an explicit 'next' destination"
            )
        choices.append(
            GateChoice(
                id=choice_id,
                label=label,
                feedback_required=feedback_required,
                next=_parse_destination(
                    item["next"], f"{choice_location}.next", version, allow_pause=False
                ),
            )
        )
    return tuple(choices)


def _parse_step(data: Any, location: str, version: int) -> Step:
    data = _require_mapping(data, location)
    name = _require_slug(data, "name", location)
    kind = _require_str(data, "kind", location)
    if kind not in STEP_KINDS:
        raise WorkflowDocumentError(
            f"{location}.kind", f"must be one of {', '.join(STEP_KINDS)}"
        )
    max_visits, on_exhausted = _parse_bound(data, location, version)
    # Format 2 adds `evidence` to every kind: a decision routes on the same
    # frozen records its neighbours were prompted with, not on a fresh scan.
    common = _COMMON_STEP_FIELDS if version == 1 else (*_COMMON_STEP_FIELDS, "evidence")
    evidence = (
        ()
        if version == 1
        else _parse_evidence(data.get("evidence"), f"{location}.evidence")
    )
    if kind == "agent":
        _reject_unknown(
            data,
            (
                *common,
                "session",
                "role",
                "prompt",
                "when",
                *(("expects_outcome",) if version == 1 else ("outcome",)),
            ),
            location,
        )
        role = data.get("role", DEFAULT_ROLE)
        if role not in MODEL_ROLES:
            raise WorkflowDocumentError(
                f"{location}.role",
                f"unknown model role {role!r}; one of {', '.join(MODEL_ROLES)}",
            )
        expects_outcome = False
        outcome: OutcomeContract | None = None
        if version == 1:
            expects_outcome = data.get("expects_outcome", False)
            if not isinstance(expects_outcome, bool):
                raise WorkflowDocumentError(
                    f"{location}.expects_outcome", "must be a boolean"
                )
        elif "outcome" not in data:
            raise WorkflowDocumentError(
                location,
                "a format-2 agent step must declare 'outcome': either null "
                "(no result is asked for) or the results it may produce",
            )
        else:
            outcome = _parse_outcome(data["outcome"], f"{location}.outcome")
        if "prompt" not in data:
            raise WorkflowDocumentError(location, "an agent step needs a 'prompt'")
        return AgentStep(
            name=name,
            session=_require_str(data, "session", location),
            role=role,
            prompt=_parse_text(data["prompt"], f"{location}.prompt", version),
            expects_outcome=expects_outcome,
            when=(
                _parse_predicate(data["when"], f"{location}.when", version)
                if "when" in data
                else LiteralPredicate(value=True)
            ),
            max_visits=max_visits,
            on_exhausted=on_exhausted,
            outcome=outcome,
            evidence=evidence,
        )
    if kind == "command":
        _reject_unknown(data, (*common, "argv", "timeout", "idempotent"), location)
        raw_argv = data.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv:
            raise WorkflowDocumentError(
                f"{location}.argv", "must be a nonempty list of literal strings"
            )
        for index, item in enumerate(raw_argv):
            if not isinstance(item, str):
                raise WorkflowDocumentError(
                    f"{location}.argv[{index}]",
                    "must be a literal string; command arguments are never "
                    "built from agent output",
                )
        timeout = data.get("timeout", DEFAULT_COMMAND_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise WorkflowDocumentError(f"{location}.timeout", "must be a number")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise WorkflowDocumentError(
                f"{location}.timeout", "must be a positive, finite number of seconds"
            )
        if data.get("idempotent") is not True:
            raise WorkflowDocumentError(
                f"{location}.idempotent",
                "a command step must declare 'idempotent: true'; a restart "
                "re-runs the step on recovery",
            )
        return CommandStep(
            name=name,
            argv=tuple(raw_argv),
            timeout=timeout,
            idempotent=True,
            max_visits=max_visits,
            on_exhausted=on_exhausted,
            evidence=evidence,
        )
    if kind == "decision":
        _reject_unknown(data, (*common, "cases", "otherwise"), location)
        raw_cases = data.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise WorkflowDocumentError(
                f"{location}.cases", "must be a nonempty ordered list of cases"
            )
        cases: list[DecisionCase] = []
        for index, item in enumerate(raw_cases):
            case_location = f"{location}.cases[{index}]"
            item = _require_mapping(item, case_location)
            _reject_unknown(item, ("when", "next"), case_location)
            if "when" not in item or "next" not in item:
                raise WorkflowDocumentError(
                    case_location, "a case needs both 'when' and 'next'"
                )
            cases.append(
                DecisionCase(
                    when=_parse_predicate(
                        item["when"], f"{case_location}.when", version
                    ),
                    next=_parse_destination(
                        item["next"], f"{case_location}.next", version
                    ),
                )
            )
        if "otherwise" not in data:
            raise WorkflowDocumentError(
                location,
                "a decision needs an explicit 'otherwise' destination; falling "
                "off the end of the cases is never implicit",
            )
        return DecisionStep(
            name=name,
            cases=tuple(cases),
            otherwise=_parse_destination(
                data["otherwise"], f"{location}.otherwise", version
            ),
            max_visits=max_visits,
            on_exhausted=on_exhausted,
            evidence=evidence,
        )
    _reject_unknown(
        data, (*common, "message", *(() if version == 1 else ("choices",))), location
    )
    if "message" not in data:
        raise WorkflowDocumentError(location, "a gate step needs a 'message'")
    return GateStep(
        name=name,
        message=_parse_text(data["message"], f"{location}.message", version),
        max_visits=max_visits,
        on_exhausted=on_exhausted,
        choices=(
            ()
            if version == 1
            else _parse_choices(data.get("choices"), f"{location}.choices", version)
        ),
        evidence=evidence,
    )


def definition_from_document(document: Mapping[str, Any]) -> WorkflowDefinition:
    """Validate one already-parsed document into an immutable definition."""
    _reject_unknown(document, ("format", "name", "sessions", "primary", "steps"), "")
    if "format" not in document:
        raise WorkflowDocumentError("format", "missing required field 'format'")
    version = document["format"]
    if version not in SUPPORTED_FORMATS:
        raise UnsupportedWorkflowFormatError(version)
    name = _require_slug(document, "name", "")
    raw_sessions = document.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise WorkflowDocumentError("sessions", "must be a nonempty list of names")
    sessions: list[str] = []
    for index, item in enumerate(raw_sessions):
        if not isinstance(item, str) or not _SLUG_RE.match(item):
            raise WorkflowDocumentError(f"sessions[{index}]", "must be slug-format")
        if item in RESERVED_NAMES:
            raise WorkflowDocumentError(
                f"sessions[{index}]", f"{item!r} is reserved by the engine"
            )
        if item in sessions:
            raise WorkflowDocumentError(f"sessions[{index}]", f"duplicate session {item!r}")
        sessions.append(item)
    primary = _require_str(document, "primary", "")
    if primary not in sessions:
        raise WorkflowDocumentError(
            "primary", f"{primary!r} is not a declared session"
        )
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowDocumentError("steps", "must be a nonempty ordered list of steps")
    if len(raw_steps) > MAX_STEPS:
        raise WorkflowDocumentError("steps", f"more than {MAX_STEPS} steps")
    steps: list[Step] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_steps):
        step = _parse_step(item, f"steps[{index}]", version)
        if step.name in RESERVED_NAMES:
            raise WorkflowDocumentError(
                f"steps[{index}].name", f"{step.name!r} is reserved by the engine"
            )
        if step.name in seen:
            raise WorkflowDocumentError(
                f"steps[{index}].name", f"duplicate step {step.name!r}"
            )
        seen.add(step.name)
        steps.append(step)
    definition = WorkflowDefinition(
        format=version,
        name=name,
        sessions=tuple(sessions),
        primary=primary,
        steps=tuple(steps),
    )
    _validate_references(definition)
    _validate_graph(definition)
    return definition


def _validate_references(definition: WorkflowDefinition) -> None:
    names = {step.name for step in definition.steps}

    def check_value(value: ValueExpr, location: str, aliases: set[str]) -> None:
        if isinstance(value, LatestValue):
            for step_name in value.steps:
                if step_name not in names:
                    raise WorkflowDocumentError(
                        location, f"references undeclared step {step_name!r}"
                    )
            if value.after is not None and value.after not in names:
                raise WorkflowDocumentError(
                    location, f"references undeclared step {value.after!r}"
                )
        elif isinstance(value, EvidenceValue):
            # An alias is *this step's* binding. Reading another step's
            # evidence would read a record this attempt never froze, which is
            # the whole thing the binding exists to pin down.
            if value.name not in aliases:
                raise WorkflowDocumentError(
                    location,
                    f"reads evidence {value.name!r}, which this step does not "
                    "declare"
                    + (
                        f"; it declares {', '.join(sorted(aliases))}"
                        if aliases
                        else "; it declares none"
                    ),
                )
        elif isinstance(value, CountValue):
            if value.step not in names:
                raise WorkflowDocumentError(
                    location, f"references undeclared step {value.step!r}"
                )
        elif isinstance(value, GetValue):
            check_value(value.value, location, aliases)
        elif isinstance(value, CoalesceValue):
            for item in value.values:
                check_value(item, location, aliases)

    def check_predicate(predicate: Predicate, location: str, aliases: set[str]) -> None:
        if isinstance(predicate, ComparePredicate):
            check_value(predicate.left, location, aliases)
            check_value(predicate.right, location, aliases)
        elif isinstance(predicate, (ExistsPredicate, IsTypePredicate)):
            check_value(predicate.value, location, aliases)
        elif isinstance(predicate, JunctionPredicate):
            for item in predicate.of:
                check_predicate(item, location, aliases)
        elif isinstance(predicate, NotPredicate):
            check_predicate(predicate.of, location, aliases)

    def check_text(text: TextDocument, location: str, aliases: set[str]) -> None:
        for part in text.parts:
            if isinstance(part, ValuePart):
                check_value(part.value, location, aliases)
            elif isinstance(part, ConditionalPart):
                check_predicate(part.when, location, aliases)
                check_text(part.then, location, aliases)
                check_text(part.otherwise, location, aliases)

    def check_destination(destination: Destination, location: str) -> None:
        if isinstance(destination, StepDestination) and destination.step not in names:
            raise WorkflowDocumentError(
                location, f"routes to undeclared step {destination.step!r}"
            )

    for index, step in enumerate(definition.steps):
        location = f"steps[{index}]"
        aliases = {selector.name for selector in step.evidence}
        for selector in step.evidence:
            selector_location = f"{location}.evidence.{selector.name}"
            for step_name in selector.steps:
                if step_name not in names:
                    raise WorkflowDocumentError(
                        f"{selector_location}.steps",
                        f"references undeclared step {step_name!r}",
                    )
            if selector.after is not None and selector.after not in names:
                raise WorkflowDocumentError(
                    f"{selector_location}.after",
                    f"references undeclared step {selector.after!r}",
                )
        if step.on_exhausted is not None:
            check_destination(step.on_exhausted, f"{location}.on_exhausted")
            target = definition.step_named(step.on_exhausted.step)
            if not isinstance(target, GateStep):
                raise WorkflowDocumentError(
                    f"{location}.on_exhausted",
                    "must name a declared gate step, so an exhausted bound "
                    "always reaches a human",
                )
        if isinstance(step, AgentStep):
            if step.session not in definition.sessions:
                raise WorkflowDocumentError(
                    f"{location}.session",
                    f"names undeclared session {step.session!r}",
                )
            check_text(step.prompt, f"{location}.prompt", aliases)
            check_predicate(step.when, f"{location}.when", aliases)
        elif isinstance(step, DecisionStep):
            for case_index, case in enumerate(step.cases):
                check_predicate(
                    case.when, f"{location}.cases[{case_index}].when", aliases
                )
                check_destination(case.next, f"{location}.cases[{case_index}].next")
            check_destination(step.otherwise, f"{location}.otherwise")
        elif isinstance(step, GateStep):
            check_text(step.message, f"{location}.message", aliases)
            for choice_index, choice in enumerate(step.choices):
                check_destination(
                    choice.next, f"{location}.choices[{choice_index}].next"
                )

    if definition.format >= 2:
        _validate_named_endings(definition)


def _validate_named_endings(definition: WorkflowDefinition) -> None:
    """Format 2: a run may only end somewhere that says what ending it is.

    Format 1 lets the last step fall off the end and calls that complete. That
    is exactly the silence this format removes: an operator reading a finished
    bugfix must be able to tell a validated fix from an abandoned one, and a
    run that ended by running out of list says neither.
    """
    for index, step in enumerate(definition.steps):
        location = f"steps[{index}]"
        if isinstance(step, DecisionStep):
            continue
        if isinstance(step, GateStep) and step.choices:
            continue
        if definition.step_after(step.name) is None:
            raise WorkflowDocumentError(
                location,
                f"step {step.name!r} is last and falls off the end of the "
                "workflow; format 2 ends only at a declared "
                "'{complete: true, result: <name>}' destination",
            )


def _successors(definition: WorkflowDefinition, step: Step) -> list[str]:
    """Every step this one can reach in one move, exhaustion aside.

    A format-2 gate's choices are edges like any other. Leaving them out would
    let a definition build a loop out of human answers — retry, retry, retry —
    that no visit bound cuts, which is the one thing graph validation is for.
    """
    targets: list[str] = []
    if isinstance(step, DecisionStep):
        destinations: list[Destination] = [case.next for case in step.cases]
        destinations.append(step.otherwise)
    elif isinstance(step, GateStep) and step.choices:
        destinations = [choice.next for choice in step.choices]
    else:
        following = definition.step_after(step.name)
        return [following.name] if following is not None else []
    for destination in destinations:
        if isinstance(destination, StepDestination):
            targets.append(destination.step)
    return targets


def _validate_graph(definition: WorkflowDefinition) -> None:
    """Every cycle must be cut by a declared visit bound.

    Removing the bounded steps must leave an acyclic graph: a loop the engine
    cannot count is a loop that can run forever, and no route predicate is
    trusted to end it — the bound is enforced by the engine, so the bound is
    what validation checks.
    """
    bounded = {step.name for step in definition.steps if step.max_visits is not None}
    remaining = {
        step.name: [
            target
            for target in _successors(definition, step)
            if target not in bounded
        ]
        for step in definition.steps
        if step.name not in bounded
    }
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(name: str, path: list[str]) -> None:
        if name in visiting:
            cycle = " → ".join([*path[path.index(name) :], name])
            raise WorkflowDocumentError(
                "steps",
                f"unbounded cycle {cycle}; every loop needs a step declaring "
                "'max_visits' and 'on_exhausted'",
            )
        if name in done:
            return
        visiting.add(name)
        for target in remaining[name]:
            walk(target, [*path, name])
        visiting.discard(name)
        done.add(name)

    for name in remaining:
        walk(name, [])

    for step in definition.steps:
        if step.max_visits is None:
            continue
        assert step.on_exhausted is not None
        # The escape has to leave the loop. A bound whose exhaustion target
        # can route back into the bounded step buys nothing.
        target = step.on_exhausted.step
        if step.name in _reachable_from(definition, target):
            raise WorkflowDocumentError(
                "steps",
                f"step {step.name!r} declares an exhaustion target {target!r} "
                "that can return to it; the exhaustion gate must be outside "
                "the bounded cycle",
            )


def _reachable_from(definition: WorkflowDefinition, start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        step = definition.step_named(name)
        if step is None:  # pragma: no cover - references are validated first
            continue
        stack.extend(_successors(definition, step))
        if step.on_exhausted is not None:
            stack.append(step.on_exhausted.step)
    return seen


# --- canonicalization ---------------------------------------------------------


def _value_document(value: ValueExpr) -> dict[str, Any]:
    if isinstance(value, LiteralValue):
        return {"op": "literal", "value": value.value}
    if isinstance(value, InputValue):
        return {"op": "input", "name": value.name}
    if isinstance(value, LatestValue):
        return {
            "op": "latest",
            "steps": list(value.steps),
            "after": value.after,
            "with_outcome": value.with_outcome,
        }
    if isinstance(value, EvidenceValue):
        return {"op": "evidence", "name": value.name}
    if isinstance(value, GetValue):
        return {
            "op": "get",
            "value": _value_document(value.value),
            "keys": list(value.keys),
        }
    if isinstance(value, CountValue):
        return {"op": "count", "step": value.step}
    return {"op": "coalesce", "values": [_value_document(v) for v in value.values]}


def _predicate_document(predicate: Predicate) -> Any:
    if isinstance(predicate, LiteralPredicate):
        return predicate.value
    if isinstance(predicate, ComparePredicate):
        return {
            "op": predicate.op,
            "left": _value_document(predicate.left),
            "right": _value_document(predicate.right),
        }
    if isinstance(predicate, ExistsPredicate):
        return {"op": "exists", "value": _value_document(predicate.value)}
    if isinstance(predicate, IsTypePredicate):
        return {
            "op": "is_type",
            "value": _value_document(predicate.value),
            "type": predicate.type,
        }
    if isinstance(predicate, JunctionPredicate):
        return {
            "op": predicate.op,
            "of": [_predicate_document(item) for item in predicate.of],
        }
    return {"op": "not", "of": _predicate_document(predicate.of)}


def _text_document(text: TextDocument) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for part in text.parts:
        if isinstance(part, TextPart):
            parts.append({"text": part.text})
        elif isinstance(part, ValuePart):
            parts.append(
                {"value": _value_document(part.value), "format": part.format}
            )
        else:
            parts.append(
                {
                    "if": _predicate_document(part.when),
                    "then": _text_document(part.then),
                    "else": _text_document(part.otherwise),
                }
            )
    return {"separator": text.separator, "parts": parts}


def _destination_document(destination: Destination, version: int) -> dict[str, Any]:
    if isinstance(destination, StepDestination):
        return {"step": destination.step}
    if isinstance(destination, CompleteDestination):
        if version == 1:
            return {"complete": True}
        return {"complete": True, "result": destination.result}
    return {"pause": True}


def _outcome_document(outcome: OutcomeContract | None) -> dict[str, Any] | None:
    if outcome is None:
        return None
    return {
        "results": {
            result.name: {"required": dict(result.required)}
            for result in outcome.results
        }
    }


def _evidence_document(
    selectors: tuple[EvidenceSelector, ...],
) -> dict[str, dict[str, Any]]:
    return {
        selector.name: {
            "steps": list(selector.steps),
            "after": selector.after,
            "with_outcome": selector.with_outcome,
            "required": selector.required,
        }
        for selector in selectors
    }


def destination_document(destination: Destination, version: int) -> dict[str, Any]:
    """A destination in its persisted form.

    Public because a gate snapshot records where each offered choice would
    have gone. The record has to hold the route as it was offered, not a name
    to look up in whatever the definition says later.
    """
    return _destination_document(destination, version)


def _step_document(step: Step, version: int) -> dict[str, Any]:
    document: dict[str, Any] = {
        "name": step.name,
        "kind": step.kind,
        "max_visits": step.max_visits,
        "on_exhausted": (
            _destination_document(step.on_exhausted, version)
            if step.on_exhausted is not None
            else None
        ),
    }
    # Format-1 canonical bytes are frozen: nothing format 2 added may appear
    # in them, or every retained revision would change identity.
    if version >= 2:
        document["evidence"] = _evidence_document(step.evidence)
    if isinstance(step, AgentStep):
        document.update(
            session=step.session,
            role=step.role,
            prompt=_text_document(step.prompt),
            when=_predicate_document(step.when),
        )
        if version == 1:
            document["expects_outcome"] = step.expects_outcome
        else:
            document["outcome"] = _outcome_document(step.outcome)
    elif isinstance(step, CommandStep):
        document.update(
            argv=list(step.argv), timeout=step.timeout, idempotent=step.idempotent
        )
    elif isinstance(step, DecisionStep):
        document.update(
            cases=[
                {
                    "when": _predicate_document(case.when),
                    "next": _destination_document(case.next, version),
                }
                for case in step.cases
            ],
            otherwise=_destination_document(step.otherwise, version),
        )
    else:
        document.update(message=_text_document(step.message))
        if version >= 2:
            document["choices"] = [
                {
                    "id": choice.id,
                    "label": choice.label,
                    "feedback_required": choice.feedback_required,
                    "next": _destination_document(choice.next, version),
                }
                for choice in step.choices
            ]
    return document


def canonical_document(definition: WorkflowDefinition) -> dict[str, Any]:
    """The normalized document: every default explicit, nothing implied."""
    return {
        "format": definition.format,
        "name": definition.name,
        "sessions": list(definition.sessions),
        "primary": definition.primary,
        "steps": [
            _step_document(step, definition.format) for step in definition.steps
        ],
    }


def canonical_bytes(definition: WorkflowDefinition) -> bytes:
    return json.dumps(
        canonical_document(definition),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def revision_for(definition: WorkflowDefinition) -> str:
    """`sha256:<digest>` of the canonical bytes.

    The full digest, not a prefix: this is a durable content identity stored
    beside tasks for as long as they exist, not a short comparison token.
    """
    return "sha256:" + hashlib.sha256(canonical_bytes(definition)).hexdigest()


def make_revision(definition: WorkflowDefinition) -> WorkflowRevision:
    return WorkflowRevision(
        revision=revision_for(definition),
        definition=definition,
        document=canonical_document(definition),
    )


def load_definition(text: str) -> WorkflowRevision:
    """Parse, validate, canonicalize, and identify one YAML definition."""
    return make_revision(definition_from_document(parse_yaml_document(text)))


def load_canonical_document(document: Mapping[str, Any]) -> WorkflowDefinition:
    """Re-validate a retained canonical document before executing it."""
    return definition_from_document(document)


# --- evaluation ---------------------------------------------------------------


@dataclass(frozen=True)
class HistoryRecord:
    """One finished (or in-flight) attempt, as an expression sees it.

    `evidence` is that attempt's own frozen bindings — alias → `{step, seq}`
    or None. It travels with the record so a later step can ask not just what
    a verifier concluded but *which* fix it was looking at, which is how a
    stale approval is caught rather than trusted.
    """

    seq: int
    step: str
    status: str
    outcome: dict[str, Any] | None
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EvidenceBinding:
    """What one selector actually selected, recorded on the attempt.

    `seq` is None when an optional selector matched nothing. That is a
    different fact from "not resolved yet", and keeping them apart is what
    lets a prompt say "no rejection report" instead of rendering an empty
    string that reads like one.
    """

    name: str
    step: str | None
    seq: int | None

    @property
    def present(self) -> bool:
        return self.seq is not None


@dataclass(frozen=True)
class EvaluationContext:
    """Everything a definition may read: pinned inputs and this task's history.

    Both are captured at attempt entry and are task-local. There is nothing
    else to read — no project, no profile, no clock, no filesystem — so two
    evaluations of the same expression over the same history agree.

    `evidence` is the format-2 addition: alias → the record view this attempt
    bound when it opened, or None for an optional selector that matched
    nothing. It is not recomputed here, which is the point — the same view is
    handed to the prompt, to the routing decision, to the gate message, and to
    recovery after a restart.
    """

    inputs: Mapping[str, str]
    records: tuple[HistoryRecord, ...]
    evidence: Mapping[str, dict[str, Any] | None] = MappingProxyType({})


class RenderError(Exception):
    """A text document could not be rendered: an unresolved conditional, a
    missing value with no fallback, or a rendered size over the bound."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _latest_attempt_seq(context: EvaluationContext, step: str) -> int:
    """The newest attempt of a step whatever its status; 0 when it never ran."""
    return max((r.seq for r in context.records if r.step == step), default=0)


def evaluate_value(value: ValueExpr, context: EvaluationContext) -> Any:
    if isinstance(value, LiteralValue):
        return value.value
    if isinstance(value, InputValue):
        return context.inputs.get(value.name, MISSING)
    if isinstance(value, EvidenceValue):
        # Absence — an optional selector that matched nothing — is MISSING,
        # not None: `exists` must say false, and a `get` through it must not
        # yield a null that reads like a written value.
        view = context.evidence.get(value.name)
        return MISSING if view is None else view
    if isinstance(value, LatestValue):
        floor = (
            _latest_attempt_seq(context, value.after) if value.after is not None else 0
        )
        candidates = [
            record
            for record in context.records
            if record.step in value.steps
            and record.status == "ok"
            and record.seq > floor
            and (record.outcome is not None or not value.with_outcome)
        ]
        if not candidates:
            return MISSING
        latest = max(candidates, key=lambda record: record.seq)
        return {
            "step": latest.step,
            "seq": latest.seq,
            "status": latest.status,
            "outcome": latest.outcome,
        }
    if isinstance(value, GetValue):
        current = evaluate_value(value.value, context)
        for key in value.keys:
            if current is MISSING or current is None:
                return MISSING
            if isinstance(key, bool):  # pragma: no cover - rejected at parse
                return MISSING
            if isinstance(key, int):
                if not isinstance(current, list) or not (
                    -len(current) <= key < len(current)
                ):
                    return MISSING
                current = current[key]
                continue
            if not isinstance(current, dict) or key not in current:
                return MISSING
            current = current[key]
        return current
    if isinstance(value, CountValue):
        return len([r for r in context.records if r.step == value.step])
    for item in value.values:
        candidate = evaluate_value(item, context)
        if candidate is not MISSING and candidate is not None:
            return candidate
    return MISSING


def _json_type_of(value: Any) -> str | None:
    if value is MISSING:
        return None
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None  # pragma: no cover


def _describe(value: ValueExpr) -> str:
    if isinstance(value, InputValue):
        return f"input {value.name}"
    if isinstance(value, LatestValue):
        return f"latest result of {', '.join(value.steps)}"
    if isinstance(value, EvidenceValue):
        return f"evidence {value.name}"
    if isinstance(value, GetValue):
        return f"{_describe(value.value)}.{'.'.join(str(k) for k in value.keys)}"
    if isinstance(value, CountValue):
        return f"attempts of {value.step}"
    if isinstance(value, CoalesceValue):
        return _describe(value.values[0])
    return "a literal"


def evaluate_predicate(
    predicate: Predicate, context: EvaluationContext
) -> bool | Unresolved:
    """Three-valued evaluation. A missing operand is never `False`."""
    if isinstance(predicate, LiteralPredicate):
        return predicate.value
    if isinstance(predicate, ExistsPredicate):
        value = evaluate_value(predicate.value, context)
        return value is not MISSING and value is not None
    if isinstance(predicate, IsTypePredicate):
        return _json_type_of(evaluate_value(predicate.value, context)) == predicate.type
    if isinstance(predicate, NotPredicate):
        inner = evaluate_predicate(predicate.of, context)
        return inner if isinstance(inner, Unresolved) else not inner
    if isinstance(predicate, JunctionPredicate):
        # Ordered, short-circuiting, three-valued: a decisive operand wins
        # even when a later one is unresolved, so an `exists` guard placed
        # first really does guard what follows.
        decisive = predicate.op == "any"
        unresolved: Unresolved | None = None
        for item in predicate.of:
            result = evaluate_predicate(item, context)
            if isinstance(result, Unresolved):
                unresolved = unresolved or result
                continue
            if result is decisive:
                return decisive
        return unresolved if unresolved is not None else (not decisive)
    left = evaluate_value(predicate.left, context)
    right = evaluate_value(predicate.right, context)
    for operand, expression in ((left, predicate.left), (right, predicate.right)):
        if operand is MISSING:
            return Unresolved(f"{_describe(expression)} is missing")
    if predicate.op in ("eq", "ne"):
        equal = _json_equal(left, right)
        return equal if predicate.op == "eq" else not equal
    left_type = _json_type_of(left)
    right_type = _json_type_of(right)
    numeric = {"integer", "number"}
    comparable = (left_type == right_type == "string") or (
        left_type in numeric and right_type in numeric
    )
    if not comparable:
        return Unresolved(
            f"cannot compare {left_type} with {right_type} "
            f"({_describe(predicate.left)})"
        )
    if predicate.op == "lt":
        return left < right
    if predicate.op == "lte":
        return left <= right
    if predicate.op == "gt":
        return left > right
    return left >= right


def _json_equal(left: Any, right: Any) -> bool:
    """Strict JSON equality: a boolean is never a number."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, dict) != isinstance(right, dict):
        return False
    if isinstance(left, list) != isinstance(right, list):
        return False
    return bool(left == right)


def _render_value(value: Any, value_format: str, expression: ValueExpr) -> str:
    if value is MISSING:
        raise RenderError(
            f"{_describe(expression)} is missing and the prompt declares no "
            "fallback for it"
        )
    if value_format == "json":
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    raise RenderError(
        f"{_describe(expression)} is a {_json_type_of(value)}; render it with "
        "format 'json' or select a scalar out of it"
    )


def render_text(text: TextDocument, context: EvaluationContext) -> str:
    """Render one text document, or refuse with a reason.

    Refusing matters as much as rendering: a prompt that silently dropped an
    unresolved section would send an agent to work with evidence the author
    said to include.
    """
    rendered = _render_parts(text, context)
    if len(rendered.encode("utf-8")) > MAX_RENDERED_BYTES:
        raise RenderError(
            f"rendered text exceeds {MAX_RENDERED_BYTES} bytes"
        )
    return rendered


def _render_parts(text: TextDocument, context: EvaluationContext) -> str:
    pieces: list[str] = []
    for part in text.parts:
        if isinstance(part, TextPart):
            pieces.append(part.text)
        elif isinstance(part, ValuePart):
            pieces.append(
                _render_value(
                    evaluate_value(part.value, context), part.format, part.value
                )
            )
        else:
            branch = evaluate_predicate(part.when, context)
            if isinstance(branch, Unresolved):
                raise RenderError(
                    f"a conditional section cannot be resolved: {branch.reason}"
                )
            pieces.append(
                _render_parts(part.then if branch else part.otherwise, context)
            )
    return text.separator.join(pieces)


# --- format 2: evidence resolution and result validation ----------------------


def record_view(record: HistoryRecord) -> dict[str, Any]:
    """One attempt as a definition sees it.

    `evidence` carries that attempt's own bindings, so a route can ask which
    fix a verifier actually checked instead of assuming it checked the newest.
    """
    return {
        "step": record.step,
        "seq": record.seq,
        "status": record.status,
        "outcome": record.outcome,
        "evidence": dict(record.evidence) if record.evidence is not None else {},
    }


def select_evidence(
    selector: EvidenceSelector, records: Sequence[HistoryRecord]
) -> HistoryRecord | None:
    """The record one selector picks, by format 1's `latest` rules.

    Same rules, different moment: this runs once, when the attempt opens, and
    what it picked is then written down.
    """
    floor = 0
    if selector.after is not None:
        floor = max(
            (r.seq for r in records if r.step == selector.after), default=0
        )
    candidates = [
        record
        for record in records
        if record.step in selector.steps
        and record.status == "ok"
        and record.seq > floor
        and (record.outcome is not None or not selector.with_outcome)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda record: record.seq)


def resolve_evidence(
    step: Step, records: Sequence[HistoryRecord]
) -> tuple[tuple[EvidenceBinding, ...], tuple[str, ...]]:
    """Bind every selector this step declares; report the required misses.

    Missing required evidence is returned rather than raised because the
    caller has to *record* the attempt before it can pause it: an attempt that
    vanished would take the reason with it.
    """
    bindings: list[EvidenceBinding] = []
    missing: list[str] = []
    for selector in step.evidence:
        found = select_evidence(selector, records)
        if found is None:
            if selector.required:
                missing.append(selector.name)
            bindings.append(EvidenceBinding(name=selector.name, step=None, seq=None))
            continue
        bindings.append(
            EvidenceBinding(name=selector.name, step=found.step, seq=found.seq)
        )
    return tuple(bindings), tuple(missing)


def evidence_views(
    bindings: Sequence[EvidenceBinding], records: Sequence[HistoryRecord]
) -> dict[str, dict[str, Any] | None]:
    """Turn recorded bindings back into the record views expressions read.

    A binding whose record is gone resolves to None rather than to some other
    record: history that cannot be shown is unavailable, never substituted.
    """
    by_seq = {record.seq: record for record in records}
    views: dict[str, dict[str, Any] | None] = {}
    for binding in bindings:
        record = by_seq.get(binding.seq) if binding.seq is not None else None
        views[binding.name] = record_view(record) if record is not None else None
    return views


def bindings_document(bindings: Sequence[EvidenceBinding]) -> dict[str, Any]:
    """The persisted shape of an attempt's frozen evidence."""
    return {
        "version": 1,
        "bindings": {
            binding.name: (
                None
                if binding.seq is None
                else {"step": binding.step, "seq": binding.seq}
            )
            for binding in bindings
        },
    }


def bindings_from_document(
    document: Mapping[str, Any] | None,
) -> tuple[EvidenceBinding, ...]:
    """Read back what an attempt froze. An absent column is no bindings."""
    if not isinstance(document, Mapping):
        return ()
    raw = document.get("bindings")
    if not isinstance(raw, Mapping):
        return ()
    bindings: list[EvidenceBinding] = []
    for name in sorted(raw):
        entry = raw[name]
        if entry is None:
            bindings.append(EvidenceBinding(name=name, step=None, seq=None))
            continue
        if not isinstance(entry, Mapping):
            continue
        seq = entry.get("seq")
        step = entry.get("step")
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        bindings.append(
            EvidenceBinding(
                name=name, step=step if isinstance(step, str) else None, seq=seq
            )
        )
    return tuple(bindings)


# The format-2 result envelope. Version 2 is the document's own, independent of
# the workflow format that asked for it, so a reader can tell which rules a
# retained result was written under.
RESULT_ENVELOPE_VERSION = 2
_ENVELOPE_KEYS = ("version", "result", "summary", "artifacts")


def validate_result_document(
    document: Any, contract: OutcomeContract
) -> tuple[dict[str, Any] | None, str | None]:
    """Check one result document against the step's declared contract.

    Returns `(document, None)` or `(None, reason)`. Every refusal names the
    field, because "invalid result" tells an operator nothing about whether to
    retry the step or fix the workflow.

    What this establishes is structure and attribution: that the step declared
    this result and wrote the evidence it promised. It says nothing about
    whether that evidence is *true* — no validator can — and nothing here may
    be read as permission to act on the content.
    """
    if not isinstance(document, dict):
        return None, "result document is not a JSON object"
    unknown = sorted(set(document) - set(_ENVELOPE_KEYS))
    if unknown:
        return None, f"unknown result field(s) {', '.join(repr(k) for k in unknown)}"
    if document.get("version") != RESULT_ENVELOPE_VERSION:
        return (
            None,
            (
                f"result version must be {RESULT_ENVELOPE_VERSION}, got "
                f"{document.get('version')!r}"
            ),
        )
    result = document.get("result")
    if not isinstance(result, str):
        return None, "result must be a string naming one of this step's results"
    declared = contract.result_named(result)
    if declared is None:
        return (
            None,
            (
                f"result {result!r} is not declared by this step; it declares "
                f"{', '.join(contract.names)}"
            ),
        )
    summary = document.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, "summary must be a nonblank string"
    artifacts = document.get("artifacts", {})
    if artifacts is None:
        artifacts = {}
    if not isinstance(artifacts, dict) or not all(
        isinstance(key, str) for key in artifacts
    ):
        return None, "artifacts must be a string-keyed object"
    for field, expected in declared.required:
        if field not in artifacts:
            return (
                None,
                f"result {result!r} requires the artifact {field!r} ({expected})",
            )
        value = artifacts[field]
        actual = _json_type_of(value)
        # An integer satisfies a declared `number`; nothing else widens.
        if actual != expected and not (expected == "number" and actual == "integer"):
            return (
                None,
                (
                    f"artifact {field!r} must be {expected}, got "
                    f"{actual if actual is not None else 'nothing'}"
                ),
            )
        if expected == "string" and not value.strip():
            return None, f"artifact {field!r} must not be blank"
    return document, None



# --- catalog description ------------------------------------------------------


@dataclass(frozen=True)
class StepDescriptor:
    name: str
    kind: str
    session: str | None
    role: str | None
    conditional: bool


@dataclass(frozen=True)
class WorkflowDescriptor:
    name: str
    revision: str
    format: int
    primary_session: str
    sessions: tuple[str, ...]
    steps: tuple[StepDescriptor, ...]


def describe(revision: WorkflowRevision) -> WorkflowDescriptor:
    """The launch preview's shape: what may run, never a promise that it will.

    A step is `conditional` when a declared route can pass it by or its own
    `when` can hold it back. The engine does not predict outcomes, so this is
    a statement about the declaration. A format-2 gate branches too: its
    choices are routes, so everything after one is reachable rather than
    certain.
    """
    definition = revision.definition
    steps: list[StepDescriptor] = []
    after_branch = False
    for step in definition.steps:
        agent = step if isinstance(step, AgentStep) else None
        gated = agent is not None and agent.when != LiteralPredicate(value=True)
        steps.append(
            StepDescriptor(
                name=step.name,
                kind=step.kind,
                session=agent.session if agent else None,
                role=agent.role if agent else None,
                conditional=after_branch or gated,
            )
        )
        if isinstance(step, DecisionStep) or (
            isinstance(step, GateStep) and step.choices
        ):
            after_branch = True
    return WorkflowDescriptor(
        name=definition.name,
        revision=revision.revision,
        format=definition.format,
        primary_session=definition.primary,
        sessions=definition.sessions,
        steps=tuple(steps),
    )
