import {
  COMPARISONS,
  DELIVERY_ACTIONS,
  DELIVERY_METADATA_FIELDS,
  type DeliveryMetadataField,
  asArray,
  asObject,
  asString,
  documentFormat,
  isDraftObject,
  stepObjects,
  type DraftObject,
  type DraftValue,
} from "./workflowDocument";
import { numberToken } from "./losslessJson";

/** What a definition declares may happen, derived once for every reader.
 *
 * Structure only. Nothing here evaluates a predicate, picks a case, or
 * predicts a route: a browser deciding which branch "wins" would be a second
 * interpreter, and a wrong one — the daemon routes on evidence this code has
 * never seen. What these edges say is *which routes exist*, which is the
 * question a library reader, a launch preview, and a task inspector all ask.
 *
 * Every kind of edge is distinguishable, because collapsing them is how a
 * diagram starts lying: an implicit fall-through, an ordered branch, a human
 * choice, and an exhausted bound are four different reasons to go somewhere.
 */

export type FlowTarget =
  | { kind: "step"; step: string; index: number | null }
  | { kind: "complete"; result: string | null }
  | { kind: "pause" }
  | { kind: "end-of-list" }
  | { kind: "unset" };

export type EdgeKind =
  | "fall-through"
  | "case"
  | "otherwise"
  | "choice"
  /** An approving answer, and the exact chain of privileged actions it
   * grants. Distinct from an ordinary choice because it is the only edge in
   * a definition that confers authority. */
  | "authorize"
  /** A delivery step continues after its effect is on record. */
  | "delivered"
  /** A capture completes and follows its declared route. */
  | "captured"
  | "exhausted"
  | "skip";

export interface FlowEdge {
  fromIndex: number;
  from: string;
  kind: EdgeKind;
  /** Why this edge is taken, in the author's own terms. */
  label: string;
  to: FlowTarget;
}

export interface FlowStep {
  index: number;
  /** Null when the card has no usable name yet — an unfinished draft, never
   * hidden and never given a made-up one. */
  name: string | null;
  kind: string | null;
  step: DraftObject;
}

export interface Flow {
  format: number | null;
  steps: FlowStep[];
  edges: FlowEdge[];
}

/** True when a step really declares a visit bound.
 *
 * Both halves are declared together, so either one being present and non-null
 * is the bound; `null` is the canonical form's way of writing "no bound". */
export function isBounded(step: DraftObject): boolean {
  const visits = step.max_visits;
  const exhausted = step.on_exhausted;
  return (
    (visits !== undefined && visits !== null) ||
    (exhausted !== undefined && exhausted !== null)
  );
}

function targetFor(
  destination: DraftValue | undefined,
  names: (string | null)[],
): FlowTarget {
  if (destination === undefined) return { kind: "unset" };
  const object = asObject(destination);
  if (object === null) return { kind: "unset" };
  if ("step" in object) {
    const step = asString(object.step);
    if (step === null) return { kind: "unset" };
    const index = names.indexOf(step);
    return { kind: "step", step, index: index === -1 ? null : index };
  }
  if ("pause" in object) return { kind: "pause" };
  if ("complete" in object) return { kind: "complete", result: asString(object.result) };
  return { kind: "unset" };
}

export function describeTarget(target: FlowTarget): string {
  switch (target.kind) {
    case "step":
      return target.index === null ? `${target.step} — no such step` : target.step;
    case "complete":
      return target.result === null ? "ends the run" : `ends · ${target.result}`;
    case "pause":
      return "pauses for you";
    case "end-of-list":
      return "the end of the step list";
    case "unset":
      return "nowhere yet";
  }
}

