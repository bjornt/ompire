"""Bounded authoring composition over ordinary format-4 workflow steps.

Only prospective source loading consults packaged resources. Retained documents
rebuild from their own verified snapshots; reusable declarations grant no authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from importlib import resources
from typing import Any

from ompire_daemon.workflow_definitions import (
    MAX_DEPTH,
    MAX_DOCUMENT_BYTES,
    MAX_NODES,
    MAX_STEPS,
    REQUIRED_TYPES,
    RESERVED_NAMES,
    STEP_KINDS,
    WorkflowDefinition,
    WorkflowDocumentError,
    _primitive_definition_from_document,
    _reject_unknown,
    _require_mapping,
    _require_slug,
    _require_str,
    canonical_document,
    check_draft_data,
    parse_yaml_document,
)

CATALOG_PACKAGE = "ompire_daemon.builtin_workflow_steps"
MAX_PARAMETERS = 32
MAX_DEFINITIONS = 64


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _matches(value: Any, kind: str) -> bool:
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, {"string": str, "array": list, "object": dict}[kind])


def _template(value: Any, parameters: Mapping[str, Any], location: str, *, in_array: bool = False) -> None:
    if isinstance(value, dict):
        if set(value) in ({"param"}, {"spread"}):
            operation = next(iter(value))
            name = value[operation]
            if not isinstance(name, str) or name not in parameters:
                raise WorkflowDocumentError(location, "placeholder names an undeclared parameter")
            if operation == "spread" and (not in_array or parameters[name]["type"] != "array"):
                raise WorkflowDocumentError(location, "spread requires an array parameter in an array")
            return
        for key, item in value.items():
            _template(item, parameters, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _template(item, parameters, f"{location}[{index}]", in_array=True)


def normalize_shared_definition(
    data: Any, location: str = "", *, require_revision: bool = True
) -> dict[str, Any]:
    """Verify and content-address one bounded, non-nested template snapshot."""
    data = _require_mapping(data, location)
    check_draft_data(data)
    if len(_json(data).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise WorkflowDocumentError(location, "shared definition exceeds the document byte bound")
    _reject_unknown(data, ("name", "label", "description", "parameters", "steps", "revision"), location)
    name = _require_slug(data, "name", location)
    label = _require_str(data, "label", location)
    description = _require_str(data, "description", location)
    if not label.strip():
        raise WorkflowDocumentError(f"{location}.label", "must not be blank")
    parameters = _require_mapping(data.get("parameters"), f"{location}.parameters")
    if len(parameters) > MAX_PARAMETERS:
        raise WorkflowDocumentError(f"{location}.parameters", f"more than {MAX_PARAMETERS} parameters")
    normalized: dict[str, Any] = {}
    for key, declaration in sorted(parameters.items()):
        parameter_location = f"{location}.parameters.{key}"
        if not key:
            raise WorkflowDocumentError(parameter_location, "parameter names must not be empty")
        declaration = _require_mapping(declaration, parameter_location)
        _reject_unknown(declaration, ("type", "default"), parameter_location)
        kind = declaration.get("type")
        if kind not in REQUIRED_TYPES:
            raise WorkflowDocumentError(f"{parameter_location}.type", "must be a supported JSON parameter type")
        normalized[key] = {"type": kind}
        if "default" in declaration:
            if not _matches(declaration["default"], kind):
                raise WorkflowDocumentError(f"{parameter_location}.default", f"must have type {kind}")
            normalized[key]["default"] = declaration["default"]
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise WorkflowDocumentError(f"{location}.steps", "must be a bounded nonempty list of ordinary templates")
    for index, step in enumerate(steps):
        step_location = f"{location}.steps[{index}]"
        step = _require_mapping(step, step_location)
        if "use" in step or step.get("kind") not in STEP_KINDS:
            raise WorkflowDocumentError(step_location, "shared templates contain only ordinary primitive steps")
        _template(step, normalized, step_location)
    body = {"name": name, "label": label, "description": description, "parameters": normalized, "steps": steps}
    revision = "sha256:" + hashlib.sha256(_json(body).encode("utf-8")).hexdigest()
    if (require_revision or "revision" in data) and data.get("revision") != revision:
        raise WorkflowDocumentError(f"{location}.revision", "shared definition content does not match its revision")
    # Canonical JSON detaches caller-owned data and orders every template mapping.
    return json.loads(_json({**body, "revision": revision}))


def shared_step_catalog() -> dict[str, dict[str, Any]]:
    """Read every packaged YAML resource, never a path supplied by an author."""
    definitions: dict[str, dict[str, Any]] = {}
    try:
        files = sorted(resources.files(CATALOG_PACKAGE).iterdir(), key=lambda item: item.name)
        for resource in files:
            if not resource.name.endswith((".yaml", ".yml")) or not resource.is_file():
                continue
            definition = normalize_shared_definition(
                parse_yaml_document(resource.read_text(encoding="utf-8")),
                f"definitions.{resource.name}",
                require_revision=False,
            )
            name = definition["name"]
            if name in definitions:
                raise WorkflowDocumentError(f"definitions.{name}", "duplicate shared definition")
            definitions[name] = definition
        if len(definitions) > MAX_DEFINITIONS:
            raise WorkflowDocumentError("definitions", f"more than {MAX_DEFINITIONS} shared definitions")
    except (ImportError, OSError) as exc:
        raise WorkflowDocumentError("definitions", "packaged shared definitions are unavailable") from exc
    return definitions


class _Expansion:
    """Count copied data before allocating it, including every substituted node."""

    def __init__(self) -> None:
        self.nodes = 0
        self.bytes = 0

    def copy(self, value: Any, location: str, depth: int = 0, bindings: Mapping[str, Any] | None = None) -> Any:
        if depth > MAX_DEPTH:
            raise WorkflowDocumentError(location, "expanded steps exceed the nesting bound")
        if bindings is not None and isinstance(value, dict) and set(value) == {"param"}:
            return self.copy(bindings[value["param"]], location, depth)
        self.nodes += 1
        self.bytes += 2
        if self.nodes > MAX_NODES or self.bytes > MAX_DOCUMENT_BYTES:
            raise WorkflowDocumentError(location, "expanded steps exceed the document bounds")
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                self.bytes += len(key.encode("utf-8"))
                result[key] = self.copy(item, f"{location}.{key}", depth + 1, bindings)
            return result
        if isinstance(value, list):
            items: list[Any] = []
            for index, item in enumerate(value):
                item_location = f"{location}[{index}]"
                if bindings is not None and isinstance(item, dict) and set(item) == {"spread"}:
                    for spread_item in bindings[item["spread"]]:
                        items.append(self.copy(spread_item, item_location, depth + 1))
                else:
                    items.append(self.copy(item, item_location, depth + 1, bindings))
            return items
        if isinstance(value, str):
            self.bytes += len(value.encode("utf-8"))
        return value


def _stages(raw: Any, defaults: list[dict[str, Any]], names: list[str], location: str) -> list[dict[str, Any]]:
    if raw is None:
        raw = defaults
    if not isinstance(raw, list) or not raw:
        raise WorkflowDocumentError(location, "must be a nonempty ordered list of stages")
    stages: list[dict[str, Any]] = []
    seen: set[str] = set()
    membership: list[str] = []
    for index, stage in enumerate(raw):
        stage_location = f"{location}[{index}]"
        stage = _require_mapping(stage, stage_location)
        _reject_unknown(stage, ("name", "label", "description", "steps"), stage_location)
        name = _require_slug(stage, "name", stage_location)
        if name in seen or name in RESERVED_NAMES:
            raise WorkflowDocumentError(f"{stage_location}.name", "stage names must be unique and not reserved")
        seen.add(name)
        label = _require_str(stage, "label", stage_location)
        description = _require_str(stage, "description", stage_location)
        if not label.strip():
            raise WorkflowDocumentError(f"{stage_location}.label", "must not be blank")
        steps = stage.get("steps")
        if not isinstance(steps, list) or not steps:
            raise WorkflowDocumentError(f"{stage_location}.steps", "must list at least one execution step")
        for offset, step in enumerate(steps):
            if not isinstance(step, str) or step not in names or step in membership:
                raise WorkflowDocumentError(f"{stage_location}.steps[{offset}]", "must name an execution step exactly once")
            membership.append(step)
        if steps != sorted(steps, key=names.index):
            raise WorkflowDocumentError(f"{stage_location}.steps", "stage members must follow declaration order")
        stages.append({"name": name, "label": label, "description": description, "steps": list(steps)})
    if set(membership) != set(names):
        raise WorkflowDocumentError(location, "stages must cover every execution step")
    starts = [names.index(stage["steps"][0]) for stage in stages]
    if starts != sorted(starts):
        raise WorkflowDocumentError(location, "stages must follow their first execution step's declaration order")
    return stages


def composition_definition(document: Mapping[str, Any], *, resolve_catalog: bool) -> WorkflowDefinition:
    """Compile source, or verify frozen source and its retained expansion."""
    check_draft_data(dict(document))
    if len(_json(dict(document)).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise WorkflowDocumentError("", "composition exceeds the document byte bound")
    canonical = "composition" in document
    _reject_unknown(document, ("format", "name", "sessions", "primary", "steps", *(("composition",) if canonical else ("definitions", "stages"))), "")
    source_location = "composition.steps" if canonical else "steps"
    definitions_location = "composition.definitions" if canonical else "definitions"
    if canonical:
        composition = _require_mapping(document["composition"], "composition")
        _reject_unknown(composition, ("steps", "definitions", "stages"), "composition")
        if any(key not in composition for key in ("steps", "definitions", "stages")):
            raise WorkflowDocumentError("composition", "retained composition needs source steps, definitions, and stages")
        source = composition.get("steps")
        embedded = composition.get("definitions")
        stage_data = composition.get("stages")
    else:
        source = document.get("steps")
        embedded = document.get("definitions", {})
        stage_data = document.get("stages")
    if (canonical or "stages" in document) and not isinstance(stage_data, list):
        raise WorkflowDocumentError("composition.stages" if canonical else "stages", "must be an ordered list of stages")
    if not isinstance(source, list) or not source or len(source) > MAX_STEPS:
        raise WorkflowDocumentError(source_location, "must be a bounded nonempty list of source steps")
    embedded = _require_mapping(embedded, definitions_location)
    if len(embedded) > MAX_DEFINITIONS:
        raise WorkflowDocumentError(definitions_location, f"more than {MAX_DEFINITIONS} definitions")
    definitions: dict[str, Any] = {}
    for name, data in embedded.items():
        snapshot = normalize_shared_definition(data, f"{definitions_location}.{name}")
        if snapshot["name"] != name:
            raise WorkflowDocumentError(f"{definitions_location}.{name}.name", "definition name does not match its key")
        definitions[name] = snapshot
    catalog: dict[str, Any] | None = None
    used: dict[str, Any] = {}
    expanded: list[Any] = []
    normalized_source: list[Any] = []
    defaults: list[dict[str, Any]] = []
    source_names: set[str] = set()
    origins: list[str] = []
    budget = _Expansion()
    for index, raw in enumerate(source):
        location = f"{source_location}[{index}]"
        raw = _require_mapping(raw, location)
        name = _require_slug(raw, "name", location)
        if name in source_names or name in RESERVED_NAMES:
            raise WorkflowDocumentError(f"{location}.name", "source names must be unique and not reserved")
        source_names.add(name)
        if "use" not in raw:
            added = [budget.copy(raw, location)]
            normalized_source.append(raw)
            label, description = name, str(raw.get("kind", ""))
        else:
            _reject_unknown(raw, ("name", "use", "bindings"), location)
            use = _require_slug(raw, "use", location)
            if use not in definitions:
                if canonical or not resolve_catalog:
                    raise WorkflowDocumentError(f"{location}.use", "shared definition is missing from frozen snapshots")
                if catalog is None:
                    catalog = shared_step_catalog()
                if use not in catalog:
                    raise WorkflowDocumentError(f"{location}.use", f"unknown shared definition {use!r}")
                definitions[use] = normalize_shared_definition(catalog[use], f"{definitions_location}.{use}")
            shared = definitions[use]
            used[use] = shared
            if len(used) > MAX_DEFINITIONS:
                raise WorkflowDocumentError(location, f"more than {MAX_DEFINITIONS} used definitions")
            bindings = _require_mapping(raw.get("bindings"), f"{location}.bindings")
            _reject_unknown(bindings, tuple(shared["parameters"]), f"{location}.bindings")
            bound: dict[str, Any] = {}
            for key, declaration in shared["parameters"].items():
                parameter_location = f"{location}.bindings.{key}"
                if key not in bindings and "default" not in declaration:
                    raise WorkflowDocumentError(parameter_location, "missing required parameter")
                value = bindings[key] if key in bindings else declaration["default"]
                if not _matches(value, declaration["type"]):
                    raise WorkflowDocumentError(parameter_location, f"must have type {declaration['type']}")
                bound[key] = value
            if len(expanded) + len(shared["steps"]) > MAX_STEPS:
                raise WorkflowDocumentError(location, "expanded steps exceed the step bound")
            added = [budget.copy(step, f"{location}.expansion[{offset}]", bindings=bound) for offset, step in enumerate(shared["steps"])]
            normalized_source.append({"name": name, "use": use, "bindings": bound})
            label, description = shared["label"], shared["description"]
        expanded.extend(added)
        origins.extend([location] * len(added))
        if len(expanded) > MAX_STEPS:
            raise WorkflowDocumentError(location, "expanded steps exceed the step bound")
        defaults.append({"name": name, "label": label, "description": description, "steps": [step.get("name") for step in added]})
    primitive_document = {key: document.get(key) for key in ("format", "name", "sessions", "primary")}
    primitive_document["steps"] = expanded
    try:
        definition = _primitive_definition_from_document(primitive_document)
    except WorkflowDocumentError as exc:
        if exc.location.startswith("steps["):
            index_text, _, suffix = exc.location[6:].partition("]")
            if index_text.isdigit() and int(index_text) < len(origins):
                index = int(index_text)
                origin = origins[index]
                raise WorkflowDocumentError(f"{origin}.expansion{suffix}", exc.reason) from exc
        raise
    ordinary_steps = canonical_document(definition)["steps"]
    expanded_index = 0
    for source_index, raw in enumerate(source):
        if "use" not in raw:
            normalized_source[source_index] = ordinary_steps[expanded_index]
            expanded_index += 1
        else:
            expanded_index += len(used[raw["use"]]["steps"])
    stages = _stages(stage_data, defaults, [step.name for step in definition.steps], "composition.stages" if canonical else "stages")
    frozen = {"steps": normalized_source, "definitions": used, "stages": stages}
    if canonical:
        if set(embedded) != set(used):
            raise WorkflowDocumentError(definitions_location, "frozen snapshots must match the used definitions")
        if _json(document.get("steps")) != _json(canonical_document(definition)["steps"]):
            raise WorkflowDocumentError("steps", "retained execution steps do not match the frozen composition expansion")
    definition = replace(definition, composition=_json(frozen))
    retained = canonical_document(definition)
    check_draft_data(retained)
    if len(_json(retained).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise WorkflowDocumentError("composition", "retained composition exceeds the document byte bound")
    return definition
