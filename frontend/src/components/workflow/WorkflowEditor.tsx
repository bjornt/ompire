import { useCallback, useMemo, useRef, useState } from "react";
import {
  DELIVERY_ACTIONS,
  DELIVERY_METADATA_FIELDS,
  DELIVERY_MODES,
  MODEL_ROLES,
  REQUIRED_TYPES,
  STEP_KINDS,
  asArray,
  asObject,
  asString,
  documentFormat,
  evidenceAliases,
  fallThrough,
  insertStep,
  moveStep,
  referencesTo,
  removeStep,
  renameEvidenceAlias,
  renameSession,
  renameStep,
  sessionNames,
  stepNames,
  stepObjects,
  updateStep,
  withKey,
  type DraftObject,
  type DraftValue,
  type Reference,
  type StepKindName,
} from "../../lib/workflowDocument";
import { isBounded, unknownStepFields } from "../../lib/workflowFlow";
import {
  CheckField,
  DestinationField,
  NumberField,
  PredicateField,
  SelectField,
  StepListField,
  TextDocumentField,
  TextField,
  type GrammarContext,
} from "./expressions";
import { FieldProblem, ProblemProvider } from "./problem";
import { useProblemWithin, useRowKeys } from "./problemContext";
import type { WorkflowDraftValidation } from "../../types";
import "./workflow.css";

/** The visual editor: the whole supported vocabulary, as forms.
 *
 * Three things it deliberately is not. It is not a second validator — the
 * daemon decides what is executable, and this shows that answer at the field
 * it is about. It is not a text tool — a rename walks the document's
 * structure, so an instruction that happens to contain the old name is left
 * alone. And it is not a place where an unfinished flow can hide: a route
 * with no destination, a reference to a step that was deleted, and a field
 * this editor does not understand all stay visible and stay unlaunchable.
 */

function blankStep(kind: StepKindName, format: number | null, session: string): DraftObject {
  const name = `${kind}-step`;
  if (kind === "agent") {
    return {
      name,
      kind,
      session,
      ...(format === 1 ? {} : { outcome: null }),
      prompt: { parts: [{ text: "" }] },
    };
  }
  if (kind === "command") {
    return { name, kind, argv: [""], idempotent: true };
  }
  if (kind === "decision") {
    return {
      name,
      kind,
      cases: [{ when: true, next: { step: "" } }],
      otherwise: format === 1 ? { complete: true } : { complete: true, result: "" },
    };
  }
  if (kind === "review") {
    return { name, kind };
  }
  if (kind === "delivery") {
    // A commit, because a chain always starts at one: no action performs a
    // missing predecessor, so a fresh card cannot usefully be a push.
    return { name, kind, action: "commit", mode: "squash", approval: "", next: { step: "" } };
  }
  return {
    name,
    kind,
    message: { parts: [{ text: "" }] },
    ...(format === 1 ? {} : { choices: [{ id: "continue", label: "Continue", next: { step: "" } }] }),
  };
}

/** The step index a located refusal belongs to, so a summary can jump to it. */
function problemStepIndex(location: string | null): number | null {
  if (location === null) return null;
  const match = /^steps\[(\d+)\]/.exec(location);
  return match === null ? null : Number(match[1]);
}

