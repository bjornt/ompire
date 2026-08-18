import { describe, expect, it } from "vitest";
import { countNeedsAttention } from "./attention";
import type { AttentionEntry, Task } from "../types";

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
