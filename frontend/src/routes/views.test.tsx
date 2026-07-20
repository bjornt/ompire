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

  emitSnapshot(payload: { projects: unknown[]; tasks: unknown[]; sessions?: unknown }) {
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
  snapshot: { projects: unknown[]; tasks: unknown[]; sessions?: unknown },
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

  it("counts failed sessions in the N-need-you pill; working sessions stay silent", async () => {
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
    });

    // Task 3 is failed twice over (registry + session) but counts once.
    expect(screen.getByText("2 need you")).toBeInTheDocument();
    expect(document.title).toBe("(2) ompire");
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
