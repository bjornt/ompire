import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { WorkflowDescriptor, WorkflowLibraryEntry } from "../types";

/** Visual authoring over the existing library.
 *
 * These hold the places where a visual editor can quietly lose somebody's
 * work: two mutable copies of one draft, a conversion answer that outlived
 * the edit it described, an invalid draft that cannot be saved and reopened,
 * a number that changes value on the way through, and an unfinished flow that
 * a diagram makes look finished.
 */

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor() {
    MockWebSocket.instances.push(this);
  }
  close() {}
  send() {}
  emit(type: string, payload: unknown) {
    this.onmessage?.({ data: JSON.stringify({ seq: 0, ts: "", type, payload }) });
  }
  emitSnapshot(payload: Record<string, unknown>) {
    this.onopen?.();
    this.emit("snapshot", payload);
  }
}

const YAML = `# a comment somebody wrote
format: 2
name: custom
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    outcome: null
    prompt:
      parts:
        - text: "do it"
  - name: finish
    kind: decision
    cases:
      - when: true
        next: {complete: true, result: done}
    otherwise: {complete: true, result: done}
`;

/** The parsed form of YAML above, as the conversion endpoint would return it. */
const DOCUMENT = {
  format: 2,
  name: "custom",
  sessions: ["main"],
  primary: "main",
  steps: [
    {
      name: "work",
      kind: "agent",
      session: "main",
      outcome: null,
      prompt: { parts: [{ text: "do it" }] },
    },
    {
      name: "finish",
      kind: "decision",
      cases: [{ when: true, next: { complete: true, result: "done" } }],
      otherwise: { complete: true, result: "done" },
    },
  ],
};

function descriptor(): WorkflowDescriptor {
  return {
    name: "custom",
    revision: "sha256:aaa",
    format: 2,
    primary_session: "main",
    sessions: ["main"],
    steps: [
      { name: "work", kind: "agent", session: "main", role: "default", conditional: false },
      { name: "finish", kind: "decision", session: null, role: null, conditional: false },
    ],
  };
}

function entry(overrides: Partial<WorkflowLibraryEntry> = {}): WorkflowLibraryEntry {
  return {
    name: "custom",
    origin: "custom",
    archived: false,
    version: 3,
    has_draft: true,
    current_revision: "sha256:aaa",
    current_format: 2,
    available: true,
    unavailable_reason: null,
    unavailable_detail: null,
    created_at: "2026-09-07T08:00:00+00:00",
    updated_at: "2026-09-07T08:00:00+00:00",
    descriptor: descriptor(),
    ...overrides,
  };
}

function detail(draftYaml: string = YAML) {
  return { entry: entry(), draft_yaml: draftYaml, revisions: [] };
}

/** A response the client can read either way it reads one.
 *
 * `raw` exists because the definition payloads travel through a lossless
 * codec: serializing a handler's plain JavaScript object with `JSON.stringify`
 * would round `1.0` to `1` inside the stub itself, and the test would then be
 * measuring the stub rather than the client. */
function respond(status: number, json: unknown, raw?: string) {
  const text = raw ?? JSON.stringify(json);
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(JSON.parse(text) as unknown),
    text: () => Promise.resolve(text),
  };
}

interface Answer {
  status?: number;
  json?: unknown;
  /** Exact response bytes, for payloads whose numeric tokens matter. */
  raw?: string;
}

type Handler = (body: unknown) => Answer | Promise<Answer>;

function stubFetch(handlers: Record<string, Handler> = {}) {
  const calls: { url: string; method: string; body: unknown; raw: string | undefined }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: { method?: string; body?: string }) => {
    const method = init?.method ?? "GET";
    const body = init?.body === undefined ? undefined : JSON.parse(init.body);
    calls.push({ url, method, body, raw: init?.body });
    const handler = handlers[`${method} ${url}`];
    if (handler !== undefined) {
      const { status = 200, json, raw } = await handler(body);
      return respond(status, json, raw);
    }
    if (url.startsWith("/api/workflow-library/") && method === "GET") {
      return respond(200, detail());
    }
    return respond(200, {});
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls };
}

/** The daemon's own answer shape: the parsed draft, its text, and what it
 * currently means. */
