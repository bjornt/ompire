import type { ReactNode } from "react";

/** A read-only reading of a workflow definition, step by step.
 *
 * The YAML remains the definition — this is a second *presentation* of the
 * same document, never a second place to edit one, and never a claim that the
 * run will take any particular route. It reads the canonical document rather
 * than the descriptor because a descriptor names the steps and this has to
 * show what each one actually says: its instruction, the results it must
 * declare, where each route goes, the answers a human is offered, and what
 * happens when a visit bound runs out.
 *
 * Everything it cannot express faithfully it renders as a labelled
 * placeholder rather than prose that might read as something the author did
 * not write.
 */

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** One value expression, as a short readable token. */
function describeValue(value: unknown): string {
  if (!isObject(value)) return String(value);
  switch (value.op) {
    case "literal":
      return JSON.stringify(value.value);
    case "input":
      return `${String(value.name)}`;
    case "evidence":
      return `evidence ${String(value.name)}`;
    case "latest":
      return `latest of ${asArray(value.steps).join(", ")}`;
    case "count":
      return `visits to ${String(value.step)}`;
    case "get":
      return `${describeValue(value.value)}.${asArray(value.keys).join(".")}`;
    case "coalesce":
      return asArray(value.values).map(describeValue).join(" or ");
    default:
      return String(value.op ?? "?");
  }
}

const COMPARISONS: Record<string, string> = {
  eq: "is",
  ne: "is not",
  lt: "<",
  lte: "≤",
  gt: ">",
  gte: "≥",
};

function describePredicate(predicate: unknown): string {
  if (typeof predicate === "boolean") return predicate ? "always" : "never";
  if (!isObject(predicate)) return "?";
  const op = String(predicate.op);
  if (op in COMPARISONS) {
    return `${describeValue(predicate.left)} ${COMPARISONS[op]} ${describeValue(predicate.right)}`;
  }
  if (op === "exists") return `${describeValue(predicate.value)} exists`;
  if (op === "is_type") return `${describeValue(predicate.value)} is ${String(predicate.type)}`;
  if (op === "not") return `not (${describePredicate(predicate.of)})`;
  const joined = asArray(predicate.of).map(describePredicate);
  return joined.length === 0 ? op : joined.join(op === "all" ? " and " : " or ");
}

/** Where a route goes. A destination is never summarized away: "ends" without
 * saying *which* ending is exactly the silence format 2 removed. */
function describeDestination(destination: unknown): string {
  if (!isObject(destination)) return "?";
  if ("step" in destination) return `go to ${String(destination.step)}`;
  if ("pause" in destination) return "pause and wait for you";
  const result = destination.result;
  return typeof result === "string" ? `end · ${result}` : "end the run";
}

/** A text document as the instruction it renders to, with each interpolated
 * value shown as the expression it is rather than as a guessed value. */
