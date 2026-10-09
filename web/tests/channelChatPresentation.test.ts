import { channelChatConversation } from "../src/components/channel/chatPresentation.js";
import type { AgentConversation, AgentSessionRun } from "../src/components/agent-session/types.js";

function assert(condition: unknown, message: string): void {
  if (!condition) throw new Error(message);
}

const failed = (id: string): AgentSessionRun => ({
  id, threadId: "main", memberId: id, status: "failed",
  parts: [{ id: `${id}-error`, runId: id, kind: "error", state: "failed", title: "Failure" }],
});
const partial = failed("partial");
partial.parts.unshift({ id: "partial-text", runId: partial.id, kind: "text", state: "completed", title: "Response", content: "Keep the whole partial reply." });
const artifact = failed("artifact");
artifact.parts.push({ id: "artifact-ref", runId: artifact.id, kind: "artifact_preview", state: "completed", title: "Evidence", contentRef: "evidence.md" });
const toolFailure = failed("tool-failure");
toolFailure.parts.push({ id: "tool", runId: toolFailure.id, kind: "tool_result", state: "completed", title: "Read requirement", content: "Tool trail remains in diagnostics." });
const source: AgentConversation = {
  id: "channel", activeThreadId: "main", surface: "channel_group",
  threads: [{ id: "main", title: "Discussion", status: "failed", turns: [{
    id: "turn", threadId: "main",
    runs: [failed("empty"), toolFailure, partial, artifact, failed("question"), failed("contribution")],
    cards: [{ id: "finding", kind: "contribution", title: "Finding", runId: "contribution" }],
  }] }],
};
const original = JSON.stringify(source);
const chat = channelChatConversation(source, new Set(["question"]));
assert(chat.threads[0].turns[0].runs.map((run) => run.id).join(",") === "partial,artifact,question,contribution", "Only empty failures may be moved out of chat");
assert(JSON.stringify(source) === original, "Activity must retain the complete unmodified source projection");
assert(chat.threads[0].turns[0].runs[0].parts[0].content === "Keep the whole partial reply.", "Failure aggregation must not truncate content");

console.log("channelChatPresentation tests passed");
