import type { GpgState, GpgStatus } from "../types";

export type GpgPresentation = {
  dot: string;
  label: string;
  description: string;
  /** What the operator should do next, when there is something to do. */
  recovery: string | null;
  /** A terminal command that resolves the state, when one exists. */
  command: string | null;
};

/** Only this state permits a ship commit. The gate fails closed on the rest. */
export function canSign(gpg: GpgStatus | null): boolean {
  return gpg?.state === "ready";
}

/** A short label for the selected key: its user ID, else its short key ID. */
export function keyLabel(gpg: GpgStatus | null): string | null {
  const selected = gpg?.selected;
  if (!selected) return null;
  return selected.uid ?? selected.key_id;
}

function formatTtl(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

/**
 * One vocabulary for the chrome chip, the Settings panel, and the Ship flow
 * banner, so the three surfaces cannot describe the same state differently.
 *
 * Every non-ready state names its own condition and its own fix. The daemon
 * never offers to unlock a key itself — warming the cache is an operator
 * action in a terminal.
 */
export function gpgPresentation(gpg: GpgStatus | null): GpgPresentation {
  const state: GpgState = gpg?.state ?? "unknown";
  const selected = gpg?.selected ?? null;
  const named = selected ? ` (${selected.uid ?? selected.fingerprint})` : "";

  switch (state) {
    case "ready": {
      // Only shown when the agent actually reports a lifetime.
      const ttl =
        gpg?.cache_ttl != null && gpg.cache_ttl > 0
          ? ` ${formatTtl(gpg.cache_ttl)}`
          : "";
      return {
        dot: "var(--green)",
        label: `gpg ready${ttl}`,
        description: `Signing key is ready${named}`,
        recovery: null,
        command: null,
      };
    }
    case "locked":
      return {
        dot: "var(--amber)",
        label: "gpg locked",
        description: `GPG signing key is locked${named}`,
        recovery:
          "Warm the passphrase cache in a terminal, then re-check the key.",
        command: selected
          ? `echo | gpg --clearsign -u ${selected.fingerprint} >/dev/null`
          : null,
      };
    case "ambiguous":
      return {
        dot: "var(--amber)",
        label: "gpg unselected",
        description: gpg?.detail ?? "Several usable GPG signing keys",
        recovery:
          "Choose which key signs, under Templates & settings → Daemon.",
        command: null,
      };
    case "no_key":
      return {
        dot: "var(--red)",
        label: "gpg no key",
        description: "No signing-capable GPG key is available to the daemon",
        recovery:
          "Generate or import a signing key for the account running the " +
          "daemon, then re-check.",
        command: null,
      };
    case "missing":
      return {
        dot: "var(--red)",
        label: "gpg missing",
        description: "The GPG command-line tools are unavailable to the daemon",
        recovery: "Install GnuPG on the daemon's host and restart the daemon.",
        command: null,
      };
    case "agent_unavailable":
      return {
        dot: "var(--red)",
        label: "gpg agent",
        description: "gpg-agent is unreachable",
        recovery: "Start the agent in a terminal, then re-check the key.",
        command: "gpg-connect-agent /bye",
      };
    case "error":
      return {
        dot: "var(--red)",
        label: "gpg error",
        description: gpg?.detail ?? "The GPG signing key state is indeterminate",
        recovery: "Resolve the reported problem, then re-check the key.",
        command: null,
      };
    default:
      return {
        dot: "var(--faint)",
        label: "gpg —",
        description: "GPG signing key has not been checked yet",
        recovery: null,
        command: null,
      };
  }
}

/** How a selection was made, for the Settings panel's provenance row. */
export function selectionSourceLabel(gpg: GpgStatus | null): string | null {
  switch (gpg?.selected?.source) {
    case "override":
      return "Selected here";
    case "config":
      return "config.toml";
    case "git":
      return "git config user.signingkey";
    case "auto":
      return "Only usable key";
    default:
      return null;
  }
}
