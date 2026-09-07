import { useCallback, useEffect, useRef, useState } from "react";
import { convertWorkflowDocument } from "./api";
import { stringifyLossless } from "./losslessJson";
import type { DraftObject } from "./workflowDocument";
import type { WorkflowDocumentConversion, WorkflowDraftValidation } from "../types";

/** The editor's working copy, in exactly one representation at a time.
 *
 * The failure this exists to prevent is two mutable copies of the same draft.
 * An editor holding both text and a parsed model has to answer "which one is
 * the truth" on every save, and gets it wrong the first time somebody edits
 * one, switches modes, and saves — silently discarding whichever half lost.
 *
 * So there is one: `text` is the draft until a *visual* edit happens, and
 * `document` is the draft after that. Serialization happens when something
 * needs the text — switching back, saving, validating, downloading — and it
 * serializes the exact generation being asked about, never an older one that
 * happened to answer later.
 *
 * Nothing here is persisted. There is no editor model in SQLite, no layout in
 * an executable definition, and no local-storage copy of somebody's draft: a
 * refresh restores what was *saved*, which is the only thing the daemon ever
 * promised to keep.
 */

export type DraftMode = "yaml" | "visual";

/** A conversion answer, tied to the edit it describes. */
interface Serialized {
  generation: number;
  yaml: string;
  validation: WorkflowDraftValidation;
}

export interface WorkflowDraft {
  mode: DraftMode;
  /** Bumped by every edit. Anything that describes the draft — a validation
   * answer, a serialization — is only current while this has not moved. */
  generation: number;
  /** The text form of the loaded draft, or of the latest serialized edit. */
  text: string;
  /** The structured draft, once visual mode has parsed the text. */
  document: DraftObject | null;
  /** The daemon's reading of the current generation, when it has answered. */
  validation: WorkflowDraftValidation | null;
  /** True when a visual edit has moved ahead of `text`. */
  visualChanged: boolean;
  /** A conversion that failed. The structured work is kept, not replaced. */
  conversionError: string | null;
  /** True while the current generation has no answer yet. */
  converting: boolean;
}

const EMPTY: WorkflowDraft = {
  mode: "yaml",
  generation: 0,
  text: "",
  document: null,
  validation: null,
  visualChanged: false,
  conversionError: null,
  converting: false,
};

export interface WorkflowDraftApi extends WorkflowDraft {
  /** Replace the loaded draft — a fresh entry, a reload, an import. */
  reset: (text: string) => void;
  /** A YAML-mode edit. */
  setText: (text: string) => void;
  /** A visual-mode edit. */
  edit: (update: (document: DraftObject) => DraftObject) => void;
  enterVisual: () => Promise<void>;
  leaveVisual: () => Promise<void>;
  /** Ask the daemon about the current generation again. */
  revalidate: () => Promise<void>;
  /** The exact text of the current generation, or null when it cannot be
   * produced. Never a stale serialization of an older edit. */
  currentText: () => Promise<string | null>;
  /** Text to hand the operator when everything else has failed. JSON is
   * valid YAML, so this imports again even though it is not what they typed. */
  fallbackText: () => string | null;
}