export function WorkflowEditor({
  document,
  onChange,
  validation,
  readOnly = false,
  stale = false,
}: {
  document: DraftObject;
  onChange: (update: (document: DraftObject) => DraftObject) => void;
  /** The daemon's reading of this exact draft, when it has answered. */
  validation: WorkflowDraftValidation | null;
  readOnly?: boolean;
  /** True while the answer describes an older edit than the one on screen. */
  stale?: boolean;
}) {
  const format = documentFormat(document);
  const steps = stepObjects(document);
  const names = stepNames(document);
  const sessions = sessionNames(document);
  const keys = useRowKeys();
  const [open, setOpen] = useState<Set<number>>(() => new Set([0]));
  const [renameError, setRenameError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const cardRefs = useRef<Map<number, HTMLDetailsElement>>(new Map());

  const accepted = validation !== null && validation.ok === true ? validation : null;
  const refusal = validation !== null && validation.ok === false ? validation : null;
  const problemLocation = refusal?.location ?? null;
  const problemIndex = problemStepIndex(problemLocation);

  const reveal = useCallback((index: number) => {
    setOpen((current) => new Set(current).add(index));
    // The card has to exist before it can be focused; a collapsed card is
    // expanded first, which is the whole point of the summary link.
    requestAnimationFrame(() => {
      const card = cardRefs.current.get(index);
      card?.scrollIntoView?.({ block: "nearest" });
      card?.querySelector("summary")?.focus();
    });
  }, []);

  const grammarFor = (index: number): GrammarContext => ({
    format,
    steps: names,
    aliases: evidenceAliases(steps[index] ?? {}),
  });

  const editStep = (index: number, update: (step: DraftObject) => DraftObject) =>
    onChange((current) => updateStep(current, index, update));

  return (
    <ProblemProvider location={problemLocation} message={refusal?.message ?? ""}>
      <div className="workflowEditor" data-testid="workflow-visual-editor">
        <p aria-live="polite" className="hint" data-testid="editor-announcement">
          {announcement}
        </p>

        {validation !== null && (
          <div
            className={refusal === null ? "checkOk" : "checkFailed"}
            data-testid="editor-validation"
          >
            {accepted !== null ? (
              <p>
                This draft is a valid format-{accepted.format} definition. Saving an
                executable revision checks it again before retaining anything.
              </p>
            ) : refusal === null ? null : (
              <p>
                {refusal.location !== null && <code className="mono">{refusal.location}</code>}{" "}
                {refusal.message}{" "}
                {problemIndex !== null && problemIndex < steps.length && (
                  <button
                    type="button"
                    className="linkButton"
                    data-testid="editor-goto-problem"
                    onClick={() => reveal(problemIndex)}
                  >
                    Go to step {problemIndex + 1}
                  </button>
                )}
              </p>
            )}
            {stale && (
              <p className="hint editorStale" data-testid="editor-validation-stale">
                You have edited since this check. It describes the previous version
                of this draft.
              </p>
            )}
          </div>
        )}

        <fieldset className="editorSection" data-testid="editor-identity">
          <legend>This workflow</legend>
          <p className="hint">
            Name <code className="mono">{asString(document.name) ?? "not set"}</code> ·
            format {format ?? "not declared"}.
          </p>
          <p className="hint">
            Neither is editable here. A name is the library entry's identity —
            renaming means creating a separate workflow — and a format is the
            rules this document is read under, which is why an old one keeps
            being read the way it was written rather than being upgraded.
          </p>
        </fieldset>

        {/* One disabled fieldset rather than a `disabled` prop threaded through
            every recursive control. A built-in or archived entry is read-only,
            and HTML already disables every descendant control of a disabled
            fieldset — including the ones nested four levels down inside a
            predicate, which is exactly where a threaded prop gets forgotten.
            The validation summary above stays outside it, so its jump link
            still works on an entry you are only reading. */}
        <fieldset className="editorEditable" disabled={readOnly}>
        <AgentsPanel
          document={document}
          onChange={onChange}
          readOnly={readOnly}
          onError={setRenameError}
        />
        {renameError !== null && (
          <p className="editorProblem" data-testid="editor-rename-error">
            {renameError}
          </p>
        )}

        <ol className="editorRows" data-testid="editor-steps">
          {steps.map((step, index) => {
            const name = asString(step.name);
            const kind = asString(step.kind) ?? "";
            const next = fallThrough(document, index);
            return (
              <li key={keys.at(index)}>
                <details
                  className="editorSection editorCard"
                  data-invalid={problemIndex === index ? "true" : undefined}
                  data-testid={`editor-step-${index}`}
                  open={open.has(index)}
                  ref={(node) => {
                    if (node === null) cardRefs.current.delete(index);
                    else cardRefs.current.set(index, node);
                  }}
                  onToggle={(event) =>
                    setOpen((current) => {
                      const updated = new Set(current);
                      if ((event.target as HTMLDetailsElement).open) updated.add(index);
                      else updated.delete(index);
                      return updated;
                    })
                  }
                >
                  <summary>
                    {index + 1}. {name ?? "unnamed step"} · {kind || "no kind"}
                    {problemIndex === index && " · has a problem"}
                  </summary>

                  <div className="editorActions">
                    <button
                      type="button"
                      className="ghostButton"
                      disabled={readOnly || index === 0}
                      data-testid={`editor-move-up-${index}`}
                      onClick={() => {
                        keys.moved(index, index - 1);
                        onChange((current) => moveStep(current, index, index - 1));
                        setOpen(new Set([index - 1]));
                        setAnnouncement(
                          `Moved ${name ?? "this step"} to position ${index}. When it finishes it now continues to ${fallThrough(moveStep(document, index, index - 1), index - 1) ?? "the end of the list"}.`,
                        );
                      }}
                    >
                      Move up
                    </button>
                    <button
                      type="button"
                      className="ghostButton"
                      disabled={readOnly || index === steps.length - 1}
                      data-testid={`editor-move-down-${index}`}
                      onClick={() => {
                        keys.moved(index, index + 1);
                        onChange((current) => moveStep(current, index, index + 1));
                        setOpen(new Set([index + 1]));
                        setAnnouncement(
                          `Moved ${name ?? "this step"} to position ${index + 2}. When it finishes it now continues to ${fallThrough(moveStep(document, index, index + 1), index + 1) ?? "the end of the list"}.`,
                        );
                      }}
                    >
                      Move down
                    </button>
                    <RemoveStepButton
                      document={document}
                      index={index}
                      name={name}
                      readOnly={readOnly}
                      onRemove={() => {
                        keys.removed(index);
                        onChange((current) => removeStep(current, index));
                        setAnnouncement(
                          `Removed ${name ?? "the step"}. Any route that named it now points at a step that does not exist.`,
                        );
                      }}
                    />
                  </div>

                  <StepNameField
                    name={name}
                    index={index}
                    readOnly={readOnly}
                    onRename={(to) => {
                      const result = renameStep(document, name ?? "", to);
                      setRenameError(result.error);
                      if (result.error === null) onChange(() => result.document);
                    }}
                  />

                  <SelectField
                    label="Kind"
                    value={kind}
                    options={STEP_KINDS.filter(
                      (option) =>
                        (option !== "review" && option !== "delivery") ||
                        (format ?? 0) >= 3,
                    ).map((option) => ({ value: option, label: option }))}
                    disabled={readOnly}
                    location={`steps[${index}].kind`}
                    testId={`editor-kind-${index}`}
                    onChange={(chosen) => {
                      // A kind change is a different card, not a relabelled
                      // one: its fields mean different things. The name and
                      // the bound are what carry over.
                      onChange((current) =>
                        updateStep(current, index, (existing) => ({
                          ...blankStep(chosen as StepKindName, format, sessions[0] ?? "main"),
                          name: existing.name,
                          ...(isBounded(existing)
                            ? { max_visits: existing.max_visits, on_exhausted: existing.on_exhausted }
                            : {}),
                        })),
                      );
                    }}
                  />

                  <p className="hint" data-testid={`editor-fallthrough-${index}`}>
                    {kind === "decision" || (kind === "gate" && format !== 1)
                      ? "This step routes explicitly; the order of the cards does not decide where it goes."
                      : next === null
                        ? "This is the last card. Finishing here falls off the end of the list."
                        : `When it finishes, the run continues to ${next}.`}
                  </p>

                  <StepBody
                    step={step}
                    index={index}
                    format={format}
                    sessions={sessions}
                    grammar={grammarFor(index)}
                    readOnly={readOnly}
                    onEdit={(update) => editStep(index, update)}
                    onDocument={onChange}
                  />

                  <BoundField
                    step={step}
                    index={index}
                    grammar={grammarFor(index)}
                    readOnly={readOnly}
                    onEdit={(update) => editStep(index, update)}
                  />

                  <EvidenceEditor
                    step={step}
                    index={index}
                    format={format}
                    names={names}
                    readOnly={readOnly}
                    onEdit={(update) => editStep(index, update)}
                    onDocument={onChange}
                  />

                  <UnknownFields step={step} />
                </details>
              </li>
            );
          })}
        </ol>

        <div className="editorActions">
          {STEP_KINDS.map((kind) => (
            <button
              key={kind}
              type="button"
              className="ghostButton"
              disabled={readOnly}
              data-testid={`editor-add-step-${kind}`}
              onClick={() => {
                keys.inserted(steps.length);
                onChange((current) =>
                  insertStep(
                    current,
                    asArray(current.steps).length,
                    blankStep(kind, format, sessionNames(current)[0] ?? "main"),
                  ),
                );
                setOpen(new Set([steps.length]));
                setAnnouncement(`Added a ${kind} step at position ${steps.length + 1}.`);
              }}
            >
              Add {kind} step
            </button>
          ))}
        </div>
        </fieldset>
      </div>
    </ProblemProvider>
  );
}

/** A rename is committed, not typed.
 *
 * Every keystroke rewriting every reference would churn the document and make
 * a half-typed name a real name for one render. The field holds local text
 * until it is committed, and only then does the structural rename run. */
function StepNameField({
  name,
  index,
  readOnly,
  onRename,
}: {
  name: string | null;
  index: number;
  readOnly: boolean;
  onRename: (to: string) => void;
}) {
  const [typed, setTyped] = useState<string | null>(null);
  const value = typed ?? name ?? "";
  const commit = () => {
    if (typed === null || typed === name) {
      setTyped(null);
      return;
    }
    onRename(typed);
    setTyped(null);
  };
  return (
    <div className="editorField">
      <label htmlFor={`step-name-${index}`}>Name</label>
      <input
        id={`step-name-${index}`}
        type="text"
        value={value}
        readOnly={readOnly}
        data-testid={`editor-name-${index}`}
        onChange={(event) => setTyped(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          }
        }}
      />
      <p className="hint">
        Renaming moves every route, selector, and visit count that names this
        step. It never rewrites an instruction that happens to contain the same
        words.
      </p>
      <FieldProblem location={`steps[${index}].name`} />
    </div>
  );
}

