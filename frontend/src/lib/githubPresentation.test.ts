import { describe, expect, it } from "vitest";
import type { GitHubIdentityState, GitHubStatus } from "../types";
import {
  canonicalGitHubTarget,
  currentGitHubTargetStatus,
  githubIdentityPresentation,
  safeGitHubDetail,
} from "./githubPresentation";

function githubStatus(state: GitHubIdentityState): GitHubStatus {
  return {
    identity: {
      state,
      host: "github.com",
      login: state === "ready" ? "octo" : null,
      credential_source: "GitHub CLI configuration",
      executable_path: "/usr/bin/gh",
      version: "gh version 2.97.0",
      detail: state === "error" ? "network unavailable" : null,
      checked_at: "t0",
    },
    targets: {},
  };
}

describe("GitHub presentation", () => {
  it.each([
    ["unknown", "gh —"],
    ["missing", "gh missing"],
    ["unauthenticated", "gh auth"],
    ["ready", "gh @octo"],
    ["error", "gh error"],
  ] as const)("renders %s identity state", (state, label) => {
    expect(githubIdentityPresentation(githubStatus(state)).label).toBe(label);
  });

  it("redacts recognizable credential material before rendering detail", () => {
    const detail = safeGitHubDetail(
      "Authorization: Bearer github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\nhttps://alice:password@example.test",
    );
    expect(detail).toBe("Authorization: [redacted]\nhttps://[redacted]@example.test");
  });

  it("only returns a target bound to the selected upstream and current identity", () => {
    const status = githubStatus("ready");
    status.targets["github.com/owner/repo"] = {
      state: "allowed",
      target: { host: "github.com", owner: "owner", repository: "repo" },
      identity: {
        host: "github.com",
        login: "octo",
        credential_source: "GitHub CLI configuration",
      },
      detail: null,
      checked_at: "t0",
    };

    expect(canonicalGitHubTarget("git@github.com:Owner/Repo.git")).toBe("github.com/owner/repo");
    expect(currentGitHubTargetStatus(status, "https://github.com/owner/repo")?.state).toBe("allowed");
    expect(currentGitHubTargetStatus(status, "https://github.com/other/repo")).toBeUndefined();

    status.identity.login = "different";
    expect(currentGitHubTargetStatus(status, "https://github.com/owner/repo")).toBeUndefined();
  });
});
