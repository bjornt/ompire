import { isLosslessNumber } from "./losslessJson";

/** The draft document a visual editor edits, and the structural operations
 * that are safe to perform on one.
 *
 * A *draft* is not a definition. It may be half-finished, may point at steps
 * that do not exist yet, and may carry fields no format understands. The
 * daemon is the only thing that decides whether one is executable; this
 * module's whole job is to let an editor change a draft's shape without
 * changing anything it did not mean to.
 *
 * That is why nothing here is text substitution. A rename walks the places
 * the grammar actually puts a step name, a session name, or an evidence
 * alias — and deliberately never enters `{op: literal, value: …}` payloads or
 * `{text: …}` prose, where an identical-looking string is a coincidence and
 * rewriting it would silently edit an instruction.
 *
 * Unknown keys survive every operation: each rewrite copies the node it
 * touches and replaces only the field it means to, so a construct a future
 * format adds is carried through rather than quietly dropped.
 */

export type DraftValue =
  | string
  | boolean
  | null
  | { toString(): string }
  | DraftValue[]
  | DraftObject;

export interface DraftObject {
  [key: string]: DraftValue;
}

export const STEP_KINDS = [
  "agent",
  "command",
  "decision",
  "gate",
  "review",
  "delivery",
  "capture",
] as const;
export type StepKindName = (typeof STEP_KINDS)[number];

/** The privileged effects a delivery step may name, in the only order a
 * chain may run them. Mirrored from the daemon's closed grammar so the editor
 * can offer exactly these — never so it can decide what is valid. */
export const DELIVERY_ACTIONS = ["commit", "push", "pr"] as const;
export type DeliveryAction = (typeof DELIVERY_ACTIONS)[number];
export const DELIVERY_MODES = ["squash", "retain"] as const;
/** The publication text a delivery gate may suggest. Each is an ordinary
 * text document over the gate's own frozen evidence. */
export const DELIVERY_METADATA_FIELDS = ["message", "pr_title", "pr_body"] as const;
export type DeliveryMetadataField = (typeof DELIVERY_METADATA_FIELDS)[number];

/** The closed vocabularies the daemon's loader accepts. Mirrored here so the
 * editor can offer exactly them — never so it can decide what is valid. */
export const INPUT_NAMES = [
  "task.prompt",
  "task.slug",
  "task.branch",
  "workspace.preamble",
] as const;
export const COMPARISONS = ["eq", "ne", "lt", "lte", "gt", "gte"] as const;
export const JSON_TYPES = [
  "null",
  "boolean",
  "integer",
  "number",
  "string",
  "array",
  "object",
] as const;
export const REQUIRED_TYPES = [
  "boolean",
  "integer",
  "number",
  "string",
  "array",
  "object",
] as const;
export const VALUE_FORMATS = ["text", "json"] as const;
export const MODEL_ROLES = ["default", "smol", "slow", "plan"] as const;

// --- reading ------------------------------------------------------------------

export function isDraftObject(value: unknown): value is DraftObject {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    !isLosslessNumber(value)
  );
}

export function asObject(value: unknown): DraftObject | null {
  return isDraftObject(value) ? value : null;
}

export function asArray(value: unknown): DraftValue[] {
  return Array.isArray(value) ? (value as DraftValue[]) : [];
}

export function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function asBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