/** Read one definition or draft as steps plus declared edges. */
export function readFlow(document: DraftObject): Flow {
  const format = documentFormat(document);
  const objects = stepObjects(document);
  const names = objects.map((step) => asString(step.name));
  const steps: FlowStep[] = objects.map((step, index) => ({
    index,
    name: names[index],
    kind: asString(step.kind),
    step,
  }));
  const edges: FlowEdge[] = [];

  const next = (index: number): FlowTarget =>
    index + 1 < objects.length
      ? { kind: "step", step: names[index + 1] ?? "", index: index + 1 }
      : { kind: "end-of-list" };

  for (const { index, name, kind, step } of steps) {
    const from = name ?? `step ${index + 1}`;
    const push = (edge: Omit<FlowEdge, "fromIndex" | "from">) =>
      edges.push({ ...edge, fromIndex: index, from });

    if (kind === "decision") {
      asArray(step.cases).forEach((entry, caseIndex) => {
        const object = asObject(entry);
        push({
          kind: "case",
          label: `${caseIndex + 1}. when ${describePredicate(object?.when)}`,
          to: targetFor(object?.next, names),
        });
      });
      push({ kind: "otherwise", label: "otherwise", to: targetFor(step.otherwise, names) });
    } else if (kind === "gate" && format !== 1) {
      asArray(step.choices).forEach((entry) => {
        const object = asObject(entry);
        const label = asString(object?.label) ?? asString(object?.id) ?? "an answer";
        const grant = asArray(asObject(object?.authorize)?.steps)
          .map((name) => asString(name))
          .filter((name): name is string => name !== null);
        push({
          kind: grant.length > 0 ? "authorize" : "choice",
          // What the answer *does*, not just where it goes. An approving
          // answer is the only thing that can grant publication, so the
          // diagram says so rather than drawing an ordinary edge.
          label:
            grant.length > 0
              ? `you answer “${label}” — authorizing ${grant.join(" → ")}`
              : `you answer “${label}”`,
          to: targetFor(object?.next, names),
        });
      });
    } else if (kind === "delivery") {
      push({
        kind: "delivered",
        label: `once the ${asString(step.action) ?? "action"} is on record`,
        to: targetFor(step.next, names),
      });
    } else if (kind === "capture") {
      push({
        kind: "captured",
        label: "once its declared files are retained",
        to: targetFor(step.next, names),
      });
    } else {
      if (step.when !== undefined && step.when !== true) {
        push({
          kind: "skip",
          label: `skipped unless ${describePredicate(step.when)}`,
          to: next(index),
        });
      }
      push({ kind: "fall-through", label: "when it finishes", to: next(index) });
    }

    // A canonical document spells out every default, so an unbounded step
    // carries `max_visits: null` rather than no key at all. Reading `null` as
    // "declared" draws a bound edge to nowhere on every ordinary step.
    if (isBounded(step)) {
      const bound = numberToken(step.max_visits);
      push({
        kind: "exhausted",
        label:
          bound === null
            ? "after its visit bound runs out"
            : `after ${bound} visit${bound === "1" ? "" : "s"}`,
        to: targetFor(step.on_exhausted, names),
      });
    }
  }
  return { format, steps, edges };
}

export function edgesFrom(flow: Flow, index: number): FlowEdge[] {
  return flow.edges.filter((edge) => edge.fromIndex === index);
}

// --- readable expressions -----------------------------------------------------
// Complete rather than tidy. The old outline summarized `after`,
// `with_outcome`, and part formats away, which reads as a simpler definition
// than the one that will actually run.

function literal(value: DraftValue | undefined): string {
  const token = numberToken(value);
  if (token !== null) return token;
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "boolean") return String(value);
  if (value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(literal).join(", ")}]`;
  if (isDraftObject(value)) {
    return `{${Object.entries(value)
      .map(([key, item]) => `${key}: ${literal(item)}`)
      .join(", ")}}`;
  }
  return "?";
}

export function describeValue(value: DraftValue | undefined): string {
  const object = asObject(value);
  if (object === null) return value === undefined ? "nothing" : literal(value);
  switch (asString(object.op)) {
    case "literal":
      return literal(object.value);
    case "input":
      return `the task's ${asString(object.name) ?? "?"}`;
    case "evidence":
      return `evidence “${asString(object.name) ?? "?"}”`;
    case "latest": {
      const steps = asArray(object.steps).map((step) => asString(step) ?? "?");
      const after = asString(object.after);
      const withOutcome = object.with_outcome === true;
      return (
        `the latest attempt of ${steps.join(" or ")}` +
        (withOutcome ? " that recorded an outcome" : "") +
        (after === null ? "" : `, no older than ${after}`)
      );
    }
    case "count":
      return `visits to ${asString(object.step) ?? "?"}`;
    case "get":
      return `${describeValue(object.value)} → ${asArray(object.keys)
        .map((key) => numberToken(key) ?? asString(key) ?? "?")
        .join(" → ")}`;
    case "coalesce":
      return asArray(object.values).map(describeValue).join(", or else ");
    default:
      return `unsupported value ${literal(object.op)}`;
  }
}

