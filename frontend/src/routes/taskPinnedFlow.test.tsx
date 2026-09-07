import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Task, TaskExecutionInputs } from "../types";

/** The procedure a task accepted, with what happened laid over it.
 *
 * What these hold is the difference between a declaration and an attempt.
 * A step nobody reached is possible work, not successful work; a step visited
 * twice keeps both records, including the rejection; and the flow shown is
 * the revision this task *pinned*, not whatever the library means by that
 * name today.
 */

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  close() {}
  send() {}
  emit(type: string, payload: unknown) {
    this.onmessage?.({ data: JSON.stringify({ seq: 0, ts: "t", type, payload }) });
  }
  emitSnapshot(payload: Record<string, unknown>) {
    this.onopen?.();
    this.emit("snapshot", payload);
  }
}

function mainSocket(): MockWebSocket {
  return MockWebSocket.instances.find(
    (s) => s.url.includes("/api/ws") && !s.url.includes("/agents/"),
  )!;
}

const project = {
  name: "maas",
  title: "MAAS",
  upstream_url: "https://example.com/maas.git",
  fork_url: null,
  checkout_path: "/home/op/proj/maas",
};

const PINNED = "sha256:pinned";

const acceptedInputs: TaskExecutionInputs = {
  workflow_binding: {
    revision: PINNED,
    source: "accepted",
    bound_at: "2026-09-06T00:00:00Z",
    legacy_through_seq: 0,
    interrupted_legacy_seq: null,
  },
  version: 2,
  provenance: "accepted",
  accepted_at: "2026-07-18T00:00:00Z",
  project_name: "maas",
  workflow_name: "bugfix",
  model_profile_name: "balanced",
  model_profile_source: "project",
  step_bindings: {},
  workspace: {
    base_branch: "master",
    branch_pattern: "bjornt/<slug>",
    workshop_additions: "project",
    preamble: "",
  },
  workspace_overrides: [],
  branch: "bjornt/fix-bug",
  checkout_path: "/home/op/proj/maas",
  fetch_remote: "origin",
  upstream_url: "https://example.com/maas.git",
  fork_url: null,
  unknown_inputs: [],
};

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 1,
    project_name: "maas",
    execution_inputs: acceptedInputs,
    needs_configuration: false,
    slug: "fix-bug",
    branch: "bjornt/fix-bug",
    clone_path: "/home/op/tasks/maas/fix-bug",
    state: "created",
    prompt: "fix it",
    error: null,
    workshop_id: "ws-maas-fix-bug",
    spawn_completed_at: "2026-07-18T00:01:00Z",
    pr_url: null,
    pr_state: null,
    pr_merged_at: null,
    workflow_name: "bugfix",
    workflow_revision: PINNED,
    workflow_revision_source: "accepted",
    workflow_ready: true,
    workflow_readiness_reason: null,
    workflow_readiness_detail: null,
    workflow_primary_session: "qa",
    workflow_sessions: ["qa", "dev"],
    workflow_status: "running",
    workflow_step: "verify",
    workflow_result: null,
    created_at: "2026-07-18T00:00:00Z",
    updated_at: "2026-07-18T00:01:00Z",
    ...overrides,
  };
}

/** The pinned definition. Deliberately different from anything the library
 * would return for the name today. */
