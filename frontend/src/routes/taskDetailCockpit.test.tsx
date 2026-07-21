import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Task } from "../types";

/* Full-app cockpit tests: the transcript reads a second WebSocket (the agent
 * event channel), and the status strip / composer hit the agent-interaction
 * REST endpoints, so the mocks here are URL-aware. */

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {}
  send() {}

  emit(type: string, payload: unknown) {
    this.onmessage?.({ data: JSON.stringify({ seq: 0, ts: "t", type, payload }) });
  }

  emitSnapshot(payload: { projects: unknown[]; tasks: unknown[]; sessions?: unknown }) {
    this.onopen?.();
    this.emit("snapshot", payload);
  }
}

function mainSocket(): MockWebSocket {
  return MockWebSocket.instances.find(
    (s) => s.url.includes("/api/ws") && !s.url.includes("/agents/"),
  )!;
}

function agentSocket(): MockWebSocket | undefined {
  return MockWebSocket.instances.find((s) => s.url.includes("/api/ws/agents/"));
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
    workshop_id: "ws-maas-fix-bug",
    spawn_completed_at: "2026-07-18T00:01:00Z",
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T00:01:00Z",
    ...overrides,
  };
}

interface FetchBodies {
  state?: unknown;
  stats?: unknown;
}

function stubFetch(bodies: FetchBodies = {}) {
  const ok = (body: unknown) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  const fetchMock = vi.fn((url: string) => {
    const u = String(url);
    if (u.endsWith("/agent/state")) {
      return ok(
        bodies.state ?? {
          isStreaming: true,
          queuedMessageCount: 0,
          todos: [{ status: "completed" }, { status: "pending" }],
          model: "opus-4.8",
          contextUsage: 0.42,
        },
      );
    }
    if (u.endsWith("/agent/stats")) {
      return ok(bodies.stats ?? { inputTokens: 1200, outputTokens: 340, totalCostUsd: 0.0123 });
    }
    if (u.includes("/agent/")) return ok({ command: "ok", success: true }); // composer POSTs
    return ok({ ...makeTask(), workshop_status: "present" }); // GET /api/tasks/:id detail
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function renderDetail(session: unknown) {
  window.history.pushState({}, "", "/tasks/1");
  render(<App />);
  act(() => {
    mainSocket().emitSnapshot({ projects: [project], tasks: [makeTask()], sessions: session ?? {} });
  });
  // Wait for the detail fetch to resolve the metadata panel.
  await screen.findByTestId("task-metadata");
}

const workingSession = { "1": { status: "working", reason: "agent_start frame", since: "t0" } };

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("cockpit transcript", () => {
  it("streams assistant text, a collapsible tool card, and thinking from the channel", async () => {
    stubFetch();
    await renderDetail(workingSession);

    const agent = agentSocket();
    expect(agent).toBeDefined();
    act(() => {
      agent!.onopen?.();
      agent!.emit("agent_start", {});
      agent!.emit("message_end", {
        message: { role: "assistant", content: [{ type: "thinking", thinking: "planning" }] },
      });
      agent!.emit("message_end", {
        message: {
          role: "assistant",
          content: [
            { type: "text", text: "running the tests" },
            { type: "tool_use", id: "tool-1", name: "bash", input: { cmd: "pytest" } },
          ],
        },
      });
    });

    const transcript = screen.getByTestId("transcript");
    expect(within(transcript).getByText("running the tests")).toBeInTheDocument();
    expect(within(transcript).getByTestId("thinking-block")).toHaveTextContent("planning");
    const card = within(transcript).getByTestId("tool-card-tool-1");
    expect(card).toHaveTextContent("bash");
    expect((card as HTMLDetailsElement).open).toBe(false); // collapsed by default
  });

  it("nests subagent activity under its parent tool call", async () => {
    stubFetch();
    await renderDetail(workingSession);
    const agent = agentSocket()!;

    act(() => {
      agent.onopen?.();
      agent.emit("message_end", {
        message: { role: "assistant", content: [{ type: "tool_use", id: "spawn-1", name: "task" }] },
      });
      agent.emit("message_end", {
        parentToolUseId: "spawn-1",
        message: { role: "assistant", content: [{ type: "text", text: "sub result" }] },
      });
    });

    const nested = screen.getByTestId("subagent-spawn-1");
    expect(nested).toHaveTextContent("sub result");
  });
});

describe("cockpit composer", () => {
  it("steers a streaming agent through the steer endpoint", async () => {
    const fetchMock = stubFetch();
    await renderDetail(workingSession);
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/agent/state"))).toBe(true),
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Steer" }));
    await user.type(screen.getByLabelText("Message"), "focus on the parser");
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/agent/steer",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("disables the composer entirely when no agent is live", async () => {
    stubFetch();
    await renderDetail({ "1": { status: "failed", reason: "exited 137", since: "t0" } });

    expect(screen.getByLabelText("Message")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(agentSocket()).toBeUndefined(); // no channel opened for a dead agent
  });

  it("disables steer/interrupt when the agent is idle, keeps follow-up", async () => {
    stubFetch({ state: { isStreaming: false, queuedMessageCount: 0 } });
    await renderDetail({ "1": { status: "idle", reason: "queue empty", since: "t0" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Follow-up" })).toBeEnabled());

    expect(screen.getByRole("button", { name: "Steer" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Follow-up" })).toBeEnabled();
  });

  it("stays enabled with a note while a question is pending (waiting-input)", async () => {
    stubFetch({ state: { isStreaming: false, queuedMessageCount: 0 } });
    await renderDetail({
      "1": {
        status: "waiting-input",
        reason: "pending question 'ask-ui-1'",
        since: "t0",
        question: {
          id: "ask-ui-1",
          kind: "ask",
          questions: [
            {
              prompt: "?",
              options: [{ value: "a", label: "A", description: null }],
              multi: false,
              recommended: null,
              allowsOther: false,
            },
          ],
        },
      },
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Steer" })).toBeEnabled());

    expect(screen.getByRole("button", { name: "Follow-up" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeEnabled();
    expect(screen.getByTestId("composer-note")).toHaveTextContent("A question is pending");
  });
});

const askQuestion = {
  id: "ask-ui-1",
  kind: "ask" as const,
  questions: [
    {
      prompt: "Apply the same lock ordering to the dhcpd6 loop?",
      options: [
        { value: "both", label: "Yes, both loops", description: "Widen the fix" },
        { value: "v4-only", label: "v4 only", description: "Match the reproducer" },
      ],
      multi: false,
      recommended: "both",
      allowsOther: true,
    },
  ],
};

const approvalQuestion = { id: "approval-ui-1", kind: "approval" as const, questions: [] };

const waitingInputSession = {
  "1": { status: "waiting-input", reason: "pending question 'ask-ui-1'", since: "t0", question: askQuestion },
};

const waitingApprovalSession = {
  "1": {
    status: "waiting-approval",
    reason: "pending approval 'approval-ui-1'",
    since: "t0",
    question: approvalQuestion,
  },
};

describe("cockpit question card", () => {
  it("renders an ask question with recommended option and answers it", async () => {
    const fetchMock = stubFetch();
    await renderDetail(waitingInputSession);

    const card = screen.getByTestId("question-card");
    expect(within(card).getByText(/lock ordering/)).toBeInTheDocument();
    const recommended = within(card).getByRole("button", { name: /Yes, both loops/ });
    expect(within(recommended).getByText("rec")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(recommended);
    await user.click(within(card).getByRole("button", { name: "Send answer" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/agent/answer",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ question_id: "ask-ui-1", selections: ["both"] }),
      }),
    );
  });

  it("renders an approval gate as approve/deny", async () => {
    const fetchMock = stubFetch();
    await renderDetail(waitingApprovalSession);

    const card = screen.getByTestId("question-card");
    const user = userEvent.setup();
    await user.click(within(card).getByRole("button", { name: "Approve" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/agent/answer",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ question_id: "approval-ui-1", approved: true }),
      }),
    );
  });

  it("removes the card once the question resolves", async () => {
    stubFetch();
    await renderDetail(waitingInputSession);
    expect(screen.getByTestId("question-card")).toBeInTheDocument();

    act(() => {
      mainSocket().emit("question_resolved", { task_id: 1, question_id: "ask-ui-1" });
    });

    expect(screen.queryByTestId("question-card")).not.toBeInTheDocument();
  });
});

describe("cockpit status strip", () => {
  it("shows session state, reason, and the polled metrics", async () => {
    stubFetch();
    await renderDetail(workingSession);

    expect(screen.getByTestId("session-state")).toHaveTextContent("working");
    expect(screen.getByTestId("session-reason")).toHaveTextContent("agent_start frame");
    await waitFor(() => expect(screen.getByTestId("metric-todos")).toHaveTextContent("1/2"));
    expect(screen.getByTestId("metric-context")).toHaveTextContent("42%");
    expect(screen.getByTestId("metric-tokens")).toHaveTextContent("1200 in / 340 out");
    expect(screen.getByTestId("metric-cost")).toHaveTextContent("$0.0123");
    expect(screen.getByTestId("metric-model")).toHaveTextContent("opus-4.8");
  });

  it("renders an amber context ring instead of plain text at/above the advisory threshold", async () => {
    stubFetch({
      state: {
        isStreaming: false,
        queuedMessageCount: 0,
        todos: [],
        model: "opus-4.8",
        contextUsage: 0.85,
      },
    });
    await renderDetail(workingSession);

    const metric = await screen.findByTestId("metric-context");
    expect(metric).toHaveTextContent("85%");
    expect(metric.querySelector("[data-testid='context-ring']")).toBeInTheDocument();
  });

  it("updates the session state live on a status_changed event", async () => {
    stubFetch();
    await renderDetail(workingSession);
    expect(screen.getByTestId("session-state")).toHaveTextContent("working");

    act(() => {
      mainSocket().emit("status_changed", {
        task_id: 1,
        from: "working",
        to: "idle",
        reason: "queue empty after 2.0s",
      });
    });

    expect(screen.getByTestId("session-state")).toHaveTextContent("idle");
    expect(screen.getByTestId("session-reason")).toHaveTextContent("queue empty after 2.0s");
  });
});