const COMPARISON_WORDS: Record<string, string> = {
  eq: "is",
  ne: "is not",
  lt: "is less than",
  lte: "is at most",
  gt: "is greater than",
  gte: "is at least",
};

export function describePredicate(predicate: DraftValue | undefined): string {
  if (predicate === true) return "always";
  if (predicate === false) return "never";
  const object = asObject(predicate);
  if (object === null) return predicate === undefined ? "nothing" : literal(predicate);
  const op = asString(object.op) ?? "";
  if ((COMPARISONS as readonly string[]).includes(op)) {
    return `${describeValue(object.left)} ${COMPARISON_WORDS[op]} ${describeValue(object.right)}`;
  }
  if (op === "exists") return `${describeValue(object.value)} is present`;
  if (op === "is_type") {
    return `${describeValue(object.value)} is a ${asString(object.type) ?? "?"}`;
  }
  if (op === "not") return `not (${describePredicate(object.of)})`;
  if (op === "all" || op === "any") {
    const joined = asArray(object.of).map((entry) => `(${describePredicate(entry)})`);
    if (joined.length === 0) return `${op} of nothing`;
    return joined.join(op === "all" ? " and " : " or ");
  }
  return `unsupported condition ${literal(object.op)}`;
}

/** One rendered piece of a text document, kept separate so an empty part and
 * a separator stay visible instead of disappearing into a joined string. */
export type TextPiece =
  | { kind: "text"; text: string }
  | { kind: "value"; description: string; format: string }
  | { kind: "conditional"; condition: string; then: TextPiece[]; otherwise: TextPiece[] }
  | { kind: "unsupported"; description: string };

export interface TextReading {
  separator: string;
  pieces: TextPiece[];
}

export function readText(document: DraftValue | undefined): TextReading {
  const object = asObject(document);
  if (object === null) return { separator: "", pieces: [] };
  return {
    separator: asString(object.separator) ?? "",
    pieces: asArray(object.parts).map((part): TextPiece => {
      const entry = asObject(part);
      if (entry === null) return { kind: "unsupported", description: literal(part) };
      if ("text" in entry) {
        return { kind: "text", text: asString(entry.text) ?? "" };
      }
      if ("value" in entry) {
        return {
          kind: "value",
          description: describeValue(entry.value),
          format: asString(entry.format) ?? "text",
        };
      }
      if ("if" in entry) {
        return {
          kind: "conditional",
          condition: describePredicate(entry.if),
          then: readText(entry.then).pieces,
          otherwise: readText(entry.else).pieces,
        };
      }
      return { kind: "unsupported", description: literal(entry) };
    }),
  };
}

/** A gate's delivery binding, as the editor and the diagram read it. */
export interface DeliveryGateReading {
  /** The evidence alias naming the review this approval is about. */
  review: string | null;
  /** The publication text the definition suggests, per field. An absent
   * field starts blank for the operator; it is not a missing value. */
  metadata: { field: DeliveryMetadataField; text: DraftValue }[];
}

export function readDeliveryGate(step: DraftObject): DeliveryGateReading | null {
  const delivery = asObject(step.delivery);
  if (delivery === null) return null;
  const metadata = asObject(delivery.metadata);
  return {
    review: asString(delivery.review),
    metadata:
      metadata === null
        ? []
        : DELIVERY_METADATA_FIELDS.filter(
            (field) => metadata[field] !== undefined && metadata[field] !== null,
          ).map((field) => ({ field, text: metadata[field] })),
  };
}