export function documentFormat(document: DraftObject): number | null {
  const format = document.format;
  if (typeof format === "number") return format;
  if (isLosslessNumber(format)) {
    const parsed = Number(format.toString());
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** The steps as objects. A non-object entry in an invalid draft is kept in
 * the document but has no card of its own; the validation summary names it. */
export function stepObjects(document: DraftObject): DraftObject[] {
  return asArray(document.steps).filter(isDraftObject);
}

export function stepNames(document: DraftObject): string[] {
  return stepObjects(document)
    .map((step) => asString(step.name))
    .filter((name): name is string => name !== null);
}

export function sessionNames(document: DraftObject): string[] {
  return asArray(document.sessions)
    .map((name) => asString(name))
    .filter((name): name is string => name !== null);
}

/** The aliases one step declares. Evidence is step-local, so an alias only
 * ever means something inside the card that declared it. */
export function evidenceAliases(step: DraftObject): string[] {
  const evidence = asObject(step.evidence);
  return evidence === null ? [] : Object.keys(evidence);
}

// --- writing ------------------------------------------------------------------

/** Replace one key, keeping every other key — including ones this editor does
 * not understand. `undefined` removes the key. */
export function withKey(
  object: DraftObject,
  key: string,
  value: DraftValue | undefined,
): DraftObject {
  const next: DraftObject = { ...object };
  if (value === undefined) delete next[key];
  else next[key] = value;
  return next;
}

export function withSteps(document: DraftObject, steps: DraftValue[]): DraftObject {
  return withKey(document, "steps", steps);
}

/** Rewrite one step in place by index, leaving the rest of the list alone. */
export function updateStep(
  document: DraftObject,
  index: number,
  update: (step: DraftObject) => DraftObject,
): DraftObject {
  const steps = asArray(document.steps);
  const target = steps[index];
  if (!isDraftObject(target)) return document;
  const next = [...steps];
  next[index] = update(target);
  return withSteps(document, next);
}

/** Move a card. Order is execution-significant: the step after this one is
 * where an agent or command step falls through to. */
export function moveStep(document: DraftObject, from: number, to: number): DraftObject {
  const steps = asArray(document.steps);
  if (from < 0 || from >= steps.length || to < 0 || to >= steps.length || from === to) {
    return document;
  }
  const next = [...steps];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return withSteps(document, next);
}

export function insertStep(
  document: DraftObject,
  index: number,
  step: DraftObject,
): DraftObject {
  const next = [...asArray(document.steps)];
  next.splice(index, 0, step);
  return withSteps(document, next);
}

export function removeStep(document: DraftObject, index: number): DraftObject {
  const next = [...asArray(document.steps)];
  next.splice(index, 1);
  return withSteps(document, next);
}

/** What runs next when a step simply finishes: the following card, or the end
 * of the list. Shown when a card moves, because moving one silently changes
 * this for two steps. */
export function fallThrough(document: DraftObject, index: number): string | null {
  const names = stepObjects(document).map((step) => asString(step.name));
  return names[index + 1] ?? null;
}

// --- structural references ----------------------------------------------------

/** One place a name is used as a *reference*, addressed the way the daemon
 * addresses a validation error. */
export interface Reference {
  /** `steps[2].cases[0].next.step` — the daemon's own location grammar. */
  location: string;
  /** The step whose card owns this reference, for navigation. */
  stepIndex: number;
  /** What the reference is, in words, for the impact list before a delete. */
  what: string;
  /** The name being referred to. */
  name: string;
  /** Which namespace it lives in, so a step and an agent sharing a name are
   * never confused for one another. */
  kind: "step" | "session" | "evidence";
}

type Rewrite = {
  step?: (name: string) => string;
  session?: (name: string) => string;
  evidenceAlias?: (name: string) => string;
};

type Visit = (reference: Reference) => void;

interface Context {
  rewrite: Rewrite;
  visit: Visit;
  stepIndex: number;
}

function rewriteName(
  name: DraftValue,
  kind: Reference["kind"],
  map: ((name: string) => string) | undefined,
  location: string,
  what: string,
  context: Context,
): DraftValue {
  const text = asString(name);
  if (text === null) return name;
  context.visit({ location, stepIndex: context.stepIndex, what, name: text, kind });
  return map === undefined ? name : map(text);
}

function rewriteValue(node: DraftValue, location: string, context: Context): DraftValue {
  const object = asObject(node);
  if (object === null) return node;
  const op = asString(object.op);
  // A literal's payload is opaque data. Walking into it is exactly how a
  // rename starts editing somebody's instruction.
  if (op === "literal") return object;
  if (op === "evidence") {
    return withKey(
      object,
      "name",
      rewriteName(
        object.name,
        "evidence",
        context.rewrite.evidenceAlias,
        `${location}.name`,
        "reads this evidence alias",
        context,
      ),
    );
  }
  if (op === "latest") {
    let next = withKey(
      object,
      "steps",
      asArray(object.steps).map((entry, index) =>
        rewriteName(
          entry,
          "step",
          context.rewrite.step,
          `${location}.steps[${index}]`,
          "selects the latest attempt of this step",
          context,
        ),
      ),
    );
    if (object.after !== undefined && object.after !== null) {
      next = withKey(
        next,
        "after",
        rewriteName(
          object.after,
          "step",
          context.rewrite.step,
          `${location}.after`,
          "anchors freshness to this step",
          context,
        ),
      );
    }
    return next;
  }
  if (op === "count") {
    return withKey(
      object,
      "step",
      rewriteName(
        object.step,
        "step",
        context.rewrite.step,
        `${location}.step`,
        "counts visits to this step",
        context,
      ),
    );
  }
  if (op === "get") {
    return withKey(object, "value", rewriteValue(object.value, `${location}.value`, context));
  }
  if (op === "coalesce") {
    return withKey(
      object,
      "values",
      asArray(object.values).map((entry, index) =>
        rewriteValue(entry, `${location}.values[${index}]`, context),
      ),
    );
  }
  return object;
}

function rewritePredicate(
  node: DraftValue,
  location: string,
  context: Context,
): DraftValue {
  const object = asObject(node);
  if (object === null) return node;
  const op = asString(object.op);
  if (op === "not") {
    return withKey(object, "of", rewritePredicate(object.of, `${location}.of`, context));
  }
  if (op === "all" || op === "any") {
    return withKey(
      object,
      "of",
      asArray(object.of).map((entry, index) =>
        rewritePredicate(entry, `${location}.of[${index}]`, context),
      ),
    );
  }
  if (op === "exists" || op === "is_type") {
    return withKey(object, "value", rewriteValue(object.value, `${location}.value`, context));
  }
  if (op !== null && (COMPARISONS as readonly string[]).includes(op)) {
    return withKey(
      withKey(object, "left", rewriteValue(object.left, `${location}.left`, context)),
      "right",
      rewriteValue(object.right, `${location}.right`, context),
    );
  }
  return object;
}

function rewriteText(node: DraftValue, location: string, context: Context): DraftValue {
  const object = asObject(node);
  if (object === null) return node;
  return withKey(
    object,
    "parts",
    asArray(object.parts).map((part, index) => {
      const partLocation = `${location}.parts[${index}]`;
      const entry = asObject(part);
      if (entry === null) return part;
      // Prose is prose. A `text` part is never traversed for references.
      if ("text" in entry) return entry;
      if ("value" in entry) {
        return withKey(
          entry,
          "value",
          rewriteValue(entry.value, `${partLocation}.value`, context),
        );
      }
      let next = withKey(entry, "if", rewritePredicate(entry.if, `${partLocation}.if`, context));
      if (entry.then !== undefined) {
        next = withKey(next, "then", rewriteText(entry.then, `${partLocation}.then`, context));
      }
      if (entry.else !== undefined) {
        next = withKey(next, "else", rewriteText(entry.else, `${partLocation}.else`, context));
      }
      return next;
    }),
  );
}

function rewriteDestination(
  node: DraftValue,
  location: string,
  context: Context,
  what: string,
): DraftValue {
  const object = asObject(node);
  if (object === null || !("step" in object)) return node;
  return withKey(
    object,
    "step",
    rewriteName(object.step, "step", context.rewrite.step, `${location}.step`, what, context),
  );
}

function rewriteEvidence(
  node: DraftValue,
  location: string,
  context: Context,
): DraftValue {
  const object = asObject(node);
  if (object === null) return node;
  const next: DraftObject = {};
  for (const [alias, selector] of Object.entries(object)) {
    const aliasLocation = `${location}.${alias}`;
    const renamed = context.rewrite.evidenceAlias?.(alias) ?? alias;
    const entry = asObject(selector);
    if (entry === null) {
      next[renamed] = selector;
      continue;
    }
    let updated = withKey(
      entry,
      "steps",
      asArray(entry.steps).map((step, index) =>
        rewriteName(
          step,
          "step",
          context.rewrite.step,
          `${aliasLocation}.steps[${index}]`,
          `is the source of evidence “${alias}”`,
          context,
        ),
      ),
    );
    if (entry.after !== undefined && entry.after !== null) {
      updated = withKey(
        updated,
        "after",
        rewriteName(
          entry.after,
          "step",
          context.rewrite.step,
          `${aliasLocation}.after`,
          `anchors the freshness of evidence “${alias}”`,
          context,
        ),
      );
    }
    next[renamed] = updated;
  }
  return next;
}

function rewriteStep(step: DraftObject, index: number, rewrite: Rewrite, visit: Visit): DraftObject {
  const context: Context = { rewrite, visit, stepIndex: index };
  const location = `steps[${index}]`;
  let next = step;
  if (step.session !== undefined) {
    next = withKey(
      next,
      "session",
      rewriteName(
        step.session,
        "session",
        rewrite.session,
        `${location}.session`,
        "runs in this agent's conversation",
        context,
      ),
    );
  }
  if (step.evidence !== undefined) {
    next = withKey(next, "evidence", rewriteEvidence(step.evidence, `${location}.evidence`, context));
  }
  if (step.on_exhausted !== undefined) {
    next = withKey(
      next,
      "on_exhausted",
      rewriteDestination(
        step.on_exhausted,
        `${location}.on_exhausted`,
        context,
        "is where this step goes when its visit bound runs out",
      ),
    );
  }
  if (step.prompt !== undefined) {
    next = withKey(next, "prompt", rewriteText(step.prompt, `${location}.prompt`, context));
  }
  if (step.message !== undefined) {
    next = withKey(next, "message", rewriteText(step.message, `${location}.message`, context));
  }
  if (step.when !== undefined) {
    next = withKey(next, "when", rewritePredicate(step.when, `${location}.when`, context));
  }
  if (step.cases !== undefined) {
    next = withKey(
      next,
      "cases",
      asArray(step.cases).map((entry, caseIndex) => {
        const object = asObject(entry);
        if (object === null) return entry;
        const caseLocation = `${location}.cases[${caseIndex}]`;
        return withKey(
          withKey(object, "when", rewritePredicate(object.when, `${caseLocation}.when`, context)),
          "next",
          rewriteDestination(object.next, `${caseLocation}.next`, context, "is a route of this decision"),
        );
      }),
    );
  }
  if (step.otherwise !== undefined) {
    next = withKey(
      next,
      "otherwise",
      rewriteDestination(
        step.otherwise,
        `${location}.otherwise`,
        context,
        "is this decision's fallback route",
      ),
    );
  }
  if (step.choices !== undefined) {
    next = withKey(
      next,
      "choices",
      asArray(step.choices).map((entry, choiceIndex) => {
        const object = asObject(entry);
        if (object === null) return entry;
        const choiceLocation = `${location}.choices[${choiceIndex}]`;
        let choice = withKey(
          object,
          "next",
          rewriteDestination(
            object.next,
            `${choiceLocation}.next`,
            context,
            "is where an answer to this gate goes",
          ),
        );
        // An approving answer names the exact actions it permits. Renaming a
        // delivery step has to move the *grant* with it, or the answer would
        // authorize a step that no longer exists.
        const grant = asObject(choice.authorize);
        if (grant !== null) {
          choice = withKey(
            choice,
            "authorize",
            withKey(
              grant,
              "steps",
              asArray(grant.steps).map((name, grantIndex) =>
                rewriteName(
                  name,
                  "step",
                  context.rewrite.step,
                  `${choiceLocation}.authorize.steps[${grantIndex}]`,
                  "is an action this answer authorizes",
                  context,
                ),
              ),
            ),
          );
        }
        return choice;
      }),
    );
  }
  // A gate's delivery binding: the review its grant rests on, and the
  // publication text it suggests. Both are references into this same card.
  const delivery = asObject(step.delivery);
  if (delivery !== null) {
    let bound = withKey(
      delivery,
      "review",
      rewriteName(
        delivery.review,
        "evidence",
        context.rewrite.evidenceAlias,
        `${location}.delivery.review`,
        "is the review this approval is about",
        context,
      ),
    );
    const metadata = asObject(delivery.metadata);
    if (metadata !== null) {
      let rewritten = metadata;
      for (const field of DELIVERY_METADATA_FIELDS) {
        if (metadata[field] === undefined) continue;
        rewritten = withKey(
          rewritten,
          field,
          rewriteText(metadata[field], `${location}.delivery.metadata.${field}`, context),
        );
      }
      bound = withKey(bound, "metadata", rewritten);
    }
    next = withKey(next, "delivery", bound);
  }
  const result = asObject(step.result);
  if (result !== null) {
    next = withKey(
      next,
      "result",
      withKey(
        result,
        "evidence",
        rewriteName(
          result.evidence,
          "evidence",
          context.rewrite.evidenceAlias,
          `${location}.result.evidence`,
          "is the retained result this gate asks about",
          context,
        ),
      ),
    );
  }
  if (step.producer !== undefined) {
    next = withKey(
      next,
      "producer",
      rewriteName(
        step.producer,
        "evidence",
        context.rewrite.evidenceAlias,
        `${location}.producer`,
        "is the evidence that produced this capture",
        context,
      ),
    );
  }
  // A delivery step's own references: the gate that can authorize it, the
  // action it consumes, and where the chain goes next.
  if (step.approval !== undefined) {
    next = withKey(
      next,
      "approval",
      rewriteName(
        step.approval,
        "step",
        context.rewrite.step,
        `${location}.approval`,
        "is the approval that can authorize this action",
        context,
      ),
    );
  }
  if (step.previous !== undefined) {
    next = withKey(
      next,
      "previous",
      rewriteName(
        step.previous,
        "step",
        context.rewrite.step,
        `${location}.previous`,
        "is the action whose result this one consumes",
        context,
      ),
    );
  }
  if (step.next !== undefined) {
    next = withKey(
      next,
      "next",
      rewriteDestination(
        step.next,
        `${location}.next`,
        context,
        "is where this action goes once it is on record",
      ),
    );
  }
  return next;
}

function rewriteDocument(
  document: DraftObject,
  rewrite: Rewrite,
  visit: Visit = () => {},
): DraftObject {
  let next = document;
  if (rewrite.session !== undefined) {
    next = withKey(
      next,
      "sessions",
      asArray(document.sessions).map((name) => {
        const text = asString(name);
        return text === null ? name : rewrite.session!(text);
      }),
    );
    const primary = asString(document.primary);
    if (primary !== null) next = withKey(next, "primary", rewrite.session(primary));
  }
  return withSteps(
    next,
    asArray(next.steps).map((step, index) =>
      isDraftObject(step) ? rewriteStep(step, index, rewrite, visit) : step,
    ),
  );
}

/** Every structural reference to a name, with where it is and what it does.
 *
 * This is what a delete confirmation lists. After a confirmed removal these
 * stay in the document as visible dangling references rather than being
 * repaired into something the author never asked for. */
export function referencesTo(
  document: DraftObject,
  kind: Reference["kind"],
  name: string,
): Reference[] {
  const found: Reference[] = [];
  rewriteDocument(document, {}, (reference) => {
    if (reference.kind === kind && reference.name === name) found.push(reference);
  });
  return found;
}

/** Rename a step everywhere the grammar puts a step name.
 *
 * Refused when the old name is ambiguous: two cards sharing a name is a state
 * an invalid draft can reach, and renaming "the" step would silently rewrite
 * references belonging to the other one. */
export function renameStep(
  document: DraftObject,
  from: string,
  to: string,
): { document: DraftObject; error: string | null } {
  const occurrences = stepNames(document).filter((name) => name === from).length;
  if (occurrences > 1) {
    return {
      document,
      error: `Two steps are called “${from}”. Give them distinct names before renaming, so the references can be moved to the right one.`,
    };
  }
  const renamed = rewriteDocument(document, { step: (name) => (name === from ? to : name) });
  return {
    document: withSteps(
      renamed,
      asArray(renamed.steps).map((step) => {
        const object = asObject(step);
        if (object === null || asString(object.name) !== from) return step;
        return withKey(object, "name", to);
      }),
    ),
    error: null,
  };
}

/** Rename an agent: its declaration, the primary marker, and every step
 * assigned to it. */
export function renameSession(
  document: DraftObject,
  from: string,
  to: string,
): { document: DraftObject; error: string | null } {
  const occurrences = sessionNames(document).filter((name) => name === from).length;
  if (occurrences > 1) {
    return {
      document,
      error: `Two agents are called “${from}”. Give them distinct names before renaming.`,
    };
  }
  return {
    document: rewriteDocument(document, { session: (name) => (name === from ? to : name) }),
    error: null,
  };
}

/** Rename one step's evidence alias, and the `{op: evidence}` reads inside
 * that same step. An alias is step-local, so nothing outside the card moves. */
export function renameEvidenceAlias(
  document: DraftObject,
  stepIndex: number,
  from: string,
  to: string,
): DraftObject {
  return updateStep(document, stepIndex, (step) =>
    rewriteStep(step, stepIndex, { evidenceAlias: (name) => (name === from ? to : name) }, () => {}),
  );
}
