import type { WorkshopAdditionsSource } from "../types";

/** The workspace/prompt fields a project supplies and one task may override,
 * in presentation order. Mirrors the daemon's own list. */
export const WORKSPACE_FIELDS = [
  "base_branch",
  "branch_pattern",
  "workshop_additions",
  "preamble",
] as const;

export type WorkspaceField = (typeof WORKSPACE_FIELDS)[number];

/** Per-field override presence. A key that is absent means "inherit"; a key
 * present with an empty string is a real override — most visibly for
 * `preamble`, where empty means "no preamble" rather than "unset". */
export interface DraftOverrides {
  base_branch?: string;
  branch_pattern?: string;
  workshop_additions?: WorkshopAdditionsSource;
  preamble?: string;
}

export interface SpawnDraft {
  workflow: string;
  project: string;
  /** Empty means "inherit the project's default profile". A name is an
   * explicit task-wide choice that replaces that inheritance and survives a
   * project change. */
  profile: string;
  slug: string;
  prompt: string;
  overrides: DraftOverrides;
}

const STORAGE_KEY = "ompire.spawnDraft";

export const emptySpawnDraft: SpawnDraft = {
  workflow: "",
  project: "",
  profile: "",
  slug: "",
  prompt: "",
  overrides: {},
};

/** Restore the draft a Settings round trip interrupted.
 *
 * The draft is transient frontend state, not authoritative registry data, so
 * it lives in session storage rather than in a new server-side entity: it
 * exists only so that leaving the form to create a model profile does not
 * cost the operator everything they had typed.
 */
export function loadSpawnDraft(preselectedProject?: string): SpawnDraft {
  let stored: SpawnDraft = emptySpawnDraft;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<SpawnDraft>;
      stored = {
        ...emptySpawnDraft,
        ...parsed,
        overrides: { ...(parsed.overrides ?? {}) },
      };
    }
  } catch {
    /* unavailable or corrupt storage is simply an empty draft */
  }
  // Entering from a project preselects it, and never restricts which
  // workflows are on offer.
  if (preselectedProject && !stored.project) {
    return { ...stored, project: preselectedProject };
  }
  return stored;
}

export function saveSpawnDraft(draft: SpawnDraft | null): void {
  try {
    if (draft === null) window.sessionStorage.removeItem(STORAGE_KEY);
    else window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
  } catch {
    /* storage being unavailable must never break the form */
  }
}
