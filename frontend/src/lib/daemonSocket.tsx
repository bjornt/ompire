// Architecture: ADR-0002 (docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md)
// Transport: ADR-0004 (docs/adr/0004-use-rest-and-websocket-snapshot-deltas.md)
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Envelope } from "../types";
import { applyEnvelope, initialDaemonState, type DaemonState } from "./daemonReducer";
import { DaemonContext } from "./daemonContext";
import { getDaemonToken } from "./token";

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 10000;

function daemonWsUrl(): string {
  const override = import.meta.env.VITE_OMPIRE_DAEMON_WS_URL as string | undefined;
  if (override) return override;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/ws`;
}

export function DaemonProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DaemonState>(initialDaemonState);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const socketRef = useRef<WebSocket | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedByUnmountRef = useRef(false);

  useEffect(() => {
    closedByUnmountRef.current = false;

    function connect() {
      const token = getDaemonToken();
      const url = new URL(daemonWsUrl());
      if (token) url.searchParams.set("token", token);

      const socket = new WebSocket(url.toString());
      socketRef.current = socket;

      socket.onopen = () => {
        backoffRef.current = INITIAL_BACKOFF_MS;
        setState((prev) => ({ ...prev, connectionState: "connected" }));
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        const envelope = JSON.parse(event.data) as Envelope;
        setState((prev) => applyEnvelope(prev, envelope));
      };

      socket.onclose = () => {
        if (closedByUnmountRef.current) return;
        setState((prev) => ({ ...prev, connectionState: "reconnecting" }));
        const delay = backoffRef.current;
        backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF_MS);
        timeoutRef.current = setTimeout(connect, delay);
      };
    }

    function reconnect() {
      socketRef.current?.close();
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      backoffRef.current = INITIAL_BACKOFF_MS;
      connect();
    }

    window.addEventListener("ompire:token-set", reconnect);

    connect();

    return () => {
      closedByUnmountRef.current = true;
      window.removeEventListener("ompire:token-set", reconnect);
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      socketRef.current?.close();
    };
  }, []);

  return <DaemonContext.Provider value={state}>{children}</DaemonContext.Provider>;
}
