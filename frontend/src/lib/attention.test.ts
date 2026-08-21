import { describe, expect, it } from "vitest";
import { countNeedsAttention, isAttentionTask, getAttentionSeverity, attentionSection } from "./attention";
import type { AttentionEntry, SessionInfo, Task } from "../types";

function task(id: number, state: Task["state"]): Task {
  return {
    id,
    project_name: "p",
    template_name: "t",
    slug: "fix",
    branch: "p/fix",
    clone_path: "/",
    state,
    prompt: "",
    error: null,
    workshop_id: null,
    spawn_completed_at: null,
    pr_url: null,
    pr_state: null,
    pr_merged_at: null,
    workflow_name: "single-step",
    workflow_status: null,
    workflow_step: null,
    created_at: "",
    updated_at: "",
  };
}

function entry(tier: AttentionEntry["tier"]): AttentionEntry {
  return { tier, status: "working", reason: "", session: "main" };
}

function session(status: SessionInfo["status"]): SessionInfo {
  return { status, reason: "", since: "" };
}

describe("countNeedsAttention", () => {
  it("counts notify and interrupt tiers regardless of settings", () => {
    const tasks = [task(1, "created"), task(2, "created")];
    const attention = { 1: entry("notify"), 2: entry("interrupt") };
    expect(countNeedsAttention(tasks, attention, {})).toBe(2);
    expect(countNeedsAttention(tasks, attention, { "tier.notify.badge": false })).toBe(2);
    expect(countNeedsAttention(tasks, attention, { "tier.interrupt.badge": false })).toBe(2);
  });

  it("counts badge-tier entries only when tier.badge.badge is enabled", () => {
    const tasks = [task(1, "created")];
    const attention = { 1: entry("badge") };
    expect(countNeedsAttention(tasks, attention, {})).toBe(0);
    expect(countNeedsAttention(tasks, attention, { "tier.badge.badge": false })).toBe(0);
    expect(countNeedsAttention(tasks, attention, { "tier.badge.desktop": true })).toBe(0);
    expect(countNeedsAttention(tasks, attention, { "tier.badge.badge": true })).toBe(1);
  });

  it("does not count silent-tier entries from attention by default", () => {
    const tasks = [task(1, "created")];
    const attention = { 1: entry("silent") };
    expect(countNeedsAttention(tasks, attention, {})).toBe(0);
  });

  it("always counts failed tasks even without attention entries", () => {
    const tasks = [task(1, "failed"), task(2, "created")];
    expect(countNeedsAttention(tasks, {}, {})).toBe(1);
  });

  it("deduplicates when a failed task also has a countable attention entry", () => {
    const tasks = [task(1, "failed")];
    const attention = { 1: entry("interrupt") };
    expect(countNeedsAttention(tasks, attention, {})).toBe(1);
  });
});

describe("isAttentionTask", () => {
  it("returns true for failed tasks without attention entries", () => {
    expect(isAttentionTask(task(1, "failed"), {}, {})).toBe(true);
  });

  it("returns true for notify-tier task regardless of badge settings", () => {
    const t = task(1, "created");
    expect(isAttentionTask(t, { 1: entry("notify") }, {})).toBe(true);
    expect(isAttentionTask(t, { 1: entry("notify") }, { "tier.notify.badge": false })).toBe(true);
  });

  it("returns true for interrupt-tier task regardless of badge settings", () => {
    const t = task(1, "created");
    expect(isAttentionTask(t, { 1: entry("interrupt") }, {})).toBe(true);
  });

  it("returns false for badge-tier when badge pref disabled", () => {
    const t = task(1, "created");
    expect(isAttentionTask(t, { 1: entry("badge") }, {})).toBe(false);
    expect(isAttentionTask(t, { 1: entry("badge") }, { "tier.badge.badge": false })).toBe(false);
  });

  it("returns true for badge-tier when badge pref enabled", () => {
    const t = task(1, "created");
    expect(isAttentionTask(t, { 1: entry("badge") }, { "tier.badge.badge": true })).toBe(true);
  });

  it("returns false for silent-tier entries", () => {
    const t = task(1, "created");
    expect(isAttentionTask(t, { 1: entry("silent") }, {})).toBe(false);
  });

  it("returns false for tasks with no attention and non-failed state", () => {
    expect(isAttentionTask(task(1, "created"), {}, {})).toBe(false);
  });
});

describe("getAttentionSeverity", () => {
  it("returns null for failed tasks without attention entries", () => {
    expect(getAttentionSeverity(task(1, "failed"), {})).toBe(null);
  });

  it("ranks interrupt above notify above badge", () => {
    const interrupt = getAttentionSeverity(task(1, "created"), { 1: entry("interrupt") });
    const notify = getAttentionSeverity(task(1, "created"), { 1: entry("notify") });
    const badge = getAttentionSeverity(task(1, "created"), { 1: entry("badge") });
    expect(interrupt).toBeGreaterThan(notify!);
    expect(notify).toBeGreaterThan(badge!);
  });

  it("returns severity for failed task with attention entry", () => {
    expect(getAttentionSeverity(task(1, "failed"), { 1: entry("interrupt") })).toBe(3);
  });

  it("returns null for tasks with no attention entry and non-failed state", () => {
    expect(getAttentionSeverity(task(1, "created"), {})).toBe(null);
  });
});

describe("attentionSection", () => {
  const emptySessions = {};

  it("assigns failed tasks to needs-you", () => {
    expect(attentionSection(task(1, "failed"), emptySessions, {}, {})).toBe("needs-you");
  });

  it("assigns interrupt-tier tasks to needs-you", () => {
    const t = task(1, "created");
    expect(attentionSection(t, emptySessions, { 1: entry("interrupt") }, {})).toBe("needs-you");
  });

  it("assigns notify-tier tasks to needs-you", () => {
    const t = task(1, "created");
    expect(attentionSection(t, emptySessions, { 1: entry("notify") }, {})).toBe("needs-you");
  });

  it("assigns silent-tier tasks to running", () => {
    const t = task(1, "created");
    expect(attentionSection(t, emptySessions, { 1: entry("silent") }, {})).toBe("running");
  });

  it("assigns working session without attention entry to running", () => {
    const t = task(1, "created");
    const sessions = { main: session("working") };
    expect(attentionSection(t, sessions, {})).toBe("running");
  });

  it("assigns starting session without attention entry to running", () => {
    const t = task(1, "created");
    const sessions = { main: session("starting") };
    expect(attentionSection(t, sessions, {})).toBe("running");
  });

  it("assigns idle session with badge entry to idle", () => {
    const t = task(1, "created");
    const sessions = { main: session("idle") };
    expect(attentionSection(t, sessions, { 1: entry("badge") }, {})).toBe("idle");
  });

  it("assigns idle session with no attention entry to idle", () => {
    const t = task(1, "created");
    const sessions = { main: session("idle") };
    expect(attentionSection(t, sessions, {})).toBe("idle");
  });

  it("assigns task with no sessions to idle", () => {
    const t = task(1, "created");
    expect(attentionSection(t, emptySessions, {})).toBe("idle");
  });

  it("prefers needs-you over running when failed and working", () => {
    // A failed task with a silent-tier attention entry still counts as needs-you
    // because isAttentionTask returns true for failed tasks first.
    const t = task(1, "failed");
    const sessions = { main: session("working") };
    expect(attentionSection(t, sessions, { 1: entry("silent") }, {})).toBe("needs-you");
  });
});