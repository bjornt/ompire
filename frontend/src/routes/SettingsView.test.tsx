import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonProvider } from "../lib/daemonSocket";
import { SettingsView } from "./SettingsView";
import type { DaemonInfo, DaemonSettings } from "../types";

type Provenance = Record<string, "default" | "config" | "override">;

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

  close() {}
  send() {}

  emit(type: string, payload: unknown) {
    this.onmessage?.({ data: JSON.stringify({ seq: 0, ts: "", type, payload }) });
  }

  emitSnapshot(payload: {
    projects?: unknown[];
    templates?: unknown[];
    tasks?: unknown[];
    settings?: DaemonSettings;
  }) {
    this.onopen?.();
    this.emit("snapshot", payload);
  }
}

function socket() {
  return MockWebSocket.instances[0];
}

function jsonResponse<T>(body: T, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

function buildFetchHandler({
  settings = {},
  provenance = {},
  daemonInfo = {
    bind: "127.0.0.1",
    port: 9000,
    version: "v1",
    config_path: "/etc/ompire.toml",
    data_dir: "/var/lib/ompire",
    audit_log_path: "/var/log/ompire/audit.log",
  },
  token = "ompire_tok_existingtoken123",
  rotateToken = "ompire_tok_rotatedtoken456",
}: {
  settings?: DaemonSettings;
  provenance?: Provenance;
  daemonInfo?: DaemonInfo;
  token?: string;
  rotateToken?: string;
} = {}) {
  return async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/settings" && method === "GET") {
      return jsonResponse({ settings, provenance });
    }
    if (url === "/api/settings" && method === "PUT") {
      const body = JSON.parse(init?.body as string) as DaemonSettings;
      const nextSettings = { ...settings, ...body };
      const nextProvenance = { ...provenance };
      for (const key of Object.keys(body)) nextProvenance[key] = "override";
      return jsonResponse({ settings: nextSettings, provenance: nextProvenance });
    }
    if (url === "/api/daemon/info" && method === "GET") {
      return jsonResponse(daemonInfo);
    }
    if (url === "/api/settings/token" && method === "GET") {
      return jsonResponse({ token });
    }
    if (url === "/api/settings/token/rotate" && method === "POST") {
      return jsonResponse({ token: rotateToken });
    }
    return jsonResponse({ detail: "not found" }, 404);
  };
}

function renderSettings(snapshotSettings: DaemonSettings = {}) {
  render(
    <DaemonProvider>
      <SettingsView />
    </DaemonProvider>,
  );
  act(() => {
    socket().emitSnapshot({
      projects: [],
      tasks: [],
      templates: [],
      settings: snapshotSettings,
    });
  });
}

describe("SettingsView daemon-settings panel", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.localStorage.clear();
    MockWebSocket.instances = [];
  });

  it("tier matrix toggles call the API and follow settings_changed", async () => {
    const user = userEvent.setup();
    const baseSettings: DaemonSettings = {
      renotify_interval: 300,
      stall_threshold: 300,
      context_advisory_threshold: 80,
      "tier.interrupt.desktop": false,
    };
    const fetchMock = vi
      .fn()
      .mockImplementation(buildFetchHandler({ settings: baseSettings, provenance: {} }));
    vi.stubGlobal("fetch", fetchMock);

    renderSettings(baseSettings);

    const checkbox = await screen.findByTestId("tier-interrupt-desktop");
    expect(checkbox).not.toBeChecked();

    await user.click(checkbox);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ "tier.interrupt.desktop": true }),
      }),
    );

    // The PUT response marks the key as an override.
    await waitFor(() =>
      expect(screen.getByTestId("override-tier-interrupt-desktop")).toBeInTheDocument(),
    );

    // A settings_changed event from the daemon updates the displayed value.
    act(() => {
      socket().emit("settings_changed", {
        settings: { ...baseSettings, "tier.interrupt.desktop": true },
      });
    });

    await waitFor(() => expect(screen.getByTestId("tier-interrupt-desktop")).toBeChecked());
  });

  it("shows override annotations from fetched provenance", async () => {
    const settings: DaemonSettings = { "tier.interrupt.desktop": true };
    const provenance: Provenance = { "tier.interrupt.desktop": "override" };
    const fetchMock = vi.fn().mockImplementation(buildFetchHandler({ settings, provenance }));
    vi.stubGlobal("fetch", fetchMock);

    renderSettings(settings);

    await waitFor(() =>
      expect(screen.getByTestId("override-tier-interrupt-desktop")).toBeInTheDocument(),
    );
  });

  it("rotate flow stashes the new daemon token", async () => {
    const user = userEvent.setup();
    const oldToken = "ompire_tok_oldtokenXYZ9";
    const newToken = "ompire_tok_newtokenABC9";
    const fetchMock = vi
      .fn()
      .mockImplementation(buildFetchHandler({ token: oldToken, rotateToken: newToken }));
    vi.stubGlobal("fetch", fetchMock);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    renderSettings();

    await waitFor(() => expect(screen.getByTestId("daemon-token")).toHaveTextContent("ompire_tok_"));
    expect(screen.getByTestId("daemon-token")).toHaveTextContent(oldToken.slice(-4));

    await user.click(screen.getByTestId("rotate-daemon-token"));

    await waitFor(() =>
      expect(window.localStorage.getItem("ompire_token")).toBe(newToken),
    );
    expect(screen.getByTestId("daemon-token")).toHaveTextContent(newToken.slice(-4));
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringContaining("invalidate the old token immediately"),
    );
  });
});
