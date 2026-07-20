import { useEffect, useRef, useState } from "react";
import type { Envelope } from "../types";
import { emptyTranscript, reduceFrame, type Transcript } from "./agentFrames";
import { getDaemonToken } from "./token";

/* Per-agent event-channel client (agent-event-stream): connects to
 * `/api/ws/agents/:taskId`, replays the daemon's ring buffer then follows live
 * events, and reconnects while the agent is live. The daemon closes the channel
 * with code 1000 ("agent exited") once its buffer is flushed, and 4404 when no
 * live agent backs the task — both are terminal, so we do not reconnect on them.
 *
 * Because a reconnect replays the buffer from the top, the transcript is reset
 * to empty on every fresh connection and rebuilt from replay + live frames. */

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 10000;
const NO_LIVE_AGENT_CLOSE_CODE = 4404;

function agentWsUrl(taskId: number): string {
  const base = import.meta.env.VITE_OMPIRE_DAEMON_WS_URL as string | undefined;
  if (base) {
    // Reuse the configured origin, swap the path to the per-agent channel.
    const url = new URL(base);
    url.pathname = `/api/ws/agents/${taskId}`;
    return url.toString();
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/ws/agents/${taskId}`;
}

export interface AgentChannel {
  transcript: Transcript;
  connected: boolean;
  /** Bumped on every `agent_start`/`agent_end`; a refresh signal for pollers
   * (the status strip re-reads state/stats when this changes). */
  turnEpoch: number;
}

/** Subscribe to a task's agent event channel while `enabled`. Disabled (no
 * live agent) tears the socket down and reports an empty, disconnected view. */
export function useAgentChannel(taskId: number, enabled: boolean): AgentChannel {
  const [transcript, setTranscript] = useState<Transcript>(emptyTranscript);
  const [connected, setConnected] = useState(false);
  const [turnEpoch, setTurnEpoch] = useState(0);

  const socketRef = useRef<WebSocket | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const closedByEffectRef = useRef(false);

  useEffect(() => {
    if (!enabled || !Number.isInteger(taskId)) {
      setTranscript(emptyTranscript);
      setConnected(false);
      return;
    }
    closedByEffectRef.current = false;
    backoffRef.current = INITIAL_BACKOFF_MS;

    function connect() {
      const token = getDaemonToken();
      const url = new URL(agentWsUrl(taskId));
      if (token) url.searchParams.set("token", token);

      const socket = new WebSocket(url.toString());
      socketRef.current = socket;
      // Fresh connection: the daemon replays the buffer from the top, so start
      // from empty to avoid duplicating already-seen frames.
      setTranscript(emptyTranscript);

      socket.onopen = () => {
        backoffRef.current = INITIAL_BACKOFF_MS;
        setConnected(true);
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        const envelope = JSON.parse(event.data) as Envelope<Record<string, unknown>>;
        if (envelope.type === "agent_start" || envelope.type === "agent_end") {
          setTurnEpoch((n) => n + 1);
        }
        setTranscript((prev) => reduceFrame(prev, { ...envelope.payload, type: envelope.type }));
      };

      socket.onclose = (event: CloseEvent) => {
        setConnected(false);
        if (closedByEffectRef.current) return;
        // Terminal closes: the agent is gone, nothing to reconnect to.
        if (event.code === NO_LIVE_AGENT_CLOSE_CODE || event.code === 1000) return;
        const delay = backoffRef.current;
        backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF_MS);
        timeoutRef.current = setTimeout(connect, delay);
      };
    }

    connect();

    return () => {
      closedByEffectRef.current = true;
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      socketRef.current?.close();
    };
  }, [taskId, enabled]);

  return { transcript, connected, turnEpoch };
}
