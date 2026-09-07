import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { WorkflowDescriptor, WorkflowLibraryEntry } from "../types";

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

const MINIMAL = `format: 1
name: custom
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - text: "do it"
`;

function descriptor(name: string, revision: string): WorkflowDescriptor {
  return {
    name,
    revision,
    format: 1,
    primary_session: "main",
    sessions: ["main"],
    steps: [
      {
        name: "work",
        kind: "agent" as const,
        session: "main",
        role: "default" as const,
        conditional: false,
      },
    ],
  };
}

function entry(overrides: Partial<WorkflowLibraryEntry> = {}): WorkflowLibraryEntry {
  const name = overrides.name ?? "custom";
  return {
    name,
    origin: "custom",
    archived: false,
    version: 3,
    has_draft: true,
    current_revision: "sha256:aaa",
    current_format: 1,
    available: true,
    unavailable_reason: null,
    unavailable_detail: null,
    created_at: "2026-09-07T08:00:00+00:00",
    updated_at: "2026-09-07T08:00:00+00:00",
    descriptor: descriptor(name, "sha256:aaa"),
    ...overrides,
  };
}

const builtin = entry({
  name: "single-step",
  origin: "builtin",
  has_draft: false,
  current_revision: "sha256:builtin",
  descriptor: descriptor("single-step", "sha256:builtin"),
});

function detail(
  over: { entry?: WorkflowLibraryEntry; draft_yaml?: string | null; revisions?: unknown[] } = {},
) {
  return {
    entry: over.entry ?? entry(),
    draft_yaml: over.draft_yaml === undefined ? MINIMAL : over.draft_yaml,
    revisions: over.revisions ?? [
      {
        revision: "sha256:aaa",
        workflow_name: "custom",
        format: 1,
        created_at: "2026-09-07T08:00:00+00:00",
      },
    ],
  };
}

/** Route every request this view makes; a test overrides only what it cares
 * about. Each handler receives the parsed body so a test can assert on what
 * was actually submitted. */
/** A response the client can read either way it reads one.
 *
 * Definition-bearing payloads go through the lossless codec, which reads the
 * body as text; everything else calls `json()`. Serving both from the same
 * value keeps the stub from deciding which client path a test exercises. */
function respond(status: number, json: unknown) {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(json),
    text: () => Promise.resolve(JSON.stringify(json)),
  };
}

