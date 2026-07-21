import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Task } from "../types";

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

  emitSnapshot(payload: { projects: unknown[]; tasks: unknown[]; sessions?: unknown; attention?: unknown }) {
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
  base_branch: "master",
  branch_pattern: "bjornt/<slug>",
};

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    project_name: "maas",
    slug: "fix-bug",
    branch: "bjornt/fix-bug",
    clone_path: "/home/op/tasks/maas/fix-bug",
    state: "created",
    prompt: "fix it",
    error: null,
    workshop_id: null,
    spawn_completed_at: "2026-07-18T00:01:00Z",
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T00:01:00Z",
    ...overrides,
  };
}

function socket(): MockWebSocket {
  return MockWebSocket.instances[0];
}

async function renderAt(
  path: string,
  snapshot: { projects: unknown[]; tasks: unknown[]; sessions?: unknown; attention?: unknown },
) {
  window.history.pushState({}, "", path);
  render(<App />);
  act(() => {
    socket().emitSnapshot(snapshot);
  });
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SpawnView", () => {
  it("previews the branch name from the project's pattern as the slug is typed", async () => {
    await renderAt("/spawn", { projects: [project], tasks: [] });
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Task slug"), "vlan-mtu");
    expect(screen.getByTestId("branch-preview")).toHaveTextContent(
      "branch: bjornt/vlan-mtu · off origin/master",
    );
  });

  it("renders pipeline progress from spawn_step events after submit", async () => {
    await renderAt("/spawn", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const spawned = makeTask({ spawn_completed_at: null });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(spawned) }),
    );

    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await user.type(screen.getByLabelText("Prompt"), "fix it");
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    act(() => {
      socket().emit("task_created", spawned);
      socket().emit("spawn_step", { task_id: 1, step: "fetch", status: "started" });
      socket().emit("spawn_step", { task_id: 1, step: "fetch", status: "ok" });
      socket().emit("spawn_step", { task_id: 1, step: "clone", status: "started" });
    });

    const progress = screen.getByTestId("spawn-progress");
    expect(within(progress).getByText("Launching · maas/fix-bug")).toBeInTheDocument();
    const steps = progress.querySelectorAll("[data-step-status]");
    expect([...steps].map((s) => s.getAttribute("data-step-status"))).toEqual([
      "ok",
      "running",
      "pending",
      "pending",
      "pending",
      "pending",
    ]);
  });

  it("renders the agent and prompt steps as they run", async () => {
    await renderAt("/spawn", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const spawned = makeTask({ spawn_completed_at: null });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(spawned) }),
    );

    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await user.type(screen.getByLabelText("Prompt"), "fix it");
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    act(() => {
      socket().emit("task_created", spawned);
      for (const step of ["fetch", "clone", "branch", "workshop", "agent"]) {
        socket().emit("spawn_step", { task_id: 1, step, status: "started" });
        socket().emit("spawn_step", { task_id: 1, step, status: "ok" });
      }
      socket().emit("spawn_step", { task_id: 1, step: "prompt", status: "started" });
    });

    const progress = screen.getByTestId("spawn-progress");
    expect(within(progress).getByText("Agent")).toBeInTheDocument();
    expect(within(progress).getByText("Prompt")).toBeInTheDocument();
    const steps = progress.querySelectorAll("[data-step-status]");
    expect([...steps].map((s) => s.getAttribute("data-step-status"))).toEqual([
      "ok",
      "ok",
      "ok",
      "ok",
      "ok",
      "running",
    ]);
  });

  it("hides the prompt step for a promptless spawn", async () => {
    await renderAt("/spawn", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const spawned = makeTask({ spawn_completed_at: null, prompt: "" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(spawned) }),
    );

    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    act(() => {
      socket().emit("task_created", spawned);
    });

    const progress = screen.getByTestId("spawn-progress");
    expect(within(progress).getByText("Agent")).toBeInTheDocument();
    expect(within(progress).queryByText("Prompt")).not.toBeInTheDocument();
  });

  it("expands stderr inline when a step fails and links to the dashboard", async () => {
    await renderAt("/spawn", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const spawned = makeTask({ spawn_completed_at: null });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(spawned) }),
    );

    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    act(() => {
      socket().emit("task_created", spawned);
      socket().emit("spawn_step", { task_id: 1, step: "fetch", status: "started" });
      socket().emit("spawn_step", {
        task_id: 1,
        step: "fetch",
        status: "failed",
        stderr: "fatal: could not read from remote",
      });
      socket().emit("task_updated", {
        ...spawned,
        state: "failed",
        error: "step 'fetch' failed",
        spawn_completed_at: "x",
      });
    });

    expect(screen.getByTestId("stderr-fetch")).toHaveTextContent(
      "fatal: could not read from remote",
    );
    expect(screen.getByText("see it on the dashboard")).toBeInTheDocument();
  });
});

