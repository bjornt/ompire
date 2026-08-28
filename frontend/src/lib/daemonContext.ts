import { createContext } from "react";
import type { DaemonState } from "./daemonReducer";

export const DaemonContext = createContext<DaemonState | null>(null);

/** Applies a mutation's own authoritative REST response to daemon state,
 * through the very reducer case its WebSocket event uses.
 *
 * This is not client-owned state: the daemon is authoritative for command
 * outcomes and observed state alike (ADR-0004), and a fresh snapshot still
 * replaces everything. It exists so a view need not wait for the event to
 * learn the outcome of a command it just issued — components still render
 * only from `DaemonContext`, never from a response value. */
export type DaemonReconcile = (type: string, payload: unknown) => void;

export const DaemonReconcileContext = createContext<DaemonReconcile | null>(null);
