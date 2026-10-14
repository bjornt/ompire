import { describe, expect, it } from "vitest";
import type { EnvironmentState } from "../types";
import { environmentPresentation, formatEnvironmentCommand } from "./environmentPresentation";

describe("environmentPresentation", () => {
  it.each<{ state: EnvironmentState; dot: string; condition: RegExp }>([
    { state: "unknown", dot: "var(--faint)", condition: /not been checked yet/i },
    { state: "ready", dot: "var(--green)", condition: /check passed/i },
    { state: "degraded", dot: "var(--amber)", condition: /did not demonstrate readiness/i },
    { state: "missing", dot: "var(--red)", condition: /program or script is missing/i },
    { state: "error", dot: "var(--red)", condition: /check could not run/i },
  ])("describes $state without implying provisioning or enforcement", ({ state, dot, condition }) => {
    const presentation = environmentPresentation(state);
    expect(presentation.label).toBe(state);
    expect(presentation.dot).toBe(dot);
    expect(presentation.description).toMatch(condition);
    expect(presentation.description).not.toMatch(/grant|launch|automatically|provisioning/i);
    if (state !== "ready" && state !== "unknown") {
      expect(presentation.description).toContain("re-check");
    }
  });
});

describe("formatEnvironmentCommand", () => {
  it("keeps ordinary command names, flags and absolute paths readable", () => {
    expect(formatEnvironmentCommand(["/qa/setup.sh", "--host=vm", "dogfood-1"])).toBe(
      "/qa/setup.sh --host=vm dogfood-1",
    );
  });

  it("distinguishes argument boundaries, empty arguments and shell metacharacters", () => {
    expect(formatEnvironmentCommand([
      "/qa/setup env.sh", "two words", "", "$HOME", "a;b", "*.toml", "a\\b", 'say "ready"',
    ])).toBe(`'/qa/setup env.sh' 'two words' '' '$HOME' 'a;b' '*.toml' 'a\\b' 'say "ready"'`);
    expect(formatEnvironmentCommand(["setup", "a b"])).not.toBe(
      formatEnvironmentCommand(["setup", "a", "b"]),
    );
  });

  it("preserves literal apostrophes, tabs and newlines within quoted arguments", () => {
    expect(formatEnvironmentCommand(["setup", "operator's vm", "one\ttwo", "one\ntwo"])).toBe(
      `setup 'operator'"'"'s vm' 'one\ttwo' 'one\ntwo'`,
    );
  });
});