describe("TasksView cards", () => {
  it("renders cards from the snapshot and hides archived tasks", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask(),
        makeTask({ id: 2, slug: "old", branch: "bjornt/old", state: "archived" }),
      ],
    });

    expect(screen.getByTestId("task-card-1")).toHaveTextContent("bjornt/fix-bug");
    expect(screen.queryByTestId("task-card-2")).not.toBeInTheDocument();
  });

  it("shows spawning until the pipeline completes, then the created state", async () => {
    const spawning = makeTask({ spawn_completed_at: null });
    await renderAt("/tasks", { projects: [project], tasks: [spawning] });

    expect(screen.getByTestId("task-card-1")).toHaveTextContent("spawning");

    act(() => {
      socket().emit("task_updated", makeTask());
    });
    expect(screen.getByTestId("task-card-1")).toHaveTextContent("created");
    expect(screen.getByTestId("task-card-1")).not.toHaveTextContent("spawning");
  });

  it("failed cards expose the captured error on demand", async () => {
    const failed = makeTask({ state: "failed", error: "step 'clone' failed:\nboom" });
    await renderAt("/tasks", { projects: [project], tasks: [failed] });
    const user = userEvent.setup();

    expect(screen.queryByTestId("task-error-1")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("task-error-toggle-1"));
    expect(screen.getByTestId("task-error-1")).toHaveTextContent("boom");
  });

  it("cleanup requires confirmation naming the clone path before calling the API", async () => {
    await renderAt("/tasks", { projects: [project], tasks: [makeTask()] });
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: () => Promise.resolve(makeTask({ state: "archived" })) });
    vi.stubGlobal("fetch", fetchMock);

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    await user.click(screen.getByRole("button", { name: "Clean up" }));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("/home/op/tasks/maas/fix-bug"));
    expect(fetchMock).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "Clean up" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/tasks/1/cleanup", expect.objectContaining({ method: "POST" }));

    act(() => {
      socket().emit("task_updated", makeTask({ state: "archived" }));
    });
    expect(screen.queryByTestId("task-card-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("tasks-empty-state")).toBeInTheDocument();
  });

  it("cleanup confirmation names the workshop container when one is recorded", async () => {
    await renderAt("/tasks", { projects: [project], tasks: [makeTask({ workshop_id: "ws-maas-fix-bug" })] });
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn());

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    await user.click(screen.getByRole("button", { name: "Clean up" }));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("ws-maas-fix-bug"));
  });
});

