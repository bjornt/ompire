import { Link, NavLink, Outlet } from "react-router-dom";
import { useEffect } from "react";
import { useDaemonState } from "../lib/useDaemonState";
import { countNeedsAttention } from "../lib/attention";
import { setFaviconBadge } from "../lib/favicon";
import { githubIdentityPresentation } from "../lib/githubPresentation";
import type { ConnectionState, GpgStatus } from "../types";
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

function formatGpgTtl(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function gpgChip(gpg: GpgStatus | null): { dot: string; label: string; title: string } {
  if (gpg?.state === "cached") {
    const ttl = gpg.ttl != null && gpg.ttl > 0 ? ` ${formatGpgTtl(gpg.ttl)}` : "";
    return {
      dot: "var(--green)",
      label: `gpg cached${ttl}`,
      title: `Signing key cached in gpg-agent${gpg.key ? ` (${gpg.key})` : ""}`,
    };
  }
  if (gpg?.state === "locked") {
    const command = gpg.key ? `echo | gpg --clearsign -u ${gpg.key} >/dev/null` : "";
    return {
      dot: "var(--amber)",
      label: "gpg locked",
      title: command
        ? `GPG signing key is locked. Warm the cache with: ${command}`
        : "GPG signing key is locked",
    };
  }
  return {
    dot: "var(--faint)",
    label: "gpg —",
    title: "GPG status unknown",
  };
}

export function Chrome() {
  const { connectionState, tasks, attention, gpg, gh, settings } = useDaemonState();
  const needsYou = countNeedsAttention(tasks, attention, settings);
  const daemonChip = DAEMON_CHIP_BY_STATE[connectionState];
  const signingChip = gpgChip(gpg);
  const githubChip = githubIdentityPresentation(gh);

  useEffect(() => {
    document.title = needsYou > 0 ? `(${needsYou}) ompire` : "ompire";
    setFaviconBadge(needsYou);
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
          <Link
            className={`chip${needsYou > 0 ? " needsYouChip" : ""}`}
            to={needsYou > 0 ? "/tasks?attention=1" : "/tasks"}
            title="Tasks needing the operator"
            data-testid="attention-chip"
          >
            {needsYou} need you
          </Link>
          <span className="chip" title={daemonChip.title}>
            <span className="dot" style={{ background: daemonChip.dot }} />
            daemon
          </span>
          <span className="chip" title={signingChip.title} data-testid="gpg-chip">
            <span className="dot" style={{ background: signingChip.dot }} />
            {signingChip.label}
          </span>
          <span
            className="chip"
            title={githubChip.description}
            aria-label={githubChip.description}
            data-testid="gh-chip"
          >
            <span className="dot" style={{ background: githubChip.dot }} />
            {githubChip.label}
          </span>
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </>
  );
}
