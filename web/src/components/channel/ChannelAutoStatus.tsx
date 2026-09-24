import { useState } from "react";
import type { ChannelSummary } from "../../api/types";

const labels: Record<string, string> = {
  queued: "等待执行", running: "执行中", retrying: "失败，正在自动重试",
  interrupted: "调用已中断，等待恢复", failed: "失败，需要重试",
};

export function ChannelAutoStatus({ rows, actionReady, onRetry }: {
  rows: ChannelSummary["auto_chain"];
  actionReady: boolean;
  onRetry: (threadId: string, purpose: string, operationId: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const visible = (rows ?? []).filter((row) => row.status !== "completed");
  if (!visible.length) return null;
  return (
    <section aria-label="自动主持人状态" aria-live="polite" className="channel-workspace-panel"
      data-testid="channel-auto-status">
      {visible.map((row) => (
        <div className="channel-workspace-row compact" key={row.operation_id}>
          <strong>自动主持人 · {row.purpose === "resolve" ? "自动答题" : "PRD 交接"}</strong>
          <span>{labels[row.status] ?? row.status} · 尝试 {row.attempt}/{row.max_attempts}</span>
          {row.thread_id !== "main" ? <span>{row.thread_id}</span> : null}
          {row.reason ? <span>{row.reason}</span> : null}
          {row.status === "failed" ? (
            <button type="button" disabled={!actionReady || !!busy} onClick={async () => {
              setBusy(row.operation_id);
              setError("");
              try { await onRetry(row.thread_id, row.purpose, row.operation_id); }
              catch (err) { setError(err instanceof Error ? err.message : String(err)); }
              finally { setBusy(""); }
            }}>{busy === row.operation_id ? "正在提交…" : "重试"}</button>
          ) : null}
        </div>
      ))}
      {error ? <div role="alert">{error}</div> : null}
    </section>
  );
}