describe("TasksView session status", () => {
  it("renders the working pill with breathing dot and slide bar from the snapshot", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: { "1": { status: "working", reason: "agent_start frame", since: "t0" } },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card).toHaveTextContent("working");
    expect(card).not.toHaveTextContent("created");
    expect(card.querySelector(".breathingDot")).toBeInTheDocument();
    expect(screen.getByTestId("slide-bar-1")).toBeInTheDocument();
  });

  it("updates pill and tier styling live on status_changed", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: { "1": { status: "working", reason: "agent_start frame", since: "t0" } },
    });

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        from: "working",
        to: "idle",
        reason: "agent_end, queue empty after 2.0s",
      });
    });

    const card = screen.getByTestId("task-card-1");
    expect(card).toHaveTextContent("idle");
    expect(screen.queryByTestId("slide-bar-1")).not.toBeInTheDocument();
    expect(card.querySelector(".breathingDot")).not.toBeInTheDocument();
  });

  it("failed sessions render interrupt styling with the reason accessible", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { status: "failed", reason: "process exited with code 137", since: "t0" },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card.className).toContain("failed");
    expect(screen.getByTestId("session-reason-1")).toHaveTextContent(
      "process exited with code 137",
    );
  });

  it("falls back to the spawn-derived pill when no session exists", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ spawn_completed_at: null })],
      sessions: {},
    });

    expect(screen.getByTestId("task-card-1")).toHaveTextContent("spawning");
  });

  it("stalled sessions render notify/amber styling with the reason accessible", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { status: "stalled", reason: "no frames for 300s", since: "t0" },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card.className).toContain("stalled");
    expect(card).toHaveTextContent("stalled");
    expect(card.querySelector(".notifyDot")).toBeInTheDocument();
    expect(card.querySelector(".statePill.notify")).toHaveAttribute(
      "title",
      "no frames for 300s",
    );
  });

  it("retrying sessions render quiet badge styling and don't raise the count", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { status: "retrying", reason: "auto_retry_start: HTTP 429", since: "t0" },
      },
      attention: {},
    });

    const card = screen.getByTestId("task-card-1");
    expect(card).toHaveTextContent("retrying");
    expect(card.querySelector(".ringDot")).toBeInTheDocument();
    expect(card.className).not.toContain("failed");
    expect(card.className).not.toContain("stalled");
    expect(screen.getByText("0 need you")).toBeInTheDocument();
  });

  it("shows an amber context ring and tokens/cost line from a stats event", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { status: "working", reason: "agent_start frame", since: "t0" },
      },
    });

    act(() => {
      socket().emit("stats", {
        task_id: 1,
        context_pct: 85,
        tokens: { input: 1200, output: 340 },
        cost: 0.0123,
      });
      socket().emit("advisory", { task_id: 1, kind: "context-high", context_pct: 85 });
    });

    const stats = screen.getByTestId("card-stats-1");
    expect(stats).toHaveTextContent("1200 in / 340 out");
    expect(stats).toHaveTextContent("$0.0123");
    expect(stats).toHaveTextContent("85%");
    expect(stats.querySelector("[data-testid='context-ring']")).toBeInTheDocument();
  });

  it("decorates an idle card with a maybe-waiting advisory", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { status: "idle", reason: "agent_end, queue empty after 2.0s", since: "t0" },
      },
    });

    act(() => {
      socket().emit("advisory", { task_id: 1, kind: "maybe-waiting" });
    });

    expect(screen.getByTestId("maybe-waiting-1")).toHaveTextContent(
      "may be waiting for a reply",
    );

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        from: "idle",
        to: "working",
        reason: "agent_start frame",
      });
      socket().emit("advisory_cleared", { task_id: 1, kind: "maybe-waiting" });
    });

    expect(screen.queryByTestId("maybe-waiting-1")).not.toBeInTheDocument();
  });

  it("counts tasks with an active daemon attention entry; working sessions stay silent", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask(),
        makeTask({ id: 2, slug: "other", branch: "bjornt/other" }),
        makeTask({ id: 3, slug: "third", branch: "bjornt/third", state: "failed", error: "x" }),
      ],
      sessions: {
        "1": { status: "working", reason: "agent_start frame", since: "t0" },
        "2": { status: "failed", reason: "process exited with code 137", since: "t0" },
        "3": { status: "failed", reason: "stopped by operator", since: "t0" },
      },
      attention: {
        "2": { tier: "interrupt", status: "failed", reason: "process exited with code 137" },
        "3": { tier: "interrupt", status: "failed", reason: "stopped by operator" },
      },
    });

    // Task 3 is failed twice over (registry + attention entry) but counts once.
    expect(screen.getByText("2 need you")).toBeInTheDocument();
    expect(document.title).toBe("(2) ompire");
  });

  it("raises the count live on an attention event and lowers it on attention_cleared", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: { "1": { status: "working", reason: "agent_start frame", since: "t0" } },
      attention: {},
    });
    expect(screen.getByText("0 need you")).toBeInTheDocument();

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        from: "working",
        to: "waiting-input",
        reason: "pending question",
      });
      socket().emit("attention", {
        task_id: 1,
        tier: "notify",
        status: "waiting-input",
        reason: "pending question",
      });
    });
    expect(screen.getByText("1 need you")).toBeInTheDocument();
    expect(document.title).toBe("(1) ompire");

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        from: "waiting-input",
        to: "working",
        reason: "operator answered the pending question",
      });
      socket().emit("attention_cleared", { task_id: 1 });
    });
    expect(screen.getByText("0 need you")).toBeInTheDocument();
    expect(document.title).toBe("ompire");
  });

  it("sets a badged favicon while the count is nonzero and reverts to the plain mark at zero", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {},
      attention: {},
    });
    const icon = () => document.querySelector("link[rel='icon']");
    expect(icon()?.getAttribute("href")).toBe("/favicon.svg");

    act(() => {
      socket().emit("attention", {
        task_id: 1,
        tier: "interrupt",
        status: "failed",
        reason: "process exited with code 1",
      });
    });
    expect(icon()?.getAttribute("href")).toMatch(/^data:image\/svg\+xml/);

    act(() => {
      socket().emit("attention_cleared", { task_id: 1 });
    });
    expect(icon()?.getAttribute("href")).toBe("/favicon.svg");
  });

  const askQuestion = {
    id: "ask-ui-1",
    kind: "ask" as const,
    questions: [
      {
        prompt: "Widen the fix to both loops?",
        options: [
          { value: "both", label: "Both loops", description: null },
          { value: "v4-only", label: "v4 only", description: null },
        ],
        multi: false,
        recommended: "both",
        allowsOther: false,
      },
    ],
  };

  it("renders an inline quick-answer for a fitting single-select ask and answers it", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));
    vi.stubGlobal("fetch", fetchMock);
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": {
          status: "waiting-input",
          reason: "pending question 'ask-ui-1'",
          since: "t0",
          question: askQuestion,
        },
      },
    });

    const quick = screen.getByTestId("quick-answer-1");
    expect(quick).toHaveTextContent("Widen the fix to both loops?");
    const recommended = within(quick).getByRole("button", { name: /Both loops/ });
    expect(recommended).toHaveTextContent("·rec");

    const user = userEvent.setup();
    await user.click(recommended);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/agent/answer",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ question_id: "ask-ui-1", selections: ["both"] }),
      }),
    );
  });

  it("defers a non-fitting question (multi-select) and an approval gate to task detail", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask(),
        makeTask({ id: 2, slug: "other", branch: "bjornt/other" }),
      ],
      sessions: {
        "1": {
          status: "waiting-input",
          reason: "pending question",
          since: "t0",
          question: {
            id: "ask-ui-2",
            kind: "ask",
            questions: [
              {
                prompt: "Pick one or more",
                options: [{ value: "a", label: "A", description: null }],
                multi: true,
                recommended: null,
                allowsOther: false,
              },
            ],
          },
        },
        "2": {
          status: "waiting-approval",
          reason: "pending approval",
          since: "t0",
          question: { id: "approval-ui-1", kind: "approval", questions: [] },
        },
      },
    });

    expect(screen.queryByTestId("quick-answer-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("quick-answer-defer-1")).toHaveTextContent("Open task detail to answer");
    expect(screen.queryByTestId("quick-answer-2")).not.toBeInTheDocument();
    expect(screen.getByTestId("quick-answer-defer-2")).toHaveTextContent("Open task detail to answer");
  });

  it("removes the quick-answer control once the question resolves", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": {
          status: "waiting-input",
          reason: "pending question",
          since: "t0",
          question: askQuestion,
        },
      },
    });
    expect(screen.getByTestId("quick-answer-1")).toBeInTheDocument();

    act(() => {
      socket().emit("question_resolved", { task_id: 1, question_id: "ask-ui-1" });
    });
    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        from: "waiting-input",
        to: "working",
        reason: "operator answered the pending question",
      });
    });

    expect(screen.queryByTestId("quick-answer-1")).not.toBeInTheDocument();
  });

  it("counts waiting-input/waiting-approval sessions in the N-need-you pill", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        makeTask(),
        makeTask({ id: 2, slug: "other", branch: "bjornt/other" }),
        makeTask({ id: 3, slug: "third", branch: "bjornt/third" }),
      ],
      sessions: {
        "1": { status: "waiting-input", reason: "pending question", since: "t0" },
        "2": { status: "waiting-approval", reason: "pending approval", since: "t0" },
        "3": { status: "working", reason: "agent_start frame", since: "t0" },
      },
      attention: {
        "1": { tier: "notify", status: "waiting-input", reason: "pending question" },
        "2": { tier: "interrupt", status: "waiting-approval", reason: "pending approval" },
      },
    });

    expect(screen.getByText("2 need you")).toBeInTheDocument();
  });
});