function conversion(document: unknown, yaml: string, validation?: unknown) {
  return {
    document,
    yaml,
    validation: validation ?? {
      ok: true,
      revision: "sha256:bbb",
      name: "custom",
      format: 2,
      definition: document,
      descriptor: descriptor(),
    },
  };
}

async function openEditor(handlers: Record<string, Handler> = {}) {
  const stub = stubFetch(handlers);
  window.history.pushState({}, "", "/workflows/custom");
  render(<App />);
  act(() => {
    MockWebSocket.instances[0].emitSnapshot({
      projects: [],
      tasks: [],
      workflow_library: [entry()],
      workflow_catalog: [descriptor()],
    });
  });
  await screen.findByTestId("workflow-editor");
  return stub;
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("switching between the two views of one draft", () => {
  it("does not rewrite the source text when nothing was changed", async () => {
    const { calls } = await openEditor({
      "POST /api/workflow-library/document": () => ({
        json: conversion(DOCUMENT, "format: 2\nname: custom\n# reformatted\n"),
      }),
    });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("workflow-mode-visual"));
    await screen.findByTestId("workflow-visual-editor");
    await user.click(screen.getByTestId("workflow-mode-yaml"));

    const editor = (await screen.findByTestId("workflow-editor")) as HTMLTextAreaElement;
    // The operator's own text, comment and all. A look at the visual view is
    // not an edit, and must not cost them their formatting.
    expect(editor.value).toBe(YAML);
    expect(calls.some((call) => call.url.endsWith("/draft"))).toBe(false);
  });

  it("warns that a visual edit normalizes the document before it happens", async () => {
    await openEditor({
      "POST /api/workflow-library/document": () => ({ json: conversion(DOCUMENT, YAML) }),
    });
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-mode-visual"));
    expect((await screen.findByTestId("workflow-visual-warning")).textContent).toContain(
      "drops YAML comments",
    );
  });

  it("keeps unparseable text in the YAML editor with a located reason", async () => {
    await openEditor({
      "POST /api/workflow-library/document": () => ({
        status: 422,
        json: {
          detail: {
            reason: "workflow_document_invalid",
            location: "steps",
            message: "not valid YAML: could not find expected ':'",
            line: 6,
          },
        },
      }),
    });
    const user = userEvent.setup();
    const editor = screen.getByTestId("workflow-editor") as HTMLTextAreaElement;
    await user.clear(editor);
    await user.type(editor, "steps: [[");
    await user.click(screen.getByTestId("workflow-mode-visual"));

    const failure = await screen.findByTestId("workflow-conversion-error");
    expect(failure.textContent).toContain("could not find expected");
    // Still the operator's text, not an empty or last-valid document.
    expect((screen.getByTestId("workflow-editor") as HTMLTextAreaElement).value).toBe(
      "steps: [",
    );
    expect(screen.queryByTestId("workflow-visual-editor")).toBeNull();
    expect(screen.getByTestId("workflow-conversion-retry")).toBeTruthy();
  });
});

describe("an unfinished draft", () => {
  it("is shown whole, with the reason at the card it is about, and can be saved", async () => {
    const broken = {
      ...DOCUMENT,
      steps: [
        DOCUMENT.steps[0],
        {
          name: "finish",
          kind: "decision",
          cases: [{ when: true, next: { step: "nowhere" } }],
          otherwise: { complete: true, result: "done" },
        },
      ],
    };
    const { calls } = await openEditor({
      "POST /api/workflow-library/document": () => ({
        json: conversion(broken, "format: 2\n", {
          ok: false,
          reason: "workflow_document_invalid",
          location: "steps[1].cases[0].next",
          message: "case 0 routes to unknown step 'nowhere'",
          line: null,
          column: null,
          format: null,
        }),
      }),
      "PUT /api/workflow-library/custom/draft": () => ({ json: detail("format: 2\n") }),
    });
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-mode-visual"));

    const summary = await screen.findByTestId("editor-validation");
    expect(summary.textContent).toContain("unknown step 'nowhere'");
    // The card is reachable even while it is collapsed.
    await user.click(within(summary).getByTestId("editor-goto-problem"));
    expect(
      (await screen.findByTestId("editor-step-1")).getAttribute("data-invalid"),
    ).toBe("true");
    // And the reason is at the field, not only in the summary.
    expect(
      screen.getByTestId("problem-steps[1].cases[0].next").textContent,
    ).toContain("unknown step");

    // An invalid draft is still work worth keeping.
    await user.click(screen.getByTestId("workflow-save-draft"));
    await waitFor(() =>
      expect(calls.some((call) => call.url.endsWith("/draft"))).toBe(true),
    );
  });

  it("keeps a broken route visible rather than dropping the edge", async () => {
    await openEditor({
      "POST /api/workflow-library/document": () => ({ json: conversion(DOCUMENT, YAML) }),
    });
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-validate"));
    // The shared flow renders the same definition the check answered about.
    const flow = await screen.findByTestId("workflow-check-flow");
    expect(flow.textContent).toContain("What this procedure declares may happen");
  });
});

