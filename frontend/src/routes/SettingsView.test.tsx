import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonProvider } from "../lib/daemonSocket";
import { SettingsView } from "./SettingsView";
import type { DaemonInfo, DaemonSettings, GitHubStatus } from "../types";

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
    gh?: GitHubStatus;
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
  githubStatus,
}: {
  settings?: DaemonSettings;
  provenance?: Provenance;
  daemonInfo?: DaemonInfo;
  token?: string;
  rotateToken?: string;
  githubStatus?: GitHubStatus;
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
    if (url === "/api/gh/recheck" && method === "POST") {
      return jsonResponse(githubStatus ?? { identity: { state: "unknown" }, targets: {} });
    }
    return jsonResponse({ detail: "not found" }, 404);
  };
}

function renderSettings(snapshotSettings: DaemonSettings = {}, githubStatus?: GitHubStatus) {
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
      gh: githubStatus,
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


describe("SettingsView GitHub CLI panel", () => {
  const readyGitHub: GitHubStatus = {
    identity: {
      state: "ready",
      host: "github.com",
      login: "octo",
      credential_source: "GitHub CLI configuration",
      executable_path: "/usr/bin/gh",
      version: "gh version 2.97.0",
      detail: null,
      checked_at: "2026-08-28T12:00:00+00:00",
    },
    targets: {},
  };

  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.localStorage.clear();
    MockWebSocket.instances = [];
  });

  it("renders only safe GitHub details and follows live status updates", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(buildFetchHandler()));
    renderSettings({}, readyGitHub);

    expect(await screen.findByTestId("daemon-gh-state")).toHaveTextContent("gh @octo");
    expect(screen.getByTestId("daemon-gh-login")).toHaveTextContent("@octo");
    expect(screen.getByTestId("daemon-gh-host")).toHaveTextContent("github.com");
    expect(screen.getByTestId("daemon-gh-source")).toHaveTextContent("GitHub CLI configuration");
    expect(screen.getByTestId("daemon-gh-executable")).toHaveTextContent("/usr/bin/gh");
    expect(screen.getByTestId("daemon-gh-version")).toHaveTextContent("gh version 2.97.0");

    const leaked = "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    act(() => {
      socket().emit("gh_status", {
        gh: {
          ...readyGitHub,
          identity: {
            ...readyGitHub.identity,
            state: "error",
            login: null,
            detail: `Authorization: Bearer ${leaked}`,
          },
        },
      });
    });

    await waitFor(() => expect(screen.getByTestId("daemon-gh-state")).toHaveTextContent("gh error"));
    expect(screen.getByTestId("daemon-gh-detail")).toHaveTextContent("Authorization: [redacted]");
    expect(document.body.textContent).not.toContain(leaked);
  });

  it("submits one global GitHub recheck while the first request is pending", async () => {
    const user = userEvent.setup();
    let resolveRecheck: ((response: ReturnType<typeof jsonResponse>) => void) | undefined;
    const pendingRecheck = new Promise<ReturnType<typeof jsonResponse>>((resolve) => {
      resolveRecheck = resolve;
    });
    const baseHandler = buildFetchHandler({ githubStatus: readyGitHub });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/gh/recheck" && init?.method === "POST") return pendingRecheck;
      return baseHandler(url, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSettings({}, readyGitHub);

    const button = await screen.findByTestId("recheck-github-button");
    await user.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    await user.click(button);
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) => url === "/api/gh/recheck" && (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toHaveLength(1);

    resolveRecheck?.(jsonResponse(readyGitHub));
    await waitFor(() => expect(button).not.toBeDisabled());
  });
});