export function useWorkflowDraft(entry: string): WorkflowDraftApi {
  const [state, setState] = useState<WorkflowDraft>(EMPTY);
  /** Monotonic per edit. An answer for an older generation is dropped rather
   * than shown beside newer work. */
  const generation = useRef(0);
  const documentRef = useRef<DraftObject | null>(null);
  const serialized = useRef<Serialized | null>(null);
  /** The entry these answers belong to. Switching workflows invalidates every
   * request in flight, not just the older ones. */
  const entryRef = useRef(entry);

  useEffect(() => {
    entryRef.current = entry;
    generation.current += 1;
    documentRef.current = null;
    serialized.current = null;
    setState({ ...EMPTY, generation: generation.current });
  }, [entry]);

  const reset = useCallback((text: string) => {
    generation.current += 1;
    documentRef.current = null;
    serialized.current = null;
    setState({ ...EMPTY, text, generation: generation.current });
  }, []);

  const setText = useCallback((text: string) => {
    generation.current += 1;
    documentRef.current = null;
    serialized.current = null;
    setState((current) => ({
      ...current,
      mode: "yaml",
      generation: generation.current,
      text,
      document: null,
      validation: null,
      visualChanged: false,
      conversionError: null,
      converting: false,
    }));
  }, []);

  /** One conversion, accepted only if it still describes the current edit of
   * the current entry. */
  const convert = useCallback(
    async (
      input: { yaml?: string; document?: DraftObject },
      forGeneration: number,
    ): Promise<WorkflowDocumentConversion | null> => {
      const forEntry = entryRef.current;
      try {
        const answer = await convertWorkflowDocument({ ...input, name: forEntry });
        if (generation.current !== forGeneration || entryRef.current !== forEntry) {
          return null;
        }
        return answer;
      } catch (error) {
        if (generation.current !== forGeneration || entryRef.current !== forEntry) {
          return null;
        }
        throw error;
      }
    },
    [],
  );

  const stateTextRef = useRef("");
  useEffect(() => {
    stateTextRef.current = state.text;
  }, [state.text]);

  const enterVisual = useCallback(async () => {
    const forGeneration = generation.current;
    setState((current) => ({ ...current, converting: true, conversionError: null }));
    let answer: WorkflowDocumentConversion | null = null;
    let failure: string | null = null;
    try {
      answer = await convert({ yaml: stateTextRef.current }, forGeneration);
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    }
    if (generation.current !== forGeneration) return;
    if (answer === null) {
      // The text stays exactly as typed. Visual mode is refused, not
      // substituted with an empty or last-valid document.
      setState((current) => ({ ...current, converting: false, conversionError: failure }));
      return;
    }
    documentRef.current = answer.document;
    serialized.current = {
      generation: forGeneration,
      yaml: answer.yaml,
      validation: answer.validation,
    };
    setState((current) => ({
      ...current,
      mode: "visual",
      document: answer.document,
      validation: answer.validation,
      converting: false,
      conversionError: null,
    }));
  }, [convert]);

  const serializeCurrent = useCallback(async (): Promise<string | null> => {
    const forGeneration = generation.current;
    const cached = serialized.current;
    if (cached !== null && cached.generation === forGeneration) return cached.yaml;
    const document = documentRef.current;
    if (document === null) return stateTextRef.current;
    setState((current) => ({ ...current, converting: true }));
    let answer: WorkflowDocumentConversion | null = null;
    let failure: string | null = null;
    try {
      answer = await convert({ document }, forGeneration);
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    }
    if (generation.current !== forGeneration) return null;
    if (answer === null) {
      setState((current) => ({ ...current, converting: false, conversionError: failure }));
      return null;
    }
    serialized.current = {
      generation: forGeneration,
      yaml: answer.yaml,
      validation: answer.validation,
    };
    setState((current) => ({
      ...current,
      text: answer.yaml,
      validation: answer.validation,
      converting: false,
      conversionError: null,
    }));
    return answer.yaml;
  }, [convert]);

  const edit = useCallback((update: (document: DraftObject) => DraftObject) => {
    const current = documentRef.current;
    if (current === null) return;
    const next = update(current);
    generation.current += 1;
    documentRef.current = next;
    setState((state) => ({
      ...state,
      generation: generation.current,
      document: next,
      visualChanged: true,
      conversionError: null,
    }));
  }, []);

  // A visual edit asks the daemon what it now means, once the operator pauses.
  // The answer carries both the text and the validation for that exact
  // generation, so saving after it lands costs no second round trip.
  useEffect(() => {
    if (state.mode !== "visual" || !state.visualChanged) return;
    const forGeneration = generation.current;
    if (serialized.current?.generation === forGeneration) return;
    const timer = setTimeout(() => void serializeCurrent(), 350);
    return () => clearTimeout(timer);
  }, [state.mode, state.visualChanged, state.document, serializeCurrent]);

  const leaveVisual = useCallback(async () => {
    if (!state.visualChanged) {
      // Nothing was changed, so the operator's own text is still the draft.
      setState((current) => ({ ...current, mode: "yaml" }));
      return;
    }
    const text = await serializeCurrent();
    if (text === null) return;
    setState((current) => ({ ...current, mode: "yaml", text }));
  }, [serializeCurrent, state.visualChanged]);

  const revalidate = useCallback(async () => {
    const forGeneration = generation.current;
    const document = documentRef.current;
    let answer: WorkflowDocumentConversion | null = null;
    let failure: string | null = null;
    setState((current) => ({ ...current, converting: true, conversionError: null }));
    try {
      answer =
        document === null
          ? await convert({ yaml: stateTextRef.current }, forGeneration)
          : await convert({ document }, forGeneration);
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    }
    if (generation.current !== forGeneration) return;
    if (answer === null) {
      setState((current) => ({ ...current, converting: false, conversionError: failure }));
      return;
    }
    serialized.current = {
      generation: forGeneration,
      yaml: answer.yaml,
      validation: answer.validation,
    };
    setState((current) => ({
      ...current,
      text: document === null ? current.text : answer.yaml,
      validation: answer.validation,
      converting: false,
      conversionError: null,
    }));
  }, [convert]);

  const currentText = useCallback(async () => {
    if (!state.visualChanged) return state.text;
    return serializeCurrent();
  }, [serializeCurrent, state.text, state.visualChanged]);

  const fallbackText = useCallback(() => {
    const document = documentRef.current;
    return document === null ? null : stringifyLossless(document);
  }, []);

  return {
    ...state,
    reset,
    setText,
    edit,
    enterVisual,
    leaveVisual,
    revalidate,
    currentText,
    fallbackText,
  };
}
