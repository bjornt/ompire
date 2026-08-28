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
    expect(initialDaemonState.snapshotReady).toBe(false);
    const next = applyEnvelope(initialDaemonState, envelope);
    expect(next.projects).toEqual([project]);
    expect(next.templates).toEqual([template]);
    expect(next.tasks).toEqual([]);
    expect(next.snapshotReady).toBe(true);
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
  workflow_name: "single-step",
  workflow_status: null,
  workflow_step: null,
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

  it("loads nested sessions from the snapshot with numeric task-id keys", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: {
        projects: [],
        tasks: [task],
        sessions: {
          "1": {
            main: { status: "working", reason: "agent_start frame", since: "t0" },
            reviewer: { status: "idle", reason: "queue empty", since: "t0" },
          },
        },
      },
    });
    expect(state.sessions[1]).toEqual({
      main: { status: "working", reason: "agent_start frame", since: "t0" },
      reviewer: { status: "idle", reason: "queue empty", since: "t0" },
    });
  });

  it("upserts on status_changed per session using the envelope timestamp", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: { task_id: 1, session: "main", from: null, to: "starting", reason: "agent spawned" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "status_changed",
      payload: {
        task_id: 1,
        session: "main",
        from: "starting",
        to: "working",
        reason: "agent_start frame",
      },
    });
    expect(state.sessions[1]).toEqual({
      main: { status: "working", reason: "agent_start frame", since: "t2" },
    });
  });

  it("tracks sessions of one task independently", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: { task_id: 1, session: "reproducer", from: null, to: "working", reason: "fr" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "status_changed",
      payload: { task_id: 1, session: "coder", from: null, to: "starting", reason: "spawned" },
    });
    state = applyEnvelope(state, {
      seq: 3,
      ts: "t3",
      type: "status_changed",
      payload: { task_id: 1, session: "reproducer", from: "working", to: "idle", reason: "done" },
    });
    expect(state.sessions[1]).toEqual({
      reproducer: { status: "idle", reason: "done", since: "t3" },
      coder: { status: "starting", reason: "spawned", since: "t2" },
    });
  });

  it("drops the session on task_deleted", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: {
        task_id: 1,
        session: "main",
        from: null,
        to: "failed",
        reason: "process exited with code 137",
      },
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

  it("upserts the pending question on question_posted for the addressed session", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: {
        task_id: 1,
        session: "main",
        from: "working",
        to: "waiting-input",
        reason: "pending question",
      },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "question_posted",
      payload: { task_id: 1, session: "main", question },
    });
    expect(state.sessions[1]).toEqual({
      main: { status: "waiting-input", reason: "pending question", since: "t1", question },
    });
  });

  it("ignores question_posted for an untracked task or session", () => {
    const unknown = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "question_posted",
      payload: { task_id: 99, session: "main", question },
    });
    expect(unknown.sessions).toEqual({});

    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: { task_id: 1, session: "main", from: null, to: "working", reason: "r" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "question_posted",
      payload: { task_id: 1, session: "reviewer", question },
    });
    expect(state.sessions[1]).toEqual({ main: { status: "working", reason: "r", since: "t1" } });
  });

  it("clears the pending question on question_resolved", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: {
        task_id: 1,
        session: "main",
        from: "working",
        to: "waiting-input",
        reason: "pending question",
      },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "question_posted",
      payload: { task_id: 1, session: "main", question },
    });
    state = applyEnvelope(state, {
      seq: 3,
      ts: "t3",
      type: "question_resolved",
      payload: { task_id: 1, session: "main", question_id: question.id },
    });
    expect(state.sessions[1]).toEqual({
      main: { status: "waiting-input", reason: "pending question", since: "t1" },
    });
  });

  it("drops the pending question on task_deleted", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "t1",
      type: "status_changed",
      payload: {
        task_id: 1,
        session: "main",
        from: "working",
        to: "waiting-input",
        reason: "pending question",
      },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "question_posted",
      payload: { task_id: 1, session: "main", question },
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
        attention: {
          "1": {
            tier: "interrupt",
            status: "failed",
            reason: "process exited with code 1",
            session: "main",
          },
        },
      },
    });
    expect(state.attention[1]).toEqual({
      tier: "interrupt",
      status: "failed",
      reason: "process exited with code 1",
      session: "main",
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
      payload: {
        task_id: 1,
        tier: "notify",
        status: "stalled",
        reason: "no frames for 300s",
        session: "main",
      },
    });
    expect(state.attention[1]).toEqual({
      tier: "notify",
      status: "stalled",
      reason: "no frames for 300s",
      session: "main",
    });

    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "attention_cleared",
      payload: { task_id: 1 },
    });
    expect(state.attention).toEqual({});
  });

  it("drops attention/stats/advisories/workflows on task_deleted", () => {
    let state = applyEnvelope(empty, { seq: 1, ts: "", type: "task_created", payload: task });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "attention",
      payload: { task_id: 1, tier: "notify", status: "stalled", reason: "x", session: "main" },
    });
    state = applyEnvelope(state, {
      seq: 3,
      ts: "",
      type: "stats",
      payload: {
        task_id: 1,
        session: "main",
        context_pct: 50,
        tokens: { input: 10, output: 5 },
        cost: 0.01,
      },
    });
    state = applyEnvelope(state, {
      seq: 4,
      ts: "",
      type: "advisory",
      payload: { task_id: 1, session: "main", kind: "maybe-waiting" },
    });
    state = applyEnvelope(state, {
      seq: 5,
      ts: "",
      type: "workflow_step",
      payload: { task_id: 1, step: "work", kind: "agent", session: "main", status: "started" },
    });
    state = applyEnvelope(state, { seq: 6, ts: "", type: "task_deleted", payload: { id: 1 } });
    expect(state.attention).toEqual({});
    expect(state.stats).toEqual({});
    expect(state.advisories).toEqual({});
    expect(state.workflows).toEqual({});
  });

  it("upserts the latest stats sample per task and session", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "",
      type: "stats",
      payload: {
        task_id: 1,
        session: "main",
        context_pct: 42,
        tokens: { input: 1200, output: 340 },
        cost: 0.0123,
      },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "stats",
      payload: {
        task_id: 1,
        session: "reviewer",
        context_pct: 10,
        tokens: { input: 100, output: 40 },
        cost: 0.001,
      },
    });
    expect(state.stats[1]).toEqual({
      main: { task_id: 1, session: "main", context_pct: 42, tokens: { input: 1200, output: 340 }, cost: 0.0123 },
      reviewer: {
        task_id: 1,
        session: "reviewer",
        context_pct: 10,
        tokens: { input: 100, output: 40 },
        cost: 0.001,
      },
    });
  });

  it("tracks active advisories per task, session, and kind, cleared independently", () => {
    let state = applyEnvelope(empty, {
      seq: 1,
      ts: "",
      type: "advisory",
      payload: { task_id: 1, session: "main", kind: "context-high", context_pct: 85 },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "",
      type: "advisory",
      payload: { task_id: 1, session: "reviewer", kind: "maybe-waiting" },
    });
    expect(state.advisories[1]).toEqual({
      main: { "context-high": { task_id: 1, session: "main", kind: "context-high", context_pct: 85 } },
      reviewer: { "maybe-waiting": { task_id: 1, session: "reviewer", kind: "maybe-waiting" } },
    });

    state = applyEnvelope(state, {
      seq: 3,
      ts: "",
      type: "advisory_cleared",
      payload: { task_id: 1, session: "main", kind: "context-high" },
    });
    expect(state.advisories[1]).toEqual({
      main: {},
      reviewer: { "maybe-waiting": { task_id: 1, session: "reviewer", kind: "maybe-waiting" } },
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
        gpg: { state: "ready", selected: { fingerprint: "A".repeat(40), key_id: "ABC", uid: null, keygrip: "abc", source: "auto", protection: "unprotected" }, candidates: [], cache_ttl: null, detail: null, checked_at: "t0" },
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
    expect(state.gpg).toEqual({ state: "ready", selected: { fingerprint: "A".repeat(40), key_id: "ABC", uid: null, keygrip: "abc", source: "auto", protection: "unprotected" }, candidates: [], cache_ttl: null, detail: null, checked_at: "t0" });
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

  it("upserts draft lifecycle from ship_step and clears errors on success", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 1,
      ts: "t1",
      type: "ship_step",
      payload: { task_id: 1, step: "draft", status: "started" },
    });
    expect(state.ships[1]).toMatchObject({
      status: "drafting",
      draft: null,
      error: null,
      last_step: { step: "draft", status: "started" },
    });

    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "ship_step",
      payload: { task_id: 1, step: "draft", status: "failed", detail: "bad markers" },
    });
    expect(state.ships[1]).toMatchObject({
      status: "error",
      error: "bad markers",
      last_step: { step: "draft", status: "failed", detail: "bad markers" },
    });

    state = applyEnvelope(state, {
      seq: 3,
      ts: "t3",
      type: "ship_draft",
      payload: { task_id: 1, draft },
    });
    expect(state.ships[1]).toMatchObject({
      status: "drafted",
      draft,
      error: null,
      last_step: { step: "draft", status: "ok" },
    });
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
    expect(state.ships[1].last_step).toEqual({ step: "push", status: "ok" });

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

describe("applyEnvelope GitHub status", () => {
  const target = {
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
  const readyGh = {
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
    targets: { "github.com/owner/repo": target },
  };

  it("tolerates a rolling snapshot without GitHub status", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [] },
    });
    expect(state.gh).toBeNull();
  });

  it("replaces GitHub status from snapshots and deltas", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [], gh: readyGh },
    });
    expect(state.gh?.targets["github.com/owner/repo"]?.state).toBe("allowed");

    state = applyEnvelope(state, {
      seq: 1,
      ts: "t1",
      type: "gh_status",
      payload: {
        gh: {
          ...readyGh,
          identity: { ...readyGh.identity, state: "error", login: null, detail: "network unavailable" },
          targets: {},
        },
      },
    });
    expect(state.gh?.identity.state).toBe("error");
    expect(state.gh?.targets).toEqual({});
  });

  it("drops target results whose identity binding no longer matches", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "gh_status",
      payload: {
        gh: {
          ...readyGh,
          identity: { ...readyGh.identity, login: "another-account" },
          targets: { "github.com/owner/repo": target },
        },
      },
    });
    expect(state.gh?.targets).toEqual({});
  });
});

