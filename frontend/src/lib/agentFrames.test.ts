import { describe, expect, it } from "vitest";
import {
  childItems,
  emptyTranscript,
  reduceFrame,
  rootItems,
  type Transcript,
} from "./agentFrames";

/** Fold a sequence of raw frames the way the channel hook does. */
function reduceAll(frames: Record<string, unknown>[]): Transcript {
  return frames.reduce(reduceFrame, emptyTranscript);
}

function textFrame(role: string, text: string, extra: Record<string, unknown> = {}) {
  return {
    type: "message_end",
    message: { role, content: [{ type: "text", text }] },
    ...extra,
  };
}

describe("reduceFrame", () => {
  it("renders assistant and user text in arrival order", () => {
    const t = reduceAll([textFrame("user", "fix it"), textFrame("assistant", "on it")]);
    const items = rootItems(t);
    expect(items.map((i) => (i.kind === "text" ? `${i.role}:${i.text}` : i.kind))).toEqual([
      "user:fix it",
      "assistant:on it",
    ]);
  });

  it("only completed messages count; message_start is skipped", () => {
    const t = reduceAll([
      { type: "message_start", message: { role: "assistant", content: [] } },
      textFrame("assistant", "hello"),
    ]);
    expect(rootItems(t)).toHaveLength(1);
  });

  it("turns a tool_use block into a running tool card, closed by its result", () => {
    const t = reduceAll([
      {
        type: "message_end",
        message: {
          role: "assistant",
          content: [{ type: "tool_use", id: "tool-1", name: "bash", input: { cmd: "ls" } }],
        },
      },
    ]);
    let tool = rootItems(t)[0];
    expect(tool.kind === "tool" && tool.name).toBe("bash");
    expect(tool.kind === "tool" && tool.done).toBe(false);
    expect(tool.kind === "tool" && tool.output).toBeNull();

    const closed = reduceFrame(t, {
      type: "message_end",
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: "tool-1", content: "a.txt\nb.txt" }],
      },
    });
    tool = rootItems(closed)[0];
    expect(tool.kind === "tool" && tool.done).toBe(true);
    expect(tool.kind === "tool" && tool.output).toBe("a.txt\nb.txt");
  });

  it("renders thinking blocks distinctly from text", () => {
    const t = reduceAll([
      {
        type: "message_end",
        message: {
          role: "assistant",
          content: [
            { type: "thinking", thinking: "let me plan" },
            { type: "text", text: "here goes" },
          ],
        },
      },
    ]);
    const kinds = rootItems(t).map((i) => i.kind);
    expect(kinds).toEqual(["thinking", "text"]);
  });

  it("groups a subagent's activity under its parent tool call", () => {
    const t = reduceAll([
      {
        type: "message_end",
        message: {
          role: "assistant",
          content: [{ type: "tool_use", id: "spawn-1", name: "task", input: {} }],
        },
      },
      // Subagent output carries the spawning tool's id as its parent.
      textFrame("assistant", "sub working", { parentToolUseId: "spawn-1" }),
    ]);
    // The subagent text is not at the top level…
    expect(rootItems(t)).toHaveLength(1);
    // …it is nested under the spawning tool call.
    const nested = childItems(t, "spawn-1");
    expect(nested).toHaveLength(1);
    expect(nested[0].kind === "text" && nested[0].text).toBe("sub working");
  });

  it("ignores frame types it does not render and keeps the same reference", () => {
    const base = reduceAll([textFrame("assistant", "hi")]);
    for (const type of ["agent_start", "agent_end", "turn_start", "ready", "note", "tool_output"]) {
      expect(reduceFrame(base, { type })).toBe(base);
    }
  });

  it("drops a tool_result with no matching tool card", () => {
    const t = reduceAll([
      {
        type: "message_end",
        message: {
          role: "user",
          content: [{ type: "tool_result", tool_use_id: "ghost", content: "x" }],
        },
      },
    ]);
    expect(rootItems(t)).toHaveLength(0);
  });
});
