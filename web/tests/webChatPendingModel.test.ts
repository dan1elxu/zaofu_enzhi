import type { WebChatControlReceipt, WebChatQueueItem, WebChatTurn } from "../src/api/types.js";
import { deriveWebChatPendingRows } from "../src/components/webchat/webChatPendingModel.js";

function assert(condition: unknown, message: string): void {
  if (!condition) throw new Error(message);
}

function queue(queueId: string, state: string, createdAt: string): WebChatQueueItem {
  return {
    queue_id: queueId,
    idempotency_key: `${queueId}-key`,
    project_id: "project",
    conversation_id: "conversation",
    thread_id: "thread",
    message: `message ${queueId}`,
    request: {},
    state,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

const turn: WebChatTurn = {
  schema_version: "webchat-turn.v1",
  turn_id: "turn-1",
  project_id: "project",
  conversation_id: "conversation",
  thread_id: "thread",
  backend: "codex-headless",
  permission_profile: "workspace_writer",
  state: "running",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
};
const receipts: WebChatControlReceipt[] = [{
  receipt_id: "steer-1",
  idempotency_key: "steer-key",
  operation: "steer",
  turn_id: "turn-1",
  status: "queued_fallback",
  fallback: "queue",
  payload: { queue_id: "queue-1" },
  created_at: "2026-10-01T00:00:01Z",
}, {
  receipt_id: "interrupt-1",
  idempotency_key: "interrupt-key",
  operation: "interrupt",
  turn_id: "turn-1",
  status: "accepted",
  created_at: "2026-10-01T00:00:04Z",
}];

const rows = deriveWebChatPendingRows({
  queue: [
    queue("queue-1", "queued", "2026-10-01T00:00:01Z"),
    queue("queue-2", "cancelled", "2026-10-01T00:00:02Z"),
    queue("queue-3", "failed", "2026-10-01T00:00:03Z"),
    queue("queue-4", "completed", "2026-10-01T00:00:04Z"),
  ],
  receipts,
  turns: [turn],
});

assert(rows.length === 4, "completed queue entries stay out of pending controls");
assert(rows[0].label === "Steer queued", "fallback receipt is not presented as applied steer");
assert(rows.some((row) => row.label === "Removed from queue"), "cancelled queue remains understandable");
assert(rows.some((row) => row.label === "Queued message failed"), "failed queue remains understandable");
assert(rows.some((row) => row.kind === "interrupt"), "accepted interrupt remains visible while turn is active");

console.log("webChatPendingModel.test.ts OK");