function describeText(document: unknown): string {
  if (!isObject(document)) return "";
  const separator = typeof document.separator === "string" ? document.separator : "";
  const parts = asArray(document.parts).map((part) => {
    if (!isObject(part)) return "";
    if ("text" in part) return String(part.text);
    if ("value" in part) return `⟨${describeValue(part.value)}⟩`;
    const branch = describeText(part.then);
    const otherwise = describeText(part.else);
    return `⟨if ${describePredicate(part.if)}: ${branch}${otherwise ? ` — otherwise: ${otherwise}` : ""}⟩`;
  });
  return parts.filter((part) => part !== "").join(separator);
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="outlineRow">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function StepCard({ step, format }: { step: Json; format: number }) {
  const kind = String(step.kind);
  const evidence = isObject(step.evidence) ? step.evidence : {};
  const outcome = isObject(step.outcome) ? step.outcome : null;
  const results = outcome !== null && isObject(outcome.results) ? outcome.results : null;
  return (
    <li className="outlineStep" data-testid={`outline-step-${String(step.name)}`}>
      <div className="outlineHead">
        <span className="outlineName">{String(step.name)}</span>
        <span className="outlineKind">{kind}</span>
        {typeof step.session === "string" && (
          <span className="outlineChip">session {step.session}</span>
        )}
        {typeof step.role === "string" && (
          <span className="outlineChip">role {step.role}</span>
        )}
      </div>
      <dl className="outlineBody">
        {kind === "agent" && (
          <Row label="Instruction">
            <pre className="outlineText">{describeText(step.prompt)}</pre>
          </Row>
        )}
        {kind === "agent" && step.when !== true && step.when !== undefined && (
          <Row label="Runs when">{describePredicate(step.when)}</Row>
        )}
        {kind === "agent" && format >= 2 && (
          <Row label="Must declare">
            {results === null ? (
              <span className="outlineMuted">no result document is asked for</span>
            ) : (
              <ul className="outlineList">
                {Object.entries(results).map(([name, contract]) => {
                  const required =
                    isObject(contract) && isObject(contract.required) ? contract.required : {};
                  const fields = Object.entries(required);
                  return (
                    <li key={name}>
                      <code className="mono">{name}</code>
                      {fields.length === 0
                        ? " — no required fields"
                        : ` — ${fields.map(([f, t]) => `${f}: ${String(t)}`).join(", ")}`}
                    </li>
                  );
                })}
              </ul>
            )}
          </Row>
        )}
        {kind === "agent" && format === 1 && step.expects_outcome === true && (
          <Row label="Must declare">an outcome document</Row>
        )}
        {kind === "command" && (
          <Row label="Runs">
            <code className="mono">{asArray(step.argv).join(" ")}</code>
            {typeof step.timeout === "number" ? ` · ${step.timeout}s timeout` : ""}
          </Row>
        )}
        {kind === "decision" && (
          <Row label="Routes">
            <ul className="outlineList">
              {asArray(step.cases).map((entry, index) => (
                <li key={index}>
                  {isObject(entry)
                    ? `${describePredicate(entry.when)} → ${describeDestination(entry.next)}`
                    : "?"}
                </li>
              ))}
              <li>otherwise → {describeDestination(step.otherwise)}</li>
            </ul>
          </Row>
        )}
        {kind === "gate" && (
          <Row label="Asks">
            <pre className="outlineText">{describeText(step.message)}</pre>
          </Row>
        )}
        {kind === "gate" && asArray(step.choices).length > 0 && (
          <Row label="Your answers">
            <ul className="outlineList">
              {asArray(step.choices).map((choice, index) => (
                <li key={index}>
                  {isObject(choice) ? (
                    <>
                      <code className="mono">{String(choice.id)}</code> — {String(choice.label)} →{" "}
                      {describeDestination(choice.next)}
                      {choice.feedback_required === true ? " · needs a note from you" : ""}
                    </>
                  ) : (
                    "?"
                  )}
                </li>
              ))}
            </ul>
          </Row>
        )}
        {Object.keys(evidence).length > 0 && (
          <Row label="Reads">
            <ul className="outlineList">
              {Object.entries(evidence).map(([alias, selector]) => {
                const from = isObject(selector) ? asArray(selector.steps).join(", ") : "";
                const optional = isObject(selector) && selector.required === false;
                return (
                  <li key={alias}>
                    <code className="mono">{alias}</code> — from {from}
                    {optional ? " · optional" : ""}
                  </li>
                );
              })}
            </ul>
          </Row>
        )}
        {typeof step.max_visits === "number" && (
          <Row label="Visit bound">
            at most {step.max_visits} · then {describeDestination(step.on_exhausted)}
          </Row>
        )}
      </dl>
    </li>
  );
}

export function WorkflowOutline({ definition }: { definition: Record<string, unknown> }) {
  const format = typeof definition.format === "number" ? definition.format : 1;
  const steps = asArray(definition.steps).filter(isObject);
  const sessions = asArray(definition.sessions).map(String);
  return (
    <div className="outline" data-testid="workflow-outline">
      <p className="hint">
        Format {format} · sessions {sessions.join(", ")} · primary{" "}
        <code className="mono">{String(definition.primary)}</code>
      </p>
      <p className="hint">
        A reading of the saved definition, not a prediction: which steps run, and where
        a run goes, is decided while it runs.
      </p>
      <ol className="outlineSteps">
        {steps.map((step) => (
          <StepCard key={String(step.name)} step={step} format={format} />
        ))}
      </ol>
    </div>
  );
}
