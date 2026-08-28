import { useContext } from "react";
import { DaemonContext, DaemonReconcileContext, type DaemonReconcile } from "./daemonContext";
import type { DaemonState } from "./daemonReducer";

export function useDaemonState(): DaemonState {
  const state = useContext(DaemonContext);
  if (!state) throw new Error("useDaemonState must be used within a DaemonProvider");
  return state;
}

/** The seam for applying a mutation's authoritative REST response to daemon
 * state — see `DaemonReconcile`. */
export function useDaemonReconcile(): DaemonReconcile {
  const reconcile = useContext(DaemonReconcileContext);
  if (!reconcile) throw new Error("useDaemonReconcile must be used within a DaemonProvider");
  return reconcile;
}
