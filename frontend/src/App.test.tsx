import { act, render, screen, within } from "@testing-library/react";
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

  it("renders the real Settings view (not a stub) inside chrome", async () => {
    await renderAppWithEmptySnapshot();
    const user = userEvent.setup();

    const header = screen.getByTestId("chrome-header");
    await user.click(within(header).getByRole("link", { name: "Templates & settings" }));
    expect(await screen.findByTestId("templates-empty-state")).toBeInTheDocument();
    expect(screen.queryByTestId("stub-page")).not.toBeInTheDocument();
    expect(screen.getByTestId("chrome-header")).toBeInTheDocument();
    const navLink = screen.getByRole("link", { name: "Templates & settings" });
    expect(navLink.className).toContain("navLinkActive");
  });

  it("renders the real Spawn view (not a stub) inside chrome", async () => {
    await renderAppWithEmptySnapshot();
    const user = userEvent.setup();

    const header = screen.getByTestId("chrome-header");
    await user.click(within(header).getByRole("link", { name: "Spawn task" }));
    expect(await screen.findByTestId("spawn-form")).toBeInTheDocument();
    expect(screen.queryByTestId("stub-page")).not.toBeInTheDocument();
    expect(screen.getByTestId("chrome-header")).toBeInTheDocument();
  });

  it("renders the real Projects view (not a stub) inside chrome", async () => {
    await renderAppWithEmptySnapshot();
    const user = userEvent.setup();

    const header = screen.getByTestId("chrome-header");
    await user.click(within(header).getByRole("link", { name: "Projects" }));
    expect(await screen.findByTestId("projects-empty-state")).toBeInTheDocument();
    expect(screen.queryByTestId("stub-page")).not.toBeInTheDocument();
    expect(screen.getByTestId("chrome-header")).toBeInTheDocument();
  });

  it("renders an explicit recovery surface for an unmatched route", async () => {
    window.history.pushState({}, "", "/not-a-real-route");
    render(<App />);
    act(() => {
      MockWebSocket.instances[0].emitSnapshot({ projects: [], tasks: [] });
    });

    const notFound = await screen.findByTestId("app-not-found");
    expect(notFound).toHaveTextContent("Page not found");
    expect(within(notFound).getByRole("link", { name: "Tasks" })).toHaveAttribute("href", "/tasks");
    expect(screen.getByTestId("chrome-header")).toBeInTheDocument();
  });
});
