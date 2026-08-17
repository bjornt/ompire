import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Task, Template } from "../types";

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

  emitSnapshot(payload: {
    projects: unknown[];
    templates?: unknown[];
    tasks: unknown[];
    sessions?: unknown;
    workflows?: unknown;
    attention?: unknown;
    reviews?: unknown;
    ships?: unknown;
    gpg?: unknown;
  }) {
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
};

function makeTemplate(overrides: Partial<Template> = {}): Template {
  return {
    name: "maas",
    project_name: "maas",
    base_branch: "master",
    branch_pattern: "bjornt/<slug>",
    workflow: "single-step",
    workshop_additions: "project",
    model: null,
    thinking: null,
    preamble: "",
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T00:00:00Z",
    ...overrides,
  };
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    project_name: "maas",
    template_name: "maas",
    slug: "fix-bug",
    branch: "bjornt/fix-bug",
    clone_path: "/home/op/tasks/maas/fix-bug",
    state: "created",
    prompt: "fix it",
    error: null,
    workshop_id: null,
    spawn_completed_at: "2026-07-18T00:01:00Z",
    pr_url: null,
    pr_state: null,
    pr_merged_at: null,
    workflow_name: "single-step",
    workflow_status: null,
    workflow_step: null,
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
  snapshot: {
    projects: unknown[];
    templates?: unknown[];
    tasks: unknown[];
    sessions?: unknown;
    workflows?: unknown;
    attention?: unknown;
    reviews?: unknown;
    ships?: unknown;
    gpg?: unknown;
  },
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
  it("previews the branch name from the selected template's pattern as the slug is typed", async () => {
    await renderAt("/spawn", { projects: [project], templates: [makeTemplate()], tasks: [] });
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Task slug"), "vlan-mtu");
    expect(screen.getByTestId("branch-preview")).toHaveTextContent(
      "branch: bjornt/vlan-mtu · off origin/master",
    );
  });

  it("lists one option per template with its summary line and an edit link", async () => {
    await renderAt("/spawn", {
      projects: [project],
      templates: [
        makeTemplate(),
        makeTemplate({ name: "llmvet", base_branch: "main", model: "haiku-4.5" }),
      ],
      tasks: [],
    });

    const picker = screen.getByLabelText("Project template");
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "maas — /home/op/proj/maas · base master · omp default · wf:single-step",
      "llmvet — /home/op/proj/maas · base main · haiku-4.5 · wf:single-step",
    ]);
    expect(screen.getByRole("link", { name: "Edit templates" })).toHaveAttribute(
      "href",
      "/settings",
    );
  });

  it("reflects the selected template in the read-only workflow block", async () => {
    await renderAt("/spawn", {
      projects: [project],
      templates: [makeTemplate(), makeTemplate({ name: "reviewer", workflow: "review-only" })],
      tasks: [],
    });
    const user = userEvent.setup();

    expect(screen.getByTestId("workflow-block")).toHaveTextContent(
      "single-step — one agent session, operator reviews from idle (default)",
    );

    // A workflow outside the registered list renders as its bare name.
    await user.selectOptions(screen.getByLabelText("Project template"), "reviewer");
    expect(screen.getByTestId("workflow-block")).toHaveTextContent(/^review-only$/);
  });

  it("defaults the override controls to the template's model and thinking", async () => {
    await renderAt("/spawn", {
      projects: [project],
      templates: [makeTemplate({ model: "fable-5", thinking: "medium" })],
      tasks: [],
    });

    expect(screen.getByLabelText("Model override")).toHaveAttribute(
      "placeholder",
      "template default (fable-5)",
    );
    const thinking = screen.getByLabelText("Thinking");
    expect(within(thinking).getAllByRole("option")[0]).toHaveTextContent(
      "template default (medium)",
    );
  });

  it("falls back to 'omp default' in the override labels for a null template model", async () => {
    await renderAt("/spawn", {
      projects: [project],
      templates: [makeTemplate()],
      tasks: [],
    });

    expect(screen.getByLabelText("Model override")).toHaveAttribute(
      "placeholder",
      "template default (omp default)",
    );
    const thinking = screen.getByLabelText("Thinking");
    expect(within(thinking).getAllByRole("option")[0]).toHaveTextContent(
      "template default (omp default)",
    );
  });

  it("posts template_name, slug, and prompt without overrides when none are set", async () => {
    await renderAt("/spawn", {
      projects: [project],
      templates: [makeTemplate({ model: "fable-5", thinking: "medium" })],
      tasks: [],
    });
    const user = userEvent.setup();
    const spawned = makeTask({ spawn_completed_at: null });
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: () => Promise.resolve(spawned) });
    vi.stubGlobal("fetch", fetchMock);

    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await user.type(screen.getByLabelText("Prompt"), "fix it");
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ template_name: "maas", slug: "fix-bug", prompt: "fix it" }),
      }),
    );
  });

  it("includes model/thinking in the submit body only when overridden", async () => {
    await renderAt("/spawn", {
      projects: [project],
      templates: [makeTemplate({ model: "fable-5", thinking: "medium" })],
      tasks: [],
    });
    const user = userEvent.setup();
    const spawned = makeTask({ spawn_completed_at: null });
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: () => Promise.resolve(spawned) });
    vi.stubGlobal("fetch", fetchMock);

    await user.type(screen.getByLabelText("Task slug"), "fix-bug");
    await user.type(screen.getByLabelText("Model override"), "haiku-4.5");
    await user.selectOptions(screen.getByLabelText("Thinking"), "high");
    await user.click(screen.getByRole("button", { name: "Spawn task" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          template_name: "maas",
          slug: "fix-bug",
          prompt: "",
          model: "haiku-4.5",
          thinking: "high",
        }),
      }),
    );
  });

  it("renders pipeline progress from spawn_step events after submit", async () => {
    await renderAt("/spawn", { projects: [project], templates: [makeTemplate()], tasks: [] });
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
    await renderAt("/spawn", { projects: [project], templates: [makeTemplate()], tasks: [] });
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
    await renderAt("/spawn", { projects: [project], templates: [makeTemplate()], tasks: [] });
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
    await renderAt("/spawn", { projects: [project], templates: [makeTemplate()], tasks: [] });
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

  it("keeps reporting workflow_step events as rows in the same progress list", async () => {
    await renderAt("/spawn", { projects: [project], templates: [makeTemplate()], tasks: [] });
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
      for (const step of ["fetch", "clone", "branch", "workshop", "agent", "prompt"]) {
        socket().emit("spawn_step", { task_id: 1, step, status: "started" });
        socket().emit("spawn_step", { task_id: 1, step, status: "ok" });
      }
      socket().emit("task_updated", { ...spawned, spawn_completed_at: "t1" });
      // The spawn pipeline hands off to the workflow run.
      socket().emit("workflow_step", {
        task_id: 1,
        step: "work",
        kind: "agent",
        session: "main",
        status: "started",
      });
      socket().emit("task_updated", {
        ...spawned,
        spawn_completed_at: "t1",
        workflow_status: "running",
        workflow_step: "work",
      });
    });

    const progress = screen.getByTestId("spawn-progress");
    const row = within(progress).getByTestId("workflow-step-work");
    expect(row).toHaveAttribute("data-step-status", "running");
    expect(row).toHaveTextContent("work");
    expect(row).toHaveTextContent("agent · session main");
    // The pipeline rows above are untouched.
    expect(progress.querySelectorAll("[data-step-status]")).toHaveLength(7);

    act(() => {
      socket().emit("workflow_step", {
        task_id: 1,
        step: "work",
        kind: "agent",
        session: "main",
        status: "failed",
        error: "agent exited with code 1",
      });
    });

    const failedRow = within(progress).getByTestId("workflow-step-work");
    expect(failedRow).toHaveAttribute("data-step-status", "failed");
    expect(
      within(progress).getByTestId("stderr-workflow-work"),
    ).toHaveTextContent("agent exited with code 1");
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
      sessions: { "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } } },
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
      sessions: { "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } } },
    });

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
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
        "1": { main: { status: "failed", reason: "process exited with code 137", since: "t0" } },
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
        "1": { main: { status: "stalled", reason: "no frames for 300s", since: "t0" } },
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
        "1": { main: { status: "retrying", reason: "auto_retry_start: HTTP 429", since: "t0" } },
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
        "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
      },
    });

    act(() => {
      socket().emit("stats", {
        task_id: 1,
        session: "main",
        context_pct: 85,
        tokens: { input: 1200, output: 340 },
        cost: 0.0123,
      });
      socket().emit("advisory", { task_id: 1, session: "main", kind: "context-high", context_pct: 85 });
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
        "1": { main: { status: "idle", reason: "agent_end, queue empty after 2.0s", since: "t0" } },
      },
    });

    act(() => {
      socket().emit("advisory", { task_id: 1, session: "main", kind: "maybe-waiting" });
    });

    expect(screen.getByTestId("maybe-waiting-1")).toHaveTextContent(
      "may be waiting for a reply",
    );

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "idle",
        to: "working",
        reason: "agent_start frame",
      });
      socket().emit("advisory_cleared", { task_id: 1, session: "main", kind: "maybe-waiting" });
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
        "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
        "2": { main: { status: "failed", reason: "process exited with code 137", since: "t0" } },
        "3": { main: { status: "failed", reason: "stopped by operator", since: "t0" } },
      },
      attention: {
        "2": { tier: "interrupt", status: "failed", reason: "process exited with code 137", session: "main" },
        "3": { tier: "interrupt", status: "failed", reason: "stopped by operator", session: "main" },
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
      sessions: { "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } } },
      attention: {},
    });
    expect(screen.getByText("0 need you")).toBeInTheDocument();

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
        from: "working",
        to: "waiting-input",
        reason: "pending question",
      });
      socket().emit("attention", {
        task_id: 1,
        tier: "notify",
        status: "waiting-input",
        reason: "pending question",
        session: "main",
      });
    });
    expect(screen.getByText("1 need you")).toBeInTheDocument();
    expect(document.title).toBe("(1) ompire");

    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
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
        session: "main",
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
          main: {
            status: "waiting-input",
            reason: "pending question 'ask-ui-1'",
            since: "t0",
            question: askQuestion,
          },
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
      "/api/tasks/1/sessions/main/agent/answer",
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
          main: {
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
        },
        "2": {
          main: {
            status: "waiting-approval",
            reason: "pending approval",
            since: "t0",
            question: { id: "approval-ui-1", kind: "approval", questions: [] },
          },
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
          main: {
            status: "waiting-input",
            reason: "pending question",
            since: "t0",
            question: askQuestion,
          },
        },
      },
    });
    expect(screen.getByTestId("quick-answer-1")).toBeInTheDocument();

    act(() => {
      socket().emit("question_resolved", { task_id: 1, session: "main", question_id: "ask-ui-1" });
    });
    act(() => {
      socket().emit("status_changed", {
        task_id: 1,
        session: "main",
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
        "1": { main: { status: "waiting-input", reason: "pending question", since: "t0" } },
        "2": { main: { status: "waiting-approval", reason: "pending approval", since: "t0" } },
        "3": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
      },
      attention: {
        "1": { tier: "notify", status: "waiting-input", reason: "pending question", session: "main" },
        "2": { tier: "interrupt", status: "waiting-approval", reason: "pending approval", session: "main" },
      },
    });

    expect(screen.getByText("2 need you")).toBeInTheDocument();
  });
});

