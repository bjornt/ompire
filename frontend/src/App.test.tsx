import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

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

  emitSnapshot(payload: { projects: unknown[]; tasks: unknown[] }) {
    this.onopen?.();
    this.onmessage?.({ data: JSON.stringify({ seq: 0, ts: "", type: "snapshot", payload }) });
  }
}

async function renderAppWithEmptySnapshot() {
  window.history.pushState({}, "", "/");
  render(<App />);
  const socket = MockWebSocket.instances[0];
  act(() => {
    socket.emitSnapshot({ projects: [], tasks: [] });
  });
}

describe("App", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the no-tasks-yet empty state on the Tasks home view", async () => {
    await renderAppWithEmptySnapshot();
    expect(await screen.findByTestId("tasks-empty-state")).toBeInTheDocument();
  });

  it("highlights the Tasks nav link as active by default", async () => {
    await renderAppWithEmptySnapshot();
    const tasksLink = await screen.findByRole("link", { name: "Tasks" });
    expect(tasksLink.className).toContain("navLinkActive");
  });

  it("navigates to each stub route and renders its placeholder inside chrome", async () => {
    await renderAppWithEmptySnapshot();
    const user = userEvent.setup();

    for (const [label, title] of [
      ["Projects", "Projects"],
      ["Spawn task", "Spawn task"],
      ["Ship flow", "Ship flow"],
      ["Templates & settings", "Templates & settings"],
    ] as const) {
      await user.click(screen.getByRole("link", { name: label }));
      expect(await screen.findByTestId("stub-page")).toHaveTextContent(title);
      expect(screen.getByTestId("chrome-header")).toBeInTheDocument();
      const navLink = screen.getByRole("link", { name: label });
      expect(navLink.className).toContain("navLinkActive");
    }
  });
});
