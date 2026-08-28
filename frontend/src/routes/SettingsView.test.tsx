import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonProvider } from "../lib/daemonSocket";
import { SettingsView } from "./SettingsView";
import type { DaemonInfo, DaemonSettings, GitHubStatus, GpgStatus } from "../types";

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
    gpg?: GpgStatus;
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
  gpgStatus,
}: {
  settings?: DaemonSettings;
  provenance?: Provenance;
  daemonInfo?: DaemonInfo;
  token?: string;
  rotateToken?: string;
  githubStatus?: GitHubStatus;
  gpgStatus?: GpgStatus;
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
    if (url === "/api/gpg/recheck" && method === "POST") {
      return jsonResponse(gpgStatus ?? { state: "unknown" });
    }
    if (url.startsWith("/api/settings/gpg_signing_key") && method === "DELETE") {
      const nextProvenance = { ...provenance, gpg_signing_key: "default" as const };
      return jsonResponse({
        settings: { ...settings, gpg_signing_key: null },
        provenance: nextProvenance,
      });
    }
    if (url === "/api/gh/recheck" && method === "POST") {
      return jsonResponse(githubStatus ?? { identity: { state: "unknown" }, targets: {} });
    }
    return jsonResponse({ detail: "not found" }, 404);
  };
}

function renderSettings(
  snapshotSettings: DaemonSettings = {},
  githubStatus?: GitHubStatus,
  gpgStatus?: GpgStatus,
) {
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
      gpg: gpgStatus,
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

describe("SettingsView commit-signing panel", () => {
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

  const KEY_A = "A".repeat(40);
  const KEY_B = "B".repeat(40);

  const candidate = (fingerprint: string, uid: string) => ({
    fingerprint,
    key_id: fingerprint.slice(-16),
    uid,
    keygrip: `grip-${fingerprint.slice(0, 4)}`,
    created_at: null,
    expires_at: null,
    primary_fingerprint: fingerprint,
  });

  const ambiguous: GpgStatus = {
    state: "ambiguous",
    selected: null,
    candidates: [
      candidate(KEY_A, "Alice <alice@example.com>"),
      candidate(KEY_B, "Bob <bob@example.com>"),
    ],
    cache_ttl: null,
    detail: "2 usable signing keys; choose one in Templates & settings",
    checked_at: "t0",
  };

  const ready: GpgStatus = {
    state: "ready",
    selected: {
      ...candidate(KEY_B, "Bob <bob@example.com>"),
      source: "override",
      protection: "protected",
    },
    candidates: ambiguous.candidates,
    cache_ttl: null,
    detail: null,
    checked_at: "t1",
  };

  it("offers every usable key when the daemon cannot choose", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(buildFetchHandler()));
    renderSettings({}, undefined, ambiguous);

    expect(await screen.findByTestId("daemon-gpg-state")).toHaveTextContent(
      "gpg unselected",
    );
    // The selector below is the action; repeating "choose one in Settings"
    // to someone already in Settings is noise.
    expect(screen.queryByTestId("daemon-gpg-recovery")).not.toBeInTheDocument();
    expect(screen.getByTestId("daemon-gpg-detail")).toHaveTextContent(
      "2 usable signing keys",
    );
    const select = screen.getByTestId("gpg-key-select") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["", KEY_A, KEY_B]);
  });

  it("persists a selection through the settings API", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation(buildFetchHandler());
    vi.stubGlobal("fetch", fetchMock);
    renderSettings({}, undefined, ambiguous);

    await user.selectOptions(await screen.findByTestId("gpg-key-select"), KEY_B);

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(
        ([url, init]) => url === "/api/settings" && init?.method === "PUT",
      );
      expect(put).toBeDefined();
      expect(JSON.parse(put![1].body as string)).toEqual({
        gpg_signing_key: KEY_B,
      });
    });
  });

  it("clearing the selection returns the daemon to auto-detection", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation(buildFetchHandler());
    vi.stubGlobal("fetch", fetchMock);
    renderSettings({}, undefined, ready);

    await user.selectOptions(await screen.findByTestId("gpg-key-select"), "");

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/gpg_signing_key",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
  });

  it("shows the selected key and how it was chosen, without key material", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(buildFetchHandler()));
    renderSettings({}, undefined, ready);

    expect(await screen.findByTestId("daemon-gpg-fingerprint")).toHaveTextContent(
      KEY_B,
    );
    expect(screen.getByTestId("daemon-gpg-uid")).toHaveTextContent("Bob");
    expect(screen.getByTestId("daemon-gpg-source")).toHaveTextContent(
      "Selected here",
    );
    const panel = screen.getByTestId("daemon-signing-panel");
    expect(panel.textContent).not.toMatch(/passphrase|PRIVATE KEY|pinentry/i);
  });

  it("re-checks the key on demand and disables the control while in flight", async () => {
    const user = userEvent.setup();
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const base = buildFetchHandler();
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/gpg/recheck") {
        await gate;
        return jsonResponse(ready);
      }
      return base(url, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSettings({}, undefined, ambiguous);

    const button = await screen.findByTestId("recheck-gpg-button");
    await user.click(button);

    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveTextContent("Checking key…");

    await act(async () => {
      release!();
      await gate;
    });
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("shows the terminal helper when the agent is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(buildFetchHandler()));
    renderSettings({}, undefined, {
      ...ready,
      state: "agent_unavailable",
      detail: "gpg-agent is not reachable",
    });

    expect(await screen.findByTestId("daemon-gpg-command")).toHaveTextContent(
      "gpg-connect-agent /bye",
    );
    // Recovery that happens outside this panel is still shown.
    expect(screen.getByTestId("daemon-gpg-recovery")).toHaveTextContent(
      "Start the agent",
    );
  });
});