describe("TasksView workflow pills", () => {
  function stepRecord(overrides: Record<string, unknown>) {
    return {
      task_id: 1,
      seq: 1,
      step: "work",
      kind: "agent",
      session: "main",
      status: "running",
      outcome: null,
      error: null,
      prompted_at: null,
      started_at: "t0",
      finished_at: null,
      ...overrides,
    };
  }

  it("prefixes the pill with the current step while an agent step runs", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "running", workflow_step: "work" })],
      sessions: {
        "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
      },
      workflows: {
        "1": { name: "single-step", status: "running", step: "work", steps: [stepRecord({})] },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card.querySelector(".statePill.live")).toHaveTextContent("work: working");
    // Tier styling and the slide bar still follow the session status.
    expect(screen.getByTestId("slide-bar-1")).toBeInTheDocument();
  });

  it("reads '<step>: waiting-input' with the waiting tier when the step's session asks", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "running", workflow_step: "validate" })],
      sessions: {
        "1": {
          main: {
            status: "waiting-input",
            reason: "pending question 'ask-1'",
            since: "t0",
            question: {
              id: "ask-1",
              kind: "ask",
              questions: [
                {
                  prompt: "Widen?",
                  options: [{ value: "y", label: "Yes", description: null }],
                  multi: false,
                  recommended: null,
                  allowsOther: false,
                },
              ],
            },
          },
        },
      },
      workflows: {
        "1": {
          name: "reproduce-and-fix",
          status: "running",
          step: "validate",
          steps: [stepRecord({ step: "validate" })],
        },
      },
    });

    expect(screen.getByTestId("task-card-1")).toHaveTextContent("validate: waiting-input");
  });

  it("reads '<step>: running' for command steps", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "running", workflow_step: "build" })],
      sessions: {
        "1": { main: { status: "idle", reason: "queue empty", since: "t0" } },
      },
      workflows: {
        "1": {
          name: "build-and-fix",
          status: "running",
          step: "build",
          steps: [
            stepRecord({ step: "work", status: "ok", finished_at: "t1" }),
            stepRecord({ seq: 2, step: "build", kind: "command", session: null }),
          ],
        },
      },
    });

    const pill = screen.getByTestId("workflow-pill-1");
    expect(pill).toHaveTextContent("build: running");
    expect(pill.className).toContain("live");
  });

  it("reads '<step>: waiting' with notify styling while parked at a gate", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "waiting", workflow_step: "confirm" })],
      sessions: {
        "1": { main: { status: "idle", reason: "queue empty", since: "t0" } },
      },
      workflows: {
        "1": {
          name: "reproduce-and-fix",
          status: "waiting",
          step: "confirm",
          steps: [
            stepRecord({ step: "fix", status: "ok", finished_at: "t1" }),
            stepRecord({
              seq: 2,
              step: "confirm",
              kind: "gate",
              session: null,
              status: "waiting",
              outcome: { message: "Ship it?" },
            }),
          ],
        },
      },
    });

    const pill = screen.getByTestId("workflow-pill-1");
    expect(pill).toHaveTextContent("confirm: waiting");
    expect(pill.className).toContain("notify");
  });

  it("renders the bare session status once the run completes", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "complete", workflow_step: "work" })],
      sessions: {
        "1": { main: { status: "idle", reason: "queue empty", since: "t0" } },
      },
      workflows: {
        "1": {
          name: "single-step",
          status: "complete",
          step: "work",
          steps: [stepRecord({ status: "ok", finished_at: "t1" })],
        },
      },
    });

    const pill = screen.getByTestId("task-card-1").querySelector(".statePill.neutral");
    expect(pill).toHaveTextContent("idle");
    expect(pill).not.toHaveTextContent("work:");
  });

  it("fails the card with the step error on the pill when the workflow fails", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ workflow_status: "failed", workflow_step: "build" })],
      sessions: {
        "1": { main: { status: "idle", reason: "queue empty", since: "t0" } },
      },
      workflows: {
        "1": {
          name: "build-and-fix",
          status: "failed",
          step: "build",
          steps: [
            stepRecord({ step: "work", status: "ok", finished_at: "t1" }),
            stepRecord({
              seq: 2,
              step: "build",
              kind: "command",
              session: null,
              status: "failed",
              error: "exit code 2",
              finished_at: "t2",
            }),
          ],
        },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card.className).toContain("failed");
    const pill = screen.getByTestId("workflow-failed-pill-1");
    expect(pill).toHaveTextContent("build: failed");
    expect(pill).toHaveAttribute("title", "exit code 2");
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

  it("annotates the spawned row with the task's template when set", async () => {
    const task = makeTask({ template_name: "maas" });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", { projects: [project], tasks: [task] });

    const meta = await screen.findByTestId("task-metadata");
    expect(meta).toHaveTextContent(/spawned.+template maas/);
  });

  it("omits the template annotation for tasks that predate templates", async () => {
    const task = makeTask({ template_name: null });
    stubDetailFetch({ ...task, workshop_status: "present" });
    await renderAt("/tasks/1", { projects: [project], tasks: [task] });

    const meta = await screen.findByTestId("task-metadata");
    expect(meta).toHaveTextContent("spawned");
    expect(meta).not.toHaveTextContent("template");
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

describe("Review capability (TasksView)", () => {
  it("renders a reviewing card with violet styling and a reopen link", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "reviewing", reason: "llmvet review on http://127.0.0.1:7180", since: "t0" } },
      },
      reviews: {
        "1": {
          status: "open",
          url: "http://127.0.0.1:7180",
          port: 7180,
          iterations: [],
        },
      },
    });

    const card = screen.getByTestId("task-card-1");
    expect(card).toHaveTextContent("reviewing");
    expect(card.querySelector(".statePill.review")).toBeInTheDocument();
    const link = card.querySelector(".reviewPillLink") as HTMLAnchorElement;
    expect(link).toBeInTheDocument();
    expect(link.href).toBe("http://127.0.0.1:7180/");
  });

  it("offers a Review action on idle cards and posts the review endpoint", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "idle", reason: "agent_end", since: "t0" } },
      },
    });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          task_id: 1,
          status: "open",
          url: "http://127.0.0.1:7180",
          port: 7180,
          iterations: [],
        }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(screen.getByTestId("review-button-1"));
    expect(fetchMock).toHaveBeenCalledWith("/api/tasks/1/review", expect.objectContaining({ method: "POST" }));
  });
});

