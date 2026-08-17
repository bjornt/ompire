import { describe, expect, it } from "vitest";
import { applyEnvelope, initialDaemonState } from "./daemonReducer";
import type { Envelope, Project, Template } from "../types";

const project: Project = {
  name: "maas",
  title: "MAAS",
  upstream_url: "https://example.com/maas.git",
  fork_url: null,
  checkout_path: "/home/op/proj/maas",
};

const template: Template = {
  name: "maas",
  project_name: "maas",
  base_branch: "master",
  branch_pattern: "bjornt/<slug>",
  workflow: "single-step",
  workshop_additions: "project",
  model: null,
  thinking: null,
  preamble: "",
  created_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T00:00:00Z",
};

describe("applyEnvelope", () => {
  it("replaces state on snapshot", () => {
    const envelope: Envelope = {
      seq: 0,
      ts: "2026-07-18T00:00:00Z",
      type: "snapshot",
      payload: { projects: [project], templates: [template], tasks: [] },
    };
    const next = applyEnvelope(initialDaemonState, envelope);
    expect(next.projects).toEqual([project]);
    expect(next.templates).toEqual([template]);
    expect(next.tasks).toEqual([]);
  });

  it("tolerates a snapshot without a templates list", () => {
    const next = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [] },
    });
    expect(next.templates).toEqual([]);
  });

  it("applies project_created as a delta", () => {
    const start = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [] },
    });
    const next = applyEnvelope(start, {
      seq: 1,
      ts: "",
      type: "project_created",
      payload: project,
    });
    expect(next.projects).toEqual([project]);
  });

  it("applies project_updated by name", () => {
    const start = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [project], tasks: [] },
    });
    const updated = { ...project, title: "MAAS renamed" };
    const next = applyEnvelope(start, {
      seq: 1,
      ts: "",
      type: "project_updated",
      payload: updated,
    });
    expect(next.projects).toEqual([updated]);
  });

  it("applies project_renamed by old name", () => {
    const start = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [project], tasks: [] },
    });
    const renamed = { ...project, name: "maas-ng" };
    const next = applyEnvelope(start, {
      seq: 1,
      ts: "",
      type: "project_renamed",
      payload: { old_name: project.name, project: renamed },
    });
    expect(next.projects).toEqual([renamed]);
  });

  it("applies project_deleted by name", () => {
    const start = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [project], tasks: [] },
    });
    const next = applyEnvelope(start, {
      seq: 1,
      ts: "",
      type: "project_deleted",
      payload: { name: project.name },
    });
    expect(next.projects).toEqual([]);
  });

  it("applies template_created as a delta", () => {
    const start = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], templates: [], tasks: [] },
    });
    const next = applyEnvelope(start, {
      seq: 1,
      ts: "",
      type: "template_created",
      payload: template,
    });
    expect(next.templates).toEqual([template]);
  });

  it("applies template_updated by name", () => {
    const start = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], templates: [template], tasks: [] },
    });
    const updated = { ...template, preamble: "Run pytest from the repo root." };
    const next = applyEnvelope(start, {
      seq: 1,
      ts: "",
      type: "template_updated",
      payload: updated,
    });
    expect(next.templates).toEqual([updated]);
  });

  it("applies template_deleted by name", () => {
    const start = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], templates: [template], tasks: [] },
    });
    const next = applyEnvelope(start, {
      seq: 1,
      ts: "",
      type: "template_deleted",
      payload: { name: template.name },
    });
    expect(next.templates).toEqual([]);
  });

  it("ignores unknown event types", () => {
    const start = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [project], tasks: [] },
    });
    const next = applyEnvelope(start, {
      seq: 1,
      ts: "",
      type: "some_future_event",
      payload: { anything: true },
    });
    expect(next).toBe(start);
  });
});

const task = {
  id: 1,
  project_name: "maas",
  template_name: "maas",
  slug: "fix-bug",
  branch: "bjornt/fix-bug",
  clone_path: "/home/op/tasks/maas/fix-bug",
  state: "created" as const,
  prompt: "fix it",
  error: null,
  workshop_id: null,
  spawn_completed_at: null,
  pr_url: null,
  created_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T00:00:00Z",
};