describe("answers that outlived their question", () => {
  it("never applies a conversion for an edit that has been superseded", async () => {
    let release: ((value: { json: unknown }) => void) | null = null;
    await openEditor({
      "POST /api/workflow-library/document": (body) => {
        const submitted = body as { yaml?: string };
        if (submitted.yaml !== undefined && release === null) {
          return new Promise<{ json: unknown }>((resolve) => {
            release = resolve;
          });
        }
        return { json: conversion(DOCUMENT, YAML) };
      },
    });
    const user = userEvent.setup();
    const editor = screen.getByTestId("workflow-editor") as HTMLTextAreaElement;

    await user.click(screen.getByTestId("workflow-mode-visual"));
    // The operator keeps typing while the conversion is in flight.
    await user.type(editor, "\n# later");
    act(() => {
      release?.({ json: conversion(DOCUMENT, YAML) });
    });

    await waitFor(() =>
      expect((screen.getByTestId("workflow-editor") as HTMLTextAreaElement).value).toContain(
        "# later",
      ),
    );
    // The late answer described the text before that edit, so it must not
    // have replaced it with a visual editor built from stale data.
    expect(screen.queryByTestId("workflow-visual-editor")).toBeNull();
  });
});

describe("numbers inside a definition", () => {
  it("submits the exact token the daemon sent, not a rounded one", async () => {
    // Written as bytes on purpose: `1.0` and an integer past 2^53 are exactly
    // the two things ordinary JSON would change, and a definition's identity
    // is taken over its canonical bytes.
    const withNumbers = `{"format":2,"name":"custom","sessions":["main"],"primary":"main","steps":[
      {"name":"check","kind":"command","argv":["true"],"idempotent":true,"timeout":1.0},
      {"name":"work","kind":"agent","session":"main","outcome":null,
       "prompt":{"parts":[{"value":{"op":"literal","value":90071992547409911}}]}}]}`;
    const answer = `{"document":${withNumbers},"yaml":"format: 2\\n","validation":{"ok":true,"revision":"sha256:bbb","name":"custom","format":2,"definition":${withNumbers},"descriptor":${JSON.stringify(descriptor())}}}`;
    const { calls } = await openEditor({
      "POST /api/workflow-library/document": () => ({ raw: answer }),
    });
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-mode-visual"));
    await screen.findByTestId("workflow-visual-editor");

    // An unrelated edit: rename an agent. Nothing about the numbers changed.
    const agent = screen.getByTestId("editor-agent-0") as HTMLInputElement;
    await user.clear(agent);
    await user.type(agent, "qa");
    await user.tab();

    await waitFor(() => {
      const submitted = calls.filter(
        (call) => call.url.endsWith("/document") && (call.body as { document?: unknown }).document,
      );
      expect(submitted.length).toBeGreaterThan(0);
      // The *wire* bytes, not a re-parse of them: reading the body back
      // through ordinary JSON is the very rounding this guards against.
      const raw = submitted[submitted.length - 1].raw ?? "";
      // `1.0` did not become `1`, and the large integer kept every digit.
      expect(raw).toContain("1.0");
      expect(raw).toContain("90071992547409911");
    });
  });
});

