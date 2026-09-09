import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { WorkflowRevision } from "../components/WorkflowRevision";
import { previewTask, spawnTask } from "../lib/api";
import type {
  BaseComparison,
  ConsumerOverrideInput,
  LaunchInput,
  LaunchPreview,
  WorkspaceOverridesInput,
} from "../lib/api";
import { PromptMentions } from "./PromptMentions";
import { useDaemonState } from "../lib/useDaemonState";
import {
  WORKSPACE_FIELDS,
  loadSpawnDraft,
  pruneConsumerOverrides,
  saveSpawnDraft,
  type DraftAttachment,
  type DraftConsumerOverrides,
  type SpawnDraft,
} from "../lib/spawnDraft";
import {
  formatResultBytes,
  revisionStatus,
  shortResultId,
} from "../lib/resultPresentation";
import type {
  ConsumerBinding,
  ModelRole,
  SpawnStepName,
  SpawnStepPayload,
  Task,
  TaskResult,
} from "../types";
import "./SpawnView.css";

/** The four abstract roles, in the daemon's presentation order. */
const MODEL_ROLES: ModelRole[] = ["default", "smol", "slow", "plan"];

/** Which override map a row belongs to. `null` marks a row with no model at
 * all — a command, decision, or gate — which gets no controls rather than
 * disabled ones for a binding it will never have. */
type ConsumerNamespace = "step" | null;

/** One row of the preview, whether or not resolution succeeded. Controls are
 * rendered from the daemon's workflow catalog, so a row whose selected
 * profile has gone missing stays on screen with a way to correct it instead
 * of disappearing with the failed resolution. */
interface ConsumerRow {
  name: string;
  kind: string;
  session: string | null;
  conditional: boolean;
  declaredRole: ModelRole | null;
  namespace: ConsumerNamespace;
  binding: ConsumerBinding | null;
}

const PIPELINE_STEPS: { name: SpawnStepName; label: string; detail: (task: Task) => string }[] = [
  { name: "fetch", label: "Fetch", detail: (t) => `git fetch (project ${t.project_name})` },
  { name: "clone", label: "Clone", detail: (t) => `git clone → ${t.clone_path}` },
  { name: "branch", label: "Branch", detail: (t) => `${t.branch} off origin base` },
  {
    name: "inputs",
    label: "Inputs",
    detail: (t) =>
      `install ${attachedFileCount(t)} reviewed handoff file(s) into the clone`,
  },
  { name: "workshop", label: "Workshop", detail: () => "my-workshop: container + SDKs (can take a while)" },
  { name: "agent", label: "Agent", detail: () => "omp --mode rpc-ui: spawn + ready handshake" },
  { name: "prompt", label: "Prompt", detail: () => "deliver the stored prompt to the agent" },
];

function attachedFileCount(task: Task): number {
  return (task.execution_inputs?.result_attachments ?? []).reduce(
    (total, attachment) => total + attachment.files.length,
    0,
  );
}

/** An empty prompt skips the prompt step server-side; hide it too, and show
 * `inputs` only for a task that actually has pinned attachments — a launch
 * without them keeps exactly the pipeline, and the progress shape, it had. */
function stepsFor(task: Task) {
  const attached = (task.execution_inputs?.result_attachments ?? []).length > 0;
  return PIPELINE_STEPS.filter(
    (step) =>
      (step.name !== "prompt" || task.prompt) &&
      (step.name !== "inputs" || attached),
  );
}

type StepStatus = "pending" | "running" | "ok" | "failed";

function stepStatus(steps: SpawnStepPayload[], name: SpawnStepName): SpawnStepPayload | undefined {
  return [...steps].reverse().find((s) => s.step === name);
}

function statusOf(steps: SpawnStepPayload[], name: SpawnStepName): StepStatus {
  const last = stepStatus(steps, name);
  if (!last) return "pending";
  if (last.status === "started") return "running";
  return last.status;
}

/** The form owns one submission at a time. Every non-idle phase locks it; the
 * terminal edge is read from the daemon's task projection rather than from the
 * events that delivered it. */
type SpawnPhase =
  | { kind: "idle" }
  | { kind: "creating" }
  | { kind: "launching"; taskId: number }
  | { kind: "failed"; taskId: number };

const WORKSPACE_LABELS: Record<(typeof WORKSPACE_FIELDS)[number], string> = {
  base_branch: "Base branch",
  branch_pattern: "Branch pattern",
  workshop_additions: "Workshop additions",
  preamble: "Prompt preamble",
};

/** Result attachments: what is selected, what the target base says about it,
 * and the acknowledgement an unvalidated base requires (ADR-0035).
 *
 * Everything authoritative here comes from the daemon's preview. The list of
 * offers is a convenience for choosing; the *consequences* — destinations,
 * conflicts, base comparison, and whether an acknowledgement is still needed —
 * are rendered from what the daemon resolved, never computed locally. A client
 * that decided for itself that a launch was safe would be inventing an
 * authority it does not have.
 *
 * File text is not rendered here. It is inspected in the producing task's
 * Results panel, through the existing escaped-source viewer. */
