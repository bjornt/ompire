import { describe, expect, it } from "vitest";
import { applyEnvelope, initialDaemonState } from "./daemonReducer";
import type { Envelope, Project } from "../types";

const project: Project = {
  name: "maas",
  title: "MAAS",
  upstream_url: "https://example.com/maas.git",
  fork_url: null,
  checkout_path: "/home/op/proj/maas",
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