describe("applyEnvelope task events", () => {
  const empty = applyEnvelope(initialDaemonState, {
    seq: 0,
    ts: "",
    type: "snapshot",
    payload: { projects: [], tasks: [] },
  });

  it("prepends on task_created", () => {
    const next = applyEnvelope(empty, { seq: 1, ts: "", type: "task_created", payload: task });
    expect(next.tasks).toEqual([task]);
  });

  it("replaces by id on task_updated", () => {
    const start = applyEnvelope(empty, { seq: 1, ts: "", type: "task_created", payload: task });
    const updated = { ...task, state: "failed", error: "boom", spawn_completed_at: "x" };
    const next = applyEnvelope(start, { seq: 2, ts: "", type: "task_updated", payload: updated });
    expect(next.tasks).toEqual([updated]);
  });

  it("removes by id and drops progress on task_deleted", () => {
    let state = applyEnvelope(empty, { seq: 1, ts: "", type: "task_created", payload: task });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "spawn_step",
      payload: { task_id: 1, step: "fetch", status: "started" },
    });
    const next = applyEnvelope(state, { seq: 3, ts: "", type: "task_deleted", payload: { id: 1 } });
    expect(next.tasks).toEqual([]);
    expect(next.spawnProgress).toEqual({});
  });

  it("accumulates spawn_step events per task", () => {
    let state = empty;
    for (const payload of [
      { task_id: 1, step: "fetch", status: "started" },
      { task_id: 1, step: "fetch", status: "ok" },
      { task_id: 2, step: "fetch", status: "started" },
    ]) {
      state = applyEnvelope(state, { seq: 0, ts: "", type: "spawn_step", payload });
    }
    expect(state.spawnProgress[1]).toHaveLength(2);
    expect(state.spawnProgress[2]).toHaveLength(1);
  });

  it("clears spawn progress on snapshot", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "",
      type: "spawn_step",
      payload: { task_id: 1, step: "fetch", status: "started" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [task] },
    });
    expect(state.spawnProgress).toEqual({});
    expect(state.tasks).toEqual([task]);
  });
});

describe("applyEnvelope session events", () => {
  const empty = applyEnvelope(initialDaemonState, {
    seq: 0,
    ts: "",
    type: "snapshot",
    payload: { projects: [], tasks: [], sessions: {} },
  });

  it("loads sessions from the snapshot with numeric task-id keys", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: {
        projects: [],
        tasks: [task],
        sessions: { "1": { status: "working", reason: "agent_start frame", since: "t0" } },
      },
    });
    expect(state.sessions[1]).toEqual({
      status: "working",
      reason: "agent_start frame",
      since: "t0",
    });
  });

  it("upserts on status_changed using the envelope timestamp", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: { task_id: 1, from: null, to: "starting", reason: "agent spawned" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "status_changed",
      payload: { task_id: 1, from: "starting", to: "working", reason: "agent_start frame" },
    });
    expect(state.sessions[1]).toEqual({
      status: "working",
      reason: "agent_start frame",
      since: "t2",
    });
  });

  it("drops the session on task_deleted", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: { task_id: 1, from: null, to: "failed", reason: "process exited with code 137" },
    });
    state = applyEnvelope(state, { seq: 2, ts: "", type: "task_deleted", payload: { id: 1 } });
    expect(state.sessions).toEqual({});
  });

  it("tolerates snapshots without a sessions map", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [] },
    });
    expect(state.sessions).toEqual({});
  });

  const question = {
    id: "ask-ui-1",
    kind: "ask" as const,
    questions: [
      {
        prompt: "Widen the fix?",
        options: [{ value: "both", label: "Both", description: null }],
        multi: false,
        recommended: "both",
        allowsOther: true,
      },
    ],
  };

  it("upserts the pending question on question_posted", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: { task_id: 1, from: "working", to: "waiting-input", reason: "pending question" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "question_posted",
      payload: { task_id: 1, question },
    });
    expect(state.sessions[1]).toEqual({
      status: "waiting-input",
      reason: "pending question",
      since: "t1",
      question,
    });
  });

  it("ignores question_posted for an untracked task", () => {
    const state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "question_posted",
      payload: { task_id: 99, question },
    });
    expect(state.sessions).toEqual({});
  });

  it("clears the pending question on question_resolved", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: { task_id: 1, from: "working", to: "waiting-input", reason: "pending question" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "question_posted",
      payload: { task_id: 1, question },
    });
    state = applyEnvelope(state, {
      seq: 3,
      ts: "t3",
      type: "question_resolved",
      payload: { task_id: 1, question_id: question.id },
    });
    expect(state.sessions[1]).toEqual({
      status: "waiting-input",
      reason: "pending question",
      since: "t1",
    });
  });

  it("drops the pending question on task_deleted", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: { task_id: 1, from: "working", to: "waiting-input", reason: "pending question" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "question_posted",
      payload: { task_id: 1, question },
    });
    state = applyEnvelope(state, { seq: 3, ts: "", type: "task_deleted", payload: { id: 1 } });
    expect(state.sessions).toEqual({});
  });
});

