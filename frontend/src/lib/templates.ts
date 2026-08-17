import type { Project, Template, ThinkingLevel } from "../types";

/** Thinking levels omp accepts, in the order the selects offer them
 * (templates capability). Kept in sync with the `ThinkingLevel` union by the
 * `satisfies` check. */
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

/** Workflows registered daemon-side. Exactly `single-step` exists until
 * ROADMAP chunk 17 lands real workflows — the Settings editor select and the
 * Spawn view's read-only workflow block both render from this list, so 17
 * extends one place. */
export const REGISTERED_WORKFLOWS = [
  {
    name: "single-step",
    label: "single-step — one agent session, operator reviews from idle (default)",
    description: "one agent session, operator reviews from idle (default)",
  },
] as const;

/** The checkout a template's spawn runs against comes from its referenced
 * project (SPEC Decision 9); a project missing from state (shouldn't happen
 * — the daemon 422s unknown project names) falls back to the name. Shared by
 * the Settings list summary and the Spawn picker's option lines. */
export function templateCheckout(template: Template, projects: Project[]): string {
  return (
    projects.find((p) => p.name === template.project_name)?.checkout_path ??
    template.project_name
  );
}
