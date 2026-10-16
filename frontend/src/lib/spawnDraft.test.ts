import { afterEach, describe, expect, it } from "vitest";
import { emptySpawnDraft, loadSpawnDraft, saveSpawnDraft } from "./spawnDraft";

afterEach(() => window.sessionStorage.clear());

describe("spawn environment grant draft", () => {
  it("defaults legacy and empty drafts to no grant", () => {
    expect(loadSpawnDraft().grantEnvironment).toBe(false);
    window.sessionStorage.setItem("ompire.spawnDraft", JSON.stringify({ workflow: "qa-work" }));
    expect(loadSpawnDraft()).toMatchObject({
      workflow: "qa-work", grantEnvironment: false, grantRevision: null,
    });
  });

  it("retains an affirmative selection with the exact workflow revision", () => {
    saveSpawnDraft({
      ...emptySpawnDraft, workflow: "qa-work", project: "project", slug: "qa",
      grantEnvironment: true, grantRevision: "sha256:reviewed",
    });
    expect(loadSpawnDraft()).toMatchObject({
      workflow: "qa-work", project: "project", slug: "qa",
      grantEnvironment: true, grantRevision: "sha256:reviewed",
    });
  });

  it.each([
    { grantEnvironment: true },
    { grantEnvironment: true, grantRevision: null },
    { grantEnvironment: "true", grantRevision: "sha256:reviewed" },
    { grantEnvironment: false, grantRevision: "sha256:reviewed" },
  ])("never restores an unbound or nonaffirmative grant: %j", (selection) => {
    window.sessionStorage.setItem("ompire.spawnDraft", JSON.stringify({
      ...emptySpawnDraft, workflow: "qa-work", ...selection,
    }));
    expect(loadSpawnDraft().grantEnvironment).toBe(false);
  });

  it("clears the saved selection after an accepted launch", () => {
    saveSpawnDraft({
      ...emptySpawnDraft, workflow: "qa-work",
      grantEnvironment: true, grantRevision: "sha256:reviewed",
    });
    saveSpawnDraft(null);
    expect(loadSpawnDraft()).toEqual(emptySpawnDraft);
  });
});