const PINNED_DEFINITION = {
  format: 2,
  name: "bugfix",
  sessions: ["qa", "dev"],
  primary: "qa",
  steps: [
    {
      name: "reproduce",
      kind: "agent",
      session: "qa",
      role: "default",
      when: true,
      evidence: {},
      outcome: { results: { reproduced: { required: {} }, not_reproduced: { required: {} } } },
      prompt: { separator: "", parts: [{ text: "Try to reproduce." }] },
    },
    {
      name: "fix",
      kind: "agent",
      session: "dev",
      role: "default",
      when: true,
      evidence: { attempt: { steps: ["reproduce"], after: null, with_outcome: true, required: true } },
      outcome: { results: { fixed: { required: {} } } },
      prompt: { separator: "", parts: [{ value: { op: "evidence", name: "attempt" }, format: "json" }] },
    },
    {
      name: "verify",
      kind: "agent",
      session: "qa",
      role: "default",
      when: true,
      evidence: { fix: { steps: ["fix"], after: null, with_outcome: true, required: true } },
      outcome: { results: { verified: { required: {} }, rejected: { required: {} } } },
      max_visits: 2,
      on_exhausted: { step: "give-up" },
      prompt: { separator: "", parts: [{ text: "Check it." }] },
    },
    {
      name: "give-up",
      kind: "gate",
      evidence: {},
      message: { separator: "", parts: [{ text: "Verification kept failing." }] },
      choices: [
        { id: "stop", label: "Stop here", feedback_required: true, next: { complete: true, result: "abandoned" } },
      ],
    },
  ],
};

/** Two visits to `verify`: a rejection and then a pass. Collapsing them into
 * the latest would erase the rejection from the run's history. */
const workflowState = {
  "1": {
    name: "bugfix",
    status: "running",
    step: "verify",
    steps: [
      {
        task_id: 1,
        seq: 1,
        step: "reproduce",
        kind: "agent",
        session: "qa",
        status: "ok",
        outcome: { version: 2, result: "not_reproduced", summary: "could not trigger it", artifacts: {} },
        error: null,
        pause: null,
        // A step declaring no selectors records none, exactly as a format-1
        // attempt does.
        evidence: null,
        prompted_at: null,
        started_at: "t0",
        finished_at: "t1",
      },
      {
        task_id: 1,
        seq: 2,
        step: "fix",
        kind: "agent",
        session: "dev",
        status: "ok",
        outcome: { version: 2, result: "fixed", summary: "guarded the null path", artifacts: {} },
        error: null,
        pause: null,
        evidence: { version: 1, bindings: { attempt: { step: "reproduce", seq: 1 } } },
        prompted_at: null,
        started_at: "t1",
        finished_at: "t2",
      },
      {
        task_id: 1,
        seq: 3,
        step: "verify",
        kind: "agent",
        session: "qa",
        status: "ok",
        outcome: { version: 2, result: "rejected", summary: "still fails on the vlan path", artifacts: {} },
        error: null,
        pause: null,
        evidence: { version: 1, bindings: { fix: { step: "fix", seq: 2 } } },
        prompted_at: null,
        started_at: "t2",
        finished_at: "t3",
      },
      {
        task_id: 1,
        seq: 4,
        step: "verify",
        kind: "agent",
        session: "qa",
        status: "running",
        outcome: null,
        error: null,
        pause: null,
        evidence: { version: 1, bindings: { fix: { step: "fix", seq: 2 } } },
        prompted_at: null,
        started_at: "t4",
        finished_at: null,
      },
    ],
  },
};

