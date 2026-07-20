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
 * model; the channel hook owns the live subscription. */

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
  return (
    <div className="panel transcriptPanel" data-testid="transcript">
      <h2 className="panelTitle">Transcript</h2>
      {items.length === 0 ? (
        <div className="transcriptEmpty">No activity yet.</div>
      ) : (
        <div className="transcriptStream">
          {items.map((item) => (
            <Item key={item.id} item={item} transcript={transcript} />
          ))}
        </div>
      )}
    </div>
  );
}
