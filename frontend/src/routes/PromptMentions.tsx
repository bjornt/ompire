import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { searchProjectFiles } from "../lib/api";
import { activeMentionAt, insertMention } from "../lib/mentions";
import type { ActiveMention } from "../lib/mentions";

/** How long typing settles before the daemon is asked again. */
const QUERY_DEBOUNCE_MS = 150;

type Suggestions =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; paths: string[]; truncated: boolean }
  | { kind: "error"; reason: string };

interface Props {
  id: string;
  value: string;
  onChange: (value: string) => void;
  /** The template's project. Null disables lookup without disabling typing. */
  projectName: string | null;
  disabled: boolean;
  rows: number;
  placeholder: string;
}

/** A prompt field whose `@` opens repository-path suggestions.
 *
 * The list is advisory: it never rewrites what was typed, and every failure
 * mode leaves the field usable. Selection inserts the literal
 * `@relative/path` that the daemon stores and omp resolves in the clone. */
export function PromptMentions({
  id,
  value,
  onChange,
  projectName,
  disabled,
  rows,
  placeholder,
}: Props) {
  const listId = useId();
  const optionId = (index: number) => `${listId}-option-${index}`;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [mention, setMention] = useState<ActiveMention | null>(null);
  const [suggestions, setSuggestions] = useState<Suggestions>({ kind: "idle" });
  const [active, setActive] = useState(0);
  // Only the newest query may write results: a slow response for an older
  // prefix must not replace what the operator is looking at now.
  const requestSeq = useRef(0);
  // Escape means "not for this mention". Without remembering that, the very
  // next keyup would recompute the same mention and reopen the list.
  const dismissedAt = useRef<number | null>(null);
  // Where the caret belongs once React has rendered the inserted path. Applied
  // in a layout effect rather than a frame callback: the operator can type the
  // next word immediately, and a deferred caret move would land mid-word.
  const pendingCaret = useRef<number | null>(null);

  const close = useCallback(() => {
    setMention(null);
    setSuggestions({ kind: "idle" });
    setActive(0);
    requestSeq.current += 1;
  }, []);

  // A new template means a different repository; stale paths must not linger.
  useEffect(() => close(), [projectName, close]);
  useEffect(() => {
    if (disabled) close();
  }, [disabled, close]);

  function syncMention(nextValue: string, caret: number) {
    if (disabled || projectName === null) return;
    const next = activeMentionAt(nextValue, caret);
    if (next === null) {
      // Moving off the mention entirely also retires its dismissal.
      dismissedAt.current = null;
      setMention(null);
      return;
    }
    setMention(dismissedAt.current === next.at ? null : next);
  }

  useEffect(() => {
    if (mention === null || projectName === null) return;
    const seq = ++requestSeq.current;
    setSuggestions((current) => (current.kind === "ready" ? current : { kind: "loading" }));
    const timer = setTimeout(() => {
      searchProjectFiles(projectName, mention.query)
        .then((result) => {
          if (seq !== requestSeq.current) return;
          setSuggestions({ kind: "ready", paths: result.paths, truncated: result.truncated });
          setActive(0);
        })
        .catch((error: unknown) => {
          if (seq !== requestSeq.current) return;
          setSuggestions({
            kind: "error",
            reason: error instanceof Error ? error.message : String(error),
          });
        });
    }, QUERY_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [mention, projectName]);

  const paths = suggestions.kind === "ready" ? suggestions.paths : [];
  const open = mention !== null && suggestions.kind !== "idle";

  function choose(path: string) {
    const textarea = textareaRef.current;
    if (mention === null || textarea === null) return;
    const next = insertMention(value, mention, textarea.selectionStart, path);
    pendingCaret.current = next.caret;
    onChange(next.value);
    close();
  }

  useLayoutEffect(() => {
    const caret = pendingCaret.current;
    const textarea = textareaRef.current;
    if (caret === null || textarea === null) return;
    pendingCaret.current = null;
    textarea.focus();
    textarea.setSelectionRange(caret, caret);
  }, [value]);

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (!open) return;
    if (event.key === "Escape") {
      // Dismissing leaves the typed `@…` exactly as written.
      event.preventDefault();
      if (mention !== null) dismissedAt.current = mention.at;
      close();
      return;
    }
    if (paths.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (index + 1) % paths.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (index - 1 + paths.length) % paths.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      choose(paths[active]);
    }
  }

  return (
    <div className="mentionField">
      <textarea
        id={id}
        ref={textareaRef}
        className="mono"
        rows={rows}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && paths.length > 0 ? optionId(active) : undefined}
        onChange={(event) => {
          onChange(event.target.value);
          syncMention(event.target.value, event.target.selectionStart);
        }}
        onKeyDown={onKeyDown}
        onKeyUp={(event) => syncMention(value, event.currentTarget.selectionStart)}
        onClick={(event) => syncMention(value, event.currentTarget.selectionStart)}
        onBlur={close}
      />
      {open && (
        <div className="mentionPopup" data-testid="mention-popup">
          {suggestions.kind === "loading" && <div className="mentionNote">Searching…</div>}
          {suggestions.kind === "error" && (
            <div className="mentionNote mentionError" role="alert">
              Could not search files — {suggestions.reason}
            </div>
          )}
          {suggestions.kind === "ready" && paths.length === 0 && (
            <div className="mentionNote">No matching files</div>
          )}
          {paths.length > 0 && (
            <ul className="mentionList" id={listId} role="listbox" aria-label="Repository files">
              {paths.map((path, index) => (
                <li
                  key={path}
                  id={optionId(index)}
                  role="option"
                  aria-selected={index === active}
                  className={index === active ? "mentionOption active" : "mentionOption"}
                  // Blur would close the list before the click landed.
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => choose(path)}
                  onMouseEnter={() => setActive(index)}
                >
                  {path}
                </li>
              ))}
            </ul>
          )}
          {suggestions.kind === "ready" && suggestions.truncated && (
            <div className="mentionNote">More matches — keep typing to narrow</div>
          )}
        </div>
      )}
      <div className="srOnly" role="status" aria-live="polite">
        {open && suggestions.kind === "ready"
          ? `${paths.length} file suggestion${paths.length === 1 ? "" : "s"}`
          : ""}
      </div>
    </div>
  );
}
