import { useId, useState } from "react";
import { LosslessNumber, numberToken } from "../../lib/losslessJson";
import { FieldProblem } from "./problem";
import { useRowKeys } from "./problemContext";
import {
  COMPARISONS,
  INPUT_NAMES,
  JSON_TYPES,
  VALUE_FORMATS,
  asArray,
  asObject,
  asString,
  withKey,
  type DraftObject,
  type DraftValue,
} from "../../lib/workflowDocument";

/** Form controls for the definition grammar — all of it, and nothing else.
 *
 * Every control here edits a construct the daemon's loader already accepts:
 * the seven value operations, the nine predicate operations, text parts,
 * destinations, and JSON literals. There is no interpolation language, no
 * expression textbox, and no place to type code — a workflow document is
 * data, and an editor that let somebody write something the loader would
 * refuse would be inventing a second grammar nobody validates.
 *
 * Nothing is evaluated here either. These controls say what a definition
 * *asks for*; whether the answer exists, and what it turns out to be, is the
 * daemon's business at run time.
 */

/** A control that knows its address in the document is addressable by it.
 *
 * The same grammar the daemon uses for a validation location, so a test — or
 * a person reading one — names a field the way a refusal does. */
function locatedTestId(location: string | undefined): string | undefined {
  return location === undefined ? undefined : `field-${location}`;
}

// --- small controls ------------------------------------------------------------

export function TextField({
  label,
  value,
  onChange,
  location,
  placeholder,
  disabled,
  testId,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  location?: string;
  placeholder?: string;
  disabled?: boolean;
  testId?: string;
}) {
  const id = useId();
  return (
    <div className="editorField">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="text"
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        data-testid={testId ?? locatedTestId(location)}
        onChange={(event) => onChange(event.target.value)}
      />
      {location !== undefined && <FieldProblem location={location} />}
    </div>
  );
}

export function SelectField({
  label,
  value,
  options,
  onChange,
  location,
  disabled,
  testId,
}: {
  label: string;
  value: string;
  options: readonly { value: string; label: string }[];
  onChange: (next: string) => void;
  location?: string;
  disabled?: boolean;
  testId?: string;
}) {
  const id = useId();
  const known = options.some((option) => option.value === value);
  return (
    <div className="editorField">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        data-testid={testId ?? locatedTestId(location)}
        onChange={(event) => onChange(event.target.value)}
      >
        {/* A value the document carries but this list does not offer stays
            selectable rather than being silently swapped for the first
            option — that would edit the definition by rendering it. */}
        {!known && <option value={value}>{value === "" ? "— not set —" : `${value} — not a known choice`}</option>}
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {location !== undefined && <FieldProblem location={location} />}
    </div>
  );
}

export function CheckField({
  label,
  checked,
  onChange,
  hint,
  disabled,
  testId,
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  hint?: string;
  disabled?: boolean;
  testId?: string;
}) {
  const id = useId();
  return (
    <div className="editorField">
      <span className="editorLabel" />
      <label htmlFor={id} className="editorCheck">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          disabled={disabled}
          data-testid={testId}
          onChange={(event) => onChange(event.target.checked)}
        />{" "}
        {label}
      </label>
      {hint !== undefined && <p className="hint">{hint}</p>}
    </div>
  );
}

const JSON_NUMBER = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?$/;

/** A number that keeps the token the author typed.
 *
 * `1` and `1.0` are different literals, and a definition's identity is taken
 * over its canonical bytes — so a control that round-tripped one into the
 * other would change what a saved revision *is*. While the text is not yet a
 * JSON number nothing is committed: the document keeps its last real value
 * rather than acquiring a string where a number belongs. */
export function NumberField({
  label,
  value,
  onChange,
  location,
  allowEmpty,
  disabled,
  testId,
}: {
  label: string;
  value: DraftValue | undefined;
  onChange: (next: DraftValue | undefined) => void;
  location?: string;
  allowEmpty?: boolean;
  disabled?: boolean;
  testId?: string;
}) {
  const id = useId();
  const committed = numberToken(value) ?? "";
  const [typed, setTyped] = useState<string | null>(null);
  const shown = typed ?? committed;
  const pending = typed !== null && typed !== "" && !JSON_NUMBER.test(typed);
  return (
    <div className="editorField">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="text"
        inputMode="decimal"
        value={shown}
        disabled={disabled}
        data-testid={testId ?? locatedTestId(location)}
        onChange={(event) => {
          const next = event.target.value;
          setTyped(next);
          if (next === "" && allowEmpty === true) onChange(undefined);
          else if (JSON_NUMBER.test(next)) onChange(new LosslessNumber(next));
        }}
        onBlur={() => setTyped(null)}
      />
      {pending && (
        <p className="editorProblem">
          Not a number yet, so nothing was changed. Use digits, an optional
          minus sign, and an optional decimal point.
        </p>
      )}
      {location !== undefined && <FieldProblem location={location} />}
    </div>
  );
}

