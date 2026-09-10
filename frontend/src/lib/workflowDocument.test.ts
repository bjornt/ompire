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
import {
  declaredActions,
  isBounded,
  readDeliveryAction,
  readDeliveryGate,
  readFlow,
  unknownStepFields,
} from "./workflowFlow";

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

/** Format 3's references: the ones that carry authority.
 *
 * A grant names actions, a delivery step names the approval that can permit
 * it and the result it consumes, and a delivery gate names the review its
 * grant rests on. Each of those is a *reference*, so a rename has to move it —
 * and a rename that missed one would leave an answer authorizing a step that
 * no longer exists, which is exactly the state validation exists to catch and
 * exactly the state an editor should never create.
 */
function deliveringDraft(): DraftObject {
  return parseLossless(
    JSON.stringify({
      format: 3,
      name: "publisher",
      sessions: ["main"],
      primary: "main",
      steps: [
        { name: "work", kind: "agent", session: "main", outcome: null, prompt: { parts: [] } },
        { name: "review", kind: "review", evidence: { work: { steps: ["work"] } } },
        {
          name: "approve",
          kind: "gate",
          evidence: { verdict: { steps: ["review"] } },
          delivery: {
            review: "verdict",
            metadata: {
              pr_title: { parts: [{ value: { op: "evidence", name: "verdict" } }] },
            },
          },
          message: { parts: [{ text: "Publish?" }] },
          choices: [
            {
              id: "publish",
              label: "Publish",
              next: { step: "commit" },
              authorize: { steps: ["commit", "push"] },
            },
            { id: "finish", label: "Finish", next: { complete: true, result: "done" } },
          ],
        },
        {
          name: "commit",
          kind: "delivery",
          action: "commit",
          mode: "squash",
          approval: "approve",
          next: { step: "push" },
        },
        {
          name: "push",
          kind: "delivery",
          action: "push",
          previous: "commit",
          approval: "approve",
          next: { complete: true, result: "published" },
        },
      ],
    }),
  ) as DraftObject;
}

describe("format-3 authority references", () => {
  it("lists every place a delivery step is referred to", () => {
    const references = referencesTo(deliveringDraft(), "step", "commit");
    expect(references.map((r) => r.location).sort()).toEqual([
      "steps[2].choices[0].authorize.steps[0]",
      "steps[2].choices[0].next.step",
      "steps[4].previous",
    ]);
    expect(references.map((r) => r.what)).toContain(
      "is an action this answer authorizes",
    );
  });

  it("renames a delivery step through its grant, its approval, and its chain", () => {
    const { document, error } = renameStep(deliveringDraft(), "commit", "sign");
    expect(error).toBeNull();
    const steps = document.steps as DraftObject[];
    const gate = steps[2] as DraftObject;
    const choice = (gate.choices as DraftObject[])[0];
    expect((choice.authorize as DraftObject).steps).toEqual(["sign", "push"]);
    expect((choice.next as DraftObject).step).toBe("sign");
    expect((steps[3] as DraftObject).name).toBe("sign");
    expect((steps[4] as DraftObject).previous).toBe("sign");
  });

  it("renames the approval a delivery step names", () => {
    const { document } = renameStep(deliveringDraft(), "approve", "decide");
    const steps = document.steps as DraftObject[];
    expect((steps[3] as DraftObject).approval).toBe("decide");
    expect((steps[4] as DraftObject).approval).toBe("decide");
  });

  it("renames the evidence alias a delivery gate is bound to", () => {
    const renamed = renameEvidenceAlias(deliveringDraft(), 2, "verdict", "graded");
    const gate = (renamed.steps as DraftObject[])[2];
    expect((gate.delivery as DraftObject).review).toBe("graded");
    expect(Object.keys(gate.evidence as DraftObject)).toEqual(["graded"]);
    // And the suggested text that reads it.
    const metadata = (gate.delivery as DraftObject).metadata as DraftObject;
    const parts = (metadata.pr_title as DraftObject).parts as DraftObject[];
    expect(((parts[0] as DraftObject).value as DraftObject).name).toBe("graded");
  });

  it("leaves a deleted delivery step's references visibly dangling", () => {
    // Deleting the card does not repair the grant into something the author
    // never asked for; the reference stays, and the daemon refuses the save.
    const before = deliveringDraft();
    const references = referencesTo(before, "step", "push");
    expect(references.length).toBeGreaterThan(0);
    expect(references.map((r) => r.what)).toContain(
      "is an action this answer authorizes",
    );
  });
});

describe("format-3 flow reading", () => {
  it("distinguishes an authorizing answer from an ordinary one", () => {
    const flow = readFlow(deliveringDraft());
    const fromGate = flow.edges.filter((edge) => edge.from === "approve");
    expect(fromGate.map((edge) => edge.kind)).toEqual(["authorize", "choice"]);
    expect(fromGate[0].label).toContain("authorizing commit → push");
    const fromCommit = flow.edges.filter((edge) => edge.from === "commit");
    expect(fromCommit.map((edge) => edge.kind)).toEqual(["delivered"]);
    expect(fromCommit[0].to).toEqual({ kind: "step", step: "push", index: 4 });
  });

  it("says plainly which effects a document can perform, and when it can perform none", () => {
    expect(declaredActions(deliveringDraft())).toEqual(["commit", "push"]);
    expect(declaredActions(draft())).toEqual([]);
  });

  it("names a delivery step's own fields rather than calling them unknown", () => {
    const steps = deliveringDraft().steps as DraftObject[];
    expect(unknownStepFields(steps[3] as DraftObject)).toEqual([]);
    expect(unknownStepFields(steps[1] as DraftObject)).toEqual([]);
    expect(unknownStepFields(steps[2] as DraftObject)).toEqual([]);
    const action = readDeliveryAction(deliveringDraft(), steps[4] as DraftObject);
    expect(action).toEqual({
      action: "push",
      mode: null,
      approval: "approve",
      previous: "commit",
      next: { kind: "complete", result: "published" },
    });
    const gate = readDeliveryGate(steps[2] as DraftObject);
    expect(gate?.review).toBe("verdict");
    expect(gate?.metadata.map((entry) => entry.field)).toEqual(["pr_title"]);
  });
});

describe("format 4 capture and result gates", () => {
  it("keeps their explicit route and evidence bindings structural", () => {
    const document = parseLossless(
      JSON.stringify({
        format: 4,
        name: "planning",
        sessions: ["planner"],
        primary: "planner",
        steps: [
          { name: "propose", kind: "agent", session: "planner", outcome: null, prompt: { parts: [] } },
          {
            name: "capture",
            kind: "capture",
            evidence: { producer: { steps: ["propose"] } },
            producer: "producer",
            paths: [{ parts: [{ text: "changes/example/PLAN.md" }] }],
            allowlist: ["changes"],
            next: { step: "decide" },
          },
          {
            name: "decide",
            kind: "gate",
            evidence: { captured: { steps: ["capture"] } },
            result: { evidence: "captured" },
            message: { parts: [] },
            choices: [],
          },
        ],
      }),
    ) as DraftObject;

    const renamed = renameEvidenceAlias(document, 2, "captured", "result");
    expect((renamed.steps as DraftObject[])[2].result).toEqual({ evidence: "result" });
    expect(referencesTo(document, "step", "decide").map((reference) => reference.location)).toContain(
      "steps[1].next.step",
    );
    expect(readFlow(document).edges.find((edge) => edge.from === "capture")).toMatchObject({
      kind: "captured",
      to: { kind: "step", step: "decide" },
    });
  });
});
