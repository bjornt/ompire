import { createContext } from "react";
import type { DaemonState } from "./daemonReducer";

export const DaemonContext = createContext<DaemonState | null>(null);
