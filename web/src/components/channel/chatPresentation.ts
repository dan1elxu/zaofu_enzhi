import type { AgentConversation, AgentPartKind } from "../agent-session/types";

// Settled-run tool/thinking trails are already omitted by the chat renderer.
// They remain in the source projection and diagnostics, not repeated messages.
const ACTIVITY_PART_KINDS = new Set<AgentPartKind>([
  "error", "status", "thinking", "tool", "tool_call", "tool_result",
  "command", "file_read", "context_ledger",
]);

/** Only empty failures move to Activity. Partial replies, artifacts, and
 * questions stay with their author; the original projection is untouched. */
export function channelChatConversation(
  conversation: AgentConversation,
  questionRunIds: ReadonlySet<string>,
): AgentConversation {
  return {
    ...conversation,
    threads: conversation.threads.map((thread) => ({
      ...thread,
      turns: thread.turns.map((turn) => ({
        ...turn,
        runs: turn.runs.filter((run) => (
          run.status !== "failed"
          || questionRunIds.has(run.id)
          || Boolean(run.proposal)
          || turn.cards.some((card) => card.runId === run.id)
          || run.parts.some((part) => !ACTIVITY_PART_KINDS.has(part.kind))
        )),
      })),
    })),
  };
}