describe("ShipFlowView", () => {
  it("renders the live review step and live commit/push steps", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      sessions: {
        "1": { main: { status: "reviewing", reason: "llmvet review", since: "t0" } },
      },
      gpg: { state: "cached", key: "ABC123", keygrip: "abc", detail: null, checked_at: "t0" },
      reviews: {
        "1": {
          status: "approved",
          url: "http://127.0.0.1:7180",
          port: 7180,
          iterations: [{ outcome: "approved", comment_count: null, stderr: null, recorded_at: "t2" }],
        },
      },
      ships: {
        "1": {
          status: "drafted",
          mode: "squash",
          draft: {
            commit_message: " agent commit",
            pr_title: " agent pr title",
            pr_body: " agent pr body",
            source: "agent",
          },
          commit_sha: null,
          pr_url: null,
          error: null,
          updated_at: "t0",
        },
      },
    });

    expect(screen.getByTestId("ship-flow")).toBeInTheDocument();
    expect(screen.getByTestId("ship-step-review")).toHaveTextContent("approved");
    expect(screen.getByTestId("ship-step-commit")).toHaveTextContent("Squash");
    expect(screen.getByTestId("ship-step-commit")).toHaveTextContent("Retain");
    expect((screen.getByTestId("commit-message") as HTMLTextAreaElement).value).toBe(" agent commit");
    expect((screen.getByTestId("pr-title") as HTMLInputElement).value).toBe(" agent pr title");
    expect((screen.getByTestId("pr-body") as HTMLTextAreaElement).value).toBe(" agent pr body");
    expect(screen.getByTestId("sign-commit-button")).not.toBeDisabled();
    expect(screen.getByTestId("push-pr-progress")).toHaveTextContent("Waiting for a signed commit.");
    expect(screen.getByTestId("ship-step-cleanup")).toBeInTheDocument();
  });

  it("blocks Sign & commit and shows the GPG locked banner with unlock command", async () => {
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      gpg: { state: "locked", key: "ABC123", keygrip: "abc", detail: null, checked_at: "t0" },
    });

    expect(screen.getByTestId("sign-commit-button")).toBeDisabled();
    expect(screen.getByTestId("gpg-locked-banner")).toBeInTheDocument();
    expect(screen.getByTestId("gpg-unlock-command")).toHaveTextContent(
      "echo | gpg --clearsign -u ABC123 >/dev/null",
    );
  });

  it("posts ship commit with edited fields and squash mode", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "committing",
          draft: null,
          commit_sha: null,
          pr_url: null,
          error: null,
          updated_at: "t1",
        }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [makeTask()],
      gpg: { state: "cached", key: "ABC123", keygrip: "abc", detail: null, checked_at: "t0" },
      ships: {
        "1": {
          status: "drafted",
          mode: "squash",
          draft: {
            commit_message: "draft commit",
            pr_title: "draft title",
            pr_body: "draft body",
            source: "agent",
          },
          commit_sha: null,
          pr_url: null,
          error: null,
          updated_at: "t0",
        },
      },
    });

    await user.clear(screen.getByTestId("commit-message"));
    await user.type(screen.getByTestId("commit-message"), "Final commit");
    await user.clear(screen.getByTestId("pr-title"));
    await user.type(screen.getByTestId("pr-title"), "Final PR title");
    await user.click(screen.getByTestId("sign-commit-button"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/ship/commit",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining("Final commit"),
      }),
    );
    const body = JSON.parse(fetchMock.mock.calls[fetchMock.mock.calls.length - 1][1].body as string);
    expect(body).toEqual({
      message: "Final commit",
      pr_title: "Final PR title",
      pr_body: "draft body",
      mode: "squash",
    });
  });

  it("shows the PR link once a ship finishes with a pr_url", async () => {
    const task = makeTask();
    await renderAt("/ship/1", {
      projects: [project],
      tasks: [task],
      ships: {
        "1": {
          status: "shipped",
          mode: "squash",
          draft: null,
          commit_sha: "abc123",
          pr_url: "https://github.com/ompire/maas/pull/42",
          error: null,
          updated_at: "t1",
        },
      },
    });

    const link = screen.getByTestId("pr-link") as HTMLAnchorElement;
    expect(link.href).toBe("https://github.com/ompire/maas/pull/42");
  });

  it("shows a not-found message for an unknown task id", async () => {
    await renderAt("/ship/999", { projects: [project], tasks: [makeTask()] });
    expect(screen.getByTestId("ship-flow-not-found")).toHaveTextContent("Task not found");
  });
});

