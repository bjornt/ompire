# Handoff: ompire web UI (six views)

## Overview
High-fidelity mockups of the browser UI for **ompire**, the oh-my-pi agent task
manager described in `SPEC.md` (included in this folder; Decisions 1–9 and
the "Web app views" section). Six views: Tasks (home), Projects, Spawn task,
Task detail, Ship flow, Templates & settings.

The spec is the source of truth for *behavior and data model*; this bundle is
the source of truth for *look, layout, and interaction detail*.

## About the Design Files
The `.dc.html` files are **design references created in HTML** — prototypes
showing intended look and behavior, not production code to ship. Recreate them
in the target codebase's environment. Per SPEC Decision 2/3 the frontend is
TypeScript/React (stateless UI, WebSocket snapshot + deltas, REST commands);
if that scaffold doesn't exist yet, create it.

To view the mockups: serve this folder over HTTP (e.g. `python3 -m http.server`)
and open `Tasks.dc.html` — `support.js` must sit next to the pages. Every page
is navigable from the top nav. All styling is inline on the elements, so any
value can be read straight out of the markup.

## Fidelity
**High-fidelity.** Colors, typography, spacing, copy, and states are final.
Recreate pixel-perfectly. The only simulated parts are data (hardcoded example
tasks/projects) and a few local toggles standing in for real daemon state.

## Design language
A terminal-native operations console: dense, 13px base, monospace identity.
Attention is encoded on two axes that never mix:

- **Tier is structural** — interrupt: 3px colored left spine + tinted card
  surface + solid pulsing status pill + primary action on the card; notify:
  spine + outlined pill with steady dot; badge: neutral chip pill; silent:
  recessed card (surface mixed 55% toward bg), no spine.
- **Hue is semantic** — red = dead/failed, amber = blocked/suspect
  (waiting-approval, stalled), cyan = question (waiting-input, gate),
  violet = reviewing, green = ok/shipped. The teal accent is interactive-only,
  never a status hue.

## Design tokens
Light theme (default) / dark theme (`prefers-color-scheme` or
`:root[data-theme]` override; note `--shadow: none` in dark):

- `--bg` #eef1f4 / #0f1418 — page background
- `--surface` #ffffff / #151c22 — cards, header
- `--surface2` #f3f6f8 / #1a232b — inset fills, active nav, buttons
- `--line` #d6dee4 / #27333d — borders; `--line-soft` #e3e9ee / #1f2a33 — dividers
- `--text` #1f2b34 / #d5dee6; `--muted` #5d6d7a / #8797a6; `--faint` #8b98a3 / #5b6a77
- `--accent` #147c6b / #4fb3a1 (teal, interactive); `--on-hue` #ffffff / #10161b
- Semantic: `--red` #c8382e/#f16a5f · `--amber` #a06a08/#e6a23c ·
  `--cyan` #146f9e/#56b9e4 · `--violet` #6d4fd3/#a78bfa · `--green` #2c8642/#63bd75
- `--shadow` 0 1px 2px rgba(24,39,50,.06), 0 2px 8px rgba(24,39,50,.05) (light only)
- Fonts: `--sans` system-ui stack (body); `--mono` ui-monospace stack
  (branch names, status pills, metrics, section headers, code)
- Tints/mixes are done with `color-mix(in srgb, var(--hue) N%, base)` —
  e.g. interrupt card bg = hue 6% into surface, border = hue 38% into line.
- Radii: 8px cards, 6px buttons/inputs, 4px pills, 20px chips.
  Body 13px/1.5; h1 17px/650; pills 10.5px mono 700 letter-spacing .05em;
  section headers 10.5px mono 700 uppercase letter-spacing .14em; metrics 11px mono.

## Global chrome (every view)
Sticky 46px header: logo (18px teal square with "»" + mono "ompire"), nav
(Tasks · Projects · Spawn task · Ship flow · Templates & settings; active =
`--surface2` bg + 600 weight — Task detail is NOT a nav item, see below), then right-side chips:
"N need you" (red-tinted pill; count = interrupt+notify tasks), "daemon"
(green dot = WebSocket up), gpg chip ("gpg 2h58m" neutral when cached;
"gpg locked" amber-tinted when locked — one global condition shared with the
Ship flow's blocked commit step). Tab title carries the badge count: "(6)".

## Screens
Each page's purpose/content contract is in SPEC.md "Web app views" (view
numbers match). Implementation notes per file:

