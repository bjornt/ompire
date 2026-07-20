import { describe, expect, it } from "vitest";
import { applyEnvelope, initialDaemonState } from "./daemonReducer";
import type { Envelope, Project } from "../types";

const project: Project = {
  name: "maas",
  title: "MAAS",
  upstream_url: "https://example.com/maas.git",
  fork_url: null,
  checkout_path: "/home/op/proj/maas",
  base_branch: "master",
  branch_pattern: "bjornt/<slug>",
};

describe("applyEnvelope", () => {
  it("replaces state on snapshot", () => {
    const envelope: Envelope = {
      seq: 0,
      ts: "2026-07-18T00:00:00Z",
      type: "snapshot",
      payload: { projects: [project], tasks: [] },
    };
    const next = applyEnvelope(initialDaemonState, envelope);
    expect(next.projects).toEqual([project]);
    expect(next.tasks).toEqual([]);
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
  slug: "fix-bug",
  branch: "bjornt/fix-bug",
  clone_path: "/home/op/tasks/maas/fix-bug",
  state: "created" as const,
  prompt: "fix it",
  error: null,
  workshop_id: null,
  spawn_completed_at: null,
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
});