function HandoffInputs({
  projectName,
  selected,
  available,
  preview,
  acknowledged,
  onAcknowledge,
  onToggle,
  disabled,
}: {
  projectName: string;
  selected: DraftAttachment[];
  available: TaskResult[];
  preview: LaunchPreview | null;
  acknowledged: boolean;
  onAcknowledge: (value: boolean) => void;
  onToggle: (entry: DraftAttachment) => void;
  disabled: boolean;
}) {
  const chosen = new Set(selected.map((entry) => entry.result_id));
  const comparisons = new Map(
    (preview?.base_comparisons ?? []).map((entry) => [entry.result_id, entry]),
  );
  // A selection the daemon did not resolve: the project changed under it, the
  // revision was purged, or a successor made it stale. Kept visible with that
  // reason rather than dropped — a silently shrinking selection is a launch
  // the operator did not choose.
  const resolved = new Set(
    (preview?.result_attachments ?? []).map((entry) => entry.result_id),
  );
  const unresolved = selected.filter((entry) => !resolved.has(entry.result_id));

  return (
    <details className="advanced" data-testid="spawn-attachments">
      <summary>
        Handoff inputs — accepted results to attach
        {selected.length > 0 ? ` (${selected.length})` : ""}
      </summary>
      {!projectName ? (
        <p className="hint">Choose a project to see the results it can offer.</p>
      ) : available.length === 0 && selected.length === 0 ? (
        <p className="hint" data-testid="spawn-attachments-empty">
          This project has no accepted result revisions. Capture and accept one
          in a task&apos;s Results panel first — an unaccepted or unreadable
          revision cannot be attached.
        </p>
      ) : (
        <>
          <ul className="spawnAttachmentList">
            {available.map((result) => {
              const isChosen = chosen.has(result.id);
              const detail = preview?.result_attachments.find(
                (entry) => entry.result_id === result.id,
              );
              const comparison = comparisons.get(result.id);
              return (
                <li key={result.id} data-testid="spawn-attachment-option">
                  <label>
                    <input
                      type="checkbox"
                      checked={isChosen}
                      disabled={disabled || result.manifest_id === null}
                      onChange={() =>
                        onToggle({
                          producer_task_id: result.task_id,
                          result_id: result.id,
                          expected_manifest_id: result.manifest_id ?? "",
                        })
                      }
                    />{" "}
                    <code>{shortResultId(result.id)}</code> — task{" "}
                    {result.task_id} · {result.file_count} file
                    {result.file_count === 1 ? "" : "s"} ·{" "}
                    {formatResultBytes(result.total_bytes)} ·{" "}
                    {revisionStatus(result)}
                  </label>
                  {isChosen && detail && (
                    <div className="spawnAttachmentDetail">
                      <p className="spawnHandoffLabel" data-testid="spawn-attachment-policy">
                        Handoff input — not publishable
                      </p>
                      <ul className="spawnAttachmentPaths">
                        {detail.destinations.map((path) => (
                          <li key={path}>
                            <code>{path}</code>
                          </li>
                        ))}
                      </ul>
                      {comparison && <BaseComparisonNote comparison={comparison} />}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
          {unresolved.length > 0 && (
            <div className="spawnAttachmentProblem" data-testid="spawn-attachment-unresolved">
              {unresolved.length} selected revision
              {unresolved.length === 1 ? "" : "s"} could not be resolved for this
              launch. The refusal above says why. Remove the selection, or choose
              another accepted bundle — nothing is dropped for you.
              <ul>
                {unresolved.map((entry) => (
                  <li key={entry.result_id}>
                    <code>{shortResultId(entry.result_id)}</code>{" "}
                    <button
                      type="button"
                      className="linkButton"
                      disabled={disabled}
                      onClick={() => onToggle(entry)}
                    >
                      remove
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {preview?.source_commit && (
            <p className="hint" data-testid="spawn-attachment-target">
              Target base: <code>{preview.source_commit.slice(0, 12)}</code> —
              the exact commit this task will be built from.
            </p>
          )}
          {selected.length > 0 && (
            <label className="spawnAcknowledge" data-testid="spawn-acknowledge">
              <input
                type="checkbox"
                checked={acknowledged}
                disabled={disabled}
                onChange={(e) => onAcknowledge(e.target.checked)}
              />{" "}
              I understand these files were not validated against this target
              base.
              {preview && !preview.needs_base_acknowledgement && (
                <span className="hint">
                  {" "}
                  Not required for this selection — every attachment was captured
                  against exactly this commit. That is not evidence the plan is
                  correct.
                </span>
              )}
            </label>
          )}
        </>
      )}
    </details>
  );
}

/** What the daemon observed about one attachment's base, in its own words. */
function BaseComparisonNote({ comparison }: { comparison: BaseComparison }) {
  if (comparison.state === "match") {
    return (
      <p className="hint" data-testid="spawn-base-match">
        Captured against this exact base. Matching identities do not prove the
        plan is right for it.
      </p>
    );
  }
  if (comparison.state === "unknown") {
    return (
      <p className="spawnAttachmentProblem" data-testid="spawn-base-unknown">
        {comparison.detail ??
          "The producing capture recorded no base observation."}{" "}
        This plan has not been validated against this target.
      </p>
    );
  }
  return (
    <div className="spawnAttachmentProblem" data-testid="spawn-base-different">
      <p>
        Captured against{" "}
        <code>{(comparison.producer_observation ?? "").slice(0, 12)}</code>, not
        this target. The plan has not been validated against this base.
      </p>
      {comparison.detail && <p className="hint">{comparison.detail}</p>}
      {comparison.changed_paths.length > 0 && (
        <>
          <p className="hint">
            Changed since then{comparison.truncated ? " (list truncated)" : ""}:
          </p>
          <ul className="spawnAttachmentPaths">
            {comparison.changed_paths.map((path) => (
              <li key={path}>
                <code>{path}</code>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function SpawnView() {
  const {
    snapshotReady,
    projects,
    modelProfiles,
    workflowCatalog,
    workflowLibrary,
    tasks,
    taskResults,
    spawnProgress,
  } = useDaemonState();
  const navigate = useNavigate();
  const location = useLocation();

  // The draft outlives a route unmount so a trip to Settings to create a
  // profile comes back to everything the operator typed. It is transient
  // frontend state, not authoritative registry data, so it lives here rather
  // than in a new server-side entity.
  const [draft, setDraft] = useState<SpawnDraft>(() =>
    loadSpawnDraft((location.state as { project?: string } | null)?.project),
  );
  const [phase, setPhase] = useState<SpawnPhase>({ kind: "idle" });
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [preview, setPreview] = useState<LaunchPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [staleReview, setStaleReview] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // Set when a workflow change — or a new executable revision of the selected
  // one — discarded row overrides, so the operator is told rather than
  // silently losing selections they made.
  const [clearedRows, setClearedRows] = useState<
    { rows: string[]; reason: "workflow" | "revision" } | null
  >(null);
  const submitLockRef = useRef(false);
  const seenRef = useRef(false);
  // Monotonic request id: a slow preview response must never replace a newer
  // draft's resolution.
  const previewGenerationRef = useRef(0);

  useEffect(() => saveSpawnDraft(draft), [draft]);

  const project = projects.find((p) => p.name === draft.project) ?? null;

  const update = useCallback((patch: Partial<SpawnDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
  }, []);

  /** Switching workflows discards every row override.
   *
   * A step name that repeats across workflows is a different step, and
   * carrying a choice over by position or by a coincidentally matching name
   * would silently apply a model policy to work the operator never looked
   * at. Slug, prompt, the task-wide profile, and the workspace overrides are
   * not workflow-scoped, so they stay. */
  const selectWorkflow = useCallback(
    (workflow: string) => {
      setDraft((current) => {
        if (workflow === current.workflow) return current;
        const cleared = Object.keys(current.stepOverrides);
        setClearedRows(
          cleared.length > 0 ? { rows: cleared.sort(), reason: "workflow" } : null,
        );
        return { ...current, workflow, stepOverrides: {} };
      });
    },
    [],
  );

  /** Change one dimension of one row. `undefined` resets that dimension
   * alone and leaves the other exactly as it was. */
  const setRowOverride = useCallback(
    (name: string, patch: { profile?: string | undefined; role?: ModelRole | undefined }) => {
      setDraft((current) => {
        const key = "stepOverrides" as const;
        const next: DraftConsumerOverrides = {
          ...current[key],
          [name]: { ...(current[key][name] ?? {}), ...patch },
        };
        // `{...entry, profile: undefined}` keeps the key, which would still
        // read as "chosen". Delete it so a reset is genuinely a reset.
        for (const dimension of ["profile", "role"] as const) {
          if (dimension in patch && patch[dimension] === undefined) {
            delete next[name][dimension];
          }
        }
        return { ...current, [key]: pruneConsumerOverrides(next) };
      });
    },
    [],
  );

  /** Apply a "Launch in Spawn" handoff, once.
   *
   * Through the same selector the form uses, so switching away from a chosen
   * workflow clears its per-step overrides and says so. The router state is
   * then cleared: it lives on the history entry, so leaving and coming Back
   * would otherwise re-apply it and silently discard whatever the operator
   * had chosen since. */
  useEffect(() => {
    const handoff = (location.state as { workflow?: string } | null)?.workflow;
    if (!handoff) return;
    selectWorkflow(handoff);
    navigate(location.pathname, { replace: true, state: null });
  }, [location.state, location.pathname, navigate, selectWorkflow]);

  /** "Start task from this result" hands over one revision.
   *
   * Applied once, like the workflow handoff: it is an explicit "attach this
   * one", not a preference to reapply on every mount. The project comes with
   * it, because a result can only be attached inside its own project. */
  useEffect(() => {
    const handoff = (location.state as { attachment?: DraftAttachment } | null)
      ?.attachment;
    const project = (location.state as { project?: string } | null)?.project;
    if (!handoff) return;
    setDraft((current) => {
      const already = current.attachments.some(
        (entry) => entry.result_id === handoff.result_id,
      );
      return {
        ...current,
        project: project ?? current.project,
        attachments: already ? current.attachments : [...current.attachments, handoff],
        acknowledgeBaseDifference: false,
      };
    });
    navigate(location.pathname, { replace: true, state: null });
  }, [location.state, location.pathname, navigate]);

  /** Add or remove one attachment.
   *
   * Every change clears the acknowledgement: it was about a specific set of
   * files against a specific target, and carrying it to a different selection
   * would be an approval nobody gave. */
  const toggleAttachment = useCallback((entry: DraftAttachment) => {
    setDraft((current) => {
      const present = current.attachments.some(
        (item) => item.result_id === entry.result_id,
      );
      return {
        ...current,
        attachments: present
          ? current.attachments.filter((item) => item.result_id !== entry.result_id)
          : [...current.attachments, entry],
        acknowledgeBaseDifference: false,
      };
    });
  }, []);

  const launchInput: LaunchInput | null = useMemo(() => {
    if (!draft.project || !draft.workflow || !draft.slug) return null;
    const overrides: WorkspaceOverridesInput = {};
    for (const field of WORKSPACE_FIELDS) {
      const value = draft.overrides[field];
      if (value !== undefined) {
        // An explicitly empty preamble is an override to "no preamble", so it
        // is sent as an empty string rather than dropped.
        overrides[field] = value as never;
      }
    }
    const consumers = (source: DraftConsumerOverrides) => {
      const out: Record<string, ConsumerOverrideInput> = {};
      for (const [name, entry] of Object.entries(source)) {
        const value: ConsumerOverrideInput = {};
        if (entry.profile !== undefined) value.model_profile = entry.profile;
        if (entry.role !== undefined) value.role = entry.role;
        if (Object.keys(value).length > 0) out[name] = value;
      }
      return out;
    };
    const stepOverrides = consumers(draft.stepOverrides);
    return {
      project_name: draft.project,
      workflow_name: draft.workflow,
      slug: draft.slug,
      prompt: draft.prompt,
      ...(draft.profile ? { model_profile: draft.profile } : {}),
      ...(Object.keys(overrides).length > 0 ? { workspace_overrides: overrides } : {}),
      ...(Object.keys(stepOverrides).length > 0 ? { step_overrides: stepOverrides } : {}),
      // Omitted entirely when nothing is attached, so an ordinary launch sends
      // exactly the request it always did.
      ...(draft.attachments.length > 0
        ? {
            result_attachments: draft.attachments,
            acknowledge_result_base_difference: draft.acknowledgeBaseDifference,
          }
        : {}),
    };
  }, [draft]);

  /** Every accepted, still-readable revision this project can offer.
   *
   * Same-project only, decided through the *task* each revision belongs to —
   * the daemon decides membership the same way, and a manifest's own project
   * label is provenance rather than authority. An unaccepted, damaged, or
   * purged revision is simply not on offer; the daemon refuses it too, so the
   * form never presents a choice that acceptance would reject. */
  const availableResults = useMemo(() => {
    if (!draft.project) return [];
    const owned = new Set(
      tasks.filter((task) => task.project_name === draft.project).map((task) => task.id),
    );
    const offered: TaskResult[] = [];
    for (const projection of Object.values(taskResults)) {
      for (const result of projection.results) {
        if (!owned.has(result.task_id)) continue;
        if (!result.available || result.accepted_at === null) continue;
        if (result.manifest_id === null) continue;
        offered.push(result);
      }
    }
    return offered.sort((a, b) => b.captured_at.localeCompare(a.captured_at));
  }, [draft.project, tasks, taskResults]);

  const workflow = workflowCatalog.find((w) => w.name === draft.workflow) ?? null;

  /** The library entry behind the selection, whether or not it can launch.
   *
   * Kept separate from the catalog lookup so a selection that has just been
   * archived, or whose saved revision has become unreadable, stays visible
   * with its reason instead of silently resolving to nothing — or, worse, to
   * another workflow. */
  const selectedEntry =
    draft.workflow === ""
      ? null
      : (workflowLibrary.find((entry) => entry.name === draft.workflow) ?? null);
  const workflowUnavailable =
    draft.workflow !== "" && snapshotReady && workflow === null
      ? (selectedEntry?.unavailable_detail ??
        `The workflow ${draft.workflow} is no longer in the library.`)
      : null;

  /** The exact revision the selected workflow currently resolves to.
   *
   * A saved executable revision changes what this launch would run, so a
   * reviewed preview is no longer about the same procedure and has to be
   * re-resolved. Editing only the entry's draft does not move this value, so
   * it deliberately does not refetch — and does not reset the row overrides,
   * which are still about these same steps. */
  const selectedRevision = workflow?.revision ?? null;
  const lastRevisionRef = useRef<string | null>(selectedRevision);

  useEffect(() => {
    if (lastRevisionRef.current === selectedRevision) return;
    const previous = lastRevisionRef.current;
    lastRevisionRef.current = selectedRevision;
    // Only a *change* to an already-selected workflow's revision clears the
    // rows; first selection and switching workflows are handled where they
    // happen. Re-resolution is not done here — `selectedRevision` is a
    // dependency of the preview effect, so the new resolution is fetched
    // whether or not there was anything to clear.
    if (previous === null || selectedRevision === null) return;
    setDraft((current) => {
      const cleared = Object.keys(current.stepOverrides);
      if (cleared.length === 0) return current;
      // Re-attaching a per-step model choice by name would apply it to a step
      // the operator never looked at, so the choices are dropped and said so.
      setClearedRows({ rows: cleared.sort(), reason: "revision" });
      return { ...current, stepOverrides: {} };
    });
  }, [selectedRevision]);

  /** The rows to render. Preview rows when a resolution exists; otherwise the
   * declared catalog, so an invalid selection can still be corrected on the
   * row that carries it. */
  const rows: ConsumerRow[] = useMemo(() => {
    if (preview !== null) {
      return preview.steps.map((step) => ({
        name: step.step,
        kind: step.kind,
        session: step.session,
        conditional: step.conditional,
        declaredRole: step.declared_role,
        namespace: step.kind === "agent" ? "step" : null,
        binding: step.binding,
      }));
    }
    if (workflow === null) return [];
    return workflow.steps.map((step) => ({
      name: step.name,
      kind: step.kind,
      session: step.session,
      conditional: step.conditional,
      declaredRole: step.role,
      namespace: (step.kind === "agent" ? "step" : null) as ConsumerNamespace,
      binding: null,
    }));
  }, [preview, workflow]);

  /** A profile a row still names but the registry no longer offers: kept as a
   * selectable option so the row shows what is wrong instead of silently
   * snapping back to inheritance. Deleting a profile never re-picks one. */
  const profileNames = useMemo(
    () => new Set(modelProfiles.map((p) => p.name)),
    [modelProfiles],
  );

  /** A signature of the profile registry.
   *
   * A profile edited or deleted while the form is open changes what these
   * selections resolve to, and a row still showing the old model would be a
   * lie — most visibly for a profile that no longer exists at all. Keyed on
   * the values rather than the array identity so an unrelated snapshot does
   * not re-resolve a draft that cannot have changed. */
  const profileRevision = useMemo(
    () => JSON.stringify(modelProfiles.map((p) => [p.name, p.roles])),
    [modelProfiles],
  );

  // Every change to an effective choice re-resolves. Stale responses are
  // dropped by generation, so the rows on screen always describe the draft
  // as it stands.
  useEffect(() => {
    if (launchInput === null) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const generation = ++previewGenerationRef.current;
    let cancelled = false;
    previewTask(launchInput)
      .then((resolved) => {
        if (cancelled || generation !== previewGenerationRef.current) return;
        setPreview(resolved);
        setPreviewError(null);
        setStaleReview(false);
      })
      .catch((error: unknown) => {
        if (cancelled || generation !== previewGenerationRef.current) return;
        setPreview(null);
        setPreviewError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
    // `profileRevision` and `selectedRevision` are dependencies, not inputs:
    // they re-resolve when the profile registry or the selected workflow's
    // saved definition moves under an open draft. A draft-only library edit
    // moves neither, so it refetches nothing.
  }, [launchInput, profileRevision, selectedRevision]);

  const locked = phase.kind !== "idle";
  const spawnedId = phase.kind === "launching" || phase.kind === "failed" ? phase.taskId : null;
  const spawnedTask = spawnedId === null ? null : (tasks.find((t) => t.id === spawnedId) ?? null);
  const steps = spawnedId === null ? [] : (spawnProgress[spawnedId] ?? []);

  useEffect(() => {
    if (phase.kind !== "launching") return;
    const task = tasks.find((candidate) => candidate.id === phase.taskId);
    if (task === undefined) {
      if (!snapshotReady || !seenRef.current) return;
      submitLockRef.current = false;
      seenRef.current = false;
      setPhase({ kind: "idle" });
      setSubmitError(`Task ${phase.taskId} is no longer present — it was deleted or purged.`);
      return;
    }
    seenRef.current = true;
    if (task.spawn_completed_at === null) return;
    if (task.state === "failed") {
      setPhase({ kind: "failed", taskId: phase.taskId });
      return;
    }
    navigate(`/tasks/${phase.taskId}`, { replace: true });
  }, [phase, tasks, snapshotReady, navigate]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (launchInput === null || preview === null || submitLockRef.current) return;
    submitLockRef.current = true;
    seenRef.current = false;
    setSubmitError(null);
    setPhase({ kind: "creating" });
    try {
      const task = await spawnTask({ ...launchInput, preview_token: preview.preview_token });
      saveSpawnDraft(null);
      setPhase({ kind: "launching", taskId: task.id });
    } catch (error) {
      // Nothing was created, so the form is immediately usable again with
      // everything the operator typed still in place. A refused submission
      // never retries under different settings — a changed resolution is a
      // fresh review.
      submitLockRef.current = false;
      setPhase({ kind: "idle" });
      const message = error instanceof Error ? error.message : String(error);
      setSubmitError(message);
      if (message.includes("changed since it was previewed")) {
        setStaleReview(true);
        previewGenerationRef.current += 1;
        previewTask(launchInput)
          .then((resolved) => setPreview(resolved))
          .catch(() => undefined);
      }
    }
  }

  function startAnother() {
    submitLockRef.current = false;
    seenRef.current = false;
    setPhase({ kind: "idle" });
    setSubmitError(null);
  }

  const noProfiles = snapshotReady && modelProfiles.length === 0;
  const noWorkflows = snapshotReady && workflowCatalog.length === 0;
  const projectBlocked =
    project !== null &&
    (project.setup_state !== "ready" || project.launch_config_state !== "reconciled");

  return (
    <>
      <div className="headerRow">
        <h1>Spawn task</h1>
        <span className="subline">workflow + project + model profile → clone, container, agent</span>
      </div>

      <div className="spawnGrid">
        <form className="panel" onSubmit={onSubmit} data-testid="spawn-form">
          <h2 className="panelTitle">New task</h2>

          <div className="field">
            <label htmlFor="spawn-workflow">Workflow</label>
            <select
              id="spawn-workflow"
              value={draft.workflow}
              onChange={(e) => selectWorkflow(e.target.value)}
              disabled={locked}
              data-testid="spawn-workflow"
            >
              <option value="">select a workflow</option>
              {workflowUnavailable !== null && (
                <option value={draft.workflow}>{draft.workflow} — unavailable</option>
              )}
              {workflowCatalog.map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.name} — {candidate.steps.length} step
                  {candidate.steps.length === 1 ? "" : "s"},{" "}
                  {candidate.sessions.length} session
                  {candidate.sessions.length === 1 ? "" : "s"}
                </option>
              ))}
            </select>
            <div className="hint">
              Every launchable workflow is available to every ready project — no template
              setup. <Link to="/workflows">Workflows</Link> is where you create, edit, and
              archive them.
            </div>
            {workflowUnavailable !== null && (
              <p className="submitError" data-testid="spawn-workflow-unavailable">
                {workflowUnavailable} Pick another workflow, or fix this one in{" "}
                <Link to={`/workflows/${encodeURIComponent(draft.workflow)}`}>
                  the library
                </Link>
                . Everything else you have entered is kept.
              </p>
            )}
            {noWorkflows && (
              <p className="hint" data-testid="spawn-no-workflows">
                No workflow can be launched right now. Save an executable revision in{" "}
                <Link to="/workflows">Workflows</Link> first.
              </p>
            )}
          </div>

          <div className="field">
            <label htmlFor="spawn-project">Project</label>
            <select
              id="spawn-project"
              value={draft.project}
              onChange={(e) => update({ project: e.target.value })}
              disabled={locked}
              data-testid="spawn-project"
            >
              <option value="">select a project</option>
              {projects.map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.name} — {candidate.checkout_path}
                  {candidate.setup_state !== "ready" ? ` · setup ${candidate.setup_state}` : ""}
                  {candidate.launch_config_state !== "reconciled"
                    ? " · needs reconciliation"
                    : ""}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="spawn-profile">Model profile</label>
            <select
              id="spawn-profile"
              value={draft.profile}
              onChange={(e) => update({ profile: e.target.value })}
              disabled={locked}
              data-testid="spawn-profile"
            >
              <option value="">
                {project?.default_model_profile
                  ? `inherit from project — ${project.default_model_profile}`
                  : "inherit from project — none set"}
              </option>
              {modelProfiles.map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.name} — {candidate.roles.default.model} ·{" "}
                  {candidate.roles.default.thinking}
                </option>
              ))}
            </select>
            {draft.profile !== "" && (
              <button
                className="linkButton"
                type="button"
                onClick={() => update({ profile: "" })}
                disabled={locked}
                data-testid="reset-profile"
              >
                Reset to project default
              </button>
            )}
            {noProfiles && (
              <div className="hint" data-testid="no-profiles">
                No model profiles exist yet, and nothing is inferred from a project or a
                credential. <Link to="/settings">Create one in Settings</Link> — this draft is
                kept for when you come back.
              </div>
            )}
          </div>

          <div className="field">
            <label htmlFor="spawn-slug">Task slug</label>
            <input
              id="spawn-slug"
              className="mono"
              type="text"
              value={draft.slug}
              onChange={(e) => update({ slug: e.target.value })}
              disabled={locked}
              placeholder="fix-the-bug"
            />
            {preview && (
              <>
                <div>
                  <span className="branchPreview" data-testid="branch-preview">
                    branch: {preview.branch} · off origin/{preview.workspace.base_branch}
                  </span>
                </div>
                <div className="hint">
                  clone → <code>~/tasks/{preview.project_name}/{draft.slug}</code>
                </div>
              </>
            )}
          </div>

          <div className="field">
            <label htmlFor="spawn-prompt">Prompt</label>
            <PromptMentions
              id="spawn-prompt"
              rows={9}
              value={draft.prompt}
              onChange={(value) => update({ prompt: value })}
              projectName={draft.project || null}
              disabled={locked}
              placeholder="What should the agent do? (delivered once the agent is ready)"
            />
            <div className="hint">
              Type <code>@</code> to attach a file from the project&apos;s repository.
            </div>
          </div>

          <HandoffInputs
            projectName={draft.project}
            selected={draft.attachments}
            available={availableResults}
            preview={preview}
            acknowledged={draft.acknowledgeBaseDifference}
            onAcknowledge={(value) => update({ acknowledgeBaseDifference: value })}
            onToggle={toggleAttachment}
            disabled={locked}
          />

          <details
            className="advanced"
            open={advancedOpen}
            onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
            data-testid="advanced"
          >
            <summary>Advanced — workspace overrides for this task</summary>
            {WORKSPACE_FIELDS.map((field) => {
              const overridden = draft.overrides[field] !== undefined;
              const inherited = preview?.inherited_workspace[field] ?? "";
              return (
                <div className="field" key={field}>
                  <label htmlFor={`spawn-${field}`}>{WORKSPACE_LABELS[field]}</label>
                  {field === "workshop_additions" ? (
                    <select
                      id={`spawn-${field}`}
                      value={overridden ? String(draft.overrides[field]) : ""}
                      onChange={(e) =>
                        update({
                          overrides: {
                            ...draft.overrides,
                            workshop_additions: e.target.value
                              ? (e.target.value as "project" | "global")
                              : undefined,
                          },
                        })
                      }
                      disabled={locked}
                    >
                      <option value="">inherit — {String(inherited)}</option>
                      <option value="project">project — this repository&apos;s additions</option>
                      <option value="global">global — your own additions file</option>
                    </select>
                  ) : field === "preamble" ? (
                    <textarea
                      id={`spawn-${field}`}
                      rows={3}
                      value={overridden ? String(draft.overrides[field]) : String(inherited)}
                      onChange={(e) =>
                        update({
                          overrides: { ...draft.overrides, preamble: e.target.value },
                        })
                      }
                      disabled={locked}
                    />
                  ) : (
                    <input
                      id={`spawn-${field}`}
                      className="mono"
                      type="text"
                      value={overridden ? String(draft.overrides[field]) : String(inherited)}
                      onChange={(e) =>
                        update({
                          overrides: { ...draft.overrides, [field]: e.target.value },
                        })
                      }
                      disabled={locked}
                    />
                  )}
                  {overridden ? (
                    <button
                      className="linkButton"
                      type="button"
                      disabled={locked}
                      data-testid={`reset-${field}`}
                      onClick={() =>
                        update({
                          overrides: { ...draft.overrides, [field]: undefined },
                        })
                      }
                    >
                      Reset to project default
                    </button>
                  ) : (
                    <div className="hint">inherited from {draft.project || "the project"}</div>
                  )}
                </div>
              );
            })}
          </details>

          <button
            className="primary"
            type="submit"
            disabled={locked || preview === null || projectBlocked}
          >
            {phase.kind === "creating"
              ? "Creating…"
              : phase.kind === "launching"
                ? "Launching…"
                : "Spawn task"}
          </button>
          {project && project.setup_state !== "ready" && (
            <div className="submitError" role="alert" data-testid="project-not-ready">
              {project.name}&apos;s checkout is {project.setup_state} — there is nothing to
              clone a workspace from yet. <Link to="/projects">Open Projects</Link>
            </div>
          )}
          {project && project.launch_config_state !== "reconciled" && (
            <div className="submitError" role="alert" data-testid="project-unreconciled">
              {project.name} still needs launch-configuration reconciliation after the
              template upgrade. <Link to="/projects">Resolve it in Projects</Link>
            </div>
          )}
          {previewError && (
            <div className="submitError" role="alert" data-testid="preview-error">
              {previewError}
            </div>
          )}
          {submitError && (
            <div className="submitError" role="alert">
              {submitError}
            </div>
          )}
        </form>

        <div className="panel" data-testid="spawn-progress">
          <h2 className="panelTitle">
            {spawnedTask
              ? `Launching · ${spawnedTask.project_name}/${spawnedTask.slug}`
              : "Before launch"}
          </h2>

          {spawnedTask === null && (
            <>
              {staleReview && (
                <div className="submitError" role="alert" data-testid="stale-review">
                  The configuration changed since you reviewed it. These are the current
                  choices — submit again to launch with them.
                </div>
              )}
              {clearedRows !== null && (
                <div className="submitError" role="status" data-testid="cleared-overrides">
                  {clearedRows.reason === "workflow"
                    ? "Changing workflow cleared the per-step choices you had made ("
                    : "This workflow was saved with a new revision, which cleared the per-step choices you had made ("}
                  {clearedRows.rows.join(", ")}). A step of that name in the changed
                  definition is a different step, so nothing was carried over.
                </div>
              )}
              {rows.length === 0 ? (
                <p className="hint">
                  Choose a workflow, a project and a slug to see every step this run can
                  execute and the model each one would use.
                </p>
              ) : (
                <>
                  {preview !== null && (
                    <div className="hint" data-testid="profile-source">
                      Profile <code>{preview.model_profile}</code>{" "}
                      {preview.model_profile_source === "task"
                        ? "— selected for this task"
                        : "— inherited from the project"}
                    </div>
                  )}
                  {preview !== null && (
                    // The exact procedure being accepted, not just its name.
                    // It is part of what the preview token covers, so an
                    // edited prompt or route invalidates this review even
                    // though the step list looks unchanged (ADR-0028).
                    <div className="hint" data-testid="preview-revision">
                      Revision{" "}
                      <WorkflowRevision
                        revision={preview.workflow_revision}
                        summaryLabel="read the whole procedure this launch would accept"
                      />
                    </div>
                  )}
                  <table className="stepTable" data-testid="step-preview">
                    <thead>
                      <tr>
                        <th>Step</th>
                        <th>Kind</th>
                        <th>Session</th>
                        <th>Profile</th>
                        <th>Role</th>
                        <th>Model</th>
                        <th>Thinking</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => {
                        const chosen =
                          row.namespace === "step" ? draft.stepOverrides[row.name] : undefined;
                        const binding = row.binding;
                        const missingProfile =
                          chosen?.profile !== undefined && !profileNames.has(chosen.profile);
                        return (
                          <tr
                            key={`${row.kind}-${row.name}`}
                            data-testid={`step-${row.name}`}
                          >
                            <td className="mono">
                              {row.name}
                              {row.conditional && (
                                <span
                                  className="conditional"
                                  title="a decision may route past this step"
                                >
                                  {" "}
                                  conditional
                                </span>
                              )}
                            </td>
                            <td>{row.kind}</td>
                            <td className="mono">{row.session ?? "—"}</td>
                            <td>
                              {/* A command, decision, or gate has no binding
                                  and therefore no controls: an override box
                                  on a step that never reaches a provider
                                  would be a fiction. */}
                              {row.namespace === null ? (
                                "—"
                              ) : (
                                <>
                                  <select
                                    aria-label={`Model profile for ${row.name}`}
                                    value={chosen?.profile ?? ""}
                                    disabled={locked}
                                    data-testid={`row-profile-${row.name}`}
                                    onChange={(e) =>
                                      setRowOverride(row.name, {
                                        profile: e.target.value || undefined,
                                      })
                                    }
                                  >
                                    <option value="">
                                      inherit — {preview?.model_profile ?? "task profile"}
                                    </option>
                                    {missingProfile && (
                                      <option value={chosen!.profile}>
                                        {chosen!.profile} — unavailable
                                      </option>
                                    )}
                                    {modelProfiles.map((candidate) => (
                                      <option key={candidate.name} value={candidate.name}>
                                        {candidate.name}
                                      </option>
                                    ))}
                                  </select>
                                  {chosen?.profile !== undefined && (
                                    <button
                                      className="linkButton"
                                      type="button"
                                      disabled={locked}
                                      data-testid={`reset-row-profile-${row.name}`}
                                      onClick={() =>
                                        setRowOverride(
                                          row.name,
                                          { profile: undefined },
                                        )
                                      }
                                    >
                                      Reset profile
                                    </button>
                                  )}
                                  <div className="hint">
                                    {binding
                                      ? binding.profile_source === "step"
                                        ? "overridden for this step"
                                        : `inherited from the ${binding.profile_source}`
                                      : "—"}
                                  </div>
                                </>
                              )}
                            </td>
                            <td>
                              {row.namespace === null ? (
                                "—"
                              ) : (
                                <>
                                  <select
                                    aria-label={`Model role for ${row.name}`}
                                    value={chosen?.role ?? ""}
                                    disabled={locked}
                                    data-testid={`row-role-${row.name}`}
                                    onChange={(e) =>
                                      setRowOverride(row.name, {
                                        role: (e.target.value || undefined) as
                                          | ModelRole
                                          | undefined,
                                      })
                                    }
                                  >
                                    <option value="">
                                      inherit — {row.declaredRole ?? "declared"}
                                    </option>
                                    {MODEL_ROLES.map((role) => (
                                      <option key={role} value={role}>
                                        {role}
                                      </option>
                                    ))}
                                  </select>
                                  {chosen?.role !== undefined && (
                                    <button
                                      className="linkButton"
                                      type="button"
                                      disabled={locked}
                                      data-testid={`reset-row-role-${row.name}`}
                                      onClick={() =>
                                        setRowOverride(
                                          row.name,
                                          { role: undefined },
                                        )
                                      }
                                    >
                                      Reset role
                                    </button>
                                  )}
                                  <div className="hint">
                                    {binding
                                      ? binding.role_source === "step"
                                        ? "overridden for this step"
                                        : "declared by the workflow"
                                      : "—"}
                                  </div>
                                </>
                              )}
                            </td>
                            <td className="mono">{binding?.roles[binding.role].model ?? "—"}</td>
                            <td>
                              {binding?.roles[binding.role].thinking ?? "—"}
                              {binding && (
                                <details data-testid={`row-policy-${row.name}`}>
                                  <summary>native roles</summary>
                                  <ul className="nativeRoles">
                                    {MODEL_ROLES.map((role) => (
                                      <li key={role}>
                                        <code>{role}</code>: {binding.roles[role].model} ·{" "}
                                        {binding.roles[role].thinking}
                                      </li>
                                    ))}
                                  </ul>
                                </details>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <p className="hint">
                    Every step this workflow declares, in order — there are no hidden
                    model consumers. A conditional step may be routed past or held back by
                    its own condition. Each row&apos;s profile and role can be set
                    independently, and every process carries the whole native <code>smol</code>/
                    <code>slow</code>/<code>plan</code> map shown under its thinking level.
                    Thinking is the policy you chose — omp may resolve <code>auto</code>{" "}
                    and <code>max</code> to a model-specific level.
                  </p>
                </>
              )}
            </>
          )}

          {spawnedTask !== null && (
            <div className="pipeline">
              {stepsFor(spawnedTask).map((stepDef, index, pipelineSteps) => {
                const status = statusOf(steps, stepDef.name);
                const failed = stepStatus(steps, stepDef.name);
                return (
                  <div className="step" key={stepDef.name} data-step-status={status}>
                    {index < pipelineSteps.length - 1 && <span className="rail" />}
                    <span className={`bullet ${status}`}>
                      {status === "ok" ? "✓" : status === "failed" ? "✕" : index + 1}
                    </span>
                    <div className="stepBody">
                      <div className={`stepLabel ${status}`}>{stepDef.label}</div>
                      <div className="stepDetail">{stepDef.detail(spawnedTask)}</div>
                      {status === "failed" && failed?.stderr && (
                        <pre className="stderr" data-testid={`stderr-${stepDef.name}`}>
                          {failed.stderr}
                        </pre>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {phase.kind === "failed" && spawnedTask && (
            <div className="failedNote" role="status" data-testid="spawn-failed">
              <div>
                Task landed as <span className="failedPill">failed</span>
                {spawnedTask.error ? ` — ${spawnedTask.error}` : ""}
              </div>
              <div className="failedActions">
                <Link
                  className="failedAction"
                  to={`/tasks/${spawnedTask.id}`}
                  data-testid="spawn-open-failed"
                >
                  Open failed task
                </Link>
                <button
                  className="failedAction"
                  type="button"
                  onClick={startAnother}
                  data-testid="spawn-start-another"
                >
                  Start another task
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
