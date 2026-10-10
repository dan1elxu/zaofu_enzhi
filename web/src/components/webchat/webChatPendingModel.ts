import type {
  WebChatControlReceipt,
  WebChatQueueItem,
  WebChatTurn,
} from "../../api/types";

export interface WebChatPendingRow {
  id: string;
  kind: "interrupt" | "queue";
  label: string;
  message: string;
  state: string;
  createdAt: string;
  queue?: WebChatQueueItem;
  receipt?: WebChatControlReceipt;
}

const INTERRUPT_VISIBLE_STATES = new Set([
  "claimed",
  "running",
  "resume_queued",
  "retry_wait",
  "submitted",
]);

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function deriveWebChatPendingRows({
  queue,
  receipts,
  turns,
}: {
  queue: WebChatQueueItem[];
  receipts: WebChatControlReceipt[];
  turns: WebChatTurn[];
}): WebChatPendingRow[] {
  const turnStates = new Map(turns.map((turn) => [turn.turn_id, turn.state]));
  const fallbackQueueIds = new Set(
    receipts
      .filter((receipt) => receipt.operation === "steer" && receipt.fallback === "queue")
      .map((receipt) => text(receipt.payload?.queue_id))
      .filter(Boolean),
  );
  const rows: WebChatPendingRow[] = queue.filter((item) => item.state !== "completed").map((item) => {
    const fallback = fallbackQueueIds.has(item.queue_id)
      || text(item.request?.fallback_from) === "steer";
    return {
      id: item.queue_id,
      kind: "queue",
      label: item.state === "claimed"
        ? "Starting next turn"
        : item.state === "failed"
          ? "Queued message failed"
          : item.state === "cancelled"
            ? "Removed from queue"
            : fallback ? "Steer queued" : "Queued for next turn",
      message: item.message,
      state: item.state,
      createdAt: item.created_at,
      queue: item,
    };
  });
  for (const receipt of receipts) {
    if (receipt.operation !== "interrupt" || receipt.status !== "accepted") continue;
    const state = turnStates.get(receipt.turn_id) ?? "";
    if (!INTERRUPT_VISIBLE_STATES.has(state)) continue;
    rows.push({
      id: receipt.receipt_id,
      kind: "interrupt",
      label: "Interrupting and resuming",
      message: "The active attempt is stopping at a safe boundary.",
      state,
      createdAt: receipt.created_at,
      receipt,
    });
  }
  return rows.sort((left, right) => left.createdAt.localeCompare(right.createdAt));
}
