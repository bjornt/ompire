import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { summarizeResults } from "../lib/daemonReducer";
import { revisionStatus } from "../lib/resultPresentation";
import type {
  Task,
  TaskExecutionInputs,
  TaskResult,
  TaskResultsProjection,
} from "../types";

/* UI behavior for durable task results (ADR-0034).
 *
 * Focused on the edges a plausible implementation gets wrong: a decision made
 * against a revision that is no longer selected, agent-authored content
 * reaching a renderer, a confirmation that understates what it destroys, and a
 * cleaned-up task disappearing from the operator's reach. */

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

const project = {
  name: "maas",
  title: "MAAS",
  upstream_url: "https://example.com/maas.git",
  fork_url: null,
  checkout_path: "/home/op/proj/maas",
  checkout_mode: "adopted",
  fetch_remote: "origin",
  setup_state: "ready",
  setup_error: null,
  default_model_profile: null,
  base_branch: "main",
  branch_pattern: "ompire/<slug>",
  workshop_additions: "project",
  preamble: "",
  launch_config_state: "reconciled",
};

const ROLES = {
  default: { model: "anthropic/claude-opus-5", thinking: "medium" },
  smol: { model: "anthropic/claude-haiku-4-5", thinking: "off" },
  slow: { model: "anthropic/claude-opus-5", thinking: "high" },
  plan: { model: "anthropic/claude-opus-5", thinking: "high" },
};

function makeInputs(): TaskExecutionInputs {
  return {
    version: 3,
    provenance: "accepted",
    accepted_at: "2026-07-18T00:00:00Z",
    project_name: "maas",
    workflow_name: "single-step",
    workflow_binding: {
      revision: "sha256:abc",
      source: "accepted",
      bound_at: "2026-07-18T00:00:00Z",
      legacy_through_seq: 0,
      interrupted_legacy_seq: null,
    },
    model_profile_name: "balanced",
    model_profile_source: "project",
    step_bindings: {
      work: {
        profile_name: "balanced",
        profile_source: "project",
        role: "default",
        role_source: "workflow",
        roles: ROLES,
      },
    },
    workspace: {
      base_branch: "main",
      branch_pattern: "ompire/<slug>",
      workshop_additions: "project",
      preamble: "",
    },
    workspace_overrides: [],
    branch: "ompire/explore-idea",
    checkout_path: "/home/op/proj/maas",
    fetch_remote: "origin",
    upstream_url: "https://example.com/maas.git",
    fork_url: null,
    unknown_inputs: [],
  } as TaskExecutionInputs;
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    project_name: "maas",
    execution_inputs: makeInputs(),
    needs_configuration: false,
    slug: "explore-idea",
    branch: "ompire/explore-idea",
    clone_path: "/home/op/tasks/maas/explore-idea",
    state: "created",
    prompt: "explore",
    error: null,
    workshop_id: null,
    spawn_completed_at: "2026-07-18T00:01:00Z",
    pr_url: null,
    pr_state: null,
    pr_merged_at: null,
    workflow_name: "single-step",
    workflow_revision: "sha256:abc",
    workflow_revision_source: "accepted",
    workflow_ready: false,
    workflow_readiness_reason: null,
    workflow_readiness_detail: null,
    workflow_primary_session: null,
    workflow_sessions: null,
    workflow_status: null,
    workflow_step: null,
    workflow_result: null,
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T00:01:00Z",
    ...overrides,
  } as Task;
}

function makeResult(overrides: Partial<TaskResult> = {}): TaskResult {
  return {
    id: "res_aaaaaaaabbbbbbbb",
    task_id: 1,
    state: "ready",
    error: null,
    unavailable_reason: null,
    available: true,
    manifest_id: "manifest-1",
    content_id: "content-1",
    predecessor_id: null,
    selection: ["epics/demo"],
    files: [
      {
        path: "epics/demo/PLAN.md",
        length: 13,
        sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        media_type: "text/markdown",
      },
    ],
    file_count: 1,
    total_bytes: 13,
    provenance: {
      capture_actor: "operator",
      workflow_name: "single-step",
      workflow_revision: null,
      producing_run: "unknown",
      producing_step: "unknown",
      producing_session: "unknown",
      launch_base_branch: null,
      capture_head_commit: null,
      capture_merge_base: null,
      git_observation: "unavailable",
      gaps: ["capture_head_commit", "launch_inputs"],
    },
    captured_at: "2026-07-18T00:05:00Z",
    started_at: "2026-07-18T00:05:00Z",
    finished_at: "2026-07-18T00:05:01Z",
    accepted_at: null,
    accepted_by: null,
    purged_at: null,
    purged_by: null,
    ...overrides,
  };
}