function stubFetch(definition: unknown = PINNED_DEFINITION) {
  const asked: string[] = [];
  const ok = (body: unknown) => ({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
  const fetchMock = vi.fn((url: string) => {
    const u = String(url);
    asked.push(u);
    if (u.includes("/api/workflows/revisions/")) {
      return Promise.resolve(
        ok({
          revision: PINNED,
          name: "bugfix",
          format: 2,
          primary_session: "qa",
          sessions: ["qa", "dev"],
          definition,
        }),
      );
    }
    if (u.endsWith("/agent/state")) {
      return Promise.resolve(ok({ isStreaming: false, queuedMessageCount: 0 }));
    }
    if (u.endsWith("/agent/stats")) return Promise.resolve(ok({}));
    if (u.includes("/agent/")) return Promise.resolve(ok({ command: "ok", success: true }));
    return Promise.resolve(ok({ ...makeTask(), workshop_status: "present" }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return asked;
}

async function openProcedure() {
  window.history.pushState({}, "", "/tasks/1");
  render(<App />);
  act(() => {
    mainSocket().emitSnapshot({
      projects: [project],
      tasks: [makeTask()],
      sessions: { "1": { qa: { status: "idle", reason: "queue empty", since: "t0" } } },
      workflows: workflowState,
    });
  });
  await screen.findByTestId("task-metadata");
  const panel = await screen.findByTestId("pinned-procedure");
  const user = userEvent.setup();
  await user.click(within(panel).getByText(/read the pinned procedure/));
  await within(panel).findByTestId("workflow-revision-flow");
  return { panel, user };
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the pinned procedure", () => {
  it("is read by the task's own revision, not by its workflow name", async () => {
    const asked = stubFetch();
    await openProcedure();
    expect(
      asked.some((url) =>
        url.includes(`/api/workflows/revisions/${encodeURIComponent(PINNED)}`),
      ),
    ).toBe(true);
    // A name lookup would answer with today's definition, which is exactly
    // what an accepted run must not be re-explained by.
    expect(asked.some((url) => /\/api\/workflows$/.test(url))).toBe(false);
  });

  it("keeps every visit separately inspectable", async () => {
    stubFetch();
    const { panel } = await openProcedure();
    const verify = within(panel).getByTestId("flow-step-verify");

    expect(within(verify).getByTestId("attempt-verify-3").textContent).toContain(
      "still fails on the vlan path",
    );
    expect(within(verify).getByTestId("attempt-verify-4").textContent).toContain(
      "running now",
    );
    expect(within(verify).getByTestId("visit-count").textContent).toContain(
      "2 recorded visits",
    );
  });

  it("shows an unreached step as possible work rather than successful work", async () => {
    stubFetch();
    const { panel } = await openProcedure();
    const gate = within(panel).getByTestId("flow-step-give-up");
    expect(within(gate).getByTestId("step-unvisited").textContent).toContain(
      "Not visited",
    );
    // And it offers no action: the run is not waiting here.
    expect(within(gate).queryByRole("button", { name: /submit/i })).toBeNull();
  });

  it("links a consumed reference to the exact attempt that produced it", async () => {
    stubFetch();
    const { panel, user } = await openProcedure();
    const link = within(panel).getByTestId("evidence-source-3-fix");
    expect(link.textContent).toContain("fix attempt 2");
    await user.click(link);
    expect(document.getElementById("attempt-2")).not.toBeNull();
  });

  it("distinguishes a step that asks for no evidence from lost history", async () => {
    // Both record `null` bindings. Calling both "not recoverable" reads as
    // history somebody lost, when one of them is a step that never asked.
    stubFetch();
    const { panel } = await openProcedure();
    const reproduce = within(panel).getByTestId("flow-step-reproduce");
    expect(within(reproduce).queryByTestId("attempt-evidence-1")).toBeNull();
    expect(reproduce.textContent).toContain("asks for no evidence");
    expect(within(reproduce).getByTestId("attempt-result-1").textContent).toContain(
      "not_reproduced",
    );
  });

  it("says so when a step that does ask for evidence recorded none", async () => {
    stubFetch({
      ...PINNED_DEFINITION,
      steps: PINNED_DEFINITION.steps.map((step) =>
        step.name === "reproduce"
          ? { ...step, evidence: { prior: { steps: ["fix"], after: null, with_outcome: true, required: false } } }
          : step,
      ),
    });
    const { panel } = await openProcedure();
    expect(within(panel).getByTestId("flow-step-reproduce").textContent).toContain(
      "not on the record",
    );
  });

  it("shows the declared routes without claiming which one a run took", async () => {
    stubFetch();
    const { panel } = await openProcedure();
    const verify = within(panel).getByTestId("flow-step-verify");
    // The bound and its exhaustion gate are both stated.
    expect(verify.textContent).toContain("at most 2 visits");
    expect(verify.textContent).toContain("after 2 visits");
    expect(verify.textContent).toContain("give-up");
    // No claim about a winning predicate anywhere in the flow.
    expect(panel.textContent).not.toContain("was taken because");
  });
});