describe("TaskDetailView", () => {
  function stubDetailFetch(detail: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(detail) }),
    );
  }

  it("renders the metadata panel with derived workshop status", async () => {
    const task = makeTask({ workshop_id: "ws-maas-fix-bug" });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", { projects: [project], tasks: [task] });

    const meta = await screen.findByTestId("task-metadata");
    expect(meta).toHaveTextContent("bjornt/fix-bug");
    expect(meta).toHaveTextContent("/home/op/tasks/maas/fix-bug");
    expect(screen.getByTestId("workshop-status")).toHaveTextContent("present · ws-maas-fix-bug");
  });

  it("shows escape-hatch commands with the task's clone path", async () => {
    const task = makeTask({ workshop_id: "ws-maas-fix-bug" });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", { projects: [project], tasks: [task] });

    const hatch = await screen.findByTestId("escape-hatch");
    expect(hatch).toHaveTextContent("cd /home/op/tasks/maas/fix-bug");
    expect(hatch).toHaveTextContent("workshop shell");
    expect(hatch).toHaveTextContent("omp --resume");
  });

  it("task cards link to the detail route", async () => {
    const task = makeTask({ workshop_id: "ws-maas-fix-bug" });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks", { projects: [project], tasks: [task] });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("task-link-1"));
    expect(await screen.findByTestId("task-metadata")).toBeInTheDocument();
  });
});
