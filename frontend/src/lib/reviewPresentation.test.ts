import { describe, expect, it } from "vitest";
import { primarySessionName } from "./daemonReducer";
import {
  formatReviewCommentCount,
  formatReviewOutcome,
  projectReview,
} from "./reviewPresentation";
import type { ReviewState, SessionInfo, WorkflowState } from "../types";

const idle: SessionInfo = { status: "idle", reason: "agent_end", since: "2026-08-26T10:00:00Z" };
const working: SessionInfo = { status: "working", reason: "prompt", since: "2026-08-26T10:00:00Z" };

function review(overrides: Partial<ReviewState> = {}): ReviewState {
  return {
    status: "open",
    url: "http://127.0.0.1:7180",
    port: 7180,
    iterations: [],
    ...overrides,
  };
}

describe("review presentation", () => {
  it("explains no-review availability from the primary session", () => {
    expect(projectReview(undefined, undefined)).toMatchObject({
      state: "no-review",
      canStart: false,
      hint: "Review is unavailable until the primary session has started.",
    });
    expect(projectReview(undefined, idle)).toMatchObject({ state: "no-review", canStart: true });
  });

  it("keeps repeated comment iterations chronological and re-enables review only when idle", () => {
    const state = review({
      iterations: [
        { outcome: "comments", comment_count: 2, stderr: null, recorded_at: "2026-08-26T10:00:00Z" },
        { outcome: "comments", comment_count: null, stderr: null, recorded_at: "2026-08-26T11:00:00Z" },
      ],
    });

    expect(projectReview(state, working)).toMatchObject({
      state: "comments",
      canStart: false,
      hint: "The primary agent is addressing review comments.",
    });
    expect(projectReview(state, idle)).toMatchObject({ state: "comments", canStart: true });
    expect(projectReview(state, idle).iterations).toEqual(state.iterations);
    expect(formatReviewCommentCount(state.iterations[0])).toBe("2 comments");
    expect(formatReviewCommentCount(state.iterations[1])).toBe("Comments submitted");
  });

  it("projects every terminal outcome with its shared vocabulary", () => {
    for (const outcome of ["approved", "aborted", "error"] as const) {
      const presentation = projectReview(
        review({
          status: outcome,
          iterations: [
            { outcome, comment_count: null, stderr: outcome === "error" ? "reviewer failed" : null, recorded_at: "t" },
          ],
        }),
        idle,
      );
      expect(presentation.state).toBe(outcome);
      expect(presentation.label).toBe(formatReviewOutcome(outcome));
      expect(presentation.canStart).toBe(true);
    }
  });

  it("distinguishes an open process from comment feedback", () => {
    expect(projectReview(review(), { ...idle, status: "reviewing" })).toMatchObject({
      state: "open",
      canCancel: true,
      canStart: false,
    });
  });
  it("keeps a reopened review open after earlier comments", () => {
    const state = review({
      iterations: [
        { outcome: "comments", comment_count: 1, stderr: null, recorded_at: "2026-08-26T10:00:00Z" },
      ],
    });
    expect(projectReview(state, { ...idle, status: "reviewing" })).toMatchObject({
      state: "open",
      canStart: false,
      canCancel: true,
    });
  });

  it("uses the workflow primary session rather than the active workflow step", () => {
    const workflow: WorkflowState = {
      name: "multi-session",
      status: "running",
      step: "validate",
      steps: [
        {
          task_id: 1,
          seq: 1,
          step: "implement",
          kind: "agent",
          session: "main",
          status: "ok",
          outcome: null,
          error: null,
          prompted_at: null,
          started_at: "t0",
          finished_at: "t1",
        },
        {
          task_id: 1,
          seq: 2,
          step: "validate",
          kind: "agent",
          session: "checker",
          status: "running",
          outcome: null,
          error: null,
          prompted_at: null,
          started_at: "t2",
          finished_at: null,
        },
      ],
    };
    expect(primarySessionName({ main: idle, checker: working }, workflow)).toBe("main");
  });
  it("uses the bugfix primary even before that session has spawned", () => {
    const workflow: WorkflowState = {
      name: "bugfix",
      status: "running",
      step: "reproduce",
      steps: [
        {
          task_id: 1,
          seq: 1,
          step: "reproduce",
          kind: "agent",
          session: "reproducer",
          status: "ok",
          outcome: null,
          error: null,
          prompted_at: null,
          started_at: "t0",
          finished_at: "t1",
        },
      ],
    };
    expect(primarySessionName({ reproducer: idle }, workflow)).toBe("coder");
  });
});
