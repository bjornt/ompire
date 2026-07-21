const RADIUS = 8;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** The amber context ring (Tasks.dc.html mockup): a small SVG donut showing
 * context usage, used both on task cards (driven by the `context-high`
 * advisory) and the task-detail status strip (driven by the REST-polled
 * context percentage) once it's at/above the advisory threshold. */
export function ContextRing({ pct, title }: { pct: number; title?: string }) {
  const clamped = Math.min(Math.max(pct, 0), 100);
  const dash = (clamped / 100) * CIRCUMFERENCE;
  return (
    <span className="contextRing" title={title ?? `context ${pct}%`} data-testid="context-ring">
      <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
        <circle cx="10" cy="10" r={RADIUS} fill="none" strokeWidth="3" className="contextRingTrack" />
        <circle
          cx="10"
          cy="10"
          r={RADIUS}
          fill="none"
          strokeWidth="3"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${CIRCUMFERENCE}`}
          transform="rotate(-90 10 10)"
          className="contextRingFill"
        />
      </svg>
      {pct}%
    </span>
  );
}