function projection(
  results: TaskResult[],
  version = 3,
): TaskResultsProjection {
  return { task_id: 1, version, results };
}

const LIMITS = {
  max_files: 128,
  max_file_bytes: 1048576,
  max_total_bytes: 8388608,
  max_path_components: 16,
  capture_deadline_seconds: 30,
  supported_extensions: [".json", ".md", ".txt", ".yaml", ".yml"],
};

type Routes = Record<string, unknown>;

/** One fetch mock for every endpoint task detail touches, so a test only has
 * to describe the responses it actually cares about. */
function stubFetch(routes: Routes = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const mock = vi.fn((url: string, init?: { method?: string; body?: string }) => {
    const method = init?.method ?? "GET";
    calls.push({
      url,
      method,
      body: init?.body ? JSON.parse(init.body) : undefined,
    });
    for (const [pattern, response] of Object.entries(routes)) {
      const [routeMethod, routePath] = pattern.split(" ");
      if (routeMethod !== method) continue;
      if (!new RegExp(routePath).test(url)) continue;
      const value = response as { status?: number; body?: unknown; blob?: boolean };
      const status = value.status ?? 200;
      return Promise.resolve({
        ok: status < 400,
        status,
        headers: new Headers({ "content-disposition": 'attachment; filename="r.zip"' }),
        json: () => Promise.resolve(value.body),
        blob: () => Promise.resolve(new Blob([""])),
      });
    }
    if (/\/api\/tasks\/\d+\/results$/.test(url) && method === "GET") {
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: () => Promise.resolve({ ...projection([]), limits: LIMITS }),
      });
    }
    if (/\/api\/tasks\/\d+$/.test(url)) {
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: () =>
          Promise.resolve({ ...makeTask(), workshop_status: "present" }),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: () => Promise.resolve({}),
    });
  });
  vi.stubGlobal("fetch", mock);
  return { mock, calls };
}

function socket(): MockWebSocket {
  return MockWebSocket.instances[0];
}