// --- JSON literals -------------------------------------------------------------

type LiteralKind = "string" | "number" | "boolean" | "null" | "array" | "object";

function literalKind(value: DraftValue | undefined): LiteralKind {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return "string";
  if (typeof value === "boolean") return "boolean";
  if (Array.isArray(value)) return "array";
  if (numberToken(value) !== null) return "number";
  return "object";
}

const EMPTY_FOR: Record<LiteralKind, DraftValue> = {
  string: "",
  number: new LosslessNumber("0"),
  boolean: false,
  null: null,
  array: [],
  object: {},
};

/** A typed JSON literal, recursively. Data, never an expression: the whole
 * point of `{op: literal}` is that its payload is inert. */
export function LiteralField({
  value,
  onChange,
  location,
  label = "Value",
}: {
  value: DraftValue | undefined;
  onChange: (next: DraftValue) => void;
  location: string;
  label?: string;
}) {
  const kind = literalKind(value);
  const keys = useRowKeys();
  return (
    <div className="editorRow">
      <SelectField
        label={`${label} type`}
        value={kind}
        options={[
          { value: "string", label: "text" },
          { value: "number", label: "number" },
          { value: "boolean", label: "true or false" },
          { value: "null", label: "null" },
          { value: "array", label: "list" },
          { value: "object", label: "mapping" },
        ]}
        onChange={(next) => onChange(EMPTY_FOR[next as LiteralKind])}
        testId={`literal-kind-${location}`}
      />
      {kind === "string" && (
        <TextField
          label={label}
          value={asString(value) ?? ""}
          onChange={onChange}
          location={location}
        />
      )}
      {kind === "number" && (
        <NumberField
          label={label}
          value={value}
          onChange={(next) => onChange(next ?? new LosslessNumber("0"))}
          location={location}
        />
      )}
      {kind === "boolean" && (
        <CheckField label="true" checked={value === true} onChange={onChange} />
      )}
      {kind === "null" && <p className="hint">null — a value the step wrote, not an absence.</p>}
      {kind === "array" && (
        <>
          <ul className="editorRows">
            {asArray(value).map((item, index) => (
              <li key={keys.at(index)} className="editorRow">
                <LiteralField
                  value={item}
                  location={`${location}[${index}]`}
                  label={`Item ${index + 1}`}
                  onChange={(next) => {
                    const list = [...asArray(value)];
                    list[index] = next;
                    onChange(list);
                  }}
                />
                <button
                  type="button"
                  className="ghostButton"
                  onClick={() => {
                    keys.removed(index);
                    const list = [...asArray(value)];
                    list.splice(index, 1);
                    onChange(list);
                  }}
                >
                  Remove item {index + 1}
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="ghostButton"
            onClick={() => {
              keys.inserted(asArray(value).length);
              onChange([...asArray(value), ""]);
            }}
          >
            Add item
          </button>
        </>
      )}
      {kind === "object" && (
        <>
          <ul className="editorRows">
            {Object.entries(asObject(value) ?? {}).map(([key, item], index) => (
              <li key={keys.at(index)} className="editorRow">
                <TextField
                  label="Key"
                  value={key}
                  onChange={(next) => {
                    const entries = Object.entries(asObject(value) ?? {});
                    entries[index] = [next, item];
                    onChange(Object.fromEntries(entries));
                  }}
                />
                <LiteralField
                  value={item}
                  location={`${location}.${key}`}
                  onChange={(next) => onChange(withKey(asObject(value) ?? {}, key, next))}
                />
                <button
                  type="button"
                  className="ghostButton"
                  onClick={() => {
                    keys.removed(index);
                    const entries = Object.entries(asObject(value) ?? {}).filter(
                      (_, position) => position !== index,
                    );
                    onChange(Object.fromEntries(entries));
                  }}
                >
                  Remove {key}
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="ghostButton"
            onClick={() => {
              const object = asObject(value) ?? {};
              let key = "key";
              let suffix = 1;
              while (key in object) key = `key${++suffix}`;
              keys.inserted(Object.keys(object).length);
              onChange(withKey(object, key, ""));
            }}
          >
            Add entry
          </button>
        </>
      )}
    </div>
  );
}

// --- value expressions ---------------------------------------------------------

export interface GrammarContext {
  /** 1 or 2 — the operations available differ, and neither is converted. */
  format: number | null;
  /** Every declared step, for the destination and selector choice lists. */
  steps: string[];
  /** The aliases the *containing step* declares. Evidence is step-local. */
  aliases: string[];
}

const VALUE_OPS_2 = ["literal", "input", "evidence", "get", "count", "coalesce"] as const;
const VALUE_OPS_1 = ["literal", "input", "latest", "get", "count", "coalesce"] as const;

const VALUE_LABELS: Record<string, string> = {
  literal: "a fixed value",
  input: "something about the task",
  evidence: "an evidence alias this step declared",
  latest: "the latest attempt of a step",
  get: "a field inside another value",
  count: "how many times a step has run",
  coalesce: "the first of several that exists",
};

export function ValueField({
  value,
  onChange,
  location,
  grammar,
  label = "Value",
}: {
  value: DraftValue | undefined;
  onChange: (next: DraftValue) => void;
  location: string;
  grammar: GrammarContext;
  label?: string;
}) {
  const object = asObject(value);
  const op = asString(object?.op) ?? "";
  const ops = grammar.format === 1 ? VALUE_OPS_1 : VALUE_OPS_2;
  const keys = useRowKeys();

  const change = (next: DraftObject) => onChange(next);
  const stepOptions = grammar.steps.map((name) => ({ value: name, label: name }));

  return (
    <fieldset className="editorRow" data-testid={`value-${location}`}>
      <legend className="editorLabel">{label}</legend>
      <SelectField
        label="Reads"
        value={op}
        options={ops.map((name) => ({ value: name, label: VALUE_LABELS[name] }))}
        onChange={(next) => {
          if (next === "literal") change({ op: "literal", value: "" });
          else if (next === "input") change({ op: "input", name: INPUT_NAMES[0] });
          else if (next === "evidence") change({ op: "evidence", name: grammar.aliases[0] ?? "" });
          else if (next === "latest") change({ op: "latest", steps: [grammar.steps[0] ?? ""] });
          else if (next === "get") change({ op: "get", value: object ?? { op: "literal", value: "" }, keys: [""] });
          else if (next === "count") change({ op: "count", step: grammar.steps[0] ?? "" });
          else change({ op: "coalesce", values: [object ?? { op: "literal", value: "" }] });
        }}
        location={location}
        testId={`value-op-${location}`}
      />
      {op === "literal" && (
        <LiteralField
          value={object?.value}
          location={`${location}.value`}
          onChange={(next) => change(withKey(object ?? {}, "value", next))}
        />
      )}
      {op === "input" && (
        <SelectField
          label="Task input"
          value={asString(object?.name) ?? ""}
          options={INPUT_NAMES.map((name) => ({ value: name, label: name }))}
          onChange={(next) => change(withKey(object ?? {}, "name", next))}
          location={`${location}.name`}
        />
      )}
      {op === "evidence" && (
        <SelectField
          label="Evidence alias"
          value={asString(object?.name) ?? ""}
          options={grammar.aliases.map((alias) => ({ value: alias, label: alias }))}
          onChange={(next) => change(withKey(object ?? {}, "name", next))}
          location={`${location}.name`}
        />
      )}
      {op === "count" && (
        <SelectField
          label="Step"
          value={asString(object?.step) ?? ""}
          options={stepOptions}
          onChange={(next) => change(withKey(object ?? {}, "step", next))}
          location={`${location}.step`}
        />
      )}
      {op === "latest" && (
        <>
          <StepListField
            label="Latest attempt of"
            value={asArray(object?.steps)}
            options={grammar.steps}
            location={`${location}.steps`}
            onChange={(next) => change(withKey(object ?? {}, "steps", next))}
          />
          <SelectField
            label="No older than"
            value={asString(object?.after) ?? ""}
            options={[{ value: "", label: "— no freshness anchor —" }, ...stepOptions]}
            onChange={(next) =>
              change(withKey(object ?? {}, "after", next === "" ? undefined : next))
            }
            location={`${location}.after`}
          />
          <CheckField
            label="Only attempts that recorded an outcome"
            checked={object?.with_outcome === true}
            onChange={(next) =>
              change(withKey(object ?? {}, "with_outcome", next ? true : undefined))
            }
          />
        </>
      )}
      {op === "get" && (
        <>
          <ValueField
            value={object?.value}
            location={`${location}.value`}
            grammar={grammar}
            label="Inside"
            onChange={(next) => change(withKey(object ?? {}, "value", next))}
          />
          <div className="editorField">
            <span className="editorLabel">Path</span>
            <ul className="editorRows">
              {asArray(object?.keys).map((key, index) => (
                <li key={keys.at(index)} className="editorRow">
                  <TextField
                    label={`Key or index ${index + 1}`}
                    value={numberToken(key) ?? asString(key) ?? ""}
                    location={`${location}.keys[${index}]`}
                    onChange={(next) => {
                      const list = [...asArray(object?.keys)];
                      // A whole number is an array index; anything else is a
                      // mapping key. The document distinguishes them, so the
                      // control has to as well.
                      list[index] = /^-?[0-9]+$/.test(next) ? new LosslessNumber(next) : next;
                      change(withKey(object ?? {}, "keys", list));
                    }}
                  />
                  <button
                    type="button"
                    className="ghostButton"
                    onClick={() => {
                      keys.removed(index);
                      const list = asArray(object?.keys).filter((_, at) => at !== index);
                      change(withKey(object ?? {}, "keys", list));
                    }}
                  >
                    Remove key {index + 1}
                  </button>
                </li>
              ))}
            </ul>
            <button
              type="button"
              className="ghostButton"
              onClick={() => {
                keys.inserted(asArray(object?.keys).length);
                change(withKey(object ?? {}, "keys", [...asArray(object?.keys), ""]));
              }}
            >
              Add key
            </button>
          </div>
        </>
      )}
      {op === "coalesce" && (
        <div className="editorField">
          <span className="editorLabel">In order</span>
          <ul className="editorRows">
            {asArray(object?.values).map((entry, index) => (
              <li key={keys.at(index)} className="editorRow">
                <ValueField
                  value={entry}
                  location={`${location}.values[${index}]`}
                  grammar={grammar}
                  label={`Option ${index + 1}`}
                  onChange={(next) => {
                    const list = [...asArray(object?.values)];
                    list[index] = next;
                    change(withKey(object ?? {}, "values", list));
                  }}
                />
                <button
                  type="button"
                  className="ghostButton"
                  onClick={() => {
                    keys.removed(index);
                    const list = asArray(object?.values).filter((_, at) => at !== index);
                    change(withKey(object ?? {}, "values", list));
                  }}
                >
                  Remove option {index + 1}
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="ghostButton"
            onClick={() => {
              keys.inserted(asArray(object?.values).length);
              change(
                withKey(object ?? {}, "values", [
                  ...asArray(object?.values),
                  { op: "literal", value: "" },
                ]),
              );
            }}
          >
            Add option
          </button>
        </div>
      )}
      {!(ops as readonly string[]).includes(op) && op !== "" && (
        <p className="flowBroken">
          {op} is not a value operation this format supports. It is kept as
          written; fix it in YAML.
        </p>
      )}
    </fieldset>
  );
}

export function StepListField({
  label,
  value,
  options,
  onChange,
  location,
}: {
  label: string;
  value: DraftValue[];
  options: string[];
  onChange: (next: DraftValue[]) => void;
  location: string;
}) {
  const keys = useRowKeys();
  return (
    <div className="editorField">
      <span className="editorLabel">{label}</span>
      <ul className="editorRows">
        {value.map((entry, index) => (
          <li key={keys.at(index)} className="editorRow">
            <SelectField
              label={`Step ${index + 1}`}
              value={asString(entry) ?? ""}
              options={options.map((name) => ({ value: name, label: name }))}
              onChange={(next) => {
                const list = [...value];
                list[index] = next;
                onChange(list);
              }}
              location={`${location}[${index}]`}
            />
            <button
              type="button"
              className="ghostButton"
              onClick={() => {
                keys.removed(index);
                onChange(value.filter((_, at) => at !== index));
              }}
            >
              Remove source {index + 1}
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="ghostButton"
        onClick={() => {
          keys.inserted(value.length);
          onChange([...value, options[0] ?? ""]);
        }}
      >
        Add source step
      </button>
      <FieldProblem location={location} />
    </div>
  );
}

// --- predicates ----------------------------------------------------------------

const PREDICATE_LABELS: Record<string, string> = {
  literal: "always or never",
  eq: "is equal to",
  ne: "is not equal to",
  lt: "is less than",
  lte: "is at most",
  gt: "is greater than",
  gte: "is at least",
  exists: "is present",
  is_type: "is of a type",
  all: "all of these",
  any: "any of these",
  not: "not this",
};

export function PredicateField({
  value,
  onChange,
  location,
  grammar,
  label = "Condition",
}: {
  value: DraftValue | undefined;
  onChange: (next: DraftValue) => void;
  location: string;
  grammar: GrammarContext;
  label?: string;
}) {
  const literalBool = typeof value === "boolean";
  const object = asObject(value);
  const op = literalBool ? "literal" : (asString(object?.op) ?? "");
  const keys = useRowKeys();
  const known = op in PREDICATE_LABELS;

  return (
    <fieldset className="editorRow" data-testid={`predicate-${location}`}>
      <legend className="editorLabel">{label}</legend>
      <SelectField
        label="Kind"
        value={op}
        options={Object.entries(PREDICATE_LABELS).map(([name, text]) => ({
          value: name,
          label: text,
        }))}
        onChange={(next) => {
          const blank = { op: "literal", value: "" };
          if (next === "literal") onChange(true);
          else if (next === "exists") onChange({ op: next, value: blank });
          else if (next === "is_type") onChange({ op: next, value: blank, type: "string" });
          else if (next === "not") onChange({ op: next, of: true });
          else if (next === "all" || next === "any") onChange({ op: next, of: [true] });
          else onChange({ op: next, left: blank, right: blank });
        }}
        location={location}
        testId={`predicate-op-${location}`}
      />
      {op === "literal" && (
        <SelectField
          label="Always true?"
          value={value === true ? "true" : "false"}
          options={[
            { value: "true", label: "always" },
            { value: "false", label: "never" },
          ]}
          onChange={(next) => onChange(next === "true")}
        />
      )}
      {(COMPARISONS as readonly string[]).includes(op) && (
        <>
          <ValueField
            value={object?.left}
            location={`${location}.left`}
            grammar={grammar}
            label="Left"
            onChange={(next) => onChange(withKey(object ?? {}, "left", next))}
          />
          <ValueField
            value={object?.right}
            location={`${location}.right`}
            grammar={grammar}
            label="Right"
            onChange={(next) => onChange(withKey(object ?? {}, "right", next))}
          />
        </>
      )}
      {(op === "exists" || op === "is_type") && (
        <ValueField
          value={object?.value}
          location={`${location}.value`}
          grammar={grammar}
          label="Value"
          onChange={(next) => onChange(withKey(object ?? {}, "value", next))}
        />
      )}
      {op === "is_type" && (
        <SelectField
          label="JSON type"
          value={asString(object?.type) ?? ""}
          options={JSON_TYPES.map((name) => ({ value: name, label: name }))}
          onChange={(next) => onChange(withKey(object ?? {}, "type", next))}
          location={`${location}.type`}
        />
      )}
      {op === "not" && (
        <PredicateField
          value={object?.of}
          location={`${location}.of`}
          grammar={grammar}
          label="Not"
          onChange={(next) => onChange(withKey(object ?? {}, "of", next))}
        />
      )}
      {(op === "all" || op === "any") && (
        <div className="editorField">
          <span className="editorLabel">{op === "all" ? "All of" : "Any of"}</span>
          <ul className="editorRows">
            {asArray(object?.of).map((entry, index) => (
              <li key={keys.at(index)} className="editorRow">
                <PredicateField
                  value={entry}
                  location={`${location}.of[${index}]`}
                  grammar={grammar}
                  label={`Condition ${index + 1}`}
                  onChange={(next) => {
                    const list = [...asArray(object?.of)];
                    list[index] = next;
                    onChange(withKey(object ?? {}, "of", list));
                  }}
                />
                <button
                  type="button"
                  className="ghostButton"
                  onClick={() => {
                    keys.removed(index);
                    onChange(
                      withKey(
                        object ?? {},
                        "of",
                        asArray(object?.of).filter((_, at) => at !== index),
                      ),
                    );
                  }}
                >
                  Remove condition {index + 1}
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="ghostButton"
            onClick={() => {
              keys.inserted(asArray(object?.of).length);
              onChange(withKey(object ?? {}, "of", [...asArray(object?.of), true]));
            }}
          >
            Add condition
          </button>
        </div>
      )}
      {!known && op !== "" && (
        <p className="flowBroken">
          {op} is not a condition this format supports. It is kept as written;
          fix it in YAML.
        </p>
      )}
    </fieldset>
  );
}

// --- text documents ------------------------------------------------------------

export function TextDocumentField({
  value,
  onChange,
  location,
  grammar,
  label,
}: {
  value: DraftValue | undefined;
  onChange: (next: DraftValue) => void;
  location: string;
  grammar: GrammarContext;
  label: string;
}) {
  const object = asObject(value) ?? {};
  const parts = asArray(object.parts);
  const keys = useRowKeys();
  const setParts = (next: DraftValue[]) => onChange(withKey(object, "parts", next));

  return (
    <fieldset className="editorSection" data-testid={`text-${location}`}>
      <legend>{label}</legend>
      <TextField
        label="Joined with"
        value={asString(object.separator) ?? ""}
        placeholder="nothing between parts"
        onChange={(next) =>
          onChange(withKey(object, "separator", next === "" ? undefined : next))
        }
        location={`${location}.separator`}
      />
      <ul className="editorRows">
        {parts.map((part, index) => {
          const entry = asObject(part) ?? {};
          const kind = "text" in entry ? "text" : "value" in entry ? "value" : "if" in entry ? "if" : "";
          const partLocation = `${location}.parts[${index}]`;
          const replace = (next: DraftValue) => {
            const list = [...parts];
            list[index] = next;
            setParts(list);
          };
          return (
            <li key={keys.at(index)} className="editorRow">
              <SelectField
                label={`Part ${index + 1}`}
                value={kind}
                options={[
                  { value: "text", label: "words you write" },
                  { value: "value", label: "a value from the run" },
                  { value: "if", label: "a conditional section" },
                ]}
                onChange={(next) => {
                  if (next === "text") replace({ text: "" });
                  else if (next === "value") replace({ value: { op: "literal", value: "" } });
                  else replace({ if: true, then: { parts: [] } });
                }}
                location={partLocation}
                testId={`part-kind-${partLocation}`}
              />
              {kind === "text" && (
                <div className="editorField">
                  <label htmlFor={`${partLocation}-text`}>Text</label>
                  <textarea
                    id={`${partLocation}-text`}
                    rows={3}
                    value={asString(entry.text) ?? ""}
                    data-testid={`part-text-${partLocation}`}
                    onChange={(event) => replace(withKey(entry, "text", event.target.value))}
                  />
                  <FieldProblem location={`${partLocation}.text`} />
                </div>
              )}
              {kind === "value" && (
                <>
                  <ValueField
                    value={entry.value}
                    location={`${partLocation}.value`}
                    grammar={grammar}
                    label="Inserts"
                    onChange={(next) => replace(withKey(entry, "value", next))}
                  />
                  <SelectField
                    label="Rendered as"
                    value={asString(entry.format) ?? "text"}
                    options={VALUE_FORMATS.map((name) => ({
                      value: name,
                      label: name === "text" ? "plain text" : "JSON",
                    }))}
                    onChange={(next) =>
                      replace(withKey(entry, "format", next === "text" ? undefined : next))
                    }
                    location={`${partLocation}.format`}
                  />
                </>
              )}
              {kind === "if" && (
                <>
                  <PredicateField
                    value={entry.if}
                    location={`${partLocation}.if`}
                    grammar={grammar}
                    label="Include when"
                    onChange={(next) => replace(withKey(entry, "if", next))}
                  />
                  <TextDocumentField
                    value={entry.then}
                    location={`${partLocation}.then`}
                    grammar={grammar}
                    label="Then say"
                    onChange={(next) => replace(withKey(entry, "then", next))}
                  />
                  <TextDocumentField
                    value={entry.else ?? { parts: [] }}
                    location={`${partLocation}.else`}
                    grammar={grammar}
                    label="Otherwise say"
                    onChange={(next) => replace(withKey(entry, "else", next))}
                  />
                </>
              )}
              <div className="editorActions">
                <button
                  type="button"
                  className="ghostButton"
                  disabled={index === 0}
                  onClick={() => {
                    keys.moved(index, index - 1);
                    const list = [...parts];
                    [list[index - 1], list[index]] = [list[index], list[index - 1]];
                    setParts(list);
                  }}
                >
                  Move part {index + 1} up
                </button>
                <button
                  type="button"
                  className="ghostButton"
                  disabled={index === parts.length - 1}
                  onClick={() => {
                    keys.moved(index, index + 1);
                    const list = [...parts];
                    [list[index + 1], list[index]] = [list[index], list[index + 1]];
                    setParts(list);
                  }}
                >
                  Move part {index + 1} down
                </button>
                <button
                  type="button"
                  className="ghostButton"
                  onClick={() => {
                    keys.removed(index);
                    setParts(parts.filter((_, at) => at !== index));
                  }}
                >
                  Remove part {index + 1}
                </button>
              </div>
            </li>
          );
        })}
      </ul>
      <div className="editorActions">
        <button
          type="button"
          className="ghostButton"
          data-testid={`add-text-part-${location}`}
          onClick={() => {
            keys.inserted(parts.length);
            setParts([...parts, { text: "" }]);
          }}
        >
          Add words
        </button>
        <button
          type="button"
          className="ghostButton"
          data-testid={`add-value-part-${location}`}
          onClick={() => {
            keys.inserted(parts.length);
            setParts([...parts, { value: { op: "literal", value: "" } }]);
          }}
        >
          Add a value
        </button>
        <button
          type="button"
          className="ghostButton"
          onClick={() => {
            keys.inserted(parts.length);
            setParts([...parts, { if: true, then: { parts: [] } }]);
          }}
        >
          Add a conditional section
        </button>
      </div>
      <FieldProblem location={`${location}.parts`} />
    </fieldset>
  );
}

// --- destinations --------------------------------------------------------------

export function DestinationField({
  value,
  onChange,
  location,
  grammar,
  label,
  allowPause = true,
}: {
  value: DraftValue | undefined;
  onChange: (next: DraftValue) => void;
  location: string;
  grammar: GrammarContext;
  label: string;
  allowPause?: boolean;
}) {
  const object = asObject(value);
  const kind =
    object === null
      ? ""
      : "step" in object
        ? "step"
        : "complete" in object
          ? "complete"
          : "pause" in object
            ? "pause"
            : "";
  return (
    <fieldset className="editorRow" data-testid={`destination-${location}`}>
      <legend className="editorLabel">{label}</legend>
      <SelectField
        label="Goes to"
        value={kind}
        options={[
          { value: "step", label: "another step" },
          {
            value: "complete",
            label: grammar.format === 1 ? "ends the run" : "a named ending",
          },
          ...(allowPause ? [{ value: "pause", label: "pauses for a person" }] : []),
        ]}
        onChange={(next) => {
          if (next === "step") onChange({ step: grammar.steps[0] ?? "" });
          else if (next === "pause") onChange({ pause: true });
          else onChange(grammar.format === 1 ? { complete: true } : { complete: true, result: "" });
        }}
        location={location}
        testId={`destination-kind-${location}`}
      />
      {kind === "step" && (
        <SelectField
          label="Step"
          value={asString(object?.step) ?? ""}
          options={grammar.steps.map((name) => ({ value: name, label: name }))}
          onChange={(next) => onChange({ ...(object ?? {}), step: next })}
          location={`${location}.step`}
          testId={`destination-step-${location}`}
        />
      )}
      {kind === "complete" && grammar.format !== 1 && (
        <TextField
          label="Ending name"
          value={asString(object?.result) ?? ""}
          placeholder="fixed, stopped, not-reproduced…"
          onChange={(next) => onChange({ complete: true, result: next })}
          location={`${location}.result`}
          testId={`destination-result-${location}`}
        />
      )}
      {kind === "pause" && (
        <p className="hint">
          The run stops and waits. Answering it is the existing retry control on
          the task, not a declared choice.
        </p>
      )}
      {kind === "" && (
        <p className="editorProblem">
          This route has no destination yet. It can be saved as a draft, but not
          as an executable revision.
        </p>
      )}
    </fieldset>
  );
}
