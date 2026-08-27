import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonProvider } from "./daemonSocket";
import { useDaemonState } from "./useDaemonState";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.onclose?.();
  }

  send() {}

  emitSnapshot(payload: { projects: unknown[]; tasks: unknown[] }) {
    this.onopen?.();
    this.onmessage?.({ data: JSON.stringify({ seq: 0, ts: "", type: "snapshot", payload }) });
  }
}

function Probe() {
  const { connectionState, snapshotReady, projects, tasks } = useDaemonState();
  return (
    <div>
      <div data-testid="conn">{connectionState}</div>
      <div data-testid="snapshot-ready">{String(snapshotReady)}</div>
      <div data-testid="projects">{JSON.stringify(projects)}</div>
      <div data-testid="tasks">{JSON.stringify(tasks)}</div>
    </div>
  );
}

describe("DaemonProvider", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("includes the localStorage token in the WebSocket URL", () => {
    window.localStorage.setItem("ompire_token", "test-token-123");

    render(
      <DaemonProvider>
        <Probe />
      </DaemonProvider>,
    );

    expect(MockWebSocket.instances[0].url).toContain("token=test-token-123");
    window.localStorage.removeItem("ompire_token");
  });

  it("waits for the first snapshot before marking a connection ready", () => {
    render(
      <DaemonProvider>
        <Probe />
      </DaemonProvider>,
    );

    const socket = MockWebSocket.instances[0];
    expect(screen.getByTestId("snapshot-ready")).toHaveTextContent("false");

    act(() => {
      socket.onopen?.();
    });
    expect(screen.getByTestId("conn")).toHaveTextContent("connected");
    expect(screen.getByTestId("snapshot-ready")).toHaveTextContent("false");

    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({ seq: 0, ts: "", type: "snapshot", payload: { projects: [], tasks: [] } }),
      });
    });
    expect(screen.getByTestId("snapshot-ready")).toHaveTextContent("true");
  });

  it("populates state from the initial snapshot", () => {
    render(
      <DaemonProvider>
        <Probe />
      </DaemonProvider>,
    );

    const socket = MockWebSocket.instances[0];
    act(() => {
      socket.emitSnapshot({ projects: [{ name: "maas" }], tasks: [] });
    });

    expect(screen.getByTestId("conn").textContent).toBe("connected");
    expect(screen.getByTestId("projects").textContent).toContain("maas");
    expect(screen.getByTestId("snapshot-ready")).toHaveTextContent("true");
  });

  it("flips connection state on disconnect and reconnects with backoff", () => {
    render(
      <DaemonProvider>
        <Probe />
      </DaemonProvider>,
    );

    const first = MockWebSocket.instances[0];
    act(() => {
      first.emitSnapshot({ projects: [{ name: "maas" }], tasks: [] });
    });

    act(() => {
      first.onclose?.();
    });
    expect(screen.getByTestId("conn").textContent).toBe("reconnecting");
    expect(screen.getByTestId("snapshot-ready")).toHaveTextContent("false");
    expect(MockWebSocket.instances).toHaveLength(1);

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);

    const second = MockWebSocket.instances[1];
    act(() => {
      second.emitSnapshot({ projects: [{ name: "different-project" }], tasks: [] });
    });

    expect(screen.getByTestId("conn").textContent).toBe("connected");
    expect(screen.getByTestId("snapshot-ready")).toHaveTextContent("true");
    const projectsText = screen.getByTestId("projects").textContent ?? "";
    expect(projectsText).toContain("different-project");
    expect(projectsText).not.toContain("\"maas\"");
  });
});
