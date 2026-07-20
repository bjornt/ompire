/* Interprets the omp `AgentSessionEvent` frame subset the transcript renders,
 * folding raw channel frames into a flat, ordered item list. Everything else
 * on the channel (agent_start/agent_end, turn_start/turn_end, ready/response,
 * extension_ui_request, agent_stderr, and anything omp invents later) is
 * ignored here — the daemon keeps forwarding it opaquely (agent-event-stream),
 * we just don't draw it (SPEC Decision 1).
 *
 * The reducer is pure and flat; nesting (subagents under their spawning tool
 * call, tool_result output onto its tool card) is expressed by a
 * `parentToolUseId` back-reference on each item and resolved at render time.
 * We read a message's full `content` blocks on `message_end` — the frame that
 * carries the completed message — rather than tracking per-token deltas. */

export interface AgentTextItem {
  kind: "text";
  id: string;
  role: string;
  text: string;
  /** Present when this text belongs to a subagent nested under a tool call. */
  parentToolUseId?: string;
}

export interface AgentThinkingItem {
  kind: "thinking";
  id: string;
  text: string;
  parentToolUseId?: string;
}

export interface AgentToolItem {
  kind: "tool";
  id: string;
  toolUseId: string;
  name: string;
  input: unknown;
  output: string | null;
  done: boolean;
  parentToolUseId?: string;
}

export type AgentItem = AgentTextItem | AgentThinkingItem | AgentToolItem;

export interface Transcript {
  items: AgentItem[];
  /** Monotonic id source for stable React keys across appends/updates. */
  nextId: number;
}

export const emptyTranscript: Transcript = { items: [], nextId: 0 };

type RawFrame = Record<string, unknown>;

interface ContentBlock {
  type?: string;
  text?: string;
  thinking?: string;
  id?: string;
  name?: string;
  input?: unknown;
  tool_use_id?: string;
  content?: unknown;
}

function contentBlocks(frame: RawFrame): ContentBlock[] {
  const message = frame.message as { content?: unknown } | undefined;
  const content = message?.content;
  return Array.isArray(content) ? (content as ContentBlock[]) : [];
}

function messageRole(frame: RawFrame): string {
  const message = frame.message as { role?: unknown } | undefined;
  return typeof message?.role === "string" ? message.role : "assistant";
}

function stringifyToolResult(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((block) =>
        block && typeof block === "object" && typeof (block as ContentBlock).text === "string"
          ? (block as ContentBlock).text
          : JSON.stringify(block),
      )
      .join("\n");
  }
  return content === undefined ? "" : JSON.stringify(content);
}

/** Fold one raw channel frame into the transcript. Pure: returns the same
 * reference when the frame contributes nothing renderable. */
export function reduceFrame(transcript: Transcript, frame: RawFrame): Transcript {
  // Only completed messages carry the full content we render; `message_start`
  // (empty for a fresh assistant turn) is skipped to avoid double-rendering.
  if (frame.type !== "message_end") return transcript;

  const blocks = contentBlocks(frame);
  if (blocks.length === 0) return transcript;

  const role = messageRole(frame);
  const parentToolUseId =
    typeof frame.parentToolUseId === "string" ? frame.parentToolUseId : undefined;

  let { items, nextId } = transcript;
  let mutated = false;
  const append = (item: AgentItem) => {
    if (!mutated) {
      items = [...items];
      mutated = true;
    }
    items.push(item);
    nextId += 1;
  };

  for (const block of blocks) {
    if (block.type === "text" && typeof block.text === "string") {
      append({ kind: "text", id: String(nextId), role, text: block.text, parentToolUseId });
    } else if (
      (block.type === "thinking" || block.type === "redacted_thinking") &&
      typeof (block.thinking ?? block.text) === "string"
    ) {
      append({
        kind: "thinking",
        id: String(nextId),
        text: (block.thinking ?? block.text) as string,
        parentToolUseId,
      });
    } else if (block.type === "tool_use" && typeof block.id === "string") {
      append({
        kind: "tool",
        id: String(nextId),
        toolUseId: block.id,
        name: typeof block.name === "string" ? block.name : "tool",
        input: block.input,
        output: null,
        done: false,
        parentToolUseId,
      });
    } else if (block.type === "tool_result" && typeof block.tool_use_id === "string") {
      // Attach output to the tool card it answers; a result without a known
      // tool card is dropped rather than rendered orphaned.
      const idx = items.findIndex(
        (item) => item.kind === "tool" && item.toolUseId === block.tool_use_id,
      );
      if (idx !== -1) {
        if (!mutated) {
          items = [...items];
          mutated = true;
        }
        const tool = items[idx] as AgentToolItem;
        items[idx] = { ...tool, output: stringifyToolResult(block.content), done: true };
      }
    }
  }

  return mutated ? { items, nextId } : transcript;
}

/** Top-level items (not nested under a tool call), in arrival order. */
export function rootItems(transcript: Transcript): AgentItem[] {
  return transcript.items.filter((item) => item.parentToolUseId === undefined);
}

/** Items produced by a subagent running under the given tool call. */
export function childItems(transcript: Transcript, toolUseId: string): AgentItem[] {
  return transcript.items.filter((item) => item.parentToolUseId === toolUseId);
}
