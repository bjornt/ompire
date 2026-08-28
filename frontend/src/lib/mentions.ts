/** Parsing and insertion for `@file` mentions in the spawn prompt.
 *
 * The daemon owns the authoritative mention rule — it is what refuses a
 * mention at submit and what re-resolves one against the clone at delivery.
 * These helpers only have to agree with it about where a mention starts, so
 * that what the operator selects is what the daemon later parses. */

export interface ActiveMention {
  /** Index of the `@` that opened this mention. */
  at: number;
  /** The characters typed after it, which are the query. */
  query: string;
}

/** The mention being typed at `caret`, or null.
 *
 * A mention starts at a word boundary, so `someone@example.com` is an email
 * address and not a file reference — the same rule the daemon and omp both
 * apply. */
export function activeMentionAt(value: string, caret: number): ActiveMention | null {
  let start = caret;
  while (start > 0 && !/\s/.test(value[start - 1])) start -= 1;
  if (value[start] !== "@") return null;
  const token = value.slice(start + 1, caret);
  if (/\s/.test(token)) return null;
  return { at: start, query: token };
}

/** Replace the mention being typed with `@path`, leaving the rest alone.
 *
 * A separating space follows the path so the next word cannot run into it,
 * unless the text already continues with whitespace — inserting mid-sentence
 * should not leave a double space behind. */
export function insertMention(
  value: string,
  mention: ActiveMention,
  caret: number,
  path: string,
): { value: string; caret: number } {
  const rest = value.slice(caret);
  const inserted = `@${path}` + (/^\s/.test(rest) ? "" : " ");
  return {
    value: value.slice(0, mention.at) + inserted + rest,
    caret: mention.at + inserted.length,
  };
}
