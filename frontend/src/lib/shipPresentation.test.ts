import { describe, expect, it } from "vitest";
import type { ReviewState, ShipProjection, Task } from "../types";
import {
  approvalBindingFor,
  buildShipIndex,
  cleanupWarning,
  hasShipFlowHandoff,
  presentShipFlow,
  unresolvedActions,
} from "./shipPresentation";

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    project_name: "maas",
    execution_inputs: null,
    needs_configuration: true,
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
    workflow_revision: "sha256:abc",
    workflow_revision_source: "accepted",
    workflow_ready: true,
    workflow_readiness_reason: null,
    workflow_readiness_detail: null,
    workflow_primary_session: "main",
    workflow_sessions: ["main"],
    workflow_status: null,
    workflow_step: null,
    workflow_result: null,
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:01:00Z",
    ...overrides,
  };
}

function approvedReview(candidateId: string | null = "cand-1"): ReviewState {
  return {
    status: "approved",
    url: "http://127.0.0.1:7180",
    port: 7180,
    candidate_id: candidateId,
    iterations: [
      {
        outcome: "approved",
        comment_count: 0,
        stderr: null,
        candidate_id: candidateId,
        recorded_at: "2026-08-20T00:00:00Z",
      },
    ],
  };
}

function ship(overrides: Partial<ShipProjection> = {}): ShipProjection {
  return {
    task_id: 1,
    version: 1,
    delivery_id: 10,
    disposition: "open",
    ending: null,
    mode: null,
    candidate_id: null,
    review_candidate_id: null,
    draft: null,
    blocked_reason: null,
    workspace_owner: null,
    completed_actions: [],
    remaining_actions: [],
    results: {},
    pr_url: null,
    legacy_publication: false,
    actions: [],
    decisions: [],
    history: [],
    ...overrides,
  };
}

const signedCommit = {
  signed_tip: "s".repeat(40),
  commit_count: 1,
  mode: "squash" as const,
  installed: true,
};

describe("presentShipFlow", () => {
  it("waits for review before anything can be delivered", () => {
    const presentation = presentShipFlow(task(), undefined, undefined);
    expect(presentation.stage).toBe("review");
    expect(presentation.activity).toBe("ready");
    expect(presentation.detail).toContain("approved review of the current content");
  });

  it("treats a completed local ending as a success, not a missing pull request", () => {
    const presentation = presentShipFlow(
      task(),
      approvedReview(),
      ship({
        disposition: "completed",
        ending: "commit",
        completed_actions: ["commit"],
        results: { commit: signedCommit },
      }),
    );
    expect(presentation.stage).toBe("delivered-commit");
    expect(presentation.label).toBe("Signed locally");
    expect(presentation.activity).toBe("complete");
    expect(presentation.error).toBeNull();
  });

  it("treats a push-only ending as complete without waiting for a merge", () => {
    const presentation = presentShipFlow(
      task(),
      approvedReview(),
      ship({
        disposition: "completed",
        ending: "push",
        completed_actions: ["commit", "push"],
        results: {
          commit: signedCommit,
          push: { head: "s".repeat(40), ref: "refs/heads/x", branch: "x" },
        },
      }),
    );
    expect(presentation.stage).toBe("delivered-push");
    expect(presentation.detail).toContain("No pull request was opened");
  });

  it("ranks an unresolved effect above every other stage", () => {
    const projection = ship({
      disposition: "unresolved",
      ending: "pr",
      blocked_reason: "the forge could not be searched completely",
      completed_actions: ["commit", "push"],
      results: {
        commit: signedCommit,
        push: { head: "s".repeat(40), ref: "refs/heads/x", branch: "x" },
      },
      actions: [
        {
          id: 3,
          kind: "pr",
          attempt: 1,
          phase: "needs_reconciliation",
          error: "the response was lost",
          updated_at: "2026-08-20T00:02:00Z",
        },
      ],
    });
    const presentation = presentShipFlow(task(), approvedReview(), projection);
    expect(presentation.stage).toBe("unresolved");
    expect(presentation.activity).toBe("unresolved");
    expect(presentation.error).toContain("could not be searched");
    expect(unresolvedActions(projection)).toHaveLength(1);
  });

  it("keeps PR state ahead of delivery progress once a pull request exists", () => {
    const merged = presentShipFlow(
      task({ pr_url: "https://github.com/o/p/pull/1", pr_state: "merged" }),
      approvedReview(),
      ship({ disposition: "completed", results: { commit: signedCommit } }),
    );
    expect(merged.stage).toBe("cleanup");
  });

  it("reports a blocked delivery with the daemon's own reason", () => {
    const presentation = presentShipFlow(
      task(),
      approvedReview(),
      ship({ disposition: "blocked", blocked_reason: "the signing key is locked" }),
    );
    expect(presentation.stage).toBe("deliver");
    expect(presentation.activity).toBe("error");
    expect(presentation.error).toBe("the signing key is locked");
  });
});

