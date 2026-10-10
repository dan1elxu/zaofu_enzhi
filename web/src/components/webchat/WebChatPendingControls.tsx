import { Clock3, Loader2, Pencil, X, ZapOff } from "lucide-react";
import type {
  WebChatControlReceipt,
  WebChatQueueItem,
  WebChatTurn,
} from "../../api/types";
import { deriveWebChatPendingRows } from "./webChatPendingModel";

export function WebChatPendingControls({
  busyId = "",
  composerHasDraft,
  queue,
  receipts,
  turns,
  onCancel,
  onRestore,
}: {
  busyId?: string;
  composerHasDraft: boolean;
  queue: WebChatQueueItem[];
  receipts: WebChatControlReceipt[];
  turns: WebChatTurn[];
  onCancel: (item: WebChatQueueItem) => void;
  onRestore: (item: WebChatQueueItem) => void;
}) {
  const rows = deriveWebChatPendingRows({ queue, receipts, turns });
  if (!rows.length) return null;

  return (
    <section
      aria-label="Pending messages and controls"
      aria-live="polite"
      className="webchat-pending-controls"
      data-testid="webchat-pending-controls"
    >
      {rows.map((row) => {
        const claimed = row.queue?.state === "claimed";
        const queued = row.queue?.state === "queued";
        const busy = busyId === row.id;
        return (
          <div className={`webchat-pending-row ${row.kind}`} key={row.id}>
            <span className="webchat-pending-icon" aria-hidden="true">
              {row.kind === "interrupt"
                ? <ZapOff size={15} />
                : claimed || busy ? <Loader2 className="agent-tool-spin" size={15} /> : <Clock3 size={15} />}
            </span>
            <span className="webchat-pending-copy">
              <strong>{row.label}</strong>
              <span title={row.message}>{row.message}</span>
            </span>
            <small>{row.state.replaceAll("_", " ")}</small>
            {row.queue ? (
              <span className="webchat-pending-actions">
                <button
                  aria-label="Edit queued message"
                  disabled={busy || claimed || composerHasDraft}
                  title={composerHasDraft ? "Clear the current draft before restoring this message" : "Edit queued message"}
                  type="button"
                  onClick={() => onRestore(row.queue as WebChatQueueItem)}
                >
                  <Pencil size={14} />
                </button>
                {queued ? (
                  <button
                    aria-label="Remove queued message"
                    disabled={busy}
                    title="Remove queued message"
                    type="button"
                    onClick={() => onCancel(row.queue as WebChatQueueItem)}
                  >
                    <X size={15} />
                  </button>
                ) : null}
              </span>
            ) : null}
          </div>
        );
      })}
    </section>
  );
}