function ReferenceList({ references }: { references: Reference[] }) {
  return (
    <ul className="editorProblems" data-testid="reference-impact">
      {references.map((reference, index) => (
        <li key={index}>
          <code className="mono">{reference.location}</code> — {reference.what}
        </li>
      ))}
    </ul>
  );
}

function RemoveStepButton({
  document,
  index,
  name,
  readOnly,
  onRemove,
}: {
  document: DraftObject;
  index: number;
  name: string | null;
  readOnly: boolean;
  onRemove: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const references = useMemo(
    () => (name === null ? [] : referencesTo(document, "step", name).filter((r) => r.stepIndex !== index)),
    [document, name, index],
  );
  if (!confirming) {
    return (
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        data-testid={`editor-remove-${index}`}
        onClick={() => setConfirming(true)}
      >
        Remove
      </button>
    );
  }
  return (
    <div className="editorRow" data-testid={`editor-remove-confirm-${index}`}>
      {references.length === 0 ? (
        <p>Nothing else in this workflow names this step.</p>
      ) : (
        <>
          <p className="editorProblem">
            {references.length} other place{references.length === 1 ? "" : "s"} name
            this step. Removing it leaves them pointing at a step that does not
            exist — they stay visible so you can repair them, and the workflow
            cannot be saved as executable until you do.
          </p>
          <ReferenceList references={references} />
        </>
      )}
      <div className="editorActions">
        <button
          type="button"
          className="ghostButton"
          data-testid={`editor-remove-confirmed-${index}`}
          onClick={() => {
            setConfirming(false);
            onRemove();
          }}
        >
          Remove anyway
        </button>
        <button type="button" className="ghostButton" onClick={() => setConfirming(false)}>
          Keep it
        </button>
      </div>
    </div>
  );
}

function AgentsPanel({
  document,
  onChange,
  readOnly,
  onError,
}: {
  document: DraftObject;
  onChange: (update: (document: DraftObject) => DraftObject) => void;
  readOnly: boolean;
  onError: (message: string | null) => void;
}) {
  const sessions = sessionNames(document);
  const primary = asString(document.primary) ?? "";
  const keys = useRowKeys();
  const [typed, setTyped] = useState<Record<number, string>>({});

  return (
    <fieldset className="editorSection" data-testid="editor-agents">
      <legend>Agents</legend>
      <p className="hint">
        Each name is one durable conversation. Reusing a name in a later step
        continues that conversation rather than starting a fresh one, which is
        what lets QA verify a fix in the session where it reproduced the bug.
        Which model an agent uses is chosen at launch, not here.
      </p>
      <ul className="editorRows">
        {sessions.map((session, index) => {
          const references = referencesTo(document, "session", session);
          return (
            <li key={keys.at(index)} className="editorRow">
              <div className="editorField">
                <label htmlFor={`agent-${index}`}>Name</label>
                <input
                  id={`agent-${index}`}
                  type="text"
                  value={typed[index] ?? session}
                  readOnly={readOnly}
                  data-testid={`editor-agent-${index}`}
                  onChange={(event) =>
                    setTyped((current) => ({ ...current, [index]: event.target.value }))
                  }
                  onBlur={() => {
                    const next = typed[index];
                    setTyped((current) => {
                      const updated = { ...current };
                      delete updated[index];
                      return updated;
                    });
                    if (next === undefined || next === session) return;
                    const result = renameSession(document, session, next);
                    onError(result.error);
                    if (result.error === null) onChange(() => result.document);
                  }}
                />
              </div>
              <div className="editorField">
                <span className="editorLabel">Primary</span>
                <label>
                  <input
                    type="radio"
                    name="workflow-primary"
                    checked={primary === session}
                    disabled={readOnly}
                    data-testid={`editor-primary-${index}`}
                    onChange={() => onChange((current) => withKey(current, "primary", session))}
                  />{" "}
                  the conversation the task opens on
                </label>
              </div>
              <p className="hint">
                {references.length === 0
                  ? "No step is assigned to this agent yet."
                  : `${references.length} step${references.length === 1 ? "" : "s"} run in this conversation.`}
              </p>
              <button
                type="button"
                className="ghostButton"
                disabled={readOnly || sessions.length <= 1}
                data-testid={`editor-remove-agent-${index}`}
                onClick={() => {
                  keys.removed(index);
                  onChange((current) =>
                    withKey(
                      current,
                      "sessions",
                      asArray(current.sessions).filter((_, at) => at !== index),
                    ),
                  );
                }}
              >
                Remove {session}
              </button>
            </li>
          );
        })}
      </ul>
      <FieldProblem location="sessions" />
      <FieldProblem location="primary" />
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        data-testid="editor-add-agent"
        onClick={() => {
          keys.inserted(sessions.length);
          let name = "agent";
          let suffix = 1;
          while (sessions.includes(name)) name = `agent-${++suffix}`;
          onChange((current) =>
            withKey(current, "sessions", [...asArray(current.sessions), name]),
          );
        }}
      >
        Add agent
      </button>
    </fieldset>
  );
}

function StepBody({
  step,
  index,
  format,
  sessions,
  grammar,
  readOnly,
  onEdit,
}: {
  step: DraftObject;
  index: number;
  format: number | null;
  sessions: string[];
  grammar: GrammarContext;
  readOnly: boolean;
  onEdit: (update: (step: DraftObject) => DraftObject) => void;
  onDocument: (update: (document: DraftObject) => DraftObject) => void;
}) {
  const kind = asString(step.kind) ?? "";
  const at = `steps[${index}]`;
  const set = (key: string, value: DraftValue | undefined) =>
    onEdit((current) => withKey(current, key, value));

  if (kind === "agent") {
    return (
      <>
        <SelectField
          label="Runs as agent"
          value={asString(step.session) ?? ""}
          options={sessions.map((name) => ({ value: name, label: name }))}
          disabled={readOnly}
          location={`${at}.session`}
          testId={`editor-session-${index}`}
          onChange={(next) => set("session", next)}
        />
        <SelectField
          label="Model role"
          value={asString(step.role) ?? "default"}
          options={MODEL_ROLES.map((role) => ({ value: role, label: role }))}
          disabled={readOnly}
          location={`${at}.role`}
          onChange={(next) => set("role", next === "default" ? undefined : next)}
        />
        <TextDocumentField
          value={step.prompt}
          location={`${at}.prompt`}
          grammar={grammar}
          label="Instruction"
          onChange={(next) => set("prompt", next)}
        />
        <details className="editorSection">
          <summary>Run this step only under a condition</summary>
          <PredicateField
            value={step.when ?? true}
            location={`${at}.when`}
            grammar={grammar}
            label="Runs when"
            onChange={(next) => set("when", next === true ? undefined : next)}
          />
        </details>
        {format === 1 ? (
          <CheckField
            label="Must write an outcome document"
            checked={step.expects_outcome === true}
            disabled={readOnly}
            onChange={(next) => set("expects_outcome", next ? true : undefined)}
            testId={`editor-expects-outcome-${index}`}
          />
        ) : (
          <OutcomeEditor step={step} index={index} readOnly={readOnly} onEdit={onEdit} />
        )}
      </>
    );
  }

  if (kind === "command") {
    const argv = asArray(step.argv);
    return (
      <fieldset className="editorSection">
        <legend>Command</legend>
        <p className="hint">
          One argument per row, and every one a literal. Arguments are never
          built from what an agent wrote, so there is no shell line to quote and
          nothing to inject into.
        </p>
        <ArgvEditor
          argv={argv}
          location={`${at}.argv`}
          readOnly={readOnly}
          onChange={(next) => set("argv", next)}
        />
        <NumberField
          label="Timeout (seconds)"
          value={step.timeout}
          allowEmpty
          disabled={readOnly}
          location={`${at}.timeout`}
          testId={`editor-timeout-${index}`}
          onChange={(next) => set("timeout", next)}
        />
        <CheckField
          label="Safe to run again after a restart"
          checked={step.idempotent === true}
          disabled={readOnly}
          testId={`editor-idempotent-${index}`}
          hint="A recovering run re-enters the step it was on. A command that is not safe to repeat cannot be saved as executable."
          onChange={(next) => set("idempotent", next ? true : undefined)}
        />
        <FieldProblem location={`${at}.idempotent`} />
      </fieldset>
    );
  }

  if (kind === "decision") {
    return (
      <CasesEditor
        step={step}
        index={index}
        grammar={grammar}
        readOnly={readOnly}
        onEdit={onEdit}
      />
    );
  }

  if (kind === "review") {
    return (
      <p className="hint" data-testid={`editor-review-${index}`}>
        An independent reviewer reads exactly what this task would publish —
        the protected candidate, not the live working tree. It has no
        instruction and no model of its own: what it produces is a recorded
        verdict (approved, comments, aborted, error, or interrupted) that the
        steps after it route on, like any other evidence.
      </p>
    );
  }

  if (kind === "delivery") {
    return (
      <DeliveryEditor
        step={step}
        index={index}
        grammar={grammar}
        readOnly={readOnly}
        onEdit={onEdit}
      />
    );
  }

  return (
    <>
      <TextDocumentField
        value={step.message}
        location={`${at}.message`}
        grammar={grammar}
        label="Question for the operator"
        onChange={(next) => set("message", next)}
      />
      {format !== 1 && (
        <ChoicesEditor
          step={step}
          index={index}
          format={format}
          grammar={grammar}
          readOnly={readOnly}
          onEdit={onEdit}
        />
      )}
      {(format ?? 0) >= 3 && (
        <DeliveryGateEditor
          step={step}
          index={index}
          grammar={grammar}
          readOnly={readOnly}
          onEdit={onEdit}
        />
      )}
    </>
  );
}

function DeliveryEditor({
  step,
  index,
  grammar,
  readOnly,
  onEdit,
}: {
  step: DraftObject;
  index: number;
  grammar: GrammarContext;
  readOnly: boolean;
  onEdit: (update: (step: DraftObject) => DraftObject) => void;
}) {
  const at = `steps[${index}]`;
  const action = asString(step.action) ?? "";
  const set = (key: string, value: DraftValue | undefined) =>
    onEdit((current) => withKey(current, key, value));

  return (
    <fieldset className="editorSection" data-testid={`editor-delivery-${index}`}>
      <legend>Publication</legend>
      <p className="hint">
        One effect, once. This step performs exactly the action named here and
        nothing else — it runs only if a person answers its approval with the
        choice that grants it, and it never performs a predecessor that has
        not completed.
      </p>
      <SelectField
        label="Action"
        value={action}
        options={DELIVERY_ACTIONS.map((option) => ({
          value: option,
          label:
            option === "commit"
              ? "commit — sign locally"
              : option === "push"
                ? "push — write the branch"
                : "pr — open a pull request",
        }))}
        disabled={readOnly}
        location={`${at}.action`}
        testId={`editor-delivery-action-${index}`}
        onChange={(next) =>
          onEdit((current) => {
            // The two fields are per-action: a commit composes history, and
            // the others consume a result. Swapping the action swaps which
            // one the card has, rather than leaving a field the grammar has
            // no place for.
            const cleared = withKey(withKey(current, "mode", undefined), "previous", undefined);
            return next === "commit"
              ? withKey(cleared, "mode", "squash")
              : withKey(cleared, "previous", "");
          })
        }
      />
      {action === "commit" ? (
        <SelectField
          label="How it composes history"
          value={asString(step.mode) ?? "squash"}
          options={DELIVERY_MODES.map((option) => ({
            value: option,
            label:
              option === "squash"
                ? "squash — one signed commit"
                : "retain — keep the existing commits",
          }))}
          disabled={readOnly}
          location={`${at}.mode`}
          testId={`editor-delivery-mode-${index}`}
          onChange={(next) => set("mode", next)}
        />
      ) : (
        <SelectField
          label="Consumes the result of"
          value={asString(step.previous) ?? ""}
          options={grammar.steps.map((name) => ({ value: name, label: name }))}
          disabled={readOnly}
          location={`${at}.previous`}
          testId={`editor-delivery-previous-${index}`}
          onChange={(next) => set("previous", next)}
        />
      )}
      <SelectField
        label="Authorized by"
        value={asString(step.approval) ?? ""}
        options={grammar.steps.map((name) => ({ value: name, label: name }))}
        disabled={readOnly}
        location={`${at}.approval`}
        testId={`editor-delivery-approval-${index}`}
        onChange={(next) => set("approval", next)}
      />
      <DestinationField
        value={step.next}
        location={`${at}.next`}
        grammar={grammar}
        label="Once it is on record, go to"
        allowPause={false}
        onChange={(next) => set("next", next)}
      />
    </fieldset>
  );
}

function DeliveryGateEditor({
  step,
  index,
  grammar,
  readOnly,
  onEdit,
}: {
  step: DraftObject;
  index: number;
  grammar: GrammarContext;
  readOnly: boolean;
  onEdit: (update: (step: DraftObject) => DraftObject) => void;
}) {
  const at = `steps[${index}].delivery`;
  const delivery = asObject(step.delivery);
  const metadata = asObject(delivery?.metadata) ?? {};
  const aliases = evidenceAliases(step);
  const setDelivery = (next: DraftObject | undefined) =>
    onEdit((current) => withKey(current, "delivery", next));

  if (delivery === null) {
    return (
      <div className="editorSection" data-testid={`editor-gate-delivery-${index}`}>
        <p className="hint">
          This question authorizes no publication. To let one of its answers
          publish, bind it to the review it is asking about — the grant rests
          on that verdict, not on the question having been reached.
        </p>
        <button
          type="button"
          className="ghostButton"
          disabled={readOnly}
          data-testid={`editor-add-gate-delivery-${index}`}
          onClick={() => setDelivery({ review: aliases[0] ?? "" })}
        >
          Let answers here authorize publication
        </button>
      </div>
    );
  }

  return (
    <fieldset className="editorSection" data-testid={`editor-gate-delivery-${index}`}>
      <legend>What this approval is about</legend>
      <SelectField
        label="The review it rests on"
        value={asString(delivery.review) ?? ""}
        options={aliases.map((alias) => ({ value: alias, label: alias }))}
        disabled={readOnly}
        location={`${at}.review`}
        testId={`editor-gate-review-${index}`}
        onChange={(next) => setDelivery(withKey(delivery, "review", next))}
      />
      <p className="hint">
        One of this card&apos;s own evidence aliases, and it may only select
        review steps: the grant rests on a trusted verdict, never on an
        agent&apos;s own claim.
      </p>
      <p className="hint">
        Publication text the workflow suggests. Whatever it renders arrives as
        an editable draft beside the decision — the operator's final text is
        what gets published. An omitted field simply starts blank.
      </p>
      {DELIVERY_METADATA_FIELDS.map((field) => {
        const present = metadata[field] !== undefined && metadata[field] !== null;
        const setField = (next: DraftValue | undefined) =>
          setDelivery(
            withKey(delivery, "metadata", withKey(metadata, field, next)),
          );
        return present ? (
          <div key={field}>
            <TextDocumentField
              value={metadata[field]}
              location={`${at}.metadata.${field}`}
              grammar={grammar}
              label={
                field === "message"
                  ? "Suggested commit message"
                  : field === "pr_title"
                    ? "Suggested pull-request title"
                    : "Suggested pull-request body"
              }
              onChange={setField}
            />
            <button
              type="button"
              className="ghostButton"
              disabled={readOnly}
              onClick={() => setField(undefined)}
            >
              Leave {field} blank instead
            </button>
          </div>
        ) : (
          <button
            key={field}
            type="button"
            className="ghostButton"
            disabled={readOnly}
            data-testid={`editor-add-metadata-${field}-${index}`}
            onClick={() => setField({ parts: [{ text: "" }] })}
          >
            Suggest {field}
          </button>
        );
      })}
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        onClick={() => setDelivery(undefined)}
      >
        Stop letting answers here authorize publication
      </button>
      <FieldProblem location={at} />
    </fieldset>
  );
}

function ArgvEditor({
  argv,
  location,
  readOnly,
  onChange,
}: {
  argv: DraftValue[];
  location: string;
  readOnly: boolean;
  onChange: (next: DraftValue[]) => void;
}) {
  const keys = useRowKeys();
  return (
    <>
      <ul className="editorRows">
        {argv.map((argument, index) => (
          <li key={keys.at(index)} className="editorRow">
            <TextField
              label={index === 0 ? "Program" : `Argument ${index}`}
              value={asString(argument) ?? ""}
              disabled={readOnly}
              location={`${location}[${index}]`}
              onChange={(next) => {
                const list = [...argv];
                list[index] = next;
                onChange(list);
              }}
            />
            <button
              type="button"
              className="ghostButton"
              disabled={readOnly}
              onClick={() => {
                keys.removed(index);
                onChange(argv.filter((_, at) => at !== index));
              }}
            >
              Remove argument {index}
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        data-testid="editor-add-argv"
        onClick={() => {
          keys.inserted(argv.length);
          onChange([...argv, ""]);
        }}
      >
        Add argument
      </button>
      <FieldProblem location={location} />
    </>
  );
}

function CasesEditor({
  step,
  index,
  grammar,
  readOnly,
  onEdit,
}: {
  step: DraftObject;
  index: number;
  grammar: GrammarContext;
  readOnly: boolean;
  onEdit: (update: (step: DraftObject) => DraftObject) => void;
}) {
  const at = `steps[${index}]`;
  const cases = asArray(step.cases);
  const keys = useRowKeys();
  const setCases = (next: DraftValue[]) =>
    onEdit((current) => withKey(current, "cases", next));

  return (
    <fieldset className="editorSection" data-testid={`editor-cases-${index}`}>
      <legend>Routes, in order</legend>
      <p className="hint">
        The first case whose condition holds decides where the run goes. Order
        matters, and a condition that cannot be decided stops the run for you
        rather than falling through.
      </p>
      <ol className="editorRows">
        {cases.map((entry, caseIndex) => {
          const object = asObject(entry) ?? {};
          const caseAt = `${at}.cases[${caseIndex}]`;
          const replace = (next: DraftObject) => {
            const list = [...cases];
            list[caseIndex] = next;
            setCases(list);
          };
          return (
            <li key={keys.at(caseIndex)} className="editorRow">
              <PredicateField
                value={object.when}
                location={`${caseAt}.when`}
                grammar={grammar}
                label={`Case ${caseIndex + 1} — when`}
                onChange={(next) => replace(withKey(object, "when", next))}
              />
              <DestinationField
                value={object.next}
                location={`${caseAt}.next`}
                grammar={grammar}
                label="then go to"
                onChange={(next) => replace(withKey(object, "next", next))}
              />
              <div className="editorActions">
                <button
                  type="button"
                  className="ghostButton"
                  disabled={readOnly || caseIndex === 0}
                  onClick={() => {
                    keys.moved(caseIndex, caseIndex - 1);
                    const list = [...cases];
                    [list[caseIndex - 1], list[caseIndex]] = [list[caseIndex], list[caseIndex - 1]];
                    setCases(list);
                  }}
                >
                  Move case {caseIndex + 1} earlier
                </button>
                <button
                  type="button"
                  className="ghostButton"
                  disabled={readOnly || caseIndex === cases.length - 1}
                  onClick={() => {
                    keys.moved(caseIndex, caseIndex + 1);
                    const list = [...cases];
                    [list[caseIndex + 1], list[caseIndex]] = [list[caseIndex], list[caseIndex + 1]];
                    setCases(list);
                  }}
                >
                  Move case {caseIndex + 1} later
                </button>
                <button
                  type="button"
                  className="ghostButton"
                  disabled={readOnly}
                  onClick={() => {
                    keys.removed(caseIndex);
                    setCases(cases.filter((_, at2) => at2 !== caseIndex));
                  }}
                >
                  Remove case {caseIndex + 1}
                </button>
              </div>
            </li>
          );
        })}
      </ol>
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        data-testid={`editor-add-case-${index}`}
        onClick={() => {
          keys.inserted(cases.length);
          setCases([...cases, { when: true, next: { step: grammar.steps[0] ?? "" } }]);
        }}
      >
        Add case
      </button>
      <FieldProblem location={`${at}.cases`} />
      <DestinationField
        value={step.otherwise}
        location={`${at}.otherwise`}
        grammar={grammar}
        label="If no case holds"
        onChange={(next) => onEdit((current) => withKey(current, "otherwise", next))}
      />
    </fieldset>
  );
}

function ChoicesEditor({
  step,
  index,
  format,
  grammar,
  readOnly,
  onEdit,
}: {
  step: DraftObject;
  index: number;
  format: number | null;
  grammar: GrammarContext;
  readOnly: boolean;
  onEdit: (update: (step: DraftObject) => DraftObject) => void;
}) {
  const at = `steps[${index}].choices`;
  const choices = asArray(step.choices);
  const keys = useRowKeys();
  const setChoices = (next: DraftValue[]) =>
    onEdit((current) => withKey(current, "choices", next));

  return (
    <fieldset className="editorSection" data-testid={`editor-choices-${index}`}>
      <legend>Answers you can give</legend>
      <p className="hint">
        Answering means choosing one of these routes. There is no generic
        Resume: what a person picked, and why, is recorded with the run.
      </p>
      <ol className="editorRows">
        {choices.map((entry, choiceIndex) => {
          const object = asObject(entry) ?? {};
          const choiceAt = `${at}[${choiceIndex}]`;
          const replace = (next: DraftObject) => {
            const list = [...choices];
            list[choiceIndex] = next;
            setChoices(list);
          };
          return (
            <li key={keys.at(choiceIndex)} className="editorRow">
              <TextField
                label="Id"
                value={asString(object.id) ?? ""}
                disabled={readOnly}
                location={`${choiceAt}.id`}
                testId={`editor-choice-id-${index}-${choiceIndex}`}
                onChange={(next) => replace(withKey(object, "id", next))}
              />
              <TextField
                label="Label"
                value={asString(object.label) ?? ""}
                disabled={readOnly}
                location={`${choiceAt}.label`}
                testId={`editor-choice-label-${index}-${choiceIndex}`}
                onChange={(next) => replace(withKey(object, "label", next))}
              />
              <CheckField
                label="Requires a written reason"
                checked={object.feedback_required === true}
                disabled={readOnly}
                onChange={(next) =>
                  replace(withKey(object, "feedback_required", next ? true : undefined))
                }
              />
              <DestinationField
                value={object.next}
                location={`${choiceAt}.next`}
                grammar={grammar}
                label="Choosing it goes to"
                allowPause={false}
                onChange={(next) => replace(withKey(object, "next", next))}
              />
              {(format ?? 0) >= 3 && (
                <GrantField
                  choice={object}
                  location={`${choiceAt}.authorize`}
                  grammar={grammar}
                  readOnly={readOnly}
                  testId={`editor-choice-authorize-${index}-${choiceIndex}`}
                  onChange={replace}
                />
              )}
              <button
                type="button"
                className="ghostButton"
                disabled={readOnly}
                onClick={() => {
                  keys.removed(choiceIndex);
                  setChoices(choices.filter((_, at2) => at2 !== choiceIndex));
                }}
              >
                Remove answer {choiceIndex + 1}
              </button>
            </li>
          );
        })}
      </ol>
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        data-testid={`editor-add-choice-${index}`}
        onClick={() => {
          keys.inserted(choices.length);
          setChoices([...choices, { id: "", label: "", next: { step: grammar.steps[0] ?? "" } }]);
        }}
      >
        Add answer
      </button>
      <FieldProblem location={at} />
    </fieldset>
  );
}

function GrantField({
  choice,
  location,
  grammar,
  readOnly,
  testId,
  onChange,
}: {
  choice: DraftObject;
  location: string;
  grammar: GrammarContext;
  readOnly: boolean;
  testId: string;
  onChange: (choice: DraftObject) => void;
}) {
  const grant = asObject(choice.authorize);
  const steps = asArray(grant?.steps)
    .map((name) => asString(name))
    .filter((name): name is string => name !== null);

  if (grant === null) {
    return (
      <div data-testid={testId}>
        <p className="hint">This answer publishes nothing.</p>
        <button
          type="button"
          className="ghostButton"
          disabled={readOnly}
          data-testid={`${testId}-add`}
          onClick={() => onChange(withKey(choice, "authorize", { steps: [] }))}
        >
          Let this answer authorize publication
        </button>
      </div>
    );
  }

  return (
    <div className="editorSection" data-testid={testId}>
      <p className="hint">
        The exact actions this answer permits, in the order they run. It must
        start where the answer goes and name the whole chain: a grant is a list
        of actions, never permission to publish in general.
      </p>
      <StepListField
        label="Authorizes"
        value={steps}
        options={grammar.steps}
        location={`${location}.steps`}
        onChange={(next) => onChange(withKey(choice, "authorize", { steps: next }))}
      />
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        onClick={() => onChange(withKey(choice, "authorize", undefined))}
      >
        Make this answer publish nothing
      </button>
      <FieldProblem location={location} />
    </div>
  );
}

function OutcomeEditor({
  step,
  index,
  readOnly,
  onEdit,
}: {
  step: DraftObject;
  index: number;
  readOnly: boolean;
  onEdit: (update: (step: DraftObject) => DraftObject) => void;
}) {
  const at = `steps[${index}].outcome`;
  const outcome = asObject(step.outcome);
  const results = asObject(outcome?.results) ?? {};
  const keys = useRowKeys();
  const declared = step.outcome !== undefined && step.outcome !== null;
  const setResults = (next: DraftObject) =>
    onEdit((current) => withKey(current, "outcome", { results: next }));

  return (
    <fieldset className="editorSection" data-testid={`editor-outcome-${index}`}>
      <legend>Required result</legend>
      <p className="hint">
        A declared result is a contract: anything outside this list, or missing
        a required field, is not a result and does not let the run continue.
        Declaring nothing is also a choice — say which one you mean.
      </p>
      <SelectField
        label="This step"
        value={declared ? "results" : "none"}
        options={[
          { value: "none", label: "is not asked for a result" },
          { value: "results", label: "must declare one of these results" },
        ]}
        disabled={readOnly}
        location={at}
        testId={`editor-outcome-kind-${index}`}
        onChange={(next) =>
          onEdit((current) =>
            withKey(current, "outcome", next === "none" ? null : { results: {} }),
          )
        }
      />
      {declared && (
        <>
          <ul className="editorRows">
            {Object.entries(results).map(([name, contract], resultIndex) => {
              const required = asObject(asObject(contract)?.required) ?? {};
              return (
                <li key={keys.at(resultIndex)} className="editorRow">
                  <TextField
                    label="Result name"
                    value={name}
                    disabled={readOnly}
                    location={`${at}.results.${name}`}
                    testId={`editor-result-${index}-${resultIndex}`}
                    onChange={(next) => {
                      const entries = Object.entries(results);
                      entries[resultIndex] = [next, contract];
                      setResults(Object.fromEntries(entries));
                    }}
                  />
                  <RequiredFields
                    required={required}
                    location={`${at}.results.${name}.required`}
                    readOnly={readOnly}
                    onChange={(next) =>
                      setResults(withKey(results, name, { required: next }))
                    }
                  />
                  <button
                    type="button"
                    className="ghostButton"
                    disabled={readOnly}
                    onClick={() => {
                      keys.removed(resultIndex);
                      setResults(
                        Object.fromEntries(
                          Object.entries(results).filter((_, at2) => at2 !== resultIndex),
                        ),
                      );
                    }}
                  >
                    Remove result {name}
                  </button>
                </li>
              );
            })}
          </ul>
          <button
            type="button"
            className="ghostButton"
            disabled={readOnly}
            data-testid={`editor-add-result-${index}`}
            onClick={() => {
              keys.inserted(Object.keys(results).length);
              let name = "result";
              let suffix = 1;
              while (name in results) name = `result-${++suffix}`;
              setResults(withKey(results, name, { required: {} }));
            }}
          >
            Add a result
          </button>
        </>
      )}
      <p className="hint">
        Whether the step's claim is <em>true</em> is not checked here or
        anywhere else. This says what it must supply, not that it is right.
      </p>
    </fieldset>
  );
}

function RequiredFields({
  required,
  location,
  readOnly,
  onChange,
}: {
  required: DraftObject;
  location: string;
  readOnly: boolean;
  onChange: (next: DraftObject) => void;
}) {
  const keys = useRowKeys();
  const entries = Object.entries(required);
  return (
    <div className="editorField">
      <span className="editorLabel">Must carry</span>
      <ul className="editorRows">
        {entries.map(([field, type], index) => (
          <li key={keys.at(index)} className="editorRow">
            <TextField
              label="Field"
              value={field}
              disabled={readOnly}
              onChange={(next) => {
                const list = [...entries];
                list[index] = [next, type];
                onChange(Object.fromEntries(list));
              }}
            />
            <SelectField
              label="Type"
              value={asString(type) ?? ""}
              options={REQUIRED_TYPES.map((name) => ({ value: name, label: name }))}
              disabled={readOnly}
              location={`${location}.${field}`}
              onChange={(next) => onChange(withKey(required, field, next))}
            />
            <button
              type="button"
              className="ghostButton"
              disabled={readOnly}
              onClick={() => {
                keys.removed(index);
                onChange(
                  Object.fromEntries(entries.filter((_, at) => at !== index)),
                );
              }}
            >
              Remove {field}
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        onClick={() => {
          keys.inserted(entries.length);
          let field = "field";
          let suffix = 1;
          while (field in required) field = `field-${++suffix}`;
          onChange(withKey(required, field, "string"));
        }}
      >
        Add a required field
      </button>
      <FieldProblem location={location} />
    </div>
  );
}

function BoundField({
  step,
  index,
  grammar,
  readOnly,
  onEdit,
}: {
  step: DraftObject;
  index: number;
  grammar: GrammarContext;
  readOnly: boolean;
  onEdit: (update: (step: DraftObject) => DraftObject) => void;
}) {
  const at = `steps[${index}]`;
  const bounded = isBounded(step);
  const within = useProblemWithin(`${at}.on_exhausted`);
  return (
    <details className="editorSection" open={bounded || within} data-testid={`editor-bound-${index}`}>
      <summary>Limit how many times this step may run</summary>
      <p className="hint">
        A loop that can spin forever is not a bound. Both halves are declared
        together: how many visits, and which gate the run reaches when they are
        used up — so exhaustion asks a person rather than ending quietly.
      </p>
      <CheckField
        label="Bound this step's visits"
        checked={bounded}
        disabled={readOnly}
        testId={`editor-bounded-${index}`}
        onChange={(next) =>
          onEdit((current) =>
            next
              ? withKey(withKey(current, "max_visits", 3 as unknown as DraftValue), "on_exhausted", {
                  step: grammar.steps[0] ?? "",
                })
              : withKey(withKey(current, "max_visits", undefined), "on_exhausted", undefined),
          )
        }
      />
      {bounded && (
        <>
          <NumberField
            label="At most this many visits"
            value={step.max_visits}
            disabled={readOnly}
            location={`${at}.max_visits`}
            testId={`editor-max-visits-${index}`}
            onChange={(next) => onEdit((current) => withKey(current, "max_visits", next))}
          />
          <DestinationField
            value={step.on_exhausted}
            location={`${at}.on_exhausted`}
            grammar={grammar}
            label="When they are used up, go to"
            allowPause={false}
            onChange={(next) => onEdit((current) => withKey(current, "on_exhausted", next))}
          />
        </>
      )}
    </details>
  );
}

function EvidenceEditor({
  step,
  index,
  format,
  names,
  readOnly,
  onEdit,
  onDocument,
}: {
  step: DraftObject;
  index: number;
  format: number | null;
  names: string[];
  readOnly: boolean;
  onEdit: (update: (step: DraftObject) => DraftObject) => void;
  onDocument: (update: (document: DraftObject) => DraftObject) => void;
}) {
  const at = `steps[${index}].evidence`;
  const evidence = asObject(step.evidence) ?? {};
  const entries = Object.entries(evidence);
  const keys = useRowKeys();
  const [typed, setTyped] = useState<Record<number, string>>({});

  if (format === 1) {
    return (
      <p className="hint">
        Format 1 has no evidence selectors. A prompt reaches back through
        “the latest attempt of” instead, which is resolved when it is read
        rather than frozen when the step opens.
      </p>
    );
  }

  const setEvidence = (next: DraftObject) =>
    onEdit((current) =>
      withKey(current, "evidence", Object.keys(next).length === 0 ? undefined : next),
    );

  return (
    <fieldset className="editorSection" data-testid={`editor-evidence-${index}`}>
      <legend>Evidence this step is handed</legend>
      <p className="hint">
        Each alias is resolved once, when the attempt opens, and then frozen.
        What the step saw is a recorded fact rather than whatever “latest”
        would mean when somebody asks again later.
      </p>
      <ul className="editorRows">
        {entries.map(([alias, selector], aliasIndex) => {
          const object = asObject(selector) ?? {};
          const aliasAt = `${at}.${alias}`;
          const replace = (next: DraftObject) => setEvidence(withKey(evidence, alias, next));
          return (
            <li key={keys.at(aliasIndex)} className="editorRow">
              <div className="editorField">
                <label htmlFor={`evidence-${index}-${aliasIndex}`}>Alias</label>
                <input
                  id={`evidence-${index}-${aliasIndex}`}
                  type="text"
                  value={typed[aliasIndex] ?? alias}
                  readOnly={readOnly}
                  data-testid={`editor-evidence-alias-${index}-${aliasIndex}`}
                  onChange={(event) =>
                    setTyped((current) => ({ ...current, [aliasIndex]: event.target.value }))
                  }
                  onBlur={() => {
                    const next = typed[aliasIndex];
                    setTyped((current) => {
                      const updated = { ...current };
                      delete updated[aliasIndex];
                      return updated;
                    });
                    if (next === undefined || next === alias) return;
                    onDocument((current) => renameEvidenceAlias(current, index, alias, next));
                  }}
                />
                <p className="hint">
                  Renaming moves the reads inside this step. An alias means
                  nothing outside the card that declared it.
                </p>
              </div>
              {/* Several sources are allowed: "the latest attempt of either
                  of these", which is how a step reads whichever of two
                  producers ran most recently. */}
              <StepListField
                label="Latest attempt of"
                value={asArray(object.steps)}
                options={names}
                location={`${aliasAt}.steps`}
                onChange={(next) => replace(withKey(object, "steps", next))}
              />
              <SelectField
                label="No older than"
                value={asString(object.after) ?? ""}
                options={[
                  { value: "", label: "— no freshness anchor —" },
                  ...names.map((name) => ({ value: name, label: name })),
                ]}
                disabled={readOnly}
                location={`${aliasAt}.after`}
                onChange={(next) =>
                  replace(withKey(object, "after", next === "" ? undefined : next))
                }
              />
              <CheckField
                label="Only attempts that recorded an outcome"
                checked={object.with_outcome !== false}
                disabled={readOnly}
                onChange={(next) =>
                  replace(withKey(object, "with_outcome", next ? undefined : false))
                }
              />
              <CheckField
                label="Required — the step stops if nothing matches"
                checked={object.required !== false}
                disabled={readOnly}
                hint="Unchecked, a missing source is an absence the step is told about rather than a reason to stop."
                onChange={(next) =>
                  replace(withKey(object, "required", next ? undefined : false))
                }
              />
              <button
                type="button"
                className="ghostButton"
                disabled={readOnly}
                onClick={() => {
                  keys.removed(aliasIndex);
                  setEvidence(
                    Object.fromEntries(entries.filter((_, at2) => at2 !== aliasIndex)),
                  );
                }}
              >
                Remove evidence {alias}
              </button>
            </li>
          );
        })}
      </ul>
      <button
        type="button"
        className="ghostButton"
        disabled={readOnly}
        data-testid={`editor-add-evidence-${index}`}
        onClick={() => {
          keys.inserted(entries.length);
          let alias = "evidence";
          let suffix = 1;
          while (alias in evidence) alias = `evidence-${++suffix}`;
          setEvidence(withKey(evidence, alias, { steps: [names[0] ?? ""] }));
        }}
      >
        Add evidence
      </button>
      <FieldProblem location={at} />
    </fieldset>
  );
}

function UnknownFields({ step }: { step: DraftObject }) {
  const unknown = unknownStepFields(step);
  if (unknown.length === 0) return null;
  return (
    <p className="flowBroken" data-testid="editor-unknown-fields">
      This card carries fields no supported format defines: {unknown.join(", ")}.
      They are kept exactly as written and are not editable here — switch to
      YAML to remove or repair them.
    </p>
  );
}