describe("Chrome GPG chip", () => {
  it("shows cached when the daemon reports a cached key", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      gpg: { state: "cached", key: "ABC123", keygrip: "abc", detail: null, checked_at: "t0", ttl: 10500 },
    });

    const chip = screen.getByTestId("gpg-chip");
    expect(chip).toHaveTextContent("gpg cached 2h 55m");
  });

  it("shows locked with an unlock instruction in the title", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
      gpg: { state: "locked", key: "ABC123", keygrip: "abc", detail: null, checked_at: "t0" },
    });

    const chip = screen.getByTestId("gpg-chip");
    expect(chip).toHaveTextContent("gpg locked");
    expect(chip).toHaveAttribute(
      "title",
      "GPG signing key is locked. Warm the cache with: echo | gpg --clearsign -u ABC123 >/dev/null",
    );
  });

  it("shows a faint placeholder when gpg state is unknown", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask()],
    });

    const chip = screen.getByTestId("gpg-chip");
    expect(chip).toHaveTextContent("gpg —");
  });
});

describe("TasksView PR link", () => {
  it("renders a PR link on a card when task.pr_url is set", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [makeTask({ pr_url: "https://github.com/ompire/maas/pull/7" })],
    });

    const link = screen.getByTestId("task-pr-link-1") as HTMLAnchorElement;
    expect(link).toBeInTheDocument();
    expect(link.href).toBe("https://github.com/ompire/maas/pull/7");
  });
});

