import type { GitHubIdentityStatus, GitHubStatus, GitHubTargetStatus } from "../types";

const REDACTED = "[redacted]";
const TOKEN_RE = /(?<![A-Za-z0-9_])(?:github_pat_[A-Za-z0-9_]+|gh[opusr]_[A-Za-z0-9]+)(?![A-Za-z0-9_])/g;
const AUTHORIZATION_RE = /(\bauthorization\s*:\s*)[^\r\n]*/gi;
const URL_USERINFO_RE = /\b([a-z][a-z0-9+.-]*:\/\/)[^/\s@]+@/gi;
const GITHUB_SSH_RE = /^git@github\.com:([^?#\s]+?)\/?$/i;
const GITHUB_HTTPS_RE = /^https:\/\/github\.com\/([^?#\s]+?)\/?$/i;

export type GitHubIdentityPresentation = {
  dot: string;
  label: string;
  description: string;
};

/** Presentation-only fallback. The daemon redacts exact active token values at
 * the subprocess boundary; this prevents a malformed or older daemon payload
 * containing recognizable credential forms from reaching the DOM. */
export function safeGitHubDetail(detail: string | null | undefined): string | null {
  if (!detail) return null;
  return detail
    .replace(AUTHORIZATION_RE, `$1${REDACTED}`)
    .replace(URL_USERINFO_RE, `$1${REDACTED}@`)
    .replace(TOKEN_RE, REDACTED);
}

export function githubIdentityPresentation(
  status: GitHubStatus | null,
): GitHubIdentityPresentation {
  const identity = status?.identity;
  switch (identity?.state) {
    case "ready":
      if (identity.login) {
        return {
          dot: "var(--green)",
          label: `gh @${identity.login}`,
          description: `GitHub CLI ready as @${identity.login} for ${identity.host}`,
        };
      }
      return {
        dot: "var(--red)",
        label: "gh error",
        description: "GitHub CLI reported a ready state without an account",
      };
    case "missing":
      return {
        dot: "var(--red)",
        label: "gh missing",
        description: "Configured GitHub CLI executable is unavailable",
      };
    case "unauthenticated":
      return {
        dot: "var(--amber)",
        label: "gh auth",
        description: `GitHub CLI authentication is unavailable for ${identity.host}`,
      };
    case "error":
      return {
        dot: "var(--red)",
        label: "gh error",
        description: safeGitHubDetail(identity.detail) ?? "GitHub CLI check failed",
      };
    default:
      return {
        dot: "var(--faint)",
        label: "gh —",
        description: "GitHub CLI status has not been checked",
      };
  }
}

/** The same supported URL shapes as the daemon's existing ship path. This only
 * compares a daemon-derived result to the route's project; it never authorizes
 * an operation or sends a target to the daemon. */
export function canonicalGitHubTarget(upstreamUrl: string | null | undefined): string | null {
  if (!upstreamUrl) return null;
  const match = GITHUB_SSH_RE.exec(upstreamUrl) ?? GITHUB_HTTPS_RE.exec(upstreamUrl);
  if (!match) return null;
  let path = match[1].replace(/^\/+|\/+$/g, "");
  if (path.endsWith(".git")) path = path.slice(0, -4);
  const pieces = path.split("/");
  if (pieces.length !== 2 || pieces.some((piece) => !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(piece))) {
    return null;
  }
  return `github.com/${pieces[0].toLowerCase()}/${pieces[1].toLowerCase()}`;
}

function targetCanonical(status: GitHubTargetStatus): string | null {
  const target = status.target;
  return target ? `${target.host}/${target.owner}/${target.repository}`.toLowerCase() : null;
}

/** Returns a target state only when it belongs to the current daemon identity
 * and exactly matches the selected task's registered upstream. */
export function currentGitHubTargetStatus(
  status: GitHubStatus | null,
  upstreamUrl: string | null | undefined,
): GitHubTargetStatus | undefined {
  const expected = canonicalGitHubTarget(upstreamUrl);
  if (!status || !expected) return undefined;
  const identity = status.identity;
  if (
    identity.state !== "ready" ||
    identity.login === null ||
    identity.credential_source === null
  ) {
    return undefined;
  }
  const target = status.targets[expected];
  if (
    !target ||
    targetCanonical(target) !== expected ||
    target.identity === null ||
    target.identity.host !== identity.host ||
    target.identity.login !== identity.login ||
    target.identity.credential_source !== identity.credential_source
  ) {
    return undefined;
  }
  return target;
}

export function githubCredentialRecovery(identity: GitHubIdentityStatus | undefined): string {
  switch (identity?.credential_source) {
    case "GH_TOKEN":
      return "GH_TOKEN takes precedence over stored GitHub CLI accounts. Correct it in the daemon launch environment, restart the daemon, then re-check.";
    case "GITHUB_TOKEN":
      return "GITHUB_TOKEN takes precedence over stored GitHub CLI accounts. Correct it in the daemon launch environment, restart the daemon, then re-check.";
    default:
      return "Use gh auth login or gh auth switch for the named host, then re-check.";
  }
}
