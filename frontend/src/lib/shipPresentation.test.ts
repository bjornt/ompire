import { describe, expect, it } from "vitest";
import type { ReviewState, ShipState, Task } from "../types";
import { buildShipIndex, hasShipFlowHandoff, presentShipFlow } from "./shipPresentation";

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    project_name: "maas",
    template_name: "maas",
    slug: "fix-bug",
    branch: "bjornt/fix-bug",
    clone_path: "/home/op/tasks/maas/fix-bug",
    state: "created",
    prompt: "fix it",
    error: null,
    workshop_id: "workshop-1",
    spawn_completed_at: "2026-08-20T00:01:00Z",
    pr_url: null,
    pr_state: null,
    pr_merged_at: null,
    workflow_name: "single-step",
    workflow_status: null,
    workflow_step: null,
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:01:00Z",
    ...overrides,
  };
}

function approvedReview(): ReviewState {
  return {
    status: "approved",
    url: "http://127.0.0.1:7180",
    port: 7180,
    iterations: [],
  };
}

function ship(overrides: Partial<ShipState> = {}): ShipState {
  return {
    status: "drafted",
    draft: {
      commit_message: "fix: ship it",
      pr_title: "Ship it",
      pr_body: "Body",
      source: "agent",
    },
    commit_sha: null,
    pr_url: null,
    error: null,
    updated_at: "2026-08-20T00:02:00Z",
    ...overrides,
  };
}

describe("ship presentation", () => {
  it("recognizes only observed review or publishing handoffs", () => {
    const current = task();

    expect(hasShipFlowHandoff(current, undefined, undefined)).toBe(false);
    expect(hasShipFlowHandoff(current, approvedReview(), undefined)).toBe(true);
    // An approval restored across a daemon restart has no live reviewer URL,
    // but still opens the ship flow.
    expect(
      hasShipFlowHandoff(current, { ...approvedReview(), url: null, port: null }, undefined),
    ).toBe(true);
    expect(hasShipFlowHandoff(current, undefined, ship())).toBe(true);
    expect(hasShipFlowHandoff({ ...current, pr_url: "https://github.com/ompire/maas/pull/1" }, undefined, undefined)).toBe(
      true,
    );
  });

  it.each([
    ["review", task(), undefined, ship({ status: "error", draft: null, error: "draft failed" }), "review"],
    ["draft", task(), approvedReview(), undefined, "draft"],
    ["sign", task(), approvedReview(), ship(), "sign"],
    ["push and PR", task(), approvedReview(), ship({ commit_sha: "abc123" }), "push-pr"],
    [
      "unpolled pull request",
      task({ pr_url: "https://github.com/ompire/maas/pull/1" }),
      approvedReview(),
      ship({ commit_sha: "abc123", pr_url: "https://github.com/ompire/maas/pull/1" }),
      "wait-merge",
    ],
    [
      "open pull request",
      task({ pr_url: "https://github.com/ompire/maas/pull/1", pr_state: "open" }),
      approvedReview(),
      ship({ commit_sha: "abc123" }),
      "wait-merge",
    ],
    [
      "merged pull request",
      task({ pr_url: "https://github.com/ompire/maas/pull/1", pr_state: "merged" }),
      approvedReview(),
      ship({ commit_sha: "abc123" }),
      "cleanup",
    ],
    [
      "closed pull request",
      task({ pr_url: "https://github.com/ompire/maas/pull/1", pr_state: "closed" }),
      approvedReview(),
      ship({ commit_sha: "abc123" }),
      "cleanup",
    ],
    [
      "cleaned-up pull request",
      task({ state: "archived", pr_url: "https://github.com/ompire/maas/pull/1", pr_state: "merged" }),
      approvedReview(),
      ship({ commit_sha: "abc123" }),
      "cleanup-complete",
    ],
  ])("chooses %s as the furthest observed stage", (_name, current, review, currentShip, stage) => {
    expect(presentShipFlow(current, review, currentShip).stage).toBe(stage);
  });

  it.each([
    ["drafting", ship({ status: "drafting", draft: null }), "draft", "in-progress"],
    ["committing", ship({ status: "committing" }), "sign", "in-progress"],
    ["pushing", ship({ status: "pushing", commit_sha: "abc123" }), "push-pr", "in-progress"],
  ])("marks %s as in progress", (_name, currentShip, stage, activity) => {
    const presentation = presentShipFlow(task(), approvedReview(), currentShip);
    expect(presentation.stage).toBe(stage);
    expect(presentation.activity).toBe(activity);
  });

  it("keeps a failed attempt at its retry stage with its daemon error", () => {
    const presentation = presentShipFlow(
      task(),
      approvedReview(),
      ship({
        status: "error",
        commit_sha: "abc123",
        error: "push/PR failed: forbidden",
        last_step: { step: "pr", status: "failed", detail: "forbidden" },
      }),
    );

    expect(presentation).toMatchObject({
      stage: "push-pr",
      activity: "error",
      error: "push/PR failed: forbidden",
    });
    expect(presentation.detail).toContain("Retry Push / PR");
  });

  it("keeps a no-review draft failure at the Draft retry stage", () => {
    const presentation = presentShipFlow(
      task(),
      undefined,
      ship({
        status: "error",
        draft: null,
        error: "could not parse draft markers",
        last_step: {
          step: "draft",
          status: "failed",
          detail: "could not parse draft markers",
        },
      }),
    );

    expect(presentation).toMatchObject({
      stage: "draft",
      activity: "error",
      error: "could not parse draft markers",
    });
  });

  it("uses a pull request over lower ship milestones", () => {
    const presentation = presentShipFlow(
      task({ pr_url: "https://github.com/ompire/maas/pull/1", pr_state: "open" }),
      approvedReview(),
      ship({ status: "error", commit_sha: "abc123", error: "old push failure" }),
    );

    expect(presentation.stage).toBe("wait-merge");
    expect(presentation.activity).toBe("ready");
  });

  it("groups each qualifying task once and sorts by task update time", () => {
    const index = buildShipIndex(
      [
        task({ id: 1, updated_at: "2026-08-20T00:01:00Z" }),
        task({
          id: 2,
          updated_at: "2026-08-20T00:04:00Z",
          pr_url: "https://github.com/ompire/maas/pull/2",
          pr_state: "merged",
        }),
        task({
          id: 3,
          state: "archived",
          updated_at: "2026-08-20T00:05:00Z",
          pr_url: "https://github.com/ompire/maas/pull/3",
          pr_state: "merged",
        }),
        task({ id: 4, updated_at: "2026-08-20T00:06:00Z" }),
        task({ id: 5, updated_at: "2026-08-20T00:03:00Z" }),
      ],
      { 1: approvedReview() },
      { 5: ship({ status: "pushing", commit_sha: "abc123" }) },
    );

    expect(index.active.map((entry) => entry.task.id)).toEqual([2, 5, 1]);
    expect(index.active.map((entry) => entry.stage)).toEqual(["cleanup", "push-pr", "draft"]);
    expect(index.recent.map((entry) => entry.task.id)).toEqual([3]);
    expect(index.recent[0]).toMatchObject({ stage: "cleanup-complete", activity: "complete" });
    expect([...index.active, ...index.recent].map((entry) => entry.task.id)).toEqual([2, 5, 1, 3]);
  });
});
