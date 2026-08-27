import { useLayoutEffect, useRef } from "react";
import {
  childItems,
  rootItems,
  type AgentItem,
  type AgentToolItem,
  type Transcript,
} from "../lib/agentFrames";

/* Renders the interpreted transcript: assistant/user text, thinking blocks,
 * and collapsible tool cards. Subagent activity nests inside the tool card that
 * spawned it (SPEC Decision 1). Rendering is a pure function of the reducer
 * model; the channel hook owns the live subscription.
 *
 * The panel is bounded (TaskDetailView.css) and follows the live stream: it
 * starts pinned to the newest output and stays pinned while the reader is
 * watching the bottom, but scrolling away suspends following so streaming
 * output cannot pull the reader off what they are reading. A stream that
 * starts over — reconnect replay, a different session tab, a different task —
 * starts pinned again. */

/** Distance from the end, in px, that still counts as watching the live
 * output. Forgiving on purpose: a stray wheel tick should not silently strand
 * the operator behind a streaming agent. */
const FOLLOW_THRESHOLD_PX = 64;

/** Instant, never animated. A smooth scroll per streamed frame would be motion
 * the operator did not ask for, so there is no animated path to suppress under
 * `prefers-reduced-motion`. */
function scrollToEnd(el: HTMLElement | null): void {
  if (el !== null) el.scrollTop = el.scrollHeight;
}

function ToolCard({ tool, transcript }: { tool: AgentToolItem; transcript: Transcript }) {
  const children = childItems(transcript, tool.toolUseId);
  return (
    <details className="toolCard" data-testid={`tool-card-${tool.toolUseId}`}>
      <summary>
        <span className={`toolDot ${tool.done ? "done" : "running"}`} />
        <span className="toolName">{tool.name}</span>
        {!tool.done && <span className="toolRunning">running…</span>}
      </summary>
      {tool.input !== undefined && (
        <pre className="toolInput">{prettyInput(tool.input)}</pre>
      )}
      {tool.output !== null && <pre className="toolOutput">{tool.output}</pre>}
      {children.length > 0 && (
        <div className="subagent" data-testid={`subagent-${tool.toolUseId}`}>
          {children.map((item) => (
            <Item key={item.id} item={item} transcript={transcript} />
          ))}
        </div>
      )}
    </details>
  );
}

function Item({ item, transcript }: { item: AgentItem; transcript: Transcript }) {
  if (item.kind === "tool") return <ToolCard tool={item} transcript={transcript} />;
  if (item.kind === "thinking") {
    return (
      <div className="thinkingBlock" data-testid="thinking-block">
        {item.text}
      </div>
    );
  }
  return (
    <div className={`messageBlock ${item.role}`} data-role={item.role}>
      {item.text}
    </div>
  );
}

function prettyInput(input: unknown): string {
  try {
    return typeof input === "string" ? input : JSON.stringify(input, null, 2);
  } catch {
    return String(input);
  }
}

export function TaskTranscript({ transcript }: { transcript: Transcript }) {
  const items = rootItems(transcript);
  const streamRef = useRef<HTMLDivElement>(null);
  // Follow state is a ref, not state: it changes on every scroll event and
  // nothing renders from it, so making it state would re-render the whole
  // transcript per wheel tick.
  const pinned = useRef(true);

  // Keyed on the transcript object, not `items.length`: `reduceFrame` returns a
  // new object exactly when a frame contributed something renderable, which
  // includes `tool_result` output attaching to an existing tool card — growth
  // that adds no root item and would otherwise scroll out of view. Layout
  // effect, so the correction lands before paint instead of flashing.
  useLayoutEffect(() => {
    // An empty transcript means the stream element was torn down and a fresh
    // one is about to arrive: `useAgentChannel` resets to `emptyTranscript` on
    // every (re)connect and whenever the task or selected session changes, then
    // replays the buffer from the top. Either way the reader's scroll position
    // died with the element, so the new stream starts pinned — one rule for
    // reconnect, tab switch, and task switch alike. Without it, a reader who
    // had scrolled up is left sitting at the beginning of the replayed history.
    if (items.length === 0) pinned.current = true;
    if (pinned.current) scrollToEnd(streamRef.current);
  }, [transcript, items.length]);

  // One rule covers both suspending and resuming, and needs no scroll-direction
  // tracking. Our own scroll-to-end leaves the distance at 0, so a programmatic
  // scroll re-affirms the pin rather than fighting it; content arriving while
  // suspended fires no scroll event, so the suspension holds.
  function onScroll(): void {
    const el = streamRef.current;
    if (el === null) return;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_THRESHOLD_PX;
  }

  return (
    <div className="panel transcriptPanel" data-testid="transcript">
      <h2 className="panelTitle">Transcript</h2>
      {items.length === 0 ? (
        <div className="transcriptEmpty">No activity yet.</div>
      ) : (
        <div
          className="transcriptStream"
          ref={streamRef}
          onScroll={onScroll}
          // Keyboard-reachable scroll region with a name of its own (the h2
          // names the panel, not the stream). Deliberately not `aria-live`:
          // announcing every streamed frame would flood a screen reader.
          role="region"
          aria-label="Transcript stream"
          tabIndex={0}
          data-testid="transcript-stream"
        >
          {items.map((item) => (
            <Item key={item.id} item={item} transcript={transcript} />
          ))}
        </div>
      )}
    </div>
  );
}