describe("TasksView Shipped section (merge-poll capability)", () => {
  const shippedTask = () =>
    makeTask({ pr_url: "https://github.com/ompire/maas/pull/7" });

  it("renders a collapsed row once a task has a pr_url", async () => {
    await renderAt("/tasks", { projects: [project], tasks: [shippedTask()] });

    const row = screen.getByTestId("shipped-row-1");
    expect(row).toHaveTextContent("shipped");
    expect(row).toHaveTextContent("maas/fix-bug");
    expect(row).toHaveTextContent("maas#7 · open");
    expect(row).toHaveTextContent("awaiting merge · cleanup deferred");
    // Live rows link to the Ship Flow view, where the cleanup action lives.
    const link = screen.getByTestId("shipped-link-1") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/ship/1");
  });

  it("shows no section when no task has shipped", async () => {
    await renderAt("/tasks", { projects: [project], tasks: [makeTask()] });
    expect(screen.queryByTestId("shipped-section")).not.toBeInTheDocument();
  });

  it("flips the row note live when a poll lands task_updated with pr_state merged", async () => {
    await renderAt("/tasks", { projects: [project], tasks: [shippedTask()] });
    expect(screen.getByTestId("shipped-row-1")).toHaveTextContent("awaiting merge");

    act(() => {
      socket().emit(
        "task_updated",
        makeTask({
          pr_url: "https://github.com/ompire/maas/pull/7",
          pr_state: "merged",
          pr_merged_at: new Date(Date.now() - 5 * 60_000).toISOString(),
        }),
      );
    });

    const row = screen.getByTestId("shipped-row-1");
    expect(row).toHaveTextContent("maas#7 · merged");
    expect(row).toHaveTextContent("merged · ready for cleanup");
  });

  it("keeps archived shipped tasks as inert cleaned-up rows", async () => {
    await renderAt("/tasks", {
      projects: [project],
      tasks: [
        shippedTask(),
        makeTask({
          id: 2,
          slug: "old-fix",
          state: "archived",
          pr_url: "https://github.com/ompire/maas/pull/3",
          pr_state: "merged",
        }),
      ],
    });

    const row = screen.getByTestId("shipped-row-2");
    expect(row).toHaveTextContent("cleaned up");
    expect(screen.queryByTestId("shipped-link-2")).not.toBeInTheDocument();
    // Archived tasks still stay out of the card grid.
    expect(screen.queryByTestId("task-card-2")).not.toBeInTheDocument();
  });
});

