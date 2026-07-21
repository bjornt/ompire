const BASE_FAVICON_HREF = "/favicon.svg";

function badgeSvg(count: number): string {
  const label = count > 9 ? "9+" : String(count);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
    <rect x="2" y="4" width="44" height="40" rx="10" fill="#863bff"/>
    <text x="21" y="32" font-family="ui-monospace, monospace" font-size="20" font-weight="700" fill="#ede6ff" text-anchor="middle">&gt;&gt;</text>
    <circle cx="37" cy="11" r="11" fill="#c8382e"/>
    <text x="37" y="16" font-family="ui-sans-serif, system-ui, sans-serif" font-size="14" font-weight="700" fill="#fff" text-anchor="middle">${label}</text>
  </svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Sets the browser-tab favicon to a badged variant when `count > 0`
 * (frontend-shell: a favicon badge alongside the tab-title/"N need you"
 * count), reverting to the plain mark otherwise. A pure SVG data URI — no
 * canvas/image loading — so it applies synchronously and is trivial to
 * unit test. */
export function setFaviconBadge(count: number): void {
  let link = document.querySelector<HTMLLinkElement>("link[rel='icon']");
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.type = "image/svg+xml";
  link.href = count > 0 ? badgeSvg(count) : BASE_FAVICON_HREF;
}
