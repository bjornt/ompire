import { useContext } from "react";
import { DaemonContext } from "./daemonContext";
import type { DaemonState } from "./daemonReducer";

export function useDaemonState(): DaemonState {
  const state = useContext(DaemonContext);
  if (!state) throw new Error("useDaemonState must be used within a DaemonProvider");
  return state;
}