describe("ShipFlowView Cleanup step (merge-poll capability)", () => {
  const prTask = (overrides: Partial<Task> = {}) =>
    makeTask({ pr_url: "https://github.com/ompire/maas/pull/7", ...overrides });

  it("stays inert before the task has shipped a PR", async () => {
    await renderAt("/ship/1", { projects: [project], tasks: [makeTask()] });

    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent("unlocks once this task has shipped");
    expect(screen.queryByTestId("cleanup-ship-button")).not.toBeInTheDocument();
  });

  it("defers cleanup while the PR is open", async () => {
    await renderAt("/ship/1", { projects: [project], tasks: [prTask({ pr_state: "open" })] });

    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent("Awaiting merge · cleanup deferred");
    expect(screen.queryByTestId("cleanup-ship-button")).not.toBeInTheDocument();
  });

  it("offers confirmed cleanup once the PR is merged", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    await renderAt("/ship/1", {
      projects: [project],
      tasks: [prTask({ pr_state: "merged", pr_merged_at: "2026-08-14T09:30:00Z" })],
    });

    await user.click(screen.getByTestId("cleanup-ship-button"));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("/home/op/tasks/maas/fix-bug"));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/cleanup",
      expect.objectContaining({ method: "POST" }),
    );

    act(() => {
      socket().emit("task_updated", prTask({ state: "archived" }));
    });
    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent("Cleaned up");
    expect(screen.queryByTestId("cleanup-ship-button")).not.toBeInTheDocument();
  });

  it("labels a closed-unmerged PR and still offers cleanup", async () => {
    await renderAt("/ship/1", { projects: [project], tasks: [prTask({ pr_state: "closed" })] });

    expect(screen.getByTestId("cleanup-hint")).toHaveTextContent("closed without merging");
    expect(screen.getByTestId("ship-step-cleanup")).toHaveTextContent("closed");
    expect(screen.getByTestId("cleanup-ship-button")).toBeInTheDocument();
  });

  it("declining the confirmation sends no request", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    await renderAt("/ship/1", { projects: [project], tasks: [prTask({ pr_state: "merged" })] });
    await user.click(screen.getByTestId("cleanup-ship-button"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("ProjectsView (projects-view capability)", () => {
  const llmvet = {
    name: "llmvet",
    title: "LLM-assisted patch review CLI",
    upstream_url: "git@github.com:bjornt/llmvet.git",
    fork_url: null,
    checkout_path: "/home/op/proj/llmvet",
  };

  it("renders one card per project with fork-less annotation and active-task pills", async () => {
    const forked = { ...project, fork_url: "git@github.com:bjornt/maas.git" };
    const tasks = [
      makeTask({ id: 1, project_name: "maas" }),
      makeTask({ id: 2, project_name: "maas", slug: "other", state: "archived" }),
      makeTask({ id: 3, project_name: "llmvet", slug: "vet-it" }),
    ];
    await renderAt("/projects", { projects: [forked, llmvet], tasks });

    const maasCard = screen.getByTestId("project-card-maas");
    // fork set: fork row present, no own-upstream note
    expect(within(maasCard).getByText("fork")).toBeInTheDocument();
    expect(maasCard).toHaveTextContent("git@github.com:bjornt/maas.git");
    expect(maasCard).not.toHaveTextContent("you own upstream");
    // 1 live + 1 archived task => "1 active task"
    expect(screen.getByTestId("active-tasks-maas")).toHaveTextContent("1 active task");

    const llmvetCard = screen.getByTestId("project-card-llmvet");
    expect(llmvetCard).toHaveTextContent("you own upstream — no fork needed");
    expect(within(llmvetCard).queryByText("fork")).not.toBeInTheDocument();
    expect(screen.getByTestId("active-tasks-llmvet")).toHaveTextContent("1 active task");
  });

  it("shows an empty state when no projects exist", async () => {
    await renderAt("/projects", { projects: [], tasks: [] });
    expect(screen.getByTestId("projects-empty-state")).toBeInTheDocument();
  });

  it("creates a project via the form and closes it", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ...llmvet }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-name"), "llmvet");
    await user.type(screen.getByTestId("new-project-title"), "LLM-assisted patch review CLI");
    await user.type(screen.getByTestId("new-project-upstream"), "git@github.com:bjornt/llmvet.git");
    await user.click(screen.getByRole("button", { name: "Create project" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "llmvet",
          title: "LLM-assisted patch review CLI",
          upstream_url: "git@github.com:bjornt/llmvet.git",
          fork_url: null,
        }),
      }),
    );

    // The form closed; the new card arrives over the wire, not from the response.
    expect(screen.queryByTestId("new-project-form")).not.toBeInTheDocument();
    act(() => {
      socket().emit("project_created", llmvet);
    });
    expect(screen.getByTestId("project-card-llmvet")).toBeInTheDocument();
  });

  it("keeps the form open with the daemon's detail on duplicate name", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () => Promise.resolve({ detail: "project 'maas' already exists" }),
      }),
    );

    await user.click(screen.getByTestId("new-project-toggle"));
    await user.type(screen.getByTestId("new-project-name"), "maas");
    await user.type(screen.getByTestId("new-project-title"), "dup");
    await user.type(screen.getByTestId("new-project-upstream"), "https://example.com/maas.git");
    await user.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByTestId("new-project-error")).toHaveTextContent(
      "project 'maas' already exists",
    );
    expect(screen.getByTestId("new-project-form")).toBeInTheDocument();
  });

  it("disables rename while tasks reference the project", async () => {
    await renderAt("/projects", { projects: [project], tasks: [makeTask()] });
    const user = userEvent.setup();

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    expect(screen.getByTestId("edit-name-maas")).toBeDisabled();
    expect(screen.getByTestId("rename-note-maas")).toHaveTextContent(
      "Referenced by 1 tasks — rename via",
    );
  });

  it("sends new_name when an unreferenced project is renamed", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const renamed = { ...project, name: "maas-ng" };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(renamed),
    });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    const nameInput = screen.getByTestId("edit-name-maas");
    expect(nameInput).toBeEnabled();
    await user.clear(nameInput);
    await user.type(nameInput, "maas-ng");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // base_branch/branch_pattern moved to templates — the save no longer
    // round-trips them (projects capability, per-project defaults removed).
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/maas",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          title: project.title,
          upstream_url: project.upstream_url,
          fork_url: null,
          checkout_path: project.checkout_path,
          new_name: "maas-ng",
        }),
      }),
    );

    act(() => {
      socket().emit("project_renamed", { old_name: "maas", project: renamed });
    });
    expect(screen.getByTestId("project-card-maas-ng")).toBeInTheDocument();
    expect(screen.queryByTestId("project-card-maas")).not.toBeInTheDocument();
  });

  it("confirms removal and surfaces the daemon's 409 inline", async () => {
    await renderAt("/projects", { projects: [project], tasks: [makeTask()] });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () =>
          Promise.resolve({
            detail: "project 'maas' has tasks referencing it: maas/fix-bug (created)",
          }),
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    await user.click(screen.getByTestId("remove-project-maas"));

    expect(await screen.findByTestId("edit-error-maas")).toHaveTextContent(
      "project 'maas' has tasks referencing it: maas/fix-bug (created)",
    );
    expect(screen.getByTestId("edit-panel-maas")).toBeInTheDocument();
  });

  it("deletes an unreferenced project after confirmation", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ deleted: "maas" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    await user.click(screen.getByTestId("remove-project-maas"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/maas",
      expect.objectContaining({ method: "DELETE" }),
    );
    act(() => {
      socket().emit("project_deleted", { name: "maas" });
    });
    expect(screen.queryByTestId("project-card-maas")).not.toBeInTheDocument();
  });

  it("declining the remove confirmation sends no request", async () => {
    await renderAt("/projects", { projects: [project], tasks: [] });
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    await user.click(within(screen.getByTestId("project-card-maas")).getByRole("button", { name: "Edit" }));
    await user.click(screen.getByTestId("remove-project-maas"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("active-tasks pill navigates to the filtered Tasks view", async () => {
    const tasks = [
      makeTask({ id: 1, project_name: "maas" }),
      makeTask({ id: 2, project_name: "llmvet", slug: "vet-it", clone_path: "/home/op/tasks/llmvet/vet-it" }),
    ];
    await renderAt("/projects", { projects: [project, llmvet], tasks });
    const user = userEvent.setup();

    await user.click(screen.getByTestId("active-tasks-llmvet"));

    expect(window.location.pathname).toBe("/tasks");
    expect(window.location.search).toBe("?project=llmvet");
    expect(screen.getByTestId("project-filter-label")).toHaveTextContent("llmvet");
    expect(screen.getByTestId("task-link-2")).toBeInTheDocument();
    expect(screen.queryByTestId("task-link-1")).not.toBeInTheDocument();
  });
});

describe("SettingsView (templates capability)", () => {
  it("renders one row per template from the snapshot with its summary line", async () => {
    await renderAt("/settings", {
      projects: [project],
      templates: [
        makeTemplate({ model: "fable-5" }),
        makeTemplate({ name: "llmvet", base_branch: "main", model: "haiku-4.5" }),
        makeTemplate({ name: "reviewer", branch_pattern: "review/<slug>" }),
      ],
      tasks: [],
    });

    expect(screen.getByRole("heading", { name: "Templates & settings" })).toBeInTheDocument();
    expect(screen.getByText("what spawn needs, and how attention reaches you")).toBeInTheDocument();

    const maasRow = screen.getByTestId("template-row-maas");
    expect(maasRow).toHaveTextContent("maas");
    expect(maasRow).toHaveTextContent(
      "/home/op/proj/maas · master · bjornt/<slug> · fable-5 · wf:single-step",
    );
    expect(screen.getByTestId("template-row-llmvet")).toHaveTextContent(
      "/home/op/proj/maas · main · bjornt/<slug> · haiku-4.5 · wf:single-step",
    );
    // A null model falls back to "omp default".
    expect(screen.getByTestId("template-row-reviewer")).toHaveTextContent(
      "/home/op/proj/maas · master · review/<slug> · omp default · wf:single-step",
    );
  });

  it("shows an empty state when no templates exist", async () => {
    await renderAt("/settings", { projects: [project], templates: [], tasks: [] });
    expect(screen.getByTestId("templates-empty-state")).toBeInTheDocument();
  });

  it("creates a template via the editor and lists the row from the broadcast", async () => {
    await renderAt("/settings", { projects: [project], templates: [], tasks: [] });
    const user = userEvent.setup();
    const created = makeTemplate({ name: "llmvet", base_branch: "main" });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(created),
    });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(screen.getByTestId("new-template-toggle"));
    const editor = screen.getByTestId("template-editor");
    expect(within(editor).getByText("New template")).toBeInTheDocument();
    // The picked project's checkout/remote render read-only beneath the picker.
    expect(screen.getByTestId("template-project-derived")).toHaveTextContent(
      "checkout /home/op/proj/maas · remote https://example.com/maas.git",
    );

    await user.type(screen.getByTestId("template-name"), "llmvet");
    await user.clear(screen.getByTestId("template-base-branch"));
    await user.type(screen.getByTestId("template-base-branch"), "main");
    await user.click(screen.getByRole("button", { name: "Create template" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/templates",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "llmvet",
          project_name: "maas",
          base_branch: "main",
          branch_pattern: "ompire/<slug>",
          workflow: "single-step",
          workshop_additions: "project",
          model: null,
          thinking: null,
          preamble: "",
        }),
      }),
    );

    // Editor closed; the row arrives over the wire, not from the response.
    expect(screen.queryByTestId("template-editor")).not.toBeInTheDocument();
    act(() => {
      socket().emit("template_created", created);
    });
    expect(screen.getByTestId("template-row-llmvet")).toBeInTheDocument();
  });

  it("saves edits with PUT and reflects the broadcast without a reload", async () => {
    await renderAt("/settings", {
      projects: [project],
      templates: [makeTemplate()],
      tasks: [],
    });
    const user = userEvent.setup();
    const updated = makeTemplate({ model: "fable-5", preamble: "Run pytest from the repo root." });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(updated),
    });
    vi.stubGlobal("fetch", fetchMock);

    await user.click(
      within(screen.getByTestId("template-row-maas")).getByRole("button", { name: "Edit" }),
    );
    const editor = screen.getByTestId("template-editor");
    expect(within(editor).getByText("Template · maas")).toBeInTheDocument();
    expect(screen.getByTestId("template-base-branch")).toHaveValue("master");

    await user.type(screen.getByTestId("template-model"), "fable-5");
    await user.type(screen.getByTestId("template-preamble"), "Run pytest from the repo root.");
    await user.click(screen.getByRole("button", { name: "Save template" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/templates/maas",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          project_name: "maas",
          base_branch: "master",
          branch_pattern: "bjornt/<slug>",
          workflow: "single-step",
          workshop_additions: "project",
          model: "fable-5",
          thinking: null,
          preamble: "Run pytest from the repo root.",
        }),
      }),
    );

    expect(screen.queryByTestId("template-editor")).not.toBeInTheDocument();
    act(() => {
      socket().emit("template_updated", updated);
    });
    expect(screen.getByTestId("template-row-maas")).toHaveTextContent("fable-5");
  });

  it("confirms removal and surfaces the daemon's 409 naming live tasks inline", async () => {
    await renderAt("/settings", {
      projects: [project],
      templates: [makeTemplate()],
      tasks: [makeTask()],
    });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () =>
          Promise.resolve({
            detail: "template 'maas' has tasks referencing it: maas/fix-bug (created)",
          }),
      }),
    );
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    await user.click(
      within(screen.getByTestId("template-row-maas")).getByRole("button", { name: "Edit" }),
    );
    await user.click(screen.getByTestId("remove-template-maas"));

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("Remove template maas?"));
    expect(await screen.findByTestId("template-editor-error")).toHaveTextContent(
      "template 'maas' has tasks referencing it: maas/fix-bug (created)",
    );
    // The editor stays open and the template is retained.
    expect(screen.getByTestId("template-editor")).toBeInTheDocument();
    expect(screen.getByTestId("template-row-maas")).toBeInTheDocument();
  });

  it("keeps the editor open with the daemon's 422 detail on an invalid save", async () => {
    await renderAt("/settings", {
      projects: [project],
      templates: [makeTemplate()],
      tasks: [],
    });
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: () =>
          Promise.resolve({ detail: "branch pattern must contain exactly one <slug>" }),
      }),
    );

    await user.click(
      within(screen.getByTestId("template-row-maas")).getByRole("button", { name: "Edit" }),
    );
    await user.clear(screen.getByTestId("template-branch-pattern"));
    await user.type(screen.getByTestId("template-branch-pattern"), "bjornt/no-slot");
    await user.click(screen.getByRole("button", { name: "Save template" }));

    expect(await screen.findByTestId("template-editor-error")).toHaveTextContent(
      "branch pattern must contain exactly one <slug>",
    );
    expect(screen.getByTestId("template-editor")).toBeInTheDocument();
  });

  it("removes an unreferenced template after confirmation", async () => {
    await renderAt("/settings", {
      projects: [project],
      templates: [makeTemplate()],
      tasks: [],
    });
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ deleted: "maas" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    await user.click(
      within(screen.getByTestId("template-row-maas")).getByRole("button", { name: "Edit" }),
    );
    await user.click(screen.getByTestId("remove-template-maas"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/templates/maas",
      expect.objectContaining({ method: "DELETE" }),
    );
    act(() => {
      socket().emit("template_deleted", { name: "maas" });
    });
    expect(screen.queryByTestId("template-row-maas")).not.toBeInTheDocument();
    expect(screen.getByTestId("templates-empty-state")).toBeInTheDocument();
  });

  it("declining the remove confirmation sends no request", async () => {
    await renderAt("/settings", {
      projects: [project],
      templates: [makeTemplate()],
      tasks: [],
    });
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    await user.click(
      within(screen.getByTestId("template-row-maas")).getByRole("button", { name: "Edit" }),
    );
    await user.click(screen.getByTestId("remove-template-maas"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