/** What one privileged action declares. Null for every other kind. */
export interface DeliveryActionReading {
  action: string | null;
  mode: string | null;
  approval: string | null;
  previous: string | null;
  next: FlowTarget;
}

export function readDeliveryAction(
  document: DraftObject,
  step: DraftObject,
): DeliveryActionReading | null {
  if (asString(step.kind) !== "delivery") return null;
  const names = stepObjects(document).map((entry) => asString(entry.name));
  return {
    action: asString(step.action),
    mode: asString(step.mode),
    approval: asString(step.approval),
    previous: asString(step.previous),
    next: targetFor(step.next, names),
  };
}

/** Every privileged effect this document can perform, in effect order.
 *
 * An empty list is a statement — this workflow publishes nothing — and the
 * editor says so rather than leaving the reader to infer it from absence. */
export function declaredActions(document: DraftObject): string[] {
  const declared = new Set(
    stepObjects(document)
      .filter((step) => asString(step.kind) === "delivery")
      .map((step) => asString(step.action)),
  );
  return DELIVERY_ACTIONS.filter((action) => declared.has(action));
}

export interface EvidenceReading {
  alias: string;
  steps: string[];
  after: string | null;
  withOutcome: boolean;
  required: boolean;
  unsupported: boolean;
}

export function readEvidence(step: DraftObject): EvidenceReading[] {
  const evidence = asObject(step.evidence);
  if (evidence === null) return [];
  return Object.entries(evidence).map(([alias, selector]) => {
    const object = asObject(selector);
    return {
      alias,
      steps: asArray(object?.steps).map((name) => asString(name) ?? "?"),
      after: asString(object?.after),
      // Both default to true in format 2, and both change what the step is
      // handed, so neither is summarized away.
      withOutcome: object?.with_outcome !== false,
      required: object?.required !== false,
      unsupported: object === null,
    };
  });
}

export interface ResultReading {
  name: string;
  required: { field: string; type: string }[];
}

export function readResults(step: DraftObject): ResultReading[] | null {
  const outcome = asObject(step.outcome);
  if (outcome === null) return null;
  const results = asObject(outcome.results);
  if (results === null) return [];
  return Object.entries(results).map(([name, contract]) => ({
    name,
    required: Object.entries(asObject(asObject(contract)?.required) ?? {}).map(
      ([field, type]) => ({ field, type: asString(type) ?? literal(type) }),
    ),
  }));
}

/** The keys of a step this editor does not know about, so an unsupported
 * construct is named rather than presented as an editable facade that would
 * drop it on the next save. */
const KNOWN_STEP_FIELDS: Record<string, readonly string[]> = {
  agent: ["name", "kind", "max_visits", "on_exhausted", "evidence", "session", "role", "prompt", "when", "expects_outcome", "outcome"],
  command: ["name", "kind", "max_visits", "on_exhausted", "evidence", "argv", "timeout", "idempotent"],
  decision: ["name", "kind", "max_visits", "on_exhausted", "evidence", "cases", "otherwise"],
  gate: ["name", "kind", "max_visits", "on_exhausted", "evidence", "message", "choices", "delivery", "result"],
  review: ["name", "kind", "max_visits", "on_exhausted", "evidence"],
  capture: ["name", "kind", "max_visits", "on_exhausted", "evidence", "producer", "paths", "allowlist", "next"],
  // Deliberately short: an action has no bound, no evidence, and no prompt.
  // Anything else on this card is a field the grammar does not have.
  delivery: ["name", "kind", "action", "mode", "approval", "previous", "next"],
};

export function unknownStepFields(step: DraftObject): string[] {
  const known = KNOWN_STEP_FIELDS[asString(step.kind) ?? ""] ?? ["name", "kind"];
  return Object.keys(step).filter((key) => !known.includes(key));
}

export function unknownDocumentFields(document: DraftObject): string[] {
  const known = ["format", "name", "sessions", "primary", "steps"];
  return Object.keys(document).filter((key) => !known.includes(key));
}
