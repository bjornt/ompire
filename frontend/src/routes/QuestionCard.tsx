import { useState } from "react";
import { answerAgent } from "../lib/api";
import type { PendingQuestion } from "../types";

/* Renders a task's pending ask/approval question (ask-approvals capability):
 * an `ask` kind shows each structured question with its options (single- or
 * multi-select per the payload), the recommended option flagged, and a
 * free-text "other" input when allowed; an `approval` kind shows a plain
 * approve/deny card. Submitting calls the answer endpoint; the card is
 * unmounted by the caller once `question_resolved` clears it from state. */

export function QuestionCard({ taskId, question }: { taskId: number; question: PendingQuestion }) {
  const [selections, setSelections] = useState<Record<number, string[]>>({});
  const [otherText, setOtherText] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function respondApproval(approved: boolean) {
    setBusy(true);
    setError(null);
    try {
      await answerAgent(taskId, { question_id: question.id, approved });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (question.kind === "approval") {
    return (
      <div className="panel questionCard approvalCard" data-testid="question-card">
        <div className="questionKind">Approval requested</div>
        <div className="questionActions">
          <button
            type="button"
            className="approveButton"
            disabled={busy}
            onClick={() => void respondApproval(true)}
          >
            Approve
          </button>
          <button
            type="button"
            className="denyButton"
            disabled={busy}
            onClick={() => void respondApproval(false)}
          >
            Deny
          </button>
        </div>
        {error && (
          <div className="composerError" data-testid="question-error">
            {error}
          </div>
        )}
      </div>
    );
  }

  function toggle(questionIdx: number, value: string, multi: boolean) {
    setSelections((prev) => {
      const current = new Set(prev[questionIdx] ?? []);
      if (multi) {
        if (current.has(value)) current.delete(value);
        else current.add(value);
      } else {
        current.clear();
        current.add(value);
      }
      return { ...prev, [questionIdx]: [...current] };
    });
  }

  async function submitAsk() {
    setBusy(true);
    setError(null);
    try {
      // The answer endpoint takes one flat selection list per pending
      // question; multiple `ask` sub-questions (design's open question on
      // multiplicity) combine into one answer here.
      const allSelections = question.questions.flatMap((_, idx) => selections[idx] ?? []);
      const combinedOther = Object.values(otherText)
        .map((t) => t.trim())
        .filter(Boolean)
        .join("\n");
      await answerAgent(taskId, {
        question_id: question.id,
        ...(allSelections.length > 0 ? { selections: allSelections } : {}),
        ...(combinedOther ? { text: combinedOther } : {}),
      });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const hasAnswer = question.questions.some(
    (_, idx) => (selections[idx]?.length ?? 0) > 0 || (otherText[idx]?.trim().length ?? 0) > 0,
  );

  return (
    <div className="panel questionCard" data-testid="question-card">
      {question.questions.map((q, idx) => (
        <div className="questionBlock" key={idx} data-testid={`question-block-${idx}`}>
          <div className="questionPrompt">{q.prompt}</div>
          <div className="questionOptions" role="group" aria-label={q.prompt}>
            {q.options.map((opt) => {
              const active = (selections[idx] ?? []).includes(opt.value);
              return (
                <button
                  type="button"
                  key={opt.value}
                  className={`questionOption ${active ? "active" : ""}`}
                  title={opt.description ?? undefined}
                  aria-pressed={active}
                  onClick={() => toggle(idx, opt.value, q.multi)}
                >
                  {opt.label}
                  {q.recommended === opt.value && <span className="recommendedTag">rec</span>}
                </button>
              );
            })}
          </div>
          {q.allowsOther && (
            <input
              type="text"
              className="questionOther"
              aria-label={`${q.prompt} — other`}
              placeholder="Or type your own answer…"
              value={otherText[idx] ?? ""}
              onChange={(e) => setOtherText((prev) => ({ ...prev, [idx]: e.target.value }))}
            />
          )}
        </div>
      ))}
      {error && (
        <div className="composerError" data-testid="question-error">
          {error}
        </div>
      )}
      <div className="questionActions">
        <button
          type="button"
          className="sendButton"
          disabled={busy || !hasAnswer}
          onClick={() => void submitAsk()}
        >
          Send answer
        </button>
      </div>
    </div>
  );
}
