import type { ModelRole, ThinkingLevel } from "../types";

/** Thinking levels omp accepts, in the order every select offers them. Since
 * the template retirement (ADR-0026) the model-profile editor is the only
 * consumer, and it always requires a level: a role binding is a model and a
 * thinking level together, with no "leave it unset" case left to support.
 * Kept in sync with the `ThinkingLevel` union by the `satisfies` check. */
export const THINKING_LEVELS = [
  "off",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "auto",
] as const satisfies readonly ThinkingLevel[];

/** The four roles a profile binds (ADR-0025), with what each is for. Fixed:
 * the editor renders exactly these rows, in this order. */
export const MODEL_ROLES: { role: ModelRole; hint: string }[] = [
  { role: "default", hint: "ordinary active agent" },
  { role: "smol", hint: "lightweight work" },
  { role: "slow", hint: "thorough reasoning" },
  { role: "plan", hint: "planning" },
];

/** Provider-qualified examples shown beside the model inputs. Illustrative
 * only — the daemon validates the shape and never checks that a provider,
 * credential, or model actually exists. */
export const MODEL_PLACEHOLDERS: Record<ModelRole, string> = {
  default: "anthropic/claude-sonnet-4.5",
  smol: "openai/gpt-4.1-mini",
  slow: "openai/o3",
  plan: "google/gemini-2.5-pro",
};
