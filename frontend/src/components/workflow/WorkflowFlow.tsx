import { useCallback, useId, useMemo, useRef, type ReactNode } from "react";
import {
  asArray,
  asObject,
  asString,
  documentFormat,
  type DraftObject,
  type DraftValue,
} from "../../lib/workflowDocument";
import { numberToken } from "../../lib/losslessJson";
import {
  declaredActions,
  describePredicate,
  describeTarget,
  edgesFrom,
  readDeliveryAction,
  readDeliveryGate,
  readEvidence,
  readFlow,
  readResults,
  readText,
  unknownDocumentFields,
  unknownStepFields,
  type FlowEdge,
  type TextPiece,
} from "../../lib/workflowFlow";
import "./workflow.css";

/** One presentation of a whole definition, shared by every reader.
 *
 * The library, the launch preview, and a task's pinned procedure all answer
 * the same question — what does this workflow say — so they read it the same
 * way rather than each summarizing it differently. Task detail adds an
 * execution overlay on top; nothing here knows about attempts.
 *
 * Two rules keep it honest. It shows *declarations*, never predictions: no
 * predicate is evaluated in the browser, so an edge means "this route exists",
 * never "this route will be taken". And it never summarizes a field away —
 * an evidence selector's freshness anchor, a text part's format, an empty
 * part, and the exact boundaries between command arguments all change what
 * runs, and a reading that drops them describes an easier workflow than the
 * one that will execute.
 */