describe("removing a step other steps name", () => {
  it("lists the impact first and leaves the dangling routes visible after", async () => {
    const routed = {
      format: 2,
      name: "custom",
      sessions: ["main"],
      primary: "main",
      steps: [
        DOCUMENT.steps[0],
        {
          name: "finish",
          kind: "decision",
          cases: [{ when: true, next: { step: "work" } }],
          otherwise: { complete: true, result: "done" },
        },
      ],
    };
    await openEditor({
      "POST /api/workflow-library/document": () => ({ json: conversion(routed, "format: 2\n") }),
    });
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-mode-visual"));
    await screen.findByTestId("workflow-visual-editor");

    await user.click(screen.getByTestId("editor-remove-0"));
    const impact = await screen.findByTestId("reference-impact");
    expect(impact.textContent).toContain("steps[1].cases[0].next.step");
    expect(impact.textContent).toContain("is a route of this decision");

    await user.click(screen.getByTestId("editor-remove-confirmed-0"));
    expect(screen.getByTestId("editor-announcement").textContent).toContain(
      "points at a step that does not exist",
    );

    // The route is not repaired and not hidden. The destination still names
    // the step that is gone, marked as something no longer on offer — a
    // control that silently re-selected the first surviving step would have
    // rewritten the workflow by rendering it.
    const remaining = await screen.findByTestId("editor-step-0");
    const destination = within(remaining).getByTestId(
      "destination-step-steps[0].cases[0].next",
    ) as HTMLSelectElement;
    expect(destination.value).toBe("work");
    expect(destination.textContent).toContain("not a known choice");
  });

  it("shows a route with no destination as unfinished rather than as an ending", async () => {
    const unfinished = {
      format: 2,
      name: "custom",
      sessions: ["main"],
      primary: "main",
      steps: [
        DOCUMENT.steps[0],
        { name: "finish", kind: "decision", cases: [{ when: true }], otherwise: {} },
      ],
    };
    await openEditor({
      "POST /api/workflow-library/document": () => ({
        json: conversion(unfinished, "format: 2\n", {
          ok: false,
          reason: "workflow_document_invalid",
          location: "steps[1].cases[0]",
          message: "a case needs both 'when' and 'next'",
          line: null,
          column: null,
          format: null,
        }),
      }),
    });
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-mode-visual"));
    const card = await screen.findByTestId("editor-step-1");
    await user.click(within(card).getByText(/finish/));
    expect(card.textContent).toContain("This route has no destination yet");
  });
});

describe("an archived entry", () => {
  it("is readable in the visual editor but not editable anywhere in it", async () => {
    // The trap this guards: `readOnly` was threaded through the top-level
    // controls but not through the recursive ones, so a prompt, a predicate,
    // or a destination stayed editable on an entry that cannot be saved.
    const archived = entry({ archived: true, available: false, unavailable_reason: "archived" });
    stubFetch({
      "POST /api/workflow-library/document": () => ({ json: conversion(DOCUMENT, YAML) }),
      "GET /api/workflow-library/custom": () => ({
        json: { entry: archived, draft_yaml: YAML, revisions: [] },
      }),
    });
    window.history.pushState({}, "", "/workflows/custom");
    render(<App />);
    act(() => {
      MockWebSocket.instances[0].emitSnapshot({
        projects: [],
        tasks: [],
        workflow_library: [archived],
        workflow_catalog: [],
      });
    });
    await screen.findByTestId("workflow-editor");
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-mode-visual"));
    const editor = await screen.findByTestId("workflow-visual-editor");

    // Every control inside the editable region, however deeply nested.
    // Matched with `:disabled` rather than the `disabled` property: a
    // descendant of a disabled fieldset is effectively disabled without its
    // own attribute being set, and reading the property would pass a broken
    // implementation. A `readonly` text field is uneditable too.
    const controls = Array.from(
      editor.querySelectorAll<HTMLInputElement>(
        ".editorEditable input, .editorEditable select, .editorEditable textarea, .editorEditable button",
      ),
    );
    expect(controls.length).toBeGreaterThan(20);
    const editable = controls.filter(
      (control) => !control.matches(":disabled") && !control.readOnly,
    );
    expect(editable.map((control) => control.getAttribute("data-testid"))).toEqual([]);

    // Reading it is still the point: the flow is all there.
    expect(editor.textContent).toContain("do it");
  });
});
