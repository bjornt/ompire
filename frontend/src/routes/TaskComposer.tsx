import { useState } from "react";
import { followUpAgent, interruptAgent, steerAgent } from "../lib/api";
import type { SessionStatus } from "../types";

/* Composer with the three omp interaction modes. Availability tracks whether
 * the agent is streaming: steering and interrupting only make sense mid-turn,
 * while a follow-up can be queued whenever an agent is live. The whole composer
 * is disabled when the task has no live agent. A pending `ask` (`waiting-input`)
 * counts as an in-flight turn too — the turn hasn't ended, it's just paused on
 * a question — so steer/follow-up stay available, with a note explaining why. */

type Mode = "steer" | "follow-up" | "interrupt";

const SEND: Record<Mode, (id: number, session: string, message: string) => Promise<unknown>> = {
  steer: steerAgent,
  "follow-up": followUpAgent,
  interrupt: interruptAgent,
};

const LABELS: Record<Mode, string> = {
  steer: "Steer",
  "follow-up": "Follow-up",
  interrupt: "Interrupt",
};

function modeEnabled(mode: Mode, streaming: boolean): boolean {
  if (mode === "follow-up") return true;
  return streaming; // steer / interrupt need an in-flight turn
}

export function TaskComposer({
  taskId,
  session,
  hasLiveAgent,
  isStreaming,
  sessionStatus,
}: {
  taskId: number;
  /** Session the composer addresses (workflow-engine design D-1). */
  session: string;
  hasLiveAgent: boolean;
  isStreaming: boolean | null;
  sessionStatus: SessionStatus | null;
}) {
  const [mode, setMode] = useState<Mode>("follow-up");
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Prefer the agent's own streaming flag; fall back to the session status
  // until the first state read lands. `waiting-input` is a paused turn, not
  // an ended one, so it counts as streaming regardless of the raw flag.
  const streaming = sessionStatus === "waiting-input" || (isStreaming ?? sessionStatus === "working");
  const currentEnabled = hasLiveAgent && modeEnabled(mode, streaming);
  const canSend = currentEnabled && message.trim().length > 0 && !busy;
  const note =
    sessionStatus === "waiting-input"
      ? "A question is pending — steer or send a follow-up while it waits."
      : sessionStatus === "waiting-approval"
        ? "Waiting on an approval decision."
        : null;

  async function send() {
    if (!canSend) return;
    setBusy(true);
    setError(null);
    try {
      await SEND[mode](taskId, session, message.trim());
      setMessage("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel composerPanel" data-testid="composer">
      <div className="composerModes" role="group" aria-label="Composer mode">
        {(Object.keys(LABELS) as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            className={`modeButton ${mode === m ? "active" : ""}`}
            aria-pressed={mode === m}
            disabled={!hasLiveAgent || !modeEnabled(m, streaming)}
            onClick={() => setMode(m)}
          >
            {LABELS[m]}
          </button>
        ))}
      </div>
      <textarea
        className="composerInput"
        aria-label="Message"
        placeholder={hasLiveAgent ? `${LABELS[mode]} the agent…` : "No live agent"}
        value={message}
        disabled={!hasLiveAgent}
        onChange={(e) => setMessage(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void send();
          }
        }}
      />
      {note && <div className="composerNote" data-testid="composer-note">{note}</div>}
      {error && <div className="composerError" data-testid="composer-error">{error}</div>}
      <div className="composerActions">
        <button type="button" className="sendButton" disabled={!canSend} onClick={() => void send()}>
          Send
        </button>
      </div>
    </div>
  );
}
