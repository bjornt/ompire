"""The declarative workflow document: format 1.

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
document *means* requires format 2; a retained format-1 document is always
read under these rules.

ADR-0028 (docs/adr/0028-retain-declarative-workflow-revisions.md)
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from ompire_daemon.model_config import MODEL_ROLES

# The document format *and* the interpreter semantics. Bumping this is a
# statement that the meaning of a document changed, not that a field was
# added: a retained format-1 document keeps being read under format-1 rules
# forever, and an unsupported version is refused rather than reinterpreted.
FORMAT_VERSION = 1
SUPPORTED_FORMATS = (1,)

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
class CoalesceValue:
    """First non-missing, non-null value. An empty string is a value."""

    values: tuple[ValueExpr, ...]


ValueExpr = (
    LiteralValue | InputValue | LatestValue | GetValue | CountValue | CoalesceValue
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
    pass


@dataclass(frozen=True)
class PauseDestination:
    pass


Destination = StepDestination | CompleteDestination | PauseDestination


@dataclass(frozen=True)
class DecisionCase:
    when: Predicate
    next: Destination


@dataclass(frozen=True)
class AgentStep:
    name: str
    session: str
    role: str
    prompt: TextDocument
    expects_outcome: bool
    when: Predicate
    max_visits: int | None
    on_exhausted: StepDestination | None
    kind: str = "agent"


@dataclass(frozen=True)
class CommandStep:
    name: str
    argv: tuple[str, ...]
    timeout: float
    idempotent: bool
    max_visits: int | None
    on_exhausted: StepDestination | None
    kind: str = "command"


@dataclass(frozen=True)
class DecisionStep:
    name: str
    cases: tuple[DecisionCase, ...]
    otherwise: Destination
    max_visits: int | None
    on_exhausted: StepDestination | None
    kind: str = "decision"


@dataclass(frozen=True)
class GateStep:
    name: str
    message: TextDocument
    max_visits: int | None
    on_exhausted: StepDestination | None
    kind: str = "gate"


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


def _parse_value(data: Any, location: str) -> ValueExpr:
    data = _require_mapping(data, location)
    op = data.get("op")
    if not isinstance(op, str):
        raise WorkflowDocumentError(location, "value expression needs a string 'op'")
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
            value=_parse_value(data["value"], f"{location}.value"),
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
                _parse_value(item, f"{location}.values[{index}]")
                for index, item in enumerate(raw_values)
            )
        )
    raise WorkflowDocumentError(location, f"unknown value operation {op!r}")


def _parse_predicate(data: Any, location: str) -> Predicate:
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
            left=_parse_value(data["left"], f"{location}.left"),
            right=_parse_value(data["right"], f"{location}.right"),
        )
    if op == "exists":
        _reject_unknown(data, ("op", "value"), location)
        if "value" not in data:
            raise WorkflowDocumentError(location, "missing required field 'value'")
        return ExistsPredicate(value=_parse_value(data["value"], f"{location}.value"))
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
            value=_parse_value(data["value"], f"{location}.value"), type=json_type
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
                _parse_predicate(item, f"{location}.of[{index}]")
                for index, item in enumerate(raw)
            ),
        )
    if op == "not":
        _reject_unknown(data, ("op", "of"), location)
        if "of" not in data:
            raise WorkflowDocumentError(location, "missing required field 'of'")
        return NotPredicate(of=_parse_predicate(data["of"], f"{location}.of"))
    raise WorkflowDocumentError(location, f"unknown predicate operation {op!r}")


def _parse_text(data: Any, location: str) -> TextDocument:
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
        parts.append(_parse_part(item, f"{location}.parts[{index}]"))
    return TextDocument(separator=separator, parts=tuple(parts))


def _parse_part(data: Any, location: str) -> Part:
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
            value=_parse_value(data["value"], f"{location}.value"),
            format=value_format,
        )
    _reject_unknown(data, ("if", "then", "else"), location)
    if "then" not in data:
        raise WorkflowDocumentError(location, "a conditional part needs 'then'")
    return ConditionalPart(
        when=_parse_predicate(data["if"], f"{location}.if"),
        then=_parse_text(data["then"], f"{location}.then"),
        otherwise=(
            _parse_text(data["else"], f"{location}.else")
            if "else" in data
            else EMPTY_TEXT
        ),
    )


def _parse_destination(data: Any, location: str) -> Destination:
    data = _require_mapping(data, location)
    present = sorted({"step", "complete", "pause"} & set(data))
    if len(present) != 1:
        raise WorkflowDocumentError(
            location,
            "a destination is exactly one of 'step', 'complete', or 'pause'",
        )
    _reject_unknown(data, (present[0],), location)
    if present[0] == "step":
        value = data["step"]
        if not isinstance(value, str):
            raise WorkflowDocumentError(f"{location}.step", "must be a step name")
        return StepDestination(step=value)
    if data[present[0]] is not True:
        raise WorkflowDocumentError(
            f"{location}.{present[0]}", "must be literally true"
        )
    return CompleteDestination() if present[0] == "complete" else PauseDestination()


_COMMON_STEP_FIELDS = ("name", "kind", "max_visits", "on_exhausted")


def _parse_bound(data: Mapping[str, Any], location: str) -> tuple[int | None, StepDestination | None]:
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
        destination = _parse_destination(on_exhausted_raw, f"{location}.on_exhausted")
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


def _parse_step(data: Any, location: str) -> Step:
    data = _require_mapping(data, location)
    name = _require_slug(data, "name", location)
    kind = _require_str(data, "kind", location)
    if kind not in STEP_KINDS:
        raise WorkflowDocumentError(
            f"{location}.kind", f"must be one of {', '.join(STEP_KINDS)}"
        )
    max_visits, on_exhausted = _parse_bound(data, location)
    if kind == "agent":
        _reject_unknown(
            data,
            (*_COMMON_STEP_FIELDS, "session", "role", "prompt", "expects_outcome", "when"),
            location,
        )
        role = data.get("role", DEFAULT_ROLE)
        if role not in MODEL_ROLES:
            raise WorkflowDocumentError(
                f"{location}.role",
                f"unknown model role {role!r}; one of {', '.join(MODEL_ROLES)}",
            )
        expects_outcome = data.get("expects_outcome", False)
        if not isinstance(expects_outcome, bool):
            raise WorkflowDocumentError(
                f"{location}.expects_outcome", "must be a boolean"
            )
        if "prompt" not in data:
            raise WorkflowDocumentError(location, "an agent step needs a 'prompt'")
        return AgentStep(
            name=name,
            session=_require_str(data, "session", location),
            role=role,
            prompt=_parse_text(data["prompt"], f"{location}.prompt"),
            expects_outcome=expects_outcome,
            when=(
                _parse_predicate(data["when"], f"{location}.when")
                if "when" in data
                else LiteralPredicate(value=True)
            ),
            max_visits=max_visits,
            on_exhausted=on_exhausted,
        )
    if kind == "command":
        _reject_unknown(
            data, (*_COMMON_STEP_FIELDS, "argv", "timeout", "idempotent"), location
        )
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
        )
    if kind == "decision":
        _reject_unknown(data, (*_COMMON_STEP_FIELDS, "cases", "otherwise"), location)
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
                    when=_parse_predicate(item["when"], f"{case_location}.when"),
                    next=_parse_destination(item["next"], f"{case_location}.next"),
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
            otherwise=_parse_destination(data["otherwise"], f"{location}.otherwise"),
            max_visits=max_visits,
            on_exhausted=on_exhausted,
        )
    _reject_unknown(data, (*_COMMON_STEP_FIELDS, "message"), location)
    if "message" not in data:
        raise WorkflowDocumentError(location, "a gate step needs a 'message'")
    return GateStep(
        name=name,
        message=_parse_text(data["message"], f"{location}.message"),
        max_visits=max_visits,
        on_exhausted=on_exhausted,
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
        step = _parse_step(item, f"steps[{index}]")
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

    def check_value(value: ValueExpr, location: str) -> None:
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
        elif isinstance(value, CountValue):
            if value.step not in names:
                raise WorkflowDocumentError(
                    location, f"references undeclared step {value.step!r}"
                )
        elif isinstance(value, GetValue):
            check_value(value.value, location)
        elif isinstance(value, CoalesceValue):
            for item in value.values:
                check_value(item, location)

    def check_predicate(predicate: Predicate, location: str) -> None:
        if isinstance(predicate, ComparePredicate):
            check_value(predicate.left, location)
            check_value(predicate.right, location)
        elif isinstance(predicate, (ExistsPredicate, IsTypePredicate)):
            check_value(predicate.value, location)
        elif isinstance(predicate, JunctionPredicate):
            for item in predicate.of:
                check_predicate(item, location)
        elif isinstance(predicate, NotPredicate):
            check_predicate(predicate.of, location)

    def check_text(text: TextDocument, location: str) -> None:
        for part in text.parts:
            if isinstance(part, ValuePart):
                check_value(part.value, location)
            elif isinstance(part, ConditionalPart):
                check_predicate(part.when, location)
                check_text(part.then, location)
                check_text(part.otherwise, location)

    def check_destination(destination: Destination, location: str) -> None:
        if isinstance(destination, StepDestination) and destination.step not in names:
            raise WorkflowDocumentError(
                location, f"routes to undeclared step {destination.step!r}"
            )

    for index, step in enumerate(definition.steps):
        location = f"steps[{index}]"
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
            check_text(step.prompt, f"{location}.prompt")
            check_predicate(step.when, f"{location}.when")
        elif isinstance(step, DecisionStep):
            for case_index, case in enumerate(step.cases):
                check_predicate(case.when, f"{location}.cases[{case_index}].when")
                check_destination(case.next, f"{location}.cases[{case_index}].next")
            check_destination(step.otherwise, f"{location}.otherwise")
        elif isinstance(step, GateStep):
            check_text(step.message, f"{location}.message")


def _successors(definition: WorkflowDefinition, step: Step) -> list[str]:
    """Every step this one can reach in one move, exhaustion aside."""
    targets: list[str] = []
    if isinstance(step, DecisionStep):
        destinations = [case.next for case in step.cases] + [step.otherwise]
        for destination in destinations:
            if isinstance(destination, StepDestination):
                targets.append(destination.step)
    else:
        following = definition.step_after(step.name)
        if following is not None:
            targets.append(following.name)
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


def _destination_document(destination: Destination) -> dict[str, Any]:
    if isinstance(destination, StepDestination):
        return {"step": destination.step}
    if isinstance(destination, CompleteDestination):
        return {"complete": True}
    return {"pause": True}


def _step_document(step: Step) -> dict[str, Any]:
    document: dict[str, Any] = {
        "name": step.name,
        "kind": step.kind,
        "max_visits": step.max_visits,
        "on_exhausted": (
            _destination_document(step.on_exhausted)
            if step.on_exhausted is not None
            else None
        ),
    }
    if isinstance(step, AgentStep):
        document.update(
            session=step.session,
            role=step.role,
            prompt=_text_document(step.prompt),
            expects_outcome=step.expects_outcome,
            when=_predicate_document(step.when),
        )
    elif isinstance(step, CommandStep):
        document.update(
            argv=list(step.argv), timeout=step.timeout, idempotent=step.idempotent
        )
    elif isinstance(step, DecisionStep):
        document.update(
            cases=[
                {
                    "when": _predicate_document(case.when),
                    "next": _destination_document(case.next),
                }
                for case in step.cases
            ],
            otherwise=_destination_document(step.otherwise),
        )
    else:
        document.update(message=_text_document(step.message))
    return document


def canonical_document(definition: WorkflowDefinition) -> dict[str, Any]:
    """The normalized document: every default explicit, nothing implied."""
    return {
        "format": definition.format,
        "name": definition.name,
        "sessions": list(definition.sessions),
        "primary": definition.primary,
        "steps": [_step_document(step) for step in definition.steps],
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
    """One finished (or in-flight) attempt, as an expression sees it."""

    seq: int
    step: str
    status: str
    outcome: dict[str, Any] | None


@dataclass(frozen=True)
class EvaluationContext:
    """Everything a definition may read: pinned inputs and this task's history.

    Both are captured at attempt entry and are task-local. There is nothing
    else to read — no project, no profile, no clock, no filesystem — so two
    evaluations of the same expression over the same history agree.
    """

    inputs: Mapping[str, str]
    records: tuple[HistoryRecord, ...]


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
    a statement about the declaration.
    """
    definition = revision.definition
    steps: list[StepDescriptor] = []
    after_decision = False
    for step in definition.steps:
        agent = step if isinstance(step, AgentStep) else None
        gated = agent is not None and agent.when != LiteralPredicate(value=True)
        steps.append(
            StepDescriptor(
                name=step.name,
                kind=step.kind,
                session=agent.session if agent else None,
                role=agent.role if agent else None,
                conditional=after_decision or gated,
            )
        )
        if isinstance(step, DecisionStep):
            after_decision = True
    return WorkflowDescriptor(
        name=definition.name,
        revision=revision.revision,
        format=definition.format,
        primary_session=definition.primary,
        sessions=definition.sessions,
        steps=tuple(steps),
    )