function stubFetch(
  handlers: Record<string, (body: unknown) => { status?: number; json: unknown }> = {},
) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const fetchMock = vi.fn((url: string, init?: { method?: string; body?: string }) => {
    const method = init?.method ?? "GET";
    const body = init?.body === undefined ? undefined : JSON.parse(init.body);
    calls.push({ url, method, body });
    const key = `${method} ${url}`;
    const handler = handlers[key];
    if (handler !== undefined) {
      const { status = 200, json } = handler(body);
      return Promise.resolve(respond(status, json));
    }
    if (url.startsWith("/api/workflow-library/") && method === "GET") {
      return Promise.resolve(respond(200, detail()));
    }
    return Promise.resolve(respond(200, {}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

async function renderAt(path: string, snapshot: Record<string, unknown>) {
  window.history.pushState({}, "", path);
  render(<App />);
  act(() => {
    MockWebSocket.instances[0].emitSnapshot(snapshot);
  });
}

function librarySnapshot(entries: WorkflowLibraryEntry[]) {
  return {
    projects: [],
    tasks: [],
    workflow_library: entries,
    workflow_catalog: entries.flatMap((e) => (e.descriptor ? [e.descriptor] : [])),
  };
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

describe("the workflow library", () => {
  it("shows loading before the snapshot, never an empty library", async () => {
    stubFetch();
    window.history.pushState({}, "", "/workflows");
    render(<App />);
    // No snapshot yet: an absence decision here would tell the operator their
    // workflows are gone.
    expect(screen.getByTestId("workflows-loading")).toBeTruthy();
    expect(screen.queryByTestId("workflows-empty")).toBeNull();
  });

  it("offers create, import, and duplication when nothing custom exists yet", async () => {
    stubFetch();
    await renderAt("/workflows", librarySnapshot([builtin]));
    expect(screen.getByTestId("workflows-empty").textContent).toContain(
      "No workflows of your own yet",
    );
    expect(screen.getByTestId("workflow-create")).toBeTruthy();
    expect(screen.getByTestId("workflow-import")).toBeTruthy();
    // The packaged example is listed and openable, which is the duplication path.
    expect(screen.getByTestId("workflow-card-single-step")).toBeTruthy();
  });

  it("distinguishes launchable, draft-only, unavailable, and archived entries", async () => {
    stubFetch();
    await renderAt(
      "/workflows",
      librarySnapshot([
        entry({ name: "ready" }),
        entry({
          name: "unsaved",
          current_revision: null,
          current_format: null,
          available: false,
          unavailable_reason: "draft_only",
          unavailable_detail: "no executable revision has been saved yet; a draft cannot run",
          descriptor: null,
        }),
        entry({
          name: "damaged",
          available: false,
          unavailable_reason: "missing",
          unavailable_detail: "no such retained revision",
          descriptor: null,
        }),
        entry({
          name: "retired",
          archived: true,
          available: false,
          unavailable_reason: "archived",
          unavailable_detail: "archived; restore it to launch it again",
          descriptor: null,
        }),
      ]),
    );
    expect(screen.getByTestId("workflow-state-ready").textContent).toBe("launchable");
    expect(screen.getByTestId("workflow-state-unsaved").textContent).toBe("draft only");
    expect(screen.getByTestId("workflow-state-damaged").textContent).toContain("missing");
    // Archived entries are behind an explicit filter, not mixed in.
    expect(screen.queryByTestId("workflow-card-retired")).toBeNull();
    await userEvent.setup().click(screen.getByTestId("workflows-show-archived"));
    expect(screen.getByTestId("workflow-state-retired").textContent).toBe("archived");
  });

  it("creates a draft under a name the operator chooses", async () => {
    const created = entry({
      name: "mine",
      current_revision: null,
      current_format: null,
      available: false,
      unavailable_reason: "draft_only",
      unavailable_detail: "no executable revision has been saved yet",
      descriptor: null,
      version: 1,
    });
    const { calls } = stubFetch({
      "POST /api/workflow-library": () => ({
        status: 201,
        json: { entry: created, draft_yaml: "starter", revisions: [] },
      }),
      "GET /api/workflow-library/mine": () => ({
        json: { entry: created, draft_yaml: "starter", revisions: [] },
      }),
    });
    await renderAt("/workflows", librarySnapshot([builtin]));
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-create"));
    await user.type(screen.getByTestId("workflow-name-input"), "mine");
    await user.click(screen.getByRole("button", { name: "Create draft" }));

    await screen.findByTestId("workflow-editor");
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ name: "mine" });
    // The new entry is a draft: it did not join the launch catalog.
    expect(screen.getByTestId("workflow-detail-state").textContent).toBe("draft only");
  });

  it("keeps the typed name when the daemon refuses it", async () => {
    stubFetch({
      "POST /api/workflow-library": () => ({
        status: 409,
        json: {
          detail: {
            reason: "workflow_name_taken",
            message: "the built-in workflow 'single-step' owns this name",
          },
        },
      }),
    });
    await renderAt("/workflows", librarySnapshot([builtin]));
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-create"));
    await user.type(screen.getByTestId("workflow-name-input"), "single-step");
    await user.click(screen.getByRole("button", { name: "Create draft" }));

    expect((await screen.findByTestId("workflow-create-error")).textContent).toContain(
      "owns this name",
    );
    expect((screen.getByTestId("workflow-name-input") as HTMLInputElement).value).toBe(
      "single-step",
    );
  });
});

describe("the workflow editor", () => {
  it("presents a built-in as a read-only example with a duplicate path", async () => {
    stubFetch({
      "GET /api/workflow-library/single-step": () => ({
        json: detail({ entry: builtin, draft_yaml: MINIMAL, revisions: [] }),
      }),
    });
    await renderAt("/workflows/single-step", librarySnapshot([builtin]));
    await screen.findByTestId("workflow-packaged-yaml");
    expect(screen.queryByTestId("workflow-editor")).toBeNull();
    expect(screen.getByTestId("workflow-duplicate")).toBeTruthy();
  });

  it("saves a draft, and never lets that change what would launch", async () => {
    const saved = entry({ version: 4 });
    const { calls } = stubFetch({
      "PUT /api/workflow-library/custom/draft": () => ({
        json: detail({ entry: saved, draft_yaml: `${MINIMAL}# a note\n` }),
      }),
    });
    await renderAt("/workflows/custom", librarySnapshot([entry()]));
    const editor = (await screen.findByTestId("workflow-editor")) as HTMLTextAreaElement;
    const user = userEvent.setup();
    await user.type(editor, "# a note\n");
    expect(screen.getByTestId("workflow-unsaved")).toBeTruthy();

    await user.click(screen.getByTestId("workflow-save-draft"));
    await waitFor(() => expect(screen.queryByTestId("workflow-unsaved")).toBeNull());
    const put = calls.find((c) => c.method === "PUT");
    // The version it loaded is what it submits, so it cannot overwrite an
    // edit it never saw.
    expect(put!.body).toMatchObject({ expected_version: 3 });
    expect(screen.getByTestId("workflow-detail-state").textContent).toBe("launchable");
  });

  it("marks a validation result stale as soon as the text changes", async () => {
    stubFetch({
      "POST /api/workflow-library/validate": () => ({
        json: {
          revision: "sha256:bbb",
          name: "custom",
          format: 1,
          definition: {
            format: 1,
            name: "custom",
            sessions: ["main"],
            primary: "main",
            steps: [
              {
                name: "work",
                kind: "agent",
                session: "main",
                role: "default",
                max_visits: null,
                on_exhausted: null,
                expects_outcome: false,
                when: true,
                prompt: { separator: "", parts: [{ text: "do it" }] },
              },
            ],
          },
          descriptor: descriptor("custom", "sha256:bbb"),
        },
      }),
    });
    await renderAt("/workflows/custom", librarySnapshot([entry()]));
    const editor = (await screen.findByTestId("workflow-editor")) as HTMLTextAreaElement;
    const user = userEvent.setup();
    await user.click(screen.getByTestId("workflow-validate"));

    const ok = await screen.findByTestId("workflow-check-ok");
    expect(ok.textContent).toContain("Valid format-1 definition");
    // The read-only reading, not a second editor.
    expect(within(ok).getByTestId("flow-step-work").textContent).toContain("do it");
    expect(screen.queryByTestId("workflow-check-stale")).toBeNull();

    await user.type(editor, "x");
    expect(screen.getByTestId("workflow-check-stale")).toBeTruthy();
  });

  it("locates a validation failure without discarding the text", async () => {
    stubFetch({
      "POST /api/workflow-library/validate": () => ({
        status: 422,
        json: {
          detail: {
            reason: "workflow_document_invalid",
            location: "steps[0].kind",
            message: "must be one of agent, command, decision, gate",
            line: 6,
            column: 5,
          },
        },
      }),
    });
    await renderAt("/workflows/custom", librarySnapshot([entry()]));
    await screen.findByTestId("workflow-editor");
    await userEvent.setup().click(screen.getByTestId("workflow-validate"));

    const failed = await screen.findByTestId("workflow-check-failed");
    expect(failed.textContent).toContain("steps[0].kind");
    expect(failed.textContent).toContain("line 6");
    expect((screen.getByTestId("workflow-editor") as HTMLTextAreaElement).value).toBe(
      MINIMAL,
    );
  });

  it("refuses a stale save, keeps the buffer, and offers recovery", async () => {
    stubFetch({
      "POST /api/workflow-library/custom/revisions": () => ({
        status: 409,
        json: {
          detail: {
            reason: "workflow_version_conflict",
            message: "workflow 'custom' was edited elsewhere (expected version 3, found 5)",
            name: "custom",
            expected_version: 3,
            current_version: 5,
          },
        },
      }),
    });
    await renderAt("/workflows/custom", librarySnapshot([entry()]));
    const editor = (await screen.findByTestId("workflow-editor")) as HTMLTextAreaElement;
    const user = userEvent.setup();
    await user.type(editor, "# my edit\n");
    await user.click(screen.getByTestId("workflow-save-revision"));

    const conflict = await screen.findByTestId("workflow-conflict");
    expect(conflict.textContent).toContain("edited elsewhere");
    // Recovery is reload-or-keep-editing. There is no force button and no
    // automatic merge.
    expect(
      within(conflict)
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["Copy my text", "Discard my edits and reload", "Keep editing"]);
    expect(screen.getByTestId("workflow-conflict-reload")).toBeTruthy();
    // The operator's text is still exactly where they left it.
    expect((screen.getByTestId("workflow-editor") as HTMLTextAreaElement).value).toContain(
      "# my edit",
    );
  });

  it("never lets a remote update overwrite an open editor", async () => {
    stubFetch();
    await renderAt("/workflows/custom", librarySnapshot([entry()]));
    const editor = (await screen.findByTestId("workflow-editor")) as HTMLTextAreaElement;
    await userEvent.setup().type(editor, "# still typing");

    act(() => {
      MockWebSocket.instances[0].emit(
        "workflow_library_updated",
        entry({ version: 9, current_revision: "sha256:zzz" }),
      );
    });

    // The saved summary moved; the buffer did not.
    expect((screen.getByTestId("workflow-editor") as HTMLTextAreaElement).value).toContain(
      "# still typing",
    );
    expect(screen.getByTestId("workflow-unsaved")).toBeTruthy();
  });

  it("archives without deleting anything, and restores", async () => {
    const archived = entry({
      version: 4,
      archived: true,
      available: false,
      unavailable_reason: "archived",
      unavailable_detail: "archived; restore it to launch it again",
      descriptor: null,
    });
    stubFetch({
      "POST /api/workflow-library/custom/archive": () => ({
        json: detail({ entry: archived }),
      }),
    });
    await renderAt("/workflows/custom", librarySnapshot([entry()]));
    await screen.findByTestId("workflow-editor");
    await userEvent.setup().click(screen.getByTestId("workflow-archive"));

    await screen.findByTestId("workflow-restore");
    expect(screen.getByTestId("workflow-detail-state").textContent).toBe("archived");
    // The draft and the saved revision are still on screen.
    expect((screen.getByTestId("workflow-editor") as HTMLTextAreaElement).value).toBe(
      MINIMAL,
    );
    expect(within(screen.getByTestId("workflow-revisions")).getAllByRole("listitem")).toHaveLength(
      1,
    );
  });

  it("reports a missing entry instead of an empty editor", async () => {
    stubFetch({
      "GET /api/workflow-library/gone": () => ({
        status: 404,
        json: { detail: "workflow 'gone' is not in the library" },
      }),
    });
    await renderAt("/workflows/gone", librarySnapshot([builtin]));
    expect((await screen.findByTestId("workflow-not-found")).textContent).toContain(
      "No workflow by that name",
    );
  });
});