describe("applyEnvelope workflow events", () => {
  const stepRecord = {
    task_id: 1,
    seq: 1,
    step: "work",
    kind: "agent",
    session: "main",
    status: "running",
    outcome: null,
    error: null,
    prompted_at: null,
    started_at: "t0",
    finished_at: null,
  };

  it("loads workflows from the snapshot with numeric task-id keys", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: {
        projects: [],
        tasks: [task],
        workflows: {
          "1": { name: "single-step", status: "running", step: "work", steps: [stepRecord] },
        },
      },
    });
    expect(state.workflows[1]).toEqual({
      name: "single-step",
      status: "running",
      step: "work",
      steps: [stepRecord],
    });
  });

  it("tolerates a snapshot without a workflows map", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [] },
    });
    expect(state.workflows).toEqual({});
  });

  it("syncs the workflows slice from task_created/task_updated payloads", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 1,
      ts: "",
      type: "task_created",
      payload: task,
    });
    expect(state.workflows[1]).toEqual({
      name: "single-step",
      status: null,
      step: null,
      steps: [],
    });

    const updated = { ...task, workflow_status: "waiting", workflow_step: "confirm" };
    state = applyEnvelope(state, { seq: 2, ts: "", type: "task_updated", payload: updated });
    expect(state.workflows[1]).toMatchObject({ status: "waiting", step: "confirm" });
  });

  it("appends a running record on workflow_step started and closes it on ok", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [task] },
    });
    state = applyEnvelope(state, {
      seq: 1,
      ts: "t1",
      type: "workflow_step",
      payload: { task_id: 1, step: "work", kind: "agent", session: "main", status: "started" },
    });
    expect(state.workflows[1]).toMatchObject({ status: "running", step: "work" });
    expect(state.workflows[1].steps).toEqual([
      {
        task_id: 1,
        seq: 1,
        step: "work",
        kind: "agent",
        session: "main",
        status: "running",
        outcome: null,
        error: null,
        prompted_at: null,
        started_at: "t1",
        finished_at: null,
      },
    ]);

    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "workflow_step",
      payload: { task_id: 1, step: "work", kind: "agent", session: "main", status: "ok" },
    });
    expect(state.workflows[1].steps).toHaveLength(1);
    expect(state.workflows[1].steps[0]).toMatchObject({ status: "ok", finished_at: "t2" });
  });

  it("keeps the run status on ok and lets task_updated land terminal states", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [task] },
    });
    state = applyEnvelope(state, {
      seq: 1,
      ts: "t1",
      type: "workflow_step",
      payload: { task_id: 1, step: "work", kind: "agent", session: "main", status: "started" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "workflow_step",
      payload: { task_id: 1, step: "work", kind: "agent", session: "main", status: "ok" },
    });
    expect(state.workflows[1].status).toBe("running");

    state = applyEnvelope(state, {
      seq: 3,
      ts: "t3",
      type: "task_updated",
      payload: { ...task, workflow_status: "complete", workflow_step: "work" },
    });
    expect(state.workflows[1].status).toBe("complete");
    // The accumulated step records survive the payload-driven sync.
    expect(state.workflows[1].steps).toHaveLength(1);
  });

  it("records the gate message in the waiting step's outcome", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [task] },
    });
    state = applyEnvelope(state, {
      seq: 1,
      ts: "t1",
      type: "workflow_step",
      payload: {
        task_id: 1,
        step: "confirm",
        kind: "gate",
        session: null,
        status: "waiting",
        message: "Review the reproducer output?",
      },
    });
    expect(state.workflows[1].status).toBe("waiting");
    expect(state.workflows[1].steps[0]).toMatchObject({
      step: "confirm",
      kind: "gate",
      status: "waiting",
      outcome: { message: "Review the reproducer output?" },
      finished_at: null,
    });
  });

  it("marks the matching record failed with its error", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [task] },
    });
    state = applyEnvelope(state, {
      seq: 1,
      ts: "t1",
      type: "workflow_step",
      payload: { task_id: 1, step: "work", kind: "agent", session: "main", status: "started" },
    });
    state = applyEnvelope(state, {
      seq: 2,
      ts: "t2",
      type: "workflow_step",
      payload: {
        task_id: 1,
        step: "work",
        kind: "agent",
        session: "main",
        status: "failed",
        error: "agent exited with code 1",
      },
    });
    expect(state.workflows[1].status).toBe("failed");
    expect(state.workflows[1].steps[0]).toMatchObject({
      status: "failed",
      error: "agent exited with code 1",
      finished_at: "t2",
    });
  });

  it("appends a second record when a step name repeats (loop/retry)", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [task] },
    });
    for (const [seq, status] of [
      [1, "started"],
      [2, "ok"],
      [3, "started"],
    ] as const) {
      state = applyEnvelope(state, {
        seq,
        ts: `t${seq}`,
        type: "workflow_step",
        payload: { task_id: 1, step: "work", kind: "agent", session: "main", status },
      });
    }
    expect(state.workflows[1].steps.map((s) => [s.seq, s.status])).toEqual([
      [1, "ok"],
      [2, "running"],
    ]);
  });
});

describe("applyEnvelope daemon settings events", () => {
  it("loads settings from the snapshot", () => {
    const settings = {
      renotify_interval: 300,
      "tier.badge.badge": true,
    };
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [], settings },
    });
    expect(state.settings).toEqual(settings);
  });

  it("defaults to an empty settings map when the snapshot omits settings", () => {
    const state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [] },
    });
    expect(state.settings).toEqual({});
  });

  it("replaces settings on settings_changed", () => {
    let state = applyEnvelope(initialDaemonState, {
      seq: 0,
      ts: "",
      type: "snapshot",
      payload: { projects: [], tasks: [], settings: { stall_threshold: 300 } },
    });
    state = applyEnvelope(state, {
      seq: 1,
      ts: "",
      type: "settings_changed",
      payload: { settings: { renotify_interval: 600 } },
    });
    expect(state.settings).toEqual({ renotify_interval: 600 });
  });
});