- **Tasks.dc.html** — home. Sections "Needs you" (red header) → Running →
  Idle → Shipped, cards in `repeat(auto-fill, minmax(360px, 1fr))` grid.
  Card anatomy: project name (top-left, 11.5px 600 muted) · status pill
  (top-right, step-prefixed for workflow tasks, e.g. "fix: waiting-input") ·
  branch (13.5px mono 600) · reason line · optional payload block (stderr
  excerpt, inline question card with answer buttons, workflow trail
  reproduce ✓ → fix → validate → ship) · footer metrics row (context ring
  16px SVG, tokens · cost, elapsed, actions right-aligned). Context ring
  turns amber at ≥80%. Working cards show an indeterminate 2px slide bar;
  shipped tasks collapse to a single dashed-border row.
- **Projects.dc.html** — project CRUD. Row per project: mono name, muted
  title, active-task chip linking to Tasks, Edit; below, an `upstream` /
  `fork` two-column mono URL grid (fork row omitted with "you own upstream —
  no fork needed" note when absent). Edit expands an inline panel
  (surface2, top border): name disabled with rename caveat, title, upstream,
  fork, Save/Cancel, "Remove project…" (red, right-aligned). "New project"
  button opens the same form as an accent-bordered card above the list, with
  field hints and the CLI equivalent line.
- **Spawn Task.dc.html** — template picker, slug → branch preview, prompt
  editor, overrides; post-submit launch pipeline (clone → workshop → agent
  ready → prompt sent) with per-step status and inline stderr expansion on
  failure.
- **Task Detail.dc.html** — a drill-in sub-page of Tasks, not a nav item:
  reached via a task card’s "Open" (route e.g. /tasks/:id); the nav keeps
  Tasks highlighted and the h1 is a breadcrumb ("← Tasks / maas /
  bjornt/fix-dhcp-races"). Densest view: workflow strip with expandable
  step outcomes (`.ompire/outcome.json` JSON expansion), per-session
  transcript tabs, streaming transcript with collapsible tool cards,
  question cards, sidebar Task panel listing per-session ids (faint mono
  "id ses_…" — for tracing, e.g. langfuse), composer with steer/follow-up/interrupt modes, status
  strip, metadata panel with escape-hatch instructions.
- **Ship Flow.dc.html** — review → commit → push/PR → cleanup stepper;
  commit step shows squash/retain choice and agent-drafted editable
  message/PR fields, and blocks with unlock instructions when gpg is locked.
- **Settings.dc.html** — template CRUD (now referencing a project by name,
  SPEC Decision 9), notification prefs per attention tier, daemon panel
  (watchdog/context thresholds, auth token).

## Interactions & behavior
- Hover: buttons brighten (accent) or gain `--faint` border (neutral);
  nav links gain surface2 bg; all links `--accent`, underline on hover.
- Focus: 2px accent outline, offset 1.
- Animations: `pulse` 1.1s (interrupt pill dots), `breathe` 2.4s (working
  dot), `slide` 1.8s (indeterminate bar). All disabled under
  `prefers-reduced-motion`.
- Buttons in mockups that only make sense against a live daemon (Approve,
  Respawn, Kill…) are inert; expandable panels (Projects edit/new, workflow
  step outcomes, Ship flow steps, Settings sections) work in the mockup and
  define the intended expand/collapse behavior.
- Theme: auto via `prefers-color-scheme`, overridable (auto/light/dark).

## State management
Drive everything from the daemon protocol in SPEC.md: fleet snapshot +
`status_changed` / `attention` / `advisory` / `stats` events over WebSocket,
REST for commands. Per-session states and attention tiers: SPEC Decision 4.
UI-local state only for expansion/tabs/theme.

## Assets
None — no images or icon fonts. The only graphics are inline SVG context
rings and CSS dots.

## Files
- `Tasks.dc.html`, `Projects.dc.html`, `Spawn Task.dc.html`,
  `Task Detail.dc.html`, `Ship Flow.dc.html`, `Settings.dc.html` — the six views
- `support.js` — mockup runtime only (renders the prototypes); not part of
  the design
