import type { EnvironmentState } from "../types";

type EnvironmentPresentation = {
  dot: string;
  label: string;
  description: string;
};

/** Readiness is observed by the daemon, never inferred from declared capabilities. */
export function environmentPresentation(state: EnvironmentState): EnvironmentPresentation {
  switch (state) {
    case "ready":
      return {
        dot: "var(--green)",
        label: "ready",
        description: "The environment check passed.",
      };
    case "degraded":
      return {
        dot: "var(--amber)",
        label: "degraded",
        description: "The environment check did not demonstrate readiness. Resolve the blockers, then re-check.",
      };
    case "missing":
      return {
        dot: "var(--red)",
        label: "missing",
        description: "A declared program or script is missing. Restore it, then re-check.",
      };
    case "error":
      return {
        dot: "var(--red)",
        label: "error",
        description: "The environment check could not run. Resolve the reported error, then re-check.",
      };
    case "unknown":
      return {
        dot: "var(--faint)",
        label: "unknown",
        description: "The environment has not been checked yet.",
      };
  }
}

/** Shell-readable argv: quoted arguments retain their boundaries and literal characters. */
export function formatEnvironmentCommand(argv: string[]): string {
  return argv
    .map((arg) => /^[a-zA-Z0-9_./:@%+=,-]+$/.test(arg) ? arg : `'${arg.replaceAll("'", `'"'"'`)}'`)
    .join(" ");
}