async function renderTaskDetail(snapshot: Record<string, unknown>) {
  window.history.pushState({}, "", "/tasks/1");
  render(<App />);
  act(() => {
    socket().emitSnapshot({ projects: [project], tasks: [makeTask()], ...snapshot });
  });
  await screen.findByTestId("task-detail-results");
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

describe("revision status", () => {
  it("separates a damaged retained revision from a failed capture", () => {
    // Both are unusable, and they mean different things: one never produced a
    // bundle, the other produced one that can no longer be read — and may
    // still carry the operator's acceptance.
    expect(revisionStatus(makeResult({ state: "failed", available: false }))).toBe(
      "failed",
    );
    expect(
      revisionStatus(
        makeResult({ available: false, unavailable_reason: "checksum mismatch" }),
      ),
    ).toBe("unavailable");
  });

  it("reports an accepted revision that later became unreadable as unavailable", () => {
    const damaged = makeResult({
      available: false,
      unavailable_reason: "a retained file is missing",
      accepted_at: "2026-07-18T01:00:00Z",
    });
    expect(revisionStatus(damaged)).toBe("unavailable");
    // …while the acceptance itself is still a fact the panel can show.
    expect(damaged.accepted_at).not.toBeNull();
  });
});

describe("Results panel", () => {
  it("explains that nothing is retained until it is captured", async () => {
    stubFetch();
    await renderTaskDetail({});

    expect(await screen.findByTestId("results-empty")).toHaveTextContent(
      /not retained until you capture them/i,
    );
  });

  it("states the fixed limits before the operator submits", async () => {
    stubFetch();
    await renderTaskDetail({});

    const hint = await screen.findByTestId("results-limits");
    expect(hint).toHaveTextContent("128 files");
    expect(hint).toHaveTextContent(".md");
    expect(hint).toHaveTextContent(/8.00 MiB/);
  });

  it("shows retained file text as escaped source, never as rendered markup", async () => {
    const dangerous = "# Plan\n<img src=x onerror=alert(1)>\n";
    stubFetch({
      "GET /results/[^/]+/file": {
        body: {
          result_id: "res_aaaaaaaabbbbbbbb",
          manifest_id: "manifest-1",
          path: "epics/demo/PLAN.md",
          media_type: "text/markdown",
          length: dangerous.length,
          sha256: "abc",
          text: dangerous,
        },
      },
    });
    await renderTaskDetail({
      task_results: { "1": projection([makeResult()]) },
    });

    await userEvent.click(await screen.findByText("epics/demo/PLAN.md"));

    const source = await screen.findByTestId("results-file-source");
    // The tag is text, not an element: nothing rendered it.
    expect(source).toHaveTextContent("<img src=x onerror=alert(1)>");
    expect(source.querySelector("img")).toBeNull();
  });

  it("names every provenance gap instead of guessing a producer", async () => {
    stubFetch();
    await renderTaskDetail({
      task_results: { "1": projection([makeResult()]) },
    });

    const provenance = await screen.findByTestId("results-provenance");
    expect(provenance).toHaveTextContent("unknown");
    expect(await screen.findByTestId("results-provenance-gaps")).toHaveTextContent(
      "capture_head_commit",
    );
  });

  it("says what acceptance does and does not authorize", async () => {
    stubFetch();
    await renderTaskDetail({
      task_results: { "1": projection([makeResult()]) },
    });

    expect(await screen.findByTestId("results-accept-scope")).toHaveTextContent(
      /does not approve code, answer a workflow question, or allow anything to be published/i,
    );
  });

  it("accepts exactly the revision on screen", async () => {
    const { calls } = stubFetch({
      "POST /results/[^/]+/accept": {
        body: {
          ...projection([makeResult({ accepted_at: "2026-07-18T01:00:00Z" })], 4),
          limits: LIMITS,
        },
      },
    });
    await renderTaskDetail({
      task_results: { "1": projection([makeResult()]) },
    });

    await userEvent.click(await screen.findByTestId("results-accept"));

    const accept = calls.find((c) => c.url.endsWith("/accept"));
    expect(accept?.body).toEqual({ expected_manifest_id: "manifest-1" });
    await waitFor(() =>
      expect(screen.getByTestId("results-accepted")).toBeInTheDocument(),
    );
  });

  it("surfaces a stale-revision refusal instead of retrying against newer files", async () => {
    stubFetch({
      "POST /results/[^/]+/accept": {
        status: 409,
        body: { detail: "result no longer matches the reviewed revision" },
      },
    });
    await renderTaskDetail({
      task_results: { "1": projection([makeResult()]) },
    });

    await userEvent.click(await screen.findByTestId("results-accept"));

    expect(await screen.findByTestId("results-action-error")).toHaveTextContent(
      /no longer matches the reviewed revision/,
    );
  });

  it("offers no accept or download action for an unavailable revision", async () => {
    stubFetch();
    await renderTaskDetail({
      task_results: {
        "1": projection([
          makeResult({
            available: false,
            unavailable_reason: "epics/demo/PLAN.md does not match its recorded checksum",
            accepted_at: "2026-07-18T01:00:00Z",
          }),
        ]),
      },
    });

    expect(await screen.findByTestId("results-unavailable")).toHaveTextContent(
      /never rebuilt from the workspace/i,
    );
    expect(screen.queryByTestId("results-accept")).toBeNull();
    expect(screen.queryByTestId("results-download-zip")).toBeNull();
    // The decision it carried is still visible.
    expect(screen.getByTestId("results-accepted")).toBeInTheDocument();
  });

  it("keeps a purged revision visible as a record with no files", async () => {
    stubFetch();
    await renderTaskDetail({
      task_results: {
        "1": projection([
          makeResult({
            state: "purged",
            available: false,
            accepted_at: "2026-07-18T01:00:00Z",
            purged_at: "2026-07-18T02:00:00Z",
            purged_by: "operator",
          }),
        ]),
      },
    });

    expect(await screen.findByTestId("results-purged")).toHaveTextContent(
      /its files are gone/i,
    );
    expect(screen.getByTestId("results-status")).toHaveTextContent("Purged");
    expect(screen.queryByTestId("results-accept")).toBeNull();
  });

  it("names an unavailable predecessor rather than reporting everything as added", async () => {
    stubFetch({
      "GET /results/[^/]+/diff": {
        body: {
          result_id: "res_aaaaaaaabbbbbbbb",
          predecessor_id: "res_older",
          predecessor_available: false,
          predecessor_reason: "the previous revision was purged",
          added: [],
          changed: [],
          omitted: [],
          unchanged: ["epics/demo/PLAN.md"],
          text: "",
          truncated: false,
        },
      },
    });
    await renderTaskDetail({
      task_results: {
        "1": projection([makeResult({ predecessor_id: "res_older" })]),
      },
    });

    await userEvent.click(await screen.findByTestId("results-diff-toggle"));

    expect(await screen.findByTestId("results-diff-unavailable")).toHaveTextContent(
      /was purged/,
    );
    expect(screen.getByTestId("results-diff-unavailable")).toHaveTextContent(
      /own files are still complete/i,
    );
  });

  it("describes an omitted path as a bundle difference, not a deletion", async () => {
    stubFetch({
      "GET /results/[^/]+/diff": {
        body: {
          result_id: "res_aaaaaaaabbbbbbbb",
          predecessor_id: "res_older",
          predecessor_available: true,
          predecessor_reason: null,
          added: [],
          changed: [],
          omitted: ["epics/demo/OLD.md"],
          unchanged: [],
          text: "",
          truncated: false,
        },
      },
    });
    await renderTaskDetail({
      task_results: {
        "1": projection([makeResult({ predecessor_id: "res_older" })]),
      },
    });

    await userEvent.click(await screen.findByTestId("results-diff-toggle"));

    const diff = await screen.findByTestId("results-diff");
    expect(diff).toHaveTextContent("not in this revision: epics/demo/OLD.md");
    expect(diff).toHaveTextContent(/not an instruction to delete anything/i);
  });

  it("does not offer capture on a cleaned-up task but keeps its revisions readable", async () => {
    stubFetch();
    await renderTaskDetail({
      tasks: [makeTask({ state: "archived" })],
      task_results: { "1": projection([makeResult()]) },
    });

    expect(await screen.findByTestId("results-capture-unavailable")).toHaveTextContent(
      /no workspace to capture from/i,
    );
    expect(screen.queryByTestId("results-capture")).toBeNull();
    // The revision itself is still fully actionable.
    expect(screen.getByTestId("results-download-zip")).toBeInTheDocument();
  });

  it("confirms a purge by naming the revision, its acceptance and its size", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    stubFetch();
    await renderTaskDetail({
      task_results: {
        "1": projection([makeResult({ accepted_at: "2026-07-18T01:00:00Z" })]),
      },
    });

    await userEvent.click(await screen.findByTestId("results-purge"));

    const message = confirm.mock.calls[0][0] as string;
    expect(message).toContain("aaaaaaaa");
    expect(message).toContain("1 retained");
    expect(message).toContain("13 B");
    expect(message).toMatch(/You accepted this revision/);
  });
});