const EDGE_WORDS: Record<FlowEdge["kind"], string> = {
  "fall-through": "then",
  case: "branch",
  otherwise: "fallback",
  choice: "your answer",
  // The one edge that confers authority, named as such: reading it as an
  // ordinary answer is how a diagram hides what a click would permit.
  authorize: "you authorize",
  delivered: "once published",
  captured: "files retained",
  exhausted: "bound reached",
  skip: "skipped",
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flowRow">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** A text document, part by part.
 *
 * Deliberately not joined into one string: an empty part and the separator
 * between parts are both invisible in a joined rendering, and both change the
 * instruction an agent is given. */
export function TextReadingView({ document }: { document: DraftValue | undefined }) {
  const reading = readText(document);
  if (reading.pieces.length === 0) {
    return <p className="flowMuted">No text — this renders to nothing.</p>;
  }
  return (
    <div className="flowText">
      {reading.separator !== "" && (
        <p className="flowMuted">
          Parts are joined with {JSON.stringify(reading.separator)}.
        </p>
      )}
      <ol className="flowParts">
        {reading.pieces.map((piece, index) => (
          <li key={index}>
            <TextPieceView piece={piece} />
          </li>
        ))}
      </ol>
    </div>
  );
}

function TextPieceView({ piece }: { piece: TextPiece }) {
  if (piece.kind === "text") {
    return piece.text === "" ? (
      <span className="flowMuted">an empty text part</span>
    ) : (
      <pre className="flowProse">{piece.text}</pre>
    );
  }
  if (piece.kind === "value") {
    return (
      <span className="flowValue">
        inserts {piece.description}
        <span className="flowChip">as {piece.format}</span>
      </span>
    );
  }
  if (piece.kind === "unsupported") {
    return <span className="flowBroken">unsupported text part — {piece.description}</span>;
  }
  return (
    <div className="flowConditional">
      <span className="flowValue">if {piece.condition}</span>
      <ol className="flowParts">
        {piece.then.map((entry, index) => (
          <li key={`t${index}`}>
            <TextPieceView piece={entry} />
          </li>
        ))}
      </ol>
      {piece.otherwise.length > 0 && (
        <>
          <span className="flowValue">otherwise</span>
          <ol className="flowParts">
            {piece.otherwise.map((entry, index) => (
              <li key={`e${index}`}>
                <TextPieceView piece={entry} />
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}

function EdgeList({
  edges,
  onGoTo,
}: {
  edges: FlowEdge[];
  onGoTo: (index: number) => void;
}) {
  if (edges.length === 0) {
    return <p className="flowMuted">No route is declared from here.</p>;
  }
  return (
    <ul className="flowEdges">
      {edges.map((edge, index) => (
        <li key={index} data-edge-kind={edge.kind}>
          <span className="flowChip">{EDGE_WORDS[edge.kind]}</span>
          <span className="flowEdgeLabel">{edge.label}</span>
          <span aria-hidden="true"> → </span>
          {edge.to.kind === "step" && edge.to.index !== null ? (
            <button
              type="button"
              className="linkButton"
              onClick={() => onGoTo(edge.to.kind === "step" ? (edge.to.index ?? 0) : 0)}
            >
              {edge.to.step}
            </button>
          ) : (
            <span className={edge.to.kind === "step" ? "flowBroken" : "flowTarget"}>
              {describeTarget(edge.to)}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

/** The overview. A second view of the same edges, never the only one.
 *
 * Geometry comes from card order alone: no stored coordinates, no graph
 * library, and no generated diagram source built out of untrusted labels —
 * every label is React text, which is escaped. A broken destination is drawn
 * as a broken destination rather than omitted, because an edge that vanishes
 * is how a diagram makes an invalid flow look finished. */
function FlowDiagram({
  names,
  edges,
  onGoTo,
  current,
}: {
  names: (string | null)[];
  edges: FlowEdge[];
  onGoTo: (index: number) => void;
  current: number | null;
}) {
  const rowHeight = 46;
  const width = 260;
  const height = Math.max(names.length, 1) * rowHeight + 16;
  const paths = edges
    .map((edge) => {
      if (edge.to.kind !== "step" || edge.to.index === null) return null;
      const from = edge.fromIndex * rowHeight + rowHeight / 2 + 8;
      const to = edge.to.index * rowHeight + rowHeight / 2 + 8;
      const backwards = edge.to.index <= edge.fromIndex;
      const gutter = backwards ? 12 : width - 12;
      const bend = backwards ? 4 : width - 4;
      return { from, to, gutter, bend, kind: edge.kind, backwards };
    })
    .filter((path): path is NonNullable<typeof path> => path !== null);

  return (
    <svg
      className="flowDiagram"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label={`Overview of ${names.length} steps and their declared routes. The list below is the same information.`}
    >
      {paths.map((path, index) => (
        <path
          key={index}
          className={`flowEdgePath kind-${path.kind}`}
          d={`M ${path.gutter} ${path.from} C ${path.bend} ${path.from}, ${path.bend} ${path.to}, ${path.gutter} ${path.to}`}
          fill="none"
        />
      ))}
      {names.map((name, index) => (
        <g
          key={index}
          className={`flowNode${current === index ? " current" : ""}`}
          transform={`translate(24 ${index * rowHeight + 8})`}
          onClick={() => onGoTo(index)}
        >
          <rect width={width - 48} height={rowHeight - 12} rx="6" />
          <text x="10" y="21">
            {name ?? `step ${index + 1}`}
          </text>
        </g>
      ))}
    </svg>
  );
}

function CommandArguments({ argv }: { argv: DraftValue | undefined }) {
  const args = asArray(argv);
  if (args.length === 0) return <span className="flowBroken">no command declared</span>;
  return (
    <ol className="flowArgv">
      {args.map((argument, index) => (
        <li key={index}>
          <code className="mono">{asString(argument) ?? "not a literal string"}</code>
        </li>
      ))}
    </ol>
  );
}

export interface FlowStepExtras {
  /** Rendered inside a card, under its declaration — the execution overlay. */
  body?: ReactNode;
  /** Rendered in the card header. */
  badge?: ReactNode;
  className?: string;
}

export function WorkflowFlow({
  definition,
  extras,
  current,
  testId = "workflow-flow",
}: {
  definition: DraftObject;
  /** Per-step additions, by declared index. Task detail overlays attempts. */
  extras?: (index: number, name: string | null, step: DraftObject) => FlowStepExtras;
  /** The step the run is on, for the overview. */
  current?: string | null;
  testId?: string;
}) {
  const flow = useMemo(() => readFlow(definition), [definition]);
  const format = documentFormat(definition);
  const domId = useId();
  const cards = useRef<Map<number, HTMLLIElement>>(new Map());

  const goTo = useCallback((index: number) => {
    const card = cards.current.get(index);
    if (card === undefined) return;
    card.scrollIntoView?.({ block: "nearest" });
    card.focus();
  }, []);

  const currentIndex =
    current == null ? null : flow.steps.findIndex((step) => step.name === current);
  const unknownTop = unknownDocumentFields(definition);
  const actions = declaredActions(definition);

  return (
    <div className="flow" data-testid={testId}>
      <p className="hint">
        Format {format ?? "not declared"} · agents{" "}
        {asArray(definition.sessions)
          .map((name) => asString(name) ?? "?")
          .join(", ") || "none declared"}{" "}
        · primary <code className="mono">{asString(definition.primary) ?? "not set"}</code>
      </p>
      <p className="hint">
        What this procedure declares may happen. Which route a run takes is
        decided while it runs, from evidence this page has never seen.
      </p>
      <p className="hint" data-testid="flow-declared-actions">
        {actions.length === 0
          ? "This workflow publishes nothing. It has no step that can sign, push, or open a pull request, so no answer anywhere in it can authorize one."
          : `It can publish: ${actions.join(", ")} — each only if you answer the approval that grants it.`}
      </p>
      {unknownTop.length > 0 && (
        <p className="flowBroken" data-testid="flow-unknown-fields">
          This document carries fields no supported format defines:{" "}
          {unknownTop.join(", ")}. They are kept as written and can only be
          removed in YAML.
        </p>
      )}
      <div className="flowLayout">
        <div className="flowOverview">
          <FlowDiagram
            names={flow.steps.map((step) => step.name)}
            edges={flow.edges}
            onGoTo={goTo}
            current={currentIndex === -1 ? null : currentIndex}
          />
        </div>
        <ol className="flowCards">
          {flow.steps.map(({ index, name, kind, step }) => {
            const extra = extras?.(index, name, step) ?? {};
            const evidence = readEvidence(step);
            const results = readResults(step);
            const unknown = unknownStepFields(step);
            const deliveryGate = readDeliveryGate(step);
            const deliveryAction = readDeliveryAction(definition, step);
            const bound = numberToken(step.max_visits);
            return (
              <li
                key={index}
                id={`${domId}-step-${index}`}
                className={`flowCard${extra.className === undefined ? "" : ` ${extra.className}`}`}
                tabIndex={-1}
                ref={(node) => {
                  if (node === null) cards.current.delete(index);
                  else cards.current.set(index, node);
                }}
                data-testid={`flow-step-${name ?? index}`}
              >
                <div className="flowHead">
                  <span className="flowIndex">{index + 1}</span>
                  <span className="flowName">
                    {name ?? <span className="flowBroken">this step has no name</span>}
                  </span>
                  <span className="flowChip">{kind ?? "no kind"}</span>
                  {typeof step.session === "string" && (
                    <span className="flowChip">agent {step.session}</span>
                  )}
                  {typeof step.role === "string" && (
                    <span className="flowChip">role {step.role}</span>
                  )}
                  {extra.badge}
                </div>
                <dl className="flowBody">
                  {kind === "agent" && (
                    <Row label="Instruction">
                      <TextReadingView document={step.prompt} />
                    </Row>
                  )}
                  {kind === "gate" && (
                    <Row label="Asks">
                      <TextReadingView document={step.message} />
                    </Row>
                  )}
                  {kind === "agent" && step.when !== undefined && step.when !== true && (
                    <Row label="Runs only when">{describePredicate(step.when)}</Row>
                  )}
                  {kind === "agent" && format !== 1 && (
                    <Row label="Must declare">
                      {results === null ? (
                        <span className="flowMuted">
                          no result document is asked for
                        </span>
                      ) : results.length === 0 ? (
                        <span className="flowBroken">
                          an outcome with no declared results
                        </span>
                      ) : (
                        <ul className="flowList">
                          {results.map((result) => (
                            <li key={result.name}>
                              <code className="mono">{result.name}</code>
                              {result.required.length === 0
                                ? " — no required fields"
                                : ` — ${result.required
                                    .map((field) => `${field.field}: ${field.type}`)
                                    .join(", ")}`}
                            </li>
                          ))}
                        </ul>
                      )}
                    </Row>
                  )}
                  {kind === "agent" && format === 1 && (
                    <Row label="Must declare">
                      {step.expects_outcome === true
                        ? "an outcome document"
                        : "nothing — this step is not asked for a result"}
                    </Row>
                  )}
                  {kind === "command" && (
                    <>
                      <Row label="Runs">
                        <CommandArguments argv={step.argv} />
                      </Row>
                      <Row label="Limits">
                        {numberToken(step.timeout) ?? "600"}s timeout ·{" "}
                        {step.idempotent === true
                          ? "declared safe to re-run after a restart"
                          : "no idempotence declaration — this cannot be saved"}
                      </Row>
                    </>
                  )}
                  {kind === "capture" && (
                    <>
                      <Row label="Retains">
                        {asArray(step.paths).length === 0
                          ? "no paths declared"
                          : `${asArray(step.paths).length} declared path${asArray(step.paths).length === 1 ? "" : "s"} under ${asArray(step.allowlist)
                              .map((root) => asString(root) ?? "?")
                              .join(", ") || "no allowlist"}`}
                      </Row>
                      <Row label="Produced by">
                        <code className="mono">{asString(step.producer) ?? "no evidence alias"}</code>
                      </Row>
                    </>
                  )}
                  {evidence.length > 0 && (
                    <Row label="Reads">
                      <ul className="flowList">
                        {evidence.map((selector) => (
                          <li key={selector.alias}>
                            <code className="mono">{selector.alias}</code> — the latest
                            attempt of {selector.steps.join(" or ")}
                            {selector.withOutcome
                              ? " that recorded an outcome"
                              : ", with or without an outcome"}
                            {selector.after === null
                              ? ""
                              : `, no older than ${selector.after}`}
                            {selector.required ? "" : " · optional"}
                          </li>
                        ))}
                      </ul>
                    </Row>
                  )}
                  {kind === "gate" && format !== 1 && (
                    <Row label="Your answers">
                      <ul className="flowList">
                        {asArray(step.choices).map((choice, choiceIndex) => {
                          const object = asObject(choice);
                          const grant = asArray(asObject(object?.authorize)?.steps)
                            .map((name) => asString(name) ?? "?")
                            .join(" → ");
                          return (
                            <li key={choiceIndex}>
                              <code className="mono">
                                {asString(object?.id) ?? "no id"}
                              </code>{" "}
                              — {asString(object?.label) ?? "no label"}
                              {object?.feedback_required === true
                                ? " · needs a reason from you"
                                : ""}
                              {object?.requires_result_acceptance === true
                                ? " · requires accepted result"
                                : ""}
                              {grant === "" ? (
                                ""
                              ) : (
                                <> · authorizes {grant}</>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    </Row>
                  )}
                  {deliveryGate !== null && (
                    <Row label="This approval is about">
                      the review bound to{" "}
                      <code className="mono">{deliveryGate.review ?? "nothing"}</code>
                      {deliveryGate.metadata.length === 0
                        ? " · it suggests no publication text"
                        : ` · it suggests ${deliveryGate.metadata
                            .map((entry) => entry.field)
                            .join(", ")}`}
                    </Row>
                  )}
                  {kind === "gate" && asObject(step.result) !== null && (
                    <Row label="Retained result">
                      This gate names the capture bound to{" "}
                      <code className="mono">{asString(asObject(step.result)?.evidence) ?? "no evidence alias"}</code>.
                      A choice marked “requires accepted result” checks that exact
                      readable revision; accepting a different result does not satisfy it.
                    </Row>
                  )}
                  {deliveryAction !== null && (
                    <>
                      <Row label="Publishes">
                        <strong>{deliveryAction.action ?? "no action"}</strong>
                        {deliveryAction.mode === null
                          ? ""
                          : ` · ${deliveryAction.mode}`}
                        {deliveryAction.previous === null
                          ? ""
                          : ` · consumes ${deliveryAction.previous}`}
                      </Row>
                      <Row label="Only if you answer">
                        <code className="mono">
                          {deliveryAction.approval ?? "no approval"}
                        </code>{" "}
                        with the choice that grants it
                      </Row>
                    </>
                  )}
                  {bound !== null && (
                    <Row label="Visit bound">
                      at most {bound} visit{bound === "1" ? "" : "s"}
                    </Row>
                  )}
                  <Row label="What happens next">
                    <EdgeList edges={edgesFrom(flow, index)} onGoTo={goTo} />
                  </Row>
                  {unknown.length > 0 && (
                    <Row label="Not understood">
                      <span className="flowBroken">
                        {unknown.join(", ")} — kept as written, editable only in YAML.
                      </span>
                    </Row>
                  )}
                </dl>
                {extra.body}
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
