const STORAGE_KEY = "ompire_token";

/** Resolves the daemon auth token for local dev, per design.md: a build-time
 * env var, a `?token=` query param (stashed to localStorage and stripped
 * from the URL), or whatever was stashed previously. There is no login UI
 * yet — see design.md's open question on real token delivery. */
export function getDaemonToken(): string | null {
  const envToken = import.meta.env.VITE_OMPIRE_TOKEN;
  if (envToken) return envToken;

  if (typeof window === "undefined") return null;

  const url = new URL(window.location.href);
  const queryToken = url.searchParams.get("token");
  if (queryToken) {
    window.localStorage.setItem(STORAGE_KEY, queryToken);
    url.searchParams.delete("token");
    window.history.replaceState(null, "", url.toString());
    return queryToken;
  }

  return window.localStorage.getItem(STORAGE_KEY);
}
