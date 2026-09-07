import type { GateSnapshot, StepRecord, WorkflowState } from "../../types";

/** What actually happened at one declared step, attempt by attempt.
 *
 * The distinction this exists to keep is between a *declaration* and an
 * *attempt*. A step that was never reached is possible work, not successful
 * work; a step visited three times has three separate records, and collapsing
 * them into "the latest one" is how a rejected fix disappears from the story
 * of a run.
 *
 * Everything here comes from what the daemon wrote down. Where history is
 * silent — a legacy run that recorded no evidence, a route whose reason was
 * never stored — this says so rather than reconstructing a plausible answer
 * from today's definition. In particular it never claims which case of a
 * decision "won": history holds the destination, not the clause, and a
 * client re-evaluating the predicate would be guessing with different data.
 */

const STATUS_WORDS: Record<string, string> = {
  ok: "finished",
  waiting: "waiting for you",
  failed: "failed",
  running: "running now",
  skipped: "skipped by a route",
};

function gateSnapshotOf(record: StepRecord): GateSnapshot | null {
  const outcome = record.outcome;
  if (!outcome || typeof outcome !== "object") return null;
  const snapshot = outcome as unknown as GateSnapshot;
  if (typeof snapshot.message !== "string" || !Array.isArray(snapshot.choices)) {
    return null;
  }
  return snapshot;
}

function resultOf(record: StepRecord): { result: string; summary: string | null } | null {
  const outcome = record.outcome;
  if (!outcome || typeof outcome !== "object") return null;
  const result = (outcome as { result?: unknown }).result;
  if (typeof result !== "string") return null;
  const summary = (outcome as { summary?: unknown }).summary;
  return { result, summary: typeof summary === "string" ? summary : null };
}

function destinationWords(next: Record<string, unknown> | undefined): string | null {
  if (next === undefined) return null;
  if (typeof next.step === "string") return next.step;
  if (typeof next.result === "string") return `the ending “${next.result}”`;
  if (next.complete === true) return "the end of the run";
  if (next.pause === true) return "a pause";
  return null;
}

export function StepAttempts({
  attempts,
  workflow,
  declaresEvidence,
  onOpenSession,
  onOpenAttempt,
}: {
  attempts: StepRecord[];
  workflow: WorkflowState;
  /** Whether the *declaration* asks for any evidence at all. Without this,
   * "no bindings were recorded" cannot be told apart from "this step never
   * asked for any", and the first reads as lost history. */
  declaresEvidence: boolean;
  onOpenSession: (session: string) => void;
  /** Jump to the attempt one piece of evidence was bound to. */
  onOpenAttempt: (seq: number) => void;
}) {
  if (attempts.length === 0) {
    return (
      <p className="flowMuted" data-testid="step-unvisited">
        Not visited. This is work the procedure allows, not work that happened.
      </p>
    );
  }
  return (
    <ol className="attemptList" data-testid="step-attempts">
      {attempts.map((record) => {
        const snapshot = gateSnapshotOf(record);
        const decision = snapshot?.decision ?? null;
        const result = snapshot === null ? resultOf(record) : null;
        const bindings = record.evidence?.bindings ?? null;
        return (
          <li
            key={record.seq}
            id={`attempt-${record.seq}`}
            className="attempt"
            tabIndex={-1}
            data-testid={`attempt-${record.step}-${record.seq}`}
            data-status={record.status}
          >
            <div className="flowHead">
              <span className="flowChip">attempt {record.seq}</span>
              <span className="flowChip">
                {STATUS_WORDS[record.status] ?? record.status}
              </span>
              {record.session !== null && (
                <button
                  type="button"
                  className="linkButton"
                  data-testid={`attempt-session-${record.seq}`}
                  onClick={() => onOpenSession(record.session as string)}
                >
                  open the {record.session} conversation
                </button>
              )}
            </div>
            {result !== null && (
              <p data-testid={`attempt-result-${record.seq}`}>
                Declared <code className="mono">{result.result}</code>
                {result.summary === null ? "" : ` — ${result.summary}`}
              </p>
            )}
            {record.error !== null && (
              <p className="flowBroken" data-testid={`attempt-error-${record.seq}`}>
                {record.error}
              </p>
            )}
            {record.pause !== null && (
              <p className="flowBroken" data-testid={`attempt-pause-${record.seq}`}>
                Stopped without deciding — {record.pause.reason}: {record.pause.message}
              </p>
            )}
            {snapshot !== null && (
              <div data-testid={`attempt-gate-${record.seq}`}>
                <p>Asked: {snapshot.message}</p>
                {decision === null ? (
                  <p className="flowMuted">No answer has been recorded yet.</p>
                ) : (
                  <p data-testid={`attempt-decision-${record.seq}`}>
                    {decision.actor} chose “{decision.label}”
                    {destinationWords(decision.next ?? decision.destination) === null
                      ? ""
                      : `, which went to ${destinationWords(decision.next ?? decision.destination)}`}
                    {decision.feedback === null || decision.feedback === ""
                      ? ""
                      : ` — “${decision.feedback}”`}
                    {` · ${decision.decided_at}`}
                  </p>
                )}
              </div>
            )}
            {bindings === null ? (
              <p className="flowMuted">
                {declaresEvidence
                  ? "This attempt recorded no evidence bindings. What it was handed is not on the record — format-1 attempts kept none."
                  : "This step asks for no evidence, so this attempt was handed none."}
              </p>
            ) : (
              Object.keys(bindings).length > 0 && (
                <ul className="flowList" data-testid={`attempt-evidence-${record.seq}`}>
                  {Object.entries(bindings).map(([alias, binding]) => (
                    <li key={alias}>
                      <code className="mono">{alias}</code>{" "}
                      {binding === null ? (
                        <span className="flowMuted">
                          matched nothing — an optional selector with no source
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="linkButton"
                          data-testid={`evidence-source-${record.seq}-${alias}`}
                          onClick={() => onOpenAttempt(binding.seq)}
                        >
                          {binding.step} attempt {binding.seq}
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )
            )}
            <p className="flowMuted">
              {record.started_at}
              {record.finished_at === null ? " · still open" : ` → ${record.finished_at}`}
            </p>
          </li>
        );
      })}
      <VisitCount attempts={attempts} workflow={workflow} />
    </ol>
  );
}

/** How many visits are on the record — not how many the engine counted.
 *
 * A run whose history predates step records has fewer of them than it had
 * visits, and inventing the difference would put a number on a bound the
 * operator cannot check. */
function VisitCount({
  attempts,
  workflow,
}: {
  attempts: StepRecord[];
  workflow: WorkflowState;
}) {
  const current = workflow.step;
  return (
    <li className="flowMuted" data-testid="visit-count">
      {attempts.length} recorded visit{attempts.length === 1 ? "" : "s"}
      {current === attempts[0]?.step ? " · the run is here now" : ""}
    </li>
  );
}