describe("applyEnvelope attention/advisory events", () => {
  const empty = applyEnvelope(initialDaemonState, {
    seq: 0,
    ts: "",
    type: "snapshot",
    payload: { projects: [], tasks: [], sessions: {}, attention: {} },
  });

  it("loads attention entries from the snapshot with numeric task-id keys", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: {
        projects: [],
        tasks: [task],
        sessions: {},
        attention: { "1": { tier: "interrupt", status: "failed", reason: "process exited with code 1" } },
      },
    });
    expect(state.attention[1]).toEqual({
      tier: "interrupt",
      status: "failed",
      reason: "process exited with code 1",
    });
  });

  it("tolerates a snapshot without an attention map", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [], sessions: {} },
    });
    expect(state.attention).toEqual({});
  });

  it("upserts on attention and drops on attention_cleared", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "",
      type: "attention",
      payload: { task_id: 1, tier: "notify", status: "stalled", reason: "no frames for 300s" },
    });
    expect(state.attention[1]).toEqual({ tier: "notify", status: "stalled", reason: "no frames for 300s" });

    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "attention_cleared",
      payload: { task_id: 1 },
    });
    expect(state.attention).toEqual({});
  });

  it("drops attention/stats/advisories on task_deleted", () => {
    let state = applyEnvelope(empty, { seq: 1, ts: "", type: "task_created", payload: task });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "attention",
      payload: { task_id: 1, tier: "notify", status: "stalled", reason: "x" },
    });
    state = applyEnvelope(state, {
      seq: 3,
      ts: "",
      type: "stats",
      payload: { task_id: 1, context_pct: 50, tokens: { input: 10, output: 5 }, cost: 0.01 },
    });
    state = applyEnvelope(state, {
      seq: 4,
      ts: "",
      type: "advisory",
      payload: { task_id: 1, kind: "maybe-waiting" },
    });
    state = applyEnvelope(state, { seq: 5, ts: "", type: "task_deleted", payload: { id: 1 } });
    expect(state.attention).toEqual({});
    expect(state.stats).toEqual({});
    expect(state.advisories).toEqual({});
  });

  it("upserts the latest stats sample per task", () => {
    const state = applyEnvelope(empty, {
      seq: 1,
      ts: "",
      type: "stats",
      payload: { task_id: 1, context_pct: 42, tokens: { input: 1200, output: 340 }, cost: 0.0123 },
    });
    expect(state.stats[1]).toEqual({
      task_id: 1,
      context_pct: 42,
      tokens: { input: 1200, output: 340 },
      cost: 0.0123,
    });
  });

  it("tracks active advisories per task and kind, cleared independently", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "",
      type: "advisory",
      payload: { task_id: 1, kind: "context-high", context_pct: 85 },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "advisory",
      payload: { task_id: 1, kind: "maybe-waiting" },
    });
    expect(state.advisories[1]).toEqual({
      "context-high": { task_id: 1, kind: "context-high", context_pct: 85 },
      "maybe-waiting": { task_id: 1, kind: "maybe-waiting" },
    });

    state = applyEnvelope(state, {
      seq: 3,
      ts: "",
      type: "advisory_cleared",
      payload: { task_id: 1, kind: "context-high" },
    });
    expect(state.advisories[1]).toEqual({
      "maybe-waiting": { task_id: 1, kind: "maybe-waiting" },
    });
  });
});

