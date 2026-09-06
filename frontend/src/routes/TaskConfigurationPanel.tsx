import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { WorkflowRevision } from "../components/WorkflowRevision";
import {
  confirmTaskConfiguration,
  continueTask,
  getTaskConfiguration,
  previewTaskConfiguration,
  type TaskConfiguration,
} from "../lib/api";
import { useDaemonState } from "../lib/useDaemonState";
import type { SessionInfo, TaskDetail, WorkshopAdditionsSource } from "../types";

/** The launch inputs one task runs under, and — for a task created before
 * those existed — the confirmation that pins what happens next.
 *
 * What is shown is the accepted document itself, not a recomputation from
 * today's project and profile settings. That distinction is the whole point:
 * a profile edited after acceptance changes the next launch, not this run
 * (ADR-0026).
 */
export function TaskConfigurationPanel({
  detail,
  sessions,
}: {
  detail: TaskDetail;
  sessions: Record<string, SessionInfo>;
}) {
  const inputs = detail.execution_inputs;
  // Two different gaps reach the same form (ADR-0026, ADR-0028): a task that
  // was never configured, and one that was — but before definitions were
  // retained, so the procedure it ran was never recorded.
  if (inputs === null || inputs.workflow_binding === null) {
    return <LegacyConfiguration detail={detail} />;
  }
  const overridden = new Set(inputs.workspace_overrides);
  return (
    <div className="panel" data-testid="task-inputs">
      <h2 className="panelTitle">Accepted configuration</h2>
      <p className="hint">
        Decided when this task was accepted
        {inputs.provenance === "legacy-confirmed" ? " (confirmed after upgrade)" : ""}, and
        unchanged since. Editing the project or the profile affects the next launch, not
        this one.
      </p>
      <dl className="metaList">
        <dt>workflow</dt>
        <dd data-testid="accepted-workflow">
          {inputs.workflow_name}
          {inputs.workflow_binding.source === "legacy-confirmed" && (
            <span className="noForkNote"> · confirmed after upgrade</span>
          )}
        </dd>
        <dt>revision</dt>
        <dd>
          <WorkflowRevision
            revision={inputs.workflow_binding.revision}
            legacyThroughSeq={inputs.workflow_binding.legacy_through_seq}
            interruptedLegacySeq={inputs.workflow_binding.interrupted_legacy_seq}
          />
        </dd>
        <dt>profile</dt>
        <dd data-testid="accepted-profile">
          {inputs.model_profile_name ?? "—"}
          <span className="noForkNote">
            {" "}
            ·{" "}
            {inputs.model_profile_source === "task"
              ? "selected for this task"
              : inputs.model_profile_source === "project"
                ? "inherited from the project"
                : "confirmed after upgrade"}
          </span>
        </dd>
        <dt>base branch</dt>
        <dd>
          {inputs.workspace.base_branch}
          {overridden.has("base_branch") && (
            <span className="noForkNote"> · overridden for this task</span>
          )}
        </dd>
        <dt>workshop additions</dt>
        <dd>
          {inputs.workspace.workshop_additions}
          {overridden.has("workshop_additions") && (
            <span className="noForkNote"> · overridden for this task</span>
          )}
        </dd>
        <dt>preamble</dt>
        <dd>
          {inputs.workspace.preamble === "" ? (
            <em>none</em>
          ) : (
            <pre className="preambleBlock">{inputs.workspace.preamble}</pre>
          )}
          {overridden.has("preamble") && (
            <span className="noForkNote"> · overridden for this task</span>
          )}
        </dd>
      </dl>

      <table className="stepTable" data-testid="accepted-roles">
        <thead>
          <tr>
            <th>Consumer</th>
            <th>Profile</th>
            <th>Role</th>
            <th>Model</th>
            <th>Thinking (accepted)</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(inputs.step_bindings).map(([name, binding]) => (
            <tr key={`step-${name}`} data-testid={`consumer-${name}`}>
              <td className="mono">{name}</td>
              <td>
                {binding.profile_name}
                <span className="noForkNote">
                  {" "}
                  ·{" "}
                  {binding.profile_source === "step"
                    ? "overridden for this step"
                    : `inherited from the ${binding.profile_source}`}
                </span>
              </td>
              <td>
                {binding.role}
                <span className="noForkNote">
                  {" "}
                  ·{" "}
                  {binding.role_source === "step"
                    ? "overridden for this step"
                    : "declared by the workflow"}
                </span>
              </td>
              <td className="mono">{binding.roles[binding.role].model}</td>
              <td>
                {binding.roles[binding.role].thinking}
                {/* The whole native map, because a `/switch slow` inside the
                    container runs one of these — the active pair alone does
                    not describe what this process can reach. */}
                <details data-testid={`consumer-policy-${name}`}>
                  <summary>native roles</summary>
                  <ul className="nativeRoles">
                    {(["default", "smol", "slow", "plan"] as const).map((role) => (
                      <li key={role}>
                        <code>{role}</code>: {binding.roles[role].model} ·{" "}
                        {binding.roles[role].thinking}
                      </li>
                    ))}
                  </ul>
                </details>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="hint">
        Each model consumer keeps the policy it was accepted under, and every one of
        them is a step you can see above — the engine runs no model of its own. Editing
        or deleting a profile since then changes what the next launch resolves to, never
        this task.
      </p>

      <NativeModelState sessions={sessions} />
      <ContinueRun detail={detail} />
    </div>
  );
}

/** Resume a run that was left in place while the task was blocked.
 *
 * Deliberately separate from confirming a configuration: confirmation records
 * what the task continues under and starts nothing (ADR-0026, ADR-0028). It
 * has to remain reachable *after* confirmation too — a task the daemon skipped
 * during recovery is still parked exactly where it stopped, and nothing else
 * re-arms it.
 *
 * The daemon applies the eligibility rule: only a run that was already
 * `running` or `waiting` is continued, and it uses the same per-task recovery
 * routine startup does, so pressing this twice is a no-op rather than a
 * second run.
 */
function ContinueRun({ detail }: { detail: TaskDetail }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (detail.workflow_status !== "running" && detail.workflow_status !== "waiting") {
    return null;
  }

  async function onContinue() {
    setBusy(true);
    setError(null);
    try {
      await continueTask(detail.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        disabled={busy}
        onClick={() => void onContinue()}
        data-testid="legacy-continue"
      >
        Continue the interrupted run
      </button>
      {error && (
        <div className="submitError" role="alert" data-testid="continue-error">
          {error}
        </div>
      )}
    </>
  );
}


/** What each live session's omp child reports it is actually running. The
 * accepted policy sits beside it because omp resolves `auto` and `max` to a
 * model-specific level — that is native behavior, not a lost override. */
function NativeModelState({ sessions }: { sessions: Record<string, SessionInfo> }) {
  const observed = Object.entries(sessions).filter(([, info]) => info.model !== undefined);
  if (observed.length === 0) return null;
  return (
    <>
      <h3 className="subheading">Running now</h3>
      <table className="stepTable" data-testid="native-model-state">
        <thead>
          <tr>
            <th>Session</th>
            <th>Model</th>
            <th>Accepted</th>
            <th>Resolved by omp</th>
          </tr>
        </thead>
        <tbody>
          {observed.map(([name, info]) => (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td className="mono">{info.model!.model}</td>
              <td>{info.model!.thinking}</td>
              <td>{info.model!.resolved_thinking ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

/** A task created before pinned inputs existed. Its records are intact; what
 * is missing is stated as missing, and confirming a continuation pins future
 * behavior without claiming the past turns used those values. */
function LegacyConfiguration({ detail }: { detail: TaskDetail }) {
  const { modelProfiles } = useDaemonState();
  const [configuration, setConfiguration] = useState<TaskConfiguration | null>(null);
  const [profile, setProfile] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [additions, setAdditions] = useState<WorkshopAdditionsSource>("project");
  const [preamble, setPreamble] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [workflowAcknowledged, setWorkflowAcknowledged] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getTaskConfiguration(detail.id)
      .then((loaded) => {
        setConfiguration(loaded);
        // The current project's values are offered as candidates to confirm,
        // never applied on the operator's behalf.
        const candidates = loaded.candidates ?? {};
        setBaseBranch(String(candidates.base_branch ?? ""));
        setAdditions(
          (candidates.workshop_additions as WorkshopAdditionsSource) ?? "project",
        );
        setPreamble(String(candidates.preamble ?? ""));
        setProfile(String(candidates.default_model_profile ?? ""));
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [detail.id]);

  // A task that already has accepted inputs is only missing its definition;
  // asking it for a model again would be re-deciding something reviewed once.
  const needsLaunchFields = configuration?.needs_configuration ?? true;

  async function onConfirm() {
    setBusy(true);
    setError(null);
    try {
      // Only a task that was never configured supplies launch fields. One
      // that merely predates retained definitions is confirming a procedure,
      // not re-deciding a model it already accepted.
      const continuation = needsLaunchFields
        ? {
            model_profile: profile,
            base_branch: baseBranch,
            workshop_additions: additions,
            preamble,
          }
        : {};
      const preview = await previewTaskConfiguration(detail.id, continuation);
      await confirmTaskConfiguration(detail.id, {
        ...continuation,
        preview_token: preview.preview_token,
        acknowledge_unknown: acknowledged,
        acknowledge_workflow: workflowAcknowledged,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (configuration === null) {
    return (
      <div className="panel" data-testid="task-inputs">
        <h2 className="panelTitle">Configuration</h2>
        <p className="hint">{error ?? "Loading this task's recorded configuration…"}</p>
      </div>
    );
  }

  if (configuration.archived) {
    return (
      <div className="panel" data-testid="task-inputs">
        <h2 className="panelTitle">Configuration</h2>
        <p className="hint" data-testid="archived-legacy">
          This task predates recorded launch inputs. It is archived history and needs no
          configuration — its branch, sessions and run records are shown as they were.
        </p>
      </div>
    );
  }

  const candidate = configuration.workflow_candidate;
  const blocked = candidate !== null && !candidate.compatible;

  return (
    <div className="panel" data-testid="task-inputs">
      <h2 className="panelTitle">Configuration needed</h2>
      <p className="hint" data-testid="legacy-unknown">
        This task ran before Ompire recorded {needsLaunchFields ? "launch inputs" : "workflow definitions"}.
        Its workspace, branch, sessions, workflow history and pull-request facts are
        intact, but what it lists below was never persisted and cannot be recovered.
        Confirm what should happen <strong>from here on</strong> — that is not a claim
        about the turns already taken.
      </p>
      <ul className="unknownList">
        {(configuration.unknown_inputs ?? []).map((name) => (
          <li key={name}>{name}</li>
        ))}
      </ul>

      {candidate !== null && (
        <div data-testid="workflow-candidate">
          <h3 className="subheading">Workflow to continue under</h3>
          {!candidate.available || candidate.revision === null ? (
            <p className="fieldNote" data-testid="workflow-candidate-blocked">
              {candidate.problems.join("; ")}
            </p>
          ) : (
            <>
              <p className="fieldNote">
                The current <code>{candidate.workflow_name}</code> definition, offered as
                the one this task continues under. It is the only candidate: this task&apos;s
                history was produced by something calling itself{" "}
                <code>{candidate.workflow_name}</code>, and pointing it at anything else
                would relabel the run rather than continue it.
              </p>
              <WorkflowRevision
                revision={candidate.revision}
                legacyThroughSeq={candidate.legacy_through_seq}
                interruptedLegacySeq={candidate.interrupted_legacy_seq}
              />
              {blocked && (
                <div className="submitError" role="alert" data-testid="workflow-incompatible">
                  <p>
                    This definition cannot explain what this task already recorded, so it
                    cannot be confirmed:
                  </p>
                  <ul className="unknownList">
                    {candidate.problems.map((problem) => (
                      <li key={problem}>{problem}</li>
                    ))}
                  </ul>
                  <p>
                    The task stays readable, stoppable and cleanable exactly as it is.
                  </p>
                </div>
              )}
              <p className="fieldNote" data-testid="uncertainty-notice">
                {candidate.uncertainty_notice}
              </p>
            </>
          )}
        </div>
      )}

      {needsLaunchFields && (
      <>
      <label className="formField">
        <span className="fieldLabel">Model profile</span>
        <select
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          data-testid="legacy-profile"
        >
          <option value="">select a profile</option>
          {modelProfiles.map((candidate) => (
            <option key={candidate.name} value={candidate.name}>
              {candidate.name}
            </option>
          ))}
        </select>
        {modelProfiles.length === 0 && (
          <span className="fieldHint">
            No profiles exist yet. <Link to="/settings">Create one in Settings</Link>.
          </span>
        )}
      </label>

      <label className="formField">
        <span className="fieldLabel">Base branch</span>
        <input
          className="mono"
          value={baseBranch}
          onChange={(e) => setBaseBranch(e.target.value)}
          data-testid="legacy-base-branch"
        />
        <span className="fieldHint">
          Used for review and ship from here on. There is no fallback to <code>main</code>.
        </span>
      </label>

      <label className="formField">
        <span className="fieldLabel">Workshop additions</span>
        <select
          value={additions}
          onChange={(e) => setAdditions(e.target.value as WorkshopAdditionsSource)}
          data-testid="legacy-additions"
        >
          <option value="project">project</option>
          <option value="global">global</option>
        </select>
      </label>

      <label className="formField">
        <span className="fieldLabel">Prompt preamble</span>
        <textarea
          rows={3}
          value={preamble}
          onChange={(e) => setPreamble(e.target.value)}
          data-testid="legacy-preamble"
        />
      </label>
      </>
      )}

      {needsLaunchFields && (
        <label className="checkboxField">
          <input
            type="checkbox"
            checked={acknowledged}
            onChange={(e) => setAcknowledged(e.target.checked)}
            data-testid="legacy-acknowledge"
          />
          <span>
            I understand the original model, thinking level, preamble and overrides are
            unknown and cannot be recovered.
          </span>
        </label>
      )}

      <label className="checkboxField">
        <input
          type="checkbox"
          checked={workflowAcknowledged}
          onChange={(e) => setWorkflowAcknowledged(e.target.checked)}
          data-testid="legacy-acknowledge-workflow"
        />
        <span>
          I understand the exact workflow definition this task already ran was never
          recorded, and that confirming this one governs only what happens next.
        </span>
      </label>

      <button
        type="button"
        className="primary"
        disabled={
          busy ||
          blocked ||
          candidate === null ||
          !candidate.available ||
          !workflowAcknowledged ||
          (needsLaunchFields && (!profile || !baseBranch || !acknowledged))
        }
        onClick={onConfirm}
        data-testid="legacy-confirm"
      >
        Confirm continuation configuration
      </button>
      <ContinueRun detail={detail} />
      {error && (
        <div className="submitError" role="alert" data-testid="legacy-error">
          {error}
        </div>
      )}
    </div>
  );
}
