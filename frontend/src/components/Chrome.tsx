import { NavLink, Outlet } from "react-router-dom";
import { useEffect } from "react";
import { useDaemonState } from "../lib/daemonSocket";
import { countNeedsAttention } from "../lib/attention";
import type { ConnectionState } from "../types";
import "./Chrome.css";

const NAV_ITEMS = [
  { to: "/tasks", label: "Tasks" },
  { to: "/projects", label: "Projects" },
  { to: "/spawn", label: "Spawn task" },
  { to: "/ship", label: "Ship flow" },
  { to: "/settings", label: "Templates & settings" },
] as const;

const DAEMON_CHIP_BY_STATE: Record<ConnectionState, { dot: string; title: string }> = {
  connecting: { dot: "var(--faint)", title: "WebSocket connecting…" },
  connected: { dot: "var(--green)", title: "WebSocket connected — snapshot + live deltas" },
  reconnecting: { dot: "var(--amber)", title: "WebSocket disconnected — reconnecting…" },
  disconnected: { dot: "var(--red)", title: "WebSocket disconnected" },
};

export function Chrome() {
  const { connectionState, tasks, sessions } = useDaemonState();
  const needsYou = countNeedsAttention(tasks, sessions);
  const daemonChip = DAEMON_CHIP_BY_STATE[connectionState];

  useEffect(() => {
    document.title = needsYou > 0 ? `(${needsYou}) ompire` : "ompire";
  }, [needsYou]);

  return (
    <>
      <header className="header" data-testid="chrome-header">
        <div className="logo">
          <span className="logoMark">»</span>ompire
        </div>
        <nav className="nav">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? "navLink navLinkActive" : "navLink")}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="chips">
          <span className="chip needsYouChip" title="Tasks needing the operator">
            {needsYou} need you
          </span>
          <span className="chip" title={daemonChip.title}>
            <span className="dot" style={{ background: daemonChip.dot }} />
            daemon
          </span>
          <span className="chip" title="Signing key cached in gpg-agent (placeholder — not wired to real gpg state yet)">
            <span className="dot" style={{ background: "var(--green)" }} />
            gpg —
          </span>
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </>
  );
}
