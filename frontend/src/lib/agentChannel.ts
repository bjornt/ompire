import { useEffect, useState } from "react";
import type { Envelope } from "../types";
import { emptyTranscript, reduceFrame, type Transcript } from "./agentFrames";
import { getDaemonToken } from "./token";

// Architecture: ADR-0004 (docs/adr/0004-use-rest-and-websocket-snapshot-deltas.md)

/* Per-agent event-channel client (agent-event-stream): connects to
 * `/api/ws/agents/:taskId/:session`, replays the daemon's ring buffer then follows live
 * events, and reconnects while the agent is live (`enabled`). The daemon closes
 * the channel with code 1000 ("agent exited") once its buffer is flushed — that
 * one is terminal, since the stream is genuinely done and `enabled` will soon
 * flip false as the session lands `failed` on the main socket anyway.
 *
 * Code 4404 ("no live agent for this task yet") is NOT treated as terminal:
 * a session can be `starting` (crash-recovery capability — recovering after a
 * daemon restart, or the ordinary spawn-in-progress window) for a long time
 * with `enabled` already true but no `AgentHandle` registered yet. Giving up
 * on the first 4404 left the channel permanently dead until the component
 * remounted (e.g. navigating away and back) even once the agent went live —
 * `enabled` staying continuously true across that whole window means the
 * connect effect never reruns on its own. Retrying with backoff here instead
 * relies on `enabled` (driven by the main socket's session status) to be the
 * sole stop signal: once a task is genuinely dead its session lands `failed`,
 * `enabled` goes false, and the effect's cleanup tears the retry loop down.
 *
 * Because a reconnect replays the buffer from the top, the transcript is reset
 * to empty on every fresh connection and rebuilt from replay + live frames. */

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 10000;

/** Handle for a reconnect-backoff timer (platform `setTimeout` return). */
type BackoffTimer = ReturnType<typeof setTimeout>;

function agentWsUrl(taskId: number, session: string): string {
  const base = import.meta.env.VITE_OMPIRE_DAEMON_WS_URL as string | undefined;
  if (base) {
    // Reuse the configured origin, swap the path to the per-agent channel.
    const url = new URL(base);
    url.pathname = `/api/ws/agents/${taskId}/${session}`;
    return url.toString();
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/ws/agents/${taskId}/${session}`;
}

export interface AgentChannel {
  transcript: Transcript;
  connected: boolean;
  /** Bumped on every `agent_start`/`agent_end`; a refresh signal for pollers
   * (the status strip re-reads state/stats when this changes). */
  turnEpoch: number;
}

/** Subscribe to one session's agent event channel while `enabled`. Disabled
 * (no live agent) tears the socket down and reports an empty, disconnected
 * view.
 *
 * Every effect run is a generation with its own socket/timer bookkeeping:
 * cleanup deactivates the run, and every socket callback and retry checks the
 * generation before touching state. Without the guard, a retry timer that has
 * already fired (or a socket mid-replay) outlives its cleanup — the daemon
 * restart that arms the backoff loop is normal operation — and the zombie
 * replays the *previous* session's buffer into the *current* session's
 * transcript, clobbering `socketRef` so the live channel can no longer be
 * closed (observed dogfooding the bugfix workflow: switching to the `judge`
 * tab kept showing the reproducer's transcript). */
export function useAgentChannel(taskId: number, session: string, enabled: boolean): AgentChannel {
  const [transcript, setTranscript] = useState<Transcript>(emptyTranscript);
  const [connected, setConnected] = useState(false);
  const [turnEpoch, setTurnEpoch] = useState(0);

  useEffect(() => {
    if (!enabled || !Number.isInteger(taskId)) {
      setTranscript(emptyTranscript);
      setConnected(false);
      return;
    }
    const run: {
      active: boolean;
      socket: WebSocket | null;
      timer: BackoffTimer | null;
      backoff: number;
    } = { active: true, socket: null, timer: null, backoff: INITIAL_BACKOFF_MS };

    function connect() {
      if (!run.active) return; // a retry queued before cleanup is dead on arrival
      const token = getDaemonToken();
      const url = new URL(agentWsUrl(taskId, session));
      if (token) url.searchParams.set("token", token);

      const socket = new WebSocket(url.toString());
      run.socket = socket;
      // Fresh connection: the daemon replays the buffer from the top, so start
      // from empty to avoid duplicating already-seen frames.
      setTranscript(emptyTranscript);

      socket.onopen = () => {
        if (!run.active) return;
        run.backoff = INITIAL_BACKOFF_MS;
        setConnected(true);
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        if (!run.active) return;
        const envelope = JSON.parse(event.data) as Envelope<Record<string, unknown>>;
        if (envelope.type === "agent_start" || envelope.type === "agent_end") {
          setTurnEpoch((n) => n + 1);
        }
        setTranscript((prev) => reduceFrame(prev, { ...envelope.payload, type: envelope.type }));
      };

      socket.onclose = (event: CloseEvent) => {
        setConnected(false);
        if (!run.active) return;
        // Only code 1000 ("agent exited", buffer flushed) is terminal; 4404
        // ("no live agent yet") retries — see the module doc comment above.
        if (event.code === 1000) return;
        const delay = run.backoff;
        run.backoff = Math.min(run.backoff * 2, MAX_BACKOFF_MS);
        run.timer = setTimeout(connect, delay);
      };
    }

    connect();

    return () => {
      run.active = false;
      if (run.timer) clearTimeout(run.timer);
      run.socket?.close();
    };
  }, [taskId, session, enabled]);

  return { transcript, connected, turnEpoch };
}