describe("approvalBindingFor", () => {
  it("accepts an approval bound to the delivery's candidate", () => {
    expect(
      approvalBindingFor(approvedReview("cand-1"), ship({ candidate_id: "cand-1" })),
    ).toBe("usable");
  });

  it("calls an approval stale once the delivery names different content", () => {
    expect(
      approvalBindingFor(approvedReview("cand-old"), ship({ candidate_id: "cand-new" })),
    ).toBe("stale");
  });

  it("calls an approval that never named its content unbound", () => {
    expect(approvalBindingFor(approvedReview(null), undefined)).toBe("unbound");
  });
});

describe("cleanupWarning", () => {
  it("warns that a local-only result has no other managed copy", () => {
    const warning = cleanupWarning(
      task(),
      ship({ disposition: "completed", results: { commit: signedCommit } }),
    );
    expect(warning).toContain("only Ompire-managed Git copy");
    expect(warning).toContain("not a backup");
  });

  it("names the remote branch and the absent pull request for a push-only result", () => {
    const warning = cleanupWarning(
      task(),
      ship({
        disposition: "completed",
        results: {
          commit: signedCommit,
          push: { head: "s".repeat(40), ref: "refs/heads/x", branch: "ompire/fix-bug" },
        },
      }),
    );
    expect(warning).toContain("ompire/fix-bug");
    expect(warning).toContain("no pull request");
    expect(warning).toContain("remote branch is left alone");
  });

  it("refuses cleanup outright while an effect is unresolved", () => {
    const warning = cleanupWarning(
      task(),
      ship({
        disposition: "unresolved",
        actions: [
          {
            id: 1,
            kind: "push",
            attempt: 1,
            phase: "needs_reconciliation",
            error: "lost",
            updated_at: "t",
          },
        ],
      }),
    );
    expect(warning).toContain("refused");
  });

  it("says nothing once a pull request exists", () => {
    expect(
      cleanupWarning(task({ pr_url: "https://github.com/o/p/pull/1" }), undefined),
    ).toBeNull();
  });
});

describe("hasShipFlowHandoff", () => {
  it("is true for an approved review, a delivery record, or a pull request", () => {
    expect(hasShipFlowHandoff(task(), approvedReview(), undefined)).toBe(true);
    expect(hasShipFlowHandoff(task(), undefined, ship())).toBe(true);
    expect(
      hasShipFlowHandoff(task({ pr_url: "https://github.com/o/p/pull/1" }), undefined, undefined),
    ).toBe(true);
  });

  it("is false for a task nothing has been decided about", () => {
    expect(hasShipFlowHandoff(task(), undefined, undefined)).toBe(false);
  });

  it("is true for a legacy publication with no journal behind it", () => {
    expect(
      hasShipFlowHandoff(task(), undefined, ship({ delivery_id: null, legacy_publication: true })),
    ).toBe(true);
  });
});

describe("buildShipIndex", () => {
  it("splits live handoffs from archived history, newest first", () => {
    const { active, recent } = buildShipIndex(
      [
        task({ id: 1, updated_at: "2026-08-20T00:01:00Z" }),
        task({ id: 2, updated_at: "2026-08-20T00:03:00Z" }),
        task({
          id: 3,
          state: "archived",
          updated_at: "2026-08-20T00:04:00Z",
          pr_url: "https://github.com/o/p/pull/3",
          pr_state: "merged",
        }),
        task({ id: 4, updated_at: "2026-08-20T00:05:00Z" }),
      ],
      { 1: approvedReview(), 2: approvedReview() },
      {},
    );
    expect(active.map((entry) => entry.task.id)).toEqual([2, 1]);
    expect(recent.map((entry) => entry.task.id)).toEqual([3]);
  });

  it("lists a local-only delivery as active work, not as a failure", () => {
    const { active } = buildShipIndex(
      [task({ id: 1 })],
      {},
      {
        1: ship({
          disposition: "completed",
          ending: "commit",
          completed_actions: ["commit"],
          results: { commit: signedCommit },
        }),
      },
    );
    expect(active).toHaveLength(1);
    expect(active[0].stage).toBe("delivered-commit");
    expect(active[0].error).toBeNull();
  });
});
