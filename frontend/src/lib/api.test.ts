import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonError, getGitHubStatus, recheckEnvironment, recheckGitHub } from "./api";
import { setDaemonToken } from "./token";

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

describe("GitHub status API", () => {
  it("gets current status and submits a bodyless global recheck", async () => {
    setDaemonToken("test-token");
    const status = {
      identity: {
        state: "ready",
        host: "github.com",
        login: "octo",
        credential_source: "GitHub CLI configuration",
        executable_path: "/usr/bin/gh",
        version: "gh version 2.97.0",
        detail: null,
        checked_at: "t0",
      },
      targets: {},
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => status,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getGitHubStatus()).resolves.toEqual(status);
    await expect(recheckGitHub()).resolves.toEqual(status);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/gh",
      expect.objectContaining({ method: "GET", headers: { Authorization: "Bearer test-token" } }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/gh/recheck",
      expect.objectContaining({ method: "POST", body: undefined }),
    );
  });

  it("posts task scope and extracts a structured safe refusal message", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        detail: {
          message: "GitHub preflight blocked shipping: GitHub CLI is unauthenticated",
          gh: { identity: { state: "unauthenticated" } },
        },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(recheckGitHub(42)).rejects.toThrow(
      "GitHub preflight blocked shipping: GitHub CLI is unauthenticated",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/gh/recheck",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ task_id: 42 }),
      }),
    );
  });
});

describe("Environment recheck API", () => {
  it("posts a bodyless authenticated recheck for the named environment and returns its observation", async () => {
    setDaemonToken("environment-token");
    const environment = {
      name: "dogfood",
      host: "vm",
      capabilities: { model: "auth-gateway" },
      provision: ["/qa/setup.sh"],
      check: ["/qa/setup.sh", "--probe"],
      status: { state: "ready", blockers: [], checked_at: "t1" },
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => environment,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(recheckEnvironment("dogfood")).resolves.toEqual(environment);
    expect(fetchMock).toHaveBeenCalledWith("/api/environments/dogfood/recheck", {
      method: "POST",
      headers: { Authorization: "Bearer environment-token" },
      body: undefined,
    });
  });

  it("preserves the daemon's unknown-environment refusal and 404 status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: "Unknown environment: absent" }),
    }));

    const error = await recheckEnvironment("absent").catch((err: unknown) => err);
    expect(error).toBeInstanceOf(DaemonError);
    expect(error).toMatchObject({ message: "Unknown environment: absent", status: 404 });
  });

  it("retains the response status when a failed recheck has no JSON diagnostic", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => { throw new Error("not JSON"); },
    }));

    await expect(recheckEnvironment("dogfood")).rejects.toMatchObject({ message: "503", status: 503 });
  });
});