describe("applyEnvelope ship/gpg events", () => {
  const draft = {
    commit_message: "fix the bug",
    pr_title: "Fix the bug",
    pr_body: "This fixes the bug.",
    source: "agent" as const,
  };

  it("loads ships and gpg from the snapshot", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: {
        projects: [],
        tasks: [task],
        ships: {
          "1": {
            status: "drafted",
            draft,
            commit_sha: null,
            pr_url: null,
            error: null,
            updated_at: "t0",
          },
        },
        gpg: { state: "cached", key: "ABC", keygrip: "abc", detail: null, checked_at: "t0" },
      },
    });
    expect(state.ships[1]).toEqual({
      status: "drafted",
      draft,
      commit_sha: null,
      pr_url: null,
      error: null,
      updated_at: "t0",
    });
    expect(state.gpg).toEqual({ state: "cached", key: "ABC", keygrip: "abc", detail: null, checked_at: "t0" });
  });

  it("upserts a ship on ship_draft", () => {
    const empty = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [task] },
    });
    const state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "ship_draft",
      payload: { task_id: 1, draft },
    });
    expect(state.ships[1]).toMatchObject({ status: "drafted", draft, updated_at: "t1" });
  });

  it("tracks step progress and failure on ship_step", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: {
        projects: [],
        tasks: [task],
        ships: {
          "1": {
            status: "committing",
            draft: null,
            commit_sha: null,
            pr_url: null,
            error: null,
            updated_at: "t0",
          },
        },
      },
    });
    state = applyEnvelope(state, {
      seq: 1,
      ts: "t1",
      type: "ship_step",
      payload: { task_id: 1, step: "push", status: "ok" },
    });
    expect(state.ships[1].status).toBe("pushing");
    expect(state.ships[1].lastStep).toEqual({ step: "push", status: "ok" });

    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "ship_step",
      payload: { task_id: 1, step: "pr", status: "failed", detail: "denied" },
    });
    expect(state.ships[1].status).toBe("error");
    expect(state.ships[1].error).toBe("denied");
  });

  it("sets shipped status and pr_url on ship_finished", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: {
        projects: [],
        tasks: [task],
        ships: {
          "1": {
            status: "pushing",
            draft: null,
            commit_sha: "abc",
            pr_url: null,
            error: null,
            updated_at: "t0",
          },
        },
      },
    });
    const next = applyEnvelope(state, {
      seq: 1,
      ts: "t1",
      type: "ship_finished",
      payload: { task_id: 1, status: "shipped", pr_url: "https://github.com/o/p/pull/1" },
    });
    expect(next.ships[1].status).toBe("shipped");
    expect(next.ships[1].pr_url).toBe("https://github.com/o/p/pull/1");
  });

  it("updates gpg state on gpg_status", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 1,
      ts: "",
      type: "gpg_status",
      payload: { status: { state: "locked", key: "ABC", keygrip: "abc", detail: null, checked_at: "t0" } },
    });
    expect(state.gpg?.state).toBe("locked");
  });

  it("drops ships on task_deleted", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: {
        projects: [],
        tasks: [task],
        ships: {
          "1": {
            status: "drafted",
            draft,
            commit_sha: null,
            pr_url: null,
            error: null,
            updated_at: "t0",
          },
        },
      },
    });
    state = applyEnvelope(state, { seq: 1, ts: "", type: "task_deleted", payload: { id: 1 } });
    expect(state.ships).toEqual({});
  });
});
