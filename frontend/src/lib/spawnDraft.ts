import type { ModelRole, WorkshopAdditionsSource } from "../types";

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

/** One consumer row's selections. An absent key means "inherit that
 * dimension"; the two are independent, so a row can override its role and
 * still follow the task profile (ADR-0027). An explicit value equal to the
 * inherited one is still explicit — that is what survives a later task
 * profile change. */
export interface DraftConsumerOverride {
  profile?: string;
  role?: ModelRole;
}

export type DraftConsumerOverrides = Record<string, DraftConsumerOverride>;

/** One selected result revision, held by the exact identity the daemon
 * requires. The manifest id is what makes this a revision rather than a
 * pointer: a successor capture on the same producing task does not match it,
 * so a stale selection is refused instead of quietly upgraded. */
export interface DraftAttachment {
  producer_task_id: number;
  result_id: string;
  expected_manifest_id: string;
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
  /** Keyed by declared agent step name. Workflow-scoped: changing workflows
   * clears these, because a step name that happens to repeat in another
   * workflow is a different step. */
  stepOverrides: DraftConsumerOverrides;
  /** Keyed by engine-reserved consumer name (today: `judge`). Also
   * workflow-scoped — its declared role comes from the workflow descriptor. */
  /** Accepted result revisions to attach (ADR-0035). Project-scoped, not
   * workflow-scoped: a bundle belongs to a project, and changing which
   * workflow runs does not change which files were reviewed. Kept even when a
   * selection has become incompatible, so it is visible with its reason
   * instead of silently disappearing. */
  attachments: DraftAttachment[];
  /** The operator acknowledging an unvalidated base. Cleared whenever the
   * selection changes, because an acknowledgement is about a specific set of
   * files and a specific target. */
  acknowledgeBaseDifference: boolean;
}

/** Drop rows that override nothing, so an opened-and-reset selector leaves
 * no trace the daemon would fingerprint differently from never having
 * touched it. */
export function pruneConsumerOverrides(
  overrides: DraftConsumerOverrides,
): DraftConsumerOverrides {
  return Object.fromEntries(
    Object.entries(overrides).filter(
      ([, entry]) => entry.profile !== undefined || entry.role !== undefined,
    ),
  );
}

const STORAGE_KEY = "ompire.spawnDraft";

export const emptySpawnDraft: SpawnDraft = {
  workflow: "",
  project: "",
  profile: "",
  slug: "",
  prompt: "",
  overrides: {},
  stepOverrides: {},
  attachments: [],
  acknowledgeBaseDifference: false,
};

/** Restore the draft a Settings or Workflows round trip interrupted.
 *
 * The draft is transient frontend state, not authoritative registry data, so
 * it lives in session storage rather than in a new server-side entity: it
 * exists only so that leaving the form to create a model profile — or to save
 * a workflow — does not cost the operator everything they had typed.
 *
 * A project preselection fills a field the operator has not chosen yet; it
 * never replaces one they have. A *workflow* handoff is a different thing —
 * it is an explicit "launch this one" — so it is applied by the view through
 * the same selector a person uses, once, rather than reapplied here on every
 * mount of the same history entry.
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
        // A draft written before row overrides existed simply has none.
        stepOverrides: pruneConsumerOverrides(parsed.stepOverrides ?? {}),
        // Likewise for a draft written before attachments existed. An
        // acknowledgement is never restored without the selection it was
        // about.
        attachments: parsed.attachments ?? [],
        acknowledgeBaseDifference:
          (parsed.attachments ?? []).length > 0 &&
          (parsed.acknowledgeBaseDifference ?? false),
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
