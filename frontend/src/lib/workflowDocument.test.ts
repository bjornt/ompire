import { describe, expect, it } from "vitest";
import { parse as parseLossless } from "lossless-json";
import {
  fallThrough,
  moveStep,
  referencesTo,
  renameEvidenceAlias,
  renameSession,
  renameStep,
  type DraftObject,
} from "./workflowDocument";
import { isBounded, readFlow } from "./workflowFlow";

/** Structural edits on a draft document.
 *
 * The bug class these defend against is a rename that becomes a search and
 * replace. A step called `verify` may also be the word "verify" inside an
 * instruction, inside a literal payload, or inside a result name — and only
 * one of those four is a reference. Rewriting the others silently edits what
 * an agent is told to do, which is the kind of change nobody reviews because
 * nobody expects a rename to make it.
 */

function draft(): DraftObject {
  return parseLossless(
    JSON.stringify({
      format: 2,
      name: "bugfix",
      sessions: ["qa", "dev"],
      primary: "qa",
      steps: [
        {
          name: "reproduce",
          kind: "agent",
          session: "qa",
          outcome: { results: { reproduce: { required: {} } } },
          prompt: {
            parts: [
              { text: "Try to reproduce. Then reproduce it again." },
              { value: { op: "literal", value: { note: "reproduce" } } },
            ],
          },
        },
        {
          name: "diagnose",
          kind: "agent",
          session: "dev",
          outcome: null,
          evidence: { attempt: { steps: ["reproduce"], after: "reproduce" } },
          prompt: { parts: [{ value: { op: "evidence", name: "attempt" } }] },
        },
        {
          name: "route",
          kind: "decision",
          cases: [
            {
              when: { op: "gt", left: { op: "count", step: "reproduce" }, right: { op: "literal", value: 2 } },
              next: { step: "reproduce" },
            },
          ],
          otherwise: { complete: true, result: "reproduce" },
        },
      ],
    }),
  ) as DraftObject;
}

describe("renaming a step", () => {
  it("moves every structural reference and nothing that merely matches", () => {
    const { document, error } = renameStep(draft(), "reproduce", "repro");
    expect(error).toBeNull();
    const steps = document.steps as DraftObject[];

    expect(steps[0].name).toBe("repro");
    expect((steps[1].evidence as DraftObject).attempt).toMatchObject({
      steps: ["repro"],
      after: "repro",
    });
    const first = (steps[2].cases as DraftObject[])[0];
    expect((first.when as DraftObject).left).toMatchObject({ op: "count", step: "repro" });
    expect(first.next).toEqual({ step: "repro" });

    // The instruction, the literal payload, and the declared result name all
    // still say "reproduce". None of them is a reference to the step.
    const parts = (steps[0].prompt as DraftObject).parts as DraftObject[];
    expect(parts[0].text).toContain("Try to reproduce. Then reproduce it again.");
    expect((parts[1].value as DraftObject).value).toEqual({ note: "reproduce" });
    expect(Object.keys((steps[0].outcome as DraftObject).results as DraftObject)).toEqual([
      "reproduce",
    ]);
    expect(steps[2].otherwise).toEqual({ complete: true, result: "reproduce" });
  });

  it("refuses when the old name is ambiguous rather than guessing", () => {
    const document = draft();
    (document.steps as DraftObject[])[1].name = "reproduce";
    const { document: unchanged, error } = renameStep(document, "reproduce", "repro");
    expect(error).toContain("Two steps are called");
    expect(unchanged).toBe(document);
  });
});

describe("renaming an agent", () => {
  it("moves the declaration, the primary marker, and every assignment", () => {
    const { document, error } = renameSession(draft(), "qa", "tester");
    expect(error).toBeNull();
    expect(document.sessions).toEqual(["tester", "dev"]);
    expect(document.primary).toBe("tester");
    expect((document.steps as DraftObject[])[0].session).toBe("tester");
    expect((document.steps as DraftObject[])[1].session).toBe("dev");
  });
});

describe("renaming an evidence alias", () => {
  it("moves the reads inside that step only", () => {
    const document = renameEvidenceAlias(draft(), 1, "attempt", "first-try");
    const step = (document.steps as DraftObject[])[1];
    expect(Object.keys(step.evidence as DraftObject)).toEqual(["first-try"]);
    expect(((step.prompt as DraftObject).parts as DraftObject[])[0].value).toEqual({
      op: "evidence",
      name: "first-try",
    });
    // The step that declares nothing is untouched.
    expect((document.steps as DraftObject[])[0].evidence).toBeUndefined();
  });
});

describe("listing what names a step", () => {
  it("finds each structural site and says what it does", () => {
    const found = referencesTo(draft(), "step", "reproduce");
    const locations = found.map((reference) => reference.location);
    expect(locations).toContain("steps[1].evidence.attempt.steps[0]");
    expect(locations).toContain("steps[1].evidence.attempt.after");
    expect(locations).toContain("steps[2].cases[0].when.left.step");
    expect(locations).toContain("steps[2].cases[0].next.step");
    expect(found.every((reference) => reference.what !== "")).toBe(true);
    // The declared result of the same name is not a reference to the step.
    expect(locations.some((location) => location.includes("otherwise"))).toBe(false);
  });
});

describe("moving a card", () => {
  it("changes which step the moved one falls through to", () => {
    const before = draft();
    expect(fallThrough(before, 0)).toBe("diagnose");
    const after = moveStep(before, 0, 1);
    expect(fallThrough(after, 0)).toBe("reproduce");
    expect(fallThrough(after, 1)).toBe("route");
    // Order is the only thing that changed; no route was rewritten.
    expect((after.steps as DraftObject[])[2]).toEqual((before.steps as DraftObject[])[2]);
  });
});

describe("reading a canonical definition", () => {
  it("does not invent a visit bound out of a spelled-out default", () => {
    // A canonical document fills in every default, so an unbounded step
    // carries `max_visits: null`. Reading that as a declared bound drew an
    // exhaustion edge to nowhere on every ordinary step — an invented route
    // in a diagram is exactly the failure a diagram must not have.
    const canonical = parseLossless(
      JSON.stringify({
        format: 2,
        name: "canonical",
        sessions: ["main"],
        primary: "main",
        steps: [
          {
            name: "work",
            kind: "agent",
            session: "main",
            role: "default",
            when: true,
            evidence: {},
            outcome: null,
            max_visits: null,
            on_exhausted: null,
            prompt: { separator: "", parts: [{ text: "do it" }] },
          },
          {
            name: "again",
            kind: "agent",
            session: "main",
            role: "default",
            when: true,
            evidence: {},
            outcome: null,
            max_visits: 2,
            on_exhausted: { step: "work" },
            prompt: { separator: "", parts: [{ text: "again" }] },
          },
        ],
      }),
    ) as DraftObject;

    const steps = canonical.steps as DraftObject[];
    expect(isBounded(steps[0])).toBe(false);
    expect(isBounded(steps[1])).toBe(true);

    const flow = readFlow(canonical);
    expect(flow.edges.filter((edge) => edge.kind === "exhausted")).toHaveLength(1);
    expect(flow.edges.find((edge) => edge.kind === "exhausted")?.from).toBe("again");
  });
});
