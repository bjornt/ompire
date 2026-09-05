import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
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
  if (inputs === null) {
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
        <dd>{inputs.workflow_name}</dd>
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
            <th>Role</th>
            <th>Model</th>
            <th>Thinking (accepted)</th>
            <th>Consumers</th>
          </tr>
        </thead>
        <tbody>
          {(["default", "smol", "slow", "plan"] as const).map((role) => {
            const steps = Object.entries(inputs.step_roles)
              .filter(([, bound]) => bound === role)
              .map(([step]) => step);
            const consumers = [
              ...steps,
              ...(inputs.judge_role === role ? ["judge (conditional)"] : []),
            ];
            return (
              <tr key={role} data-testid={`role-${role}`}>
                <td>{role}</td>
                <td className="mono">{inputs.roles[role].model}</td>
                <td>{inputs.roles[role].thinking}</td>
                <td>{consumers.length > 0 ? consumers.join(", ") : "auxiliary only"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <NativeModelState sessions={sessions} />
    </div>
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

  async function onConfirm() {
    setBusy(true);
    setError(null);
    try {
      const continuation = {
        model_profile: profile,
        base_branch: baseBranch,
        workshop_additions: additions,
        preamble,
      };
      const preview = await previewTaskConfiguration(detail.id, continuation);
      await confirmTaskConfiguration(detail.id, {
        ...continuation,
        preview_token: preview.preview_token,
        acknowledge_unknown: acknowledged,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
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

  return (
    <div className="panel" data-testid="task-inputs">
      <h2 className="panelTitle">Configuration needed</h2>
      <p className="hint" data-testid="legacy-unknown">
        This task ran before Ompire recorded launch inputs. Its workspace, branch, sessions,
        workflow history and pull-request facts are intact, but the model, thinking level,
        preamble and overrides it actually used were never persisted and cannot be
        recovered. Confirm what should happen <strong>from here on</strong> — that is not a
        claim about the turns already taken.
      </p>
      <ul className="unknownList">
        {(configuration.unknown_inputs ?? []).map((name) => (
          <li key={name}>{name}</li>
        ))}
      </ul>

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

      <button
        type="button"
        className="primary"
        disabled={busy || !profile || !baseBranch || !acknowledged}
        onClick={onConfirm}
        data-testid="legacy-confirm"
      >
        Confirm continuation configuration
      </button>
      {(detail.workflow_status === "running" || detail.workflow_status === "waiting") && (
        <button
          type="button"
          disabled={busy}
          onClick={onContinue}
          data-testid="legacy-continue"
        >
          Continue the interrupted run
        </button>
      )}
      {error && (
        <div className="submitError" role="alert" data-testid="legacy-error">
          {error}
        </div>
      )}
    </div>
  );
}
