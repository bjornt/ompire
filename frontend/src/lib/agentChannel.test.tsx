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
