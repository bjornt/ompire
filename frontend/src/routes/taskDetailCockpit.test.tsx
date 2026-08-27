import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

  emitSnapshot(payload: {
    projects: unknown[];
    tasks: unknown[];
    sessions?: unknown;
    workflows?: unknown;
  }) {
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
};

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
    workshop_id: "ws-maas-fix-bug",
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

async function renderDetail(session: unknown, workflows?: unknown) {
  window.history.pushState({}, "", "/tasks/1");
  render(<App />);
  act(() => {
    mainSocket().emitSnapshot({
      projects: [project],
      tasks: [makeTask()],
      sessions: session ?? {},
      ...(workflows !== undefined ? { workflows } : {}),
    });
  });
  // Wait for the detail fetch to resolve the metadata panel.
  await screen.findByTestId("task-metadata");
}

const workingSession = {
  "1": { main: { status: "working", reason: "agent_start frame", since: "t0" } },
};

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
    await act(async () => {
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

/* Follow-the-stream behavior. jsdom performs no layout, so the transcript's
 * scroll geometry is whatever the test defines: `scrollTop` is backed by a real
 * variable and clamped the way a browser clamps it, so the component's writes
 * are observable and "at the end" means here what it means in a browser. The
 * real bound and the real overflow are verified in the browser, not here. */
function mockScrollGeometry(el: HTMLElement, clientHeight: number, scrollHeight: number) {
  let top = 0;
  let height = scrollHeight;
  const end = () => Math.max(0, height - clientHeight);
  const clamp = (v: number) => Math.max(0, Math.min(v, end()));
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => clientHeight });
  Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => height });
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = clamp(v);
    },
  });
  return {
    get scrollTop() {
      return top;
    },
    end,
    /** More output arrived: the stream got taller. */
    grow(by: number) {
      height += by;
    },
    /** The reader moved the scrollbar themselves. */
    readerScrollsTo(next: number) {
      top = clamp(next);
      fireEvent.scroll(el);
    },
  };
}

function lastAgentSocket(): MockWebSocket | undefined {
  return MockWebSocket.instances.filter((s) => s.url.includes("/api/ws/agents/")).at(-1);
}

function emitText(agent: MockWebSocket, text: string) {
  agent.emit("message_end", {
    message: { role: "assistant", content: [{ type: "text", text }] },
  });
}

/** Task detail with a live session, one transcript item so the scroll region
 * exists, and its geometry under the test's control. */
async function renderFollowingTranscript(session: unknown = workingSession, workflows?: unknown) {
  stubFetch();
  await renderDetail(session, workflows);
  const agent = lastAgentSocket()!;
  act(() => {
    agent.onopen?.();
    emitText(agent, "first line");
  });
  const geo = mockScrollGeometry(screen.getByTestId("transcript-stream"), 400, 1000);
  return { agent, geo };
}