describe("cleanup", () => {
  it("is offered on task detail for a task that never opened a pull request", async () => {
    stubFetch();
    await renderTaskDetail({});

    expect(await screen.findByTestId("task-detail-cleanup-panel")).toBeInTheDocument();
  });

  it("warns about uncaptured edits and states what stays retained", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    stubFetch();
    await renderTaskDetail({
      retained_results: { "1": { total: 2, retained: 2, accepted: 1, bytes: 40 } },
    });

    await userEvent.click(await screen.findByTestId("task-detail-cleanup"));

    const message = confirm.mock.calls[0][0] as string;
    expect(message).toMatch(/have not captured as a result will be lost/i);
    expect(message).toMatch(/2 captured result revision\(s\), 1 accepted, are retained/);
  });
});

describe("Retained results index", () => {
  async function renderTasks(snapshot: Record<string, unknown>) {
    window.history.pushState({}, "", "/tasks");
    render(<App />);
    act(() => {
      socket().emitSnapshot({ projects: [project], tasks: [], ...snapshot });
    });
  }

  it("keeps a cleaned-up task with no PR reachable", async () => {
    stubFetch();
    await renderTasks({
      tasks: [makeTask({ state: "archived", pr_url: null })],
      retained_results: { "1": { total: 1, retained: 1, accepted: 1, bytes: 13 } },
    });

    const section = await screen.findByTestId("retained-results-section");
    expect(within(section).getByTestId("retained-link-1")).toHaveAttribute(
      "href",
      "/tasks/1",
    );
    expect(section).toHaveTextContent("1 revision retained · 1 accepted");
  });

  it("labels a task whose captures all failed or were purged as history only", async () => {
    stubFetch();
    await renderTasks({
      tasks: [makeTask({ state: "archived" })],
      retained_results: { "1": { total: 2, retained: 0, accepted: 0, bytes: 0 } },
    });

    expect(
      await screen.findByTestId("retained-history-only-1"),
    ).toHaveTextContent(/no retained files/i);
  });

  it("honors the project filter", async () => {
    stubFetch();
    window.history.pushState({}, "", "/tasks?project=other");
    render(<App />);
    act(() => {
      socket().emitSnapshot({
        projects: [project],
        tasks: [makeTask({ state: "archived" })],
        retained_results: { "1": { total: 1, retained: 1, accepted: 0, bytes: 13 } },
      });
    });

    await waitFor(() =>
      expect(screen.queryByTestId("retained-results-section")).toBeNull(),
    );
  });
});

describe("result projections in daemon state", () => {
  it("derives index counts from the same document the panel renders", () => {
    const counts = summarizeResults(
      projection([
        makeResult({ id: "res_1", accepted_at: "2026-07-18T01:00:00Z" }),
        makeResult({ id: "res_2" }),
        makeResult({ id: "res_3", state: "purged", available: false }),
      ]),
    );

    expect(counts).toEqual({ total: 3, retained: 2, accepted: 1, bytes: 26 });
  });
});
