import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAgentChannel } from "./agentChannel";

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

  emitClose(code: number) {
    this.onclose?.({ code });
  }
}

function Probe({
  taskId,
  session,
  enabled,
}: {
  taskId: number;
  session: string;
  enabled: boolean;
}) {
  const { connected } = useAgentChannel(taskId, session, enabled);
  return <div data-testid="connected">{String(connected)}</div>;
}

describe("useAgentChannel reconnection", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("retries with backoff after a 4404 (no live agent yet) instead of giving up", () => {
    render(<Probe taskId={1} session="main" enabled={true} />);
    expect(MockWebSocket.instances).toHaveLength(1);

    // The daemon closes with 4404 because recovery/spawn hasn't registered a
    // live AgentHandle yet — `enabled` stays true throughout (design: a
    // `starting` session already counts as live), so this must not be
    // treated as a dead end.
    act(() => {
      MockWebSocket.instances[0].emitClose(4404);
    });
    expect(screen.getByTestId("connected").textContent).toBe("false");

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it("keeps retrying across repeated 4404s until the agent is actually live", () => {
    render(<Probe taskId={1} session="main" enabled={true} />);

    act(() => {
      MockWebSocket.instances[0].emitClose(4404);
    });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);

    act(() => {
      MockWebSocket.instances[1].emitClose(4404);
    });
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  it("does not reconnect after code 1000 (agent exited, buffer flushed)", () => {
    render(<Probe taskId={1} session="main" enabled={true} />);

    act(() => {
      MockWebSocket.instances[0].emitClose(1000);
    });
    act(() => {
      vi.advanceTimersByTime(10000);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("connects to the session-scoped channel URL", () => {
    render(<Probe taskId={7} session="reviewer" enabled={true} />);
    expect(MockWebSocket.instances[0].url).toContain("/api/ws/agents/7/reviewer");
  });

  it("reconnects on the new session's channel when the tab switches", () => {
    const { rerender } = render(<Probe taskId={1} session="main" enabled={true} />);
    expect(MockWebSocket.instances[0].url).toContain("/api/ws/agents/1/main");

    rerender(<Probe taskId={1} session="reviewer" enabled={true} />);
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(MockWebSocket.instances[1].url).toContain("/api/ws/agents/1/reviewer");
  });

  it("opens no channel while disabled", () => {
    render(<Probe taskId={1} session="main" enabled={false} />);
    expect(MockWebSocket.instances).toHaveLength(0);
  });
});

describe("useAgentChannel generation guard", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  /* Dogfood finding (bugfix-workflow): after a daemon restart arms the
   * reconnect backoff, a tab switch must kill the old session's generation
   * outright — a zombie socket replaying its buffer into the new session's
   * transcript is the bug this defends. */
  function TranscriptProbe({ session }: { session: string }) {
    const { transcript } = useAgentChannel(1, session, true);
    return (
      <div data-testid="items">
        {transcript.items
          .map((i) => ("text" in i ? i.text : ""))
          .filter(Boolean)
          .join("|")}
      </div>
    );
  }

  function emitText(socket: MockWebSocket, text: string) {
    socket.onmessage?.({
      data: JSON.stringify({
        type: "message_end",
        payload: { message: { role: "assistant", content: [{ type: "text", text }] } },
      }),
    });
  }

  it("a torn-down generation writes nothing and spawns no retries", () => {
    const { rerender } = render(<TranscriptProbe session="main" />);
    const mainSocket = MockWebSocket.instances[0];
    act(() => {
      mainSocket.onopen?.();
      emitText(mainSocket, "main speaking");
    });
    expect(screen.getByTestId("items")).toHaveTextContent("main speaking");

    // Daemon-restart close (abnormal) arms the backoff retry loop…
    act(() => {
      mainSocket.emitClose(1006);
    });

    // …and the tab switch tears the generation down before the retry fires.
    rerender(<TranscriptProbe session="reviewer" />);
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(MockWebSocket.instances[1].url).toContain("/api/ws/agents/1/reviewer");
    // The switch reset the transcript for the new session's replay.
    expect(screen.getByTestId("items")).not.toHaveTextContent("main speaking");

    // A zombie frame from the old socket never reaches the new transcript.
    act(() => {
      emitText(mainSocket, "zombie frame");
    });
    expect(screen.getByTestId("items")).not.toHaveTextContent("zombie frame");

    // No retry from the dead generation may spawn later either.
    act(() => {
      vi.advanceTimersByTime(60000);
    });
    expect(MockWebSocket.instances.filter((s) => s.url.includes("/main"))).toHaveLength(1);

    // The live generation still streams normally.
    act(() => {
      MockWebSocket.instances[1].onopen?.();
      emitText(MockWebSocket.instances[1], "reviewer speaking");
    });
    expect(screen.getByTestId("items")).toHaveTextContent("reviewer speaking");
  });
});