describe("cockpit transcript follow", () => {
  it("keeps the newest output in view while the reader is at the end", async () => {
    const { agent, geo } = await renderFollowingTranscript();

    act(() => {
      geo.grow(600);
      emitText(agent, "second line");
    });

    expect(geo.scrollTop).toBe(geo.end());
  });

  it("stays pinned when the reader is within the threshold of the end", async () => {
    const { agent, geo } = await renderFollowingTranscript();

    geo.readerScrollsTo(geo.end() - 30); // a stray wheel tick, still watching
    act(() => {
      geo.grow(600);
      emitText(agent, "second line");
    });

    expect(geo.scrollTop).toBe(geo.end());
  });

  it("suspends following when the reader scrolls away from the end", async () => {
    const { agent, geo } = await renderFollowingTranscript();

    geo.readerScrollsTo(120); // reading back through earlier output
    act(() => {
      geo.grow(600);
      emitText(agent, "second line");
    });

    expect(geo.scrollTop).toBe(120); // live output did not move the reader
  });

  it("resumes following when the reader returns to the end", async () => {
    const { agent, geo } = await renderFollowingTranscript();

    geo.readerScrollsTo(120);
    act(() => {
      geo.grow(600);
      emitText(agent, "second line");
    });
    expect(geo.scrollTop).toBe(120);

    geo.readerScrollsTo(geo.end());
    act(() => {
      geo.grow(600);
      emitText(agent, "third line");
    });
    expect(geo.scrollTop).toBe(geo.end());
  });

  it("follows tool output that attaches to an existing tool card", async () => {
    const { agent, geo } = await renderFollowingTranscript();
    act(() => {
      agent.emit("message_end", {
        message: {
          role: "assistant",
          content: [{ type: "tool_use", id: "tool-9", name: "bash", input: { cmd: "pytest" } }],
        },
      });
    });

    const stream = screen.getByTestId("transcript-stream");
    const rootItems = stream.childElementCount;
    geo.readerScrollsTo(geo.end());
    act(() => {
      geo.grow(600);
      agent.emit("message_end", {
        message: {
          role: "user",
          content: [{ type: "tool_result", tool_use_id: "tool-9", content: "exit 0" }],
        },
      });
    });

    // The card grew without a new root item — the case an item-count trigger
    // would miss.
    expect(stream.childElementCount).toBe(rootItems);
    expect(within(stream).getByTestId("tool-card-tool-9")).toHaveTextContent("exit 0");
    expect(geo.scrollTop).toBe(geo.end());
  });

  it("follows a subagent item nested under an existing tool call", async () => {
    const { agent, geo } = await renderFollowingTranscript();
    act(() => {
      agent.emit("message_end", {
        message: {
          role: "assistant",
          content: [{ type: "tool_use", id: "spawn-3", name: "task" }],
        },
      });
    });

    const stream = screen.getByTestId("transcript-stream");
    const rootItems = stream.childElementCount;
    geo.readerScrollsTo(geo.end());
    act(() => {
      geo.grow(600);
      agent.emit("message_end", {
        parentToolUseId: "spawn-3",
        message: { role: "assistant", content: [{ type: "text", text: "sub result" }] },
      });
    });

    // Nested under the card, so no new root item — the other half of the growth
    // an item-count trigger would miss.
    expect(stream.childElementCount).toBe(rootItems);
    expect(within(stream).getByTestId("subagent-spawn-3")).toHaveTextContent("sub result");
    expect(geo.scrollTop).toBe(geo.end());
  });

  it("does not follow when the reader expands a tool card", async () => {
    const { agent, geo } = await renderFollowingTranscript();
    act(() => {
      agent.emit("message_end", {
        message: {
          role: "assistant",
          content: [{ type: "tool_use", id: "tool-7", name: "bash", input: { cmd: "pytest" } }],
        },
      });
    });

    geo.readerScrollsTo(120);
    const user = userEvent.setup();
    await user.click(within(screen.getByTestId("transcript-stream")).getByText("bash"));

    expect(geo.scrollTop).toBe(120); // expanding is a reader action, not output
  });

  it("resets to the newest output when the session tab changes", async () => {
    const { geo } = await renderFollowingTranscript(
      twoSessionSnapshots.sessions,
      twoSessionSnapshots.workflows,
    );
    geo.readerScrollsTo(0); // following suspended in the coder stream

    const user = userEvent.setup();
    await user.click(screen.getByTestId("session-tab-reproducer"));

    const agent = lastAgentSocket()!;
    act(() => {
      agent.onopen?.();
      emitText(agent, "reproducer line");
    });
    const next = mockScrollGeometry(screen.getByTestId("transcript-stream"), 400, 1000);
    act(() => {
      next.grow(600);
      emitText(agent, "another reproducer line");
    });

    expect(next.scrollTop).toBe(next.end()); // suspension did not follow the tab
  });

  it("returns to the newest output when the channel reconnects and replays", async () => {
    const { agent, geo } = await renderFollowingTranscript();
    geo.readerScrollsTo(0); // reading back when the transport drops

    vi.useFakeTimers();
    try {
      act(() => agent.onclose?.({ code: 1006 })); // not 1000, so the channel retries
      act(() => {
        vi.advanceTimersByTime(1000); // INITIAL_BACKOFF_MS
      });
      const replayed = lastAgentSocket()!;
      expect(replayed).not.toBe(agent);

      // The channel resets to an empty transcript and the daemon replays the
      // buffer from the top; the reader must not be left at the beginning.
      act(() => {
        replayed.onopen?.();
        emitText(replayed, "replayed line");
      });
      const next = mockScrollGeometry(screen.getByTestId("transcript-stream"), 400, 1000);
      act(() => {
        next.grow(600);
        emitText(replayed, "newest line");
      });

      expect(next.scrollTop).toBe(next.end());
    } finally {
      vi.useRealTimers();
    }
  });

  it("exposes the stream as a focusable, named scroll region", async () => {
    await renderFollowingTranscript();

    const stream = screen.getByRole("region", { name: "Transcript stream" });
    expect(stream).toHaveAttribute("tabindex", "0");
    stream.focus();
    expect(stream).toHaveFocus();
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
      "/api/tasks/1/sessions/main/agent/steer",
      expect.objectContaining({ method: "POST" }),
    );
    expect(agentSocket()!.url).toContain("/api/ws/agents/1/main");
  });

  it("disables the composer entirely when no agent is live", async () => {
    stubFetch();
    await renderDetail({ "1": { main: { status: "failed", reason: "exited 137", since: "t0" } } });

    expect(screen.getByLabelText("Message")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(agentSocket()).toBeUndefined(); // no channel opened for a dead agent
  });

  it("disables steer/interrupt when the agent is idle, keeps follow-up", async () => {
    stubFetch({ state: { isStreaming: false, queuedMessageCount: 0 } });
    await renderDetail({ "1": { main: { status: "idle", reason: "queue empty", since: "t0" } } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Follow-up" })).toBeEnabled());

    expect(screen.getByRole("button", { name: "Steer" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Follow-up" })).toBeEnabled();
  });

  it("stays enabled with a note while a question is pending (waiting-input)", async () => {
    stubFetch({ state: { isStreaming: false, queuedMessageCount: 0 } });
    await renderDetail({
      "1": {
        main: {
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
  "1": {
    main: {
      status: "waiting-input",
      reason: "pending question 'ask-ui-1'",
      since: "t0",
      question: askQuestion,
    },
  },
};

const waitingApprovalSession = {
  "1": {
    main: {
      status: "waiting-approval",
      reason: "pending approval 'approval-ui-1'",
      since: "t0",
      question: approvalQuestion,
    },
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
      "/api/tasks/1/sessions/main/agent/answer",
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
      "/api/tasks/1/sessions/main/agent/answer",
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
      mainSocket().emit("question_resolved", { task_id: 1, session: "main", question_id: "ask-ui-1" });
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
        session: "main",
        from: "working",
        to: "idle",
        reason: "queue empty after 2.0s",
      });
    });

    expect(screen.getByTestId("session-state")).toHaveTextContent("idle");
    expect(screen.getByTestId("session-reason")).toHaveTextContent("queue empty after 2.0s");
  });
});

/* Workflow-engine surfaces (design D-9): the session tab bar, the workflow
 * strip, and the gate card. */

const twoSessionSnapshots = {
  sessions: {
    "1": {
      reproducer: { status: "idle", reason: "queue empty", since: "t0" },
      coder: { status: "working", reason: "agent_start frame", since: "t0" },
    },
  },
  workflows: {
    "1": {
      name: "reproduce-and-fix",
      status: "running",
      step: "fix",
      steps: [
        {
          task_id: 1,
          seq: 1,
          step: "reproduce",
          kind: "agent",
          session: "reproducer",
          status: "ok",
          outcome: { summary: "reproduced on vlan-mtu" },
          error: null,
          prompted_at: null,
          started_at: "t0",
          finished_at: "t1",
        },
        {
          task_id: 1,
          seq: 2,
          step: "fix",
          kind: "agent",
          session: "coder",
          status: "running",
          outcome: null,
          error: null,
          prompted_at: null,
          started_at: "t1",
          finished_at: null,
        },
      ],
    },
  },
};

describe("session tabs", () => {
  it("renders one tab per session, defaulting to the current step's session", async () => {
    stubFetch();
    await renderDetail(twoSessionSnapshots.sessions, twoSessionSnapshots.workflows);

    const tabs = screen.getByTestId("session-tabs");
    expect(within(tabs).getByTestId("session-tab-reproducer")).toBeInTheDocument();
    const coder = within(tabs).getByTestId("session-tab-coder");
    expect(coder).toHaveAttribute("aria-selected", "true");
    expect(within(tabs).getByTestId("session-tab-reproducer")).toHaveAttribute(
      "aria-selected",
      "false",
    );
    // The default tab drives the agent channel URL.
    expect(agentSocket()!.url).toContain("/api/ws/agents/1/coder");
  });

  it("switches the transcript channel and composer to the clicked tab", async () => {
    const fetchMock = stubFetch();
    await renderDetail(twoSessionSnapshots.sessions, twoSessionSnapshots.workflows);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("session-tab-reproducer"));
    expect(screen.getByTestId("session-tab-reproducer")).toHaveAttribute("aria-selected", "true");

    // The composer's steer POST targets the selected session's endpoint.
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((c) => String(c[0]).includes("/sessions/reproducer/agent/state")),
      ).toBe(true),
    );
    await user.click(screen.getByRole("button", { name: "Follow-up" }));
    await user.type(screen.getByLabelText("Message"), "try the other loop");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/sessions/reproducer/agent/follow-up",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("hides the tab bar for a single-session workflow", async () => {
    stubFetch();
    await renderDetail(workingSession);
    expect(screen.queryByTestId("session-tabs")).not.toBeInTheDocument();
  });

  it("disables tabs for unspawned sessions and marks a tab with a pending question", async () => {
    stubFetch();
    await renderDetail(
      {
        "1": {
          reproducer: {
            status: "waiting-input",
            reason: "pending question 'ask-1'",
            since: "t0",
            question: askQuestion,
          },
        },
      },
      twoSessionSnapshots.workflows,
    );

    // The default tab is the current step's session (coder) — known from the
    // workflow records but not yet spawned, so it stays inactive.
    const coder = screen.getByTestId("session-tab-coder");
    expect(coder).toBeDisabled();
    expect(coder).toHaveAttribute("title", "coder: not started");
    // No channel opens for the unspawned default tab.
    expect(agentSocket()).toBeUndefined();

    // The other session's pending question marks its tab instead of a card.
    expect(screen.getByTestId("tab-question-reproducer")).toBeInTheDocument();
    expect(screen.queryByTestId("question-card")).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByTestId("session-tab-reproducer"));
    expect(screen.queryByTestId("tab-question-reproducer")).not.toBeInTheDocument();
    expect(screen.getByTestId("question-card")).toBeInTheDocument();
  });
});

describe("workflow strip", () => {
  it("renders one chip per executed step with the current step highlighted", async () => {
    stubFetch();
    await renderDetail(twoSessionSnapshots.sessions, twoSessionSnapshots.workflows);

    const strip = screen.getByTestId("workflow-strip");
    expect(within(strip).getByTestId("workflow-run-status")).toHaveTextContent(
      "reproduce-and-fix · running",
    );
    const first = within(strip).getByTestId("workflow-chip-1");
    expect(first).toHaveTextContent("reproduce");
    expect(first.className).toContain("ok");
    expect(first).toHaveAttribute("title", "reproduced on vlan-mtu");
    const second = within(strip).getByTestId("workflow-chip-2");
    expect(second).toHaveTextContent("fix");
    expect(second.className).toContain("current");
  });

  it("updates live on workflow_step events", async () => {
    stubFetch();
    await renderDetail(twoSessionSnapshots.sessions, twoSessionSnapshots.workflows);

    await act(async () => {
      mainSocket().emit("workflow_step", {
        task_id: 1,
        step: "fix",
        kind: "agent",
        session: "coder",
        status: "ok",
      });
      mainSocket().emit("workflow_step", {
        task_id: 1,
        step: "confirm",
        kind: "gate",
        session: null,
        status: "waiting",
        message: "Ship it?",
      });
    });

    const strip = screen.getByTestId("workflow-strip");
    expect(within(strip).getByTestId("workflow-chip-2").className).toContain("ok");
    const gate = within(strip).getByTestId("workflow-chip-3");
    expect(gate).toHaveTextContent("confirm");
    expect(gate.className).toContain("waiting");
    expect(gate).toHaveAttribute("title", "Ship it?");
  });

  it("omits the strip before any step has executed", async () => {
    stubFetch();
    await renderDetail(workingSession);
    expect(screen.queryByTestId("workflow-strip")).not.toBeInTheDocument();
  });
});

describe("gate card", () => {
  const waitingGate = {
    "1": {
      name: "reproduce-and-fix",
      status: "waiting",
      step: "confirm",
      steps: [
        {
          ...twoSessionSnapshots.workflows["1"].steps[0],
          status: "ok",
        },
        {
          task_id: 1,
          seq: 2,
          step: "confirm",
          kind: "gate",
          session: null,
          status: "waiting",
          outcome: { message: "Review the reproducer output?" },
          error: null,
          prompted_at: null,
          started_at: "t1",
          finished_at: null,
        },
      ],
    },
  };

  it("renders the gate message from the snapshot and resumes with a note", async () => {
    const fetchMock = stubFetch();
    await renderDetail(twoSessionSnapshots.sessions, waitingGate);

    const card = screen.getByTestId("gate-card");
    expect(within(card).getByTestId("gate-message")).toHaveTextContent(
      "Review the reproducer output?",
    );

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Resume note"), "looks right");
    await user.click(within(card).getByTestId("gate-resume"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/workflow/resume",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ note: "looks right" }),
      }),
    );
  });

  it("posts a null note when the field is empty", async () => {
    const fetchMock = stubFetch();
    await renderDetail(twoSessionSnapshots.sessions, waitingGate);

    const user = userEvent.setup();
    await user.click(screen.getByTestId("gate-resume"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tasks/1/workflow/resume",
      expect.objectContaining({ body: JSON.stringify({ note: null }) }),
    );
  });

  it("surfaces a 409 inline when the run already moved on", async () => {
    const fetchMock = stubFetch();
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes("/workflow/resume")) {
        return Promise.resolve({
          ok: false,
          status: 409,
          json: () => Promise.resolve({ detail: "workflow run is not waiting" }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    await renderDetail(twoSessionSnapshots.sessions, waitingGate);

    const user = userEvent.setup();
    await user.click(screen.getByTestId("gate-resume"));

    expect(await screen.findByTestId("gate-error")).toHaveTextContent(
      "workflow run is not waiting",
    );
  });

  it("disappears once the run leaves waiting", async () => {
    stubFetch();
    await renderDetail(twoSessionSnapshots.sessions, waitingGate);
    expect(screen.getByTestId("gate-card")).toBeInTheDocument();

    act(() => {
      mainSocket().emit("workflow_step", {
        task_id: 1,
        step: "confirm",
        kind: "gate",
        session: null,
        status: "ok",
      });
      mainSocket().emit("task_updated", {
        ...makeTask(),
        workflow_status: "running",
        workflow_step: "fix",
      });
    });

    expect(screen.queryByTestId("gate-card")).not.toBeInTheDocument();
  });

  it("renders no gate card while the run is running", async () => {
    stubFetch();
    await renderDetail(twoSessionSnapshots.sessions, twoSessionSnapshots.workflows);
    expect(screen.queryByTestId("gate-card")).not.toBeInTheDocument();
  });
});
