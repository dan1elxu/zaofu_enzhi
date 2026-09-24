import type { ChannelSummary } from "../../api/types";

const labels: Record<string, string> = {
  queued: "排队中",
  running: "评估中",
  retryable_failure: "等待重试",
  completed: "已完成",
  inconclusive: "无法确定（inconclusive）",
};

export function ChannelEvaluationStatus({ evaluation }: { evaluation: ChannelSummary["evaluation"] }) {
  const stages = evaluation?.stages ?? [];
  if (!stages.length) return null;
  return (
    <details className="channel-workspace-panel" data-testid="channel-evaluation-status">
      <summary>
        评估：{stages.map((row) => `${String(row.judge_type)} ${labels[String(row.status)] ?? String(row.status)}`).join(" · ")}
      </summary>
      <div className="channel-workspace-list compact">
        {stages.map((row) => (
          <div className="channel-workspace-row compact" key={String(row.job_id)}>
            <strong>{String(row.judge_type)}</strong>
            <span>{labels[String(row.status)] ?? String(row.status)}</span>
            {row.verdict ? <span>{String(row.verdict)}</span> : null}
            <span>尝试 {String(row.attempt ?? 0)}/{String(row.max_attempts ?? 2)}</span>
            {Number(row.slot_id) >= 0 ? <span>slot {String(row.slot_id)}</span> : null}
            {row.phase ? <span>{String(row.phase)}</span> : null}
            {row.error ? <span>{String(row.error)}</span> : null}
          </div>
        ))}
      </div>
    </details>
  );
}
