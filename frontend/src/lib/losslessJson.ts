import { LosslessNumber, isLosslessNumber, parse, stringify } from "lossless-json";

/** JSON that does not round a number on the way through.
 *
 * A workflow definition is executable data, and `{op: literal, value: …}` may
 * carry any integer a Python daemon accepted. Two of those survive ordinary
 * `JSON.parse`/`JSON.stringify` badly: an integer past 2^53 loses digits, and
 * `1.0` comes back as `1`. Neither is cosmetic — the canonical bytes of a
 * definition are what its revision identity is taken over, so a client that
 * re-serializes a document through the ordinary codec can hand the daemon a
 * *different* procedure than the one it was shown.
 *
 * So definitions, drafts, and validation responses cross the wire through
 * this codec instead, where a number keeps its exact source token until
 * somebody edits it. Everything else the daemon sends — tasks, sessions,
 * settings — keeps using ordinary JSON: those are ordinary numbers, and a
 * `LosslessNumber` where a view expects a `number` would be a new bug class
 * for no benefit.
 */

export { LosslessNumber, isLosslessNumber };

export function parseLossless(text: string): unknown {
  return parse(text) as unknown;
}

export function stringifyLossless(value: unknown): string {
  return stringify(value) ?? "null";
}

/** The exact source token of a number, or null if this is not one.
 *
 * `1.0` and `1` are different tokens on purpose: a form control shows and
 * keeps what the author wrote, and only an actual edit changes it.
 */
export function numberToken(value: unknown): string | null {
  if (isLosslessNumber(value)) return value.toString();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

/** A checked conversion to a JavaScript number, for layout and indexing only.
 *
 * Returns null rather than a rounded approximation when the token cannot be
 * represented exactly, so no caller can quietly compute with a value that is
 * not the one in the document.
 */
function exactNumber(value: unknown): number | null {
  const token = numberToken(value);
  if (token === null) return null;
  const parsed = Number(token);
  if (!Number.isFinite(parsed)) return null;
  // An integer past 2^53 does not survive the conversion, so this is not that
  // number and saying so is the only honest answer.
  if (Number.isInteger(parsed) && !Number.isSafeInteger(parsed)) return null;
  return parsed;
}

/** A number the daemon guarantees is small — a format version, a line number.
 *
 * Used for envelope fields only, never for anything inside a definition. */
export function envelopeNumber(value: unknown): number {
  const parsed = exactNumber(value);
  return parsed ?? 0;
}
