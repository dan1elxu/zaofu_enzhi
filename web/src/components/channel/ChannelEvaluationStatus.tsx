import type { ChannelSummary } from "../../api/types";
import {
  canonicalEvaluationStages,
  evaluationStatusLabel,
} from "./channelEvaluationStatusModel";

export function ChannelEvaluationStatus({ evaluation }: { evaluation: ChannelSummary["evaluation"] }) {
  // Keep the UI fail-closed if a stale projection or an older API response
  // still contains retired judge types.
  const stages = canonicalEvaluationStages(evaluation?.stages);
  if (!stages.length) return null;
  return (
    <details className="channel-workspace-panel" data-testid="channel-evaluation-status">
      <summary>
        评估：{stages.map((row) => `${String(row.judge_type)} ${evaluationStatusLabel(row)}`).join(" · ")}
      </summary>
      <div className="channel-workspace-list compact">
        {stages.map((row) => (
          <div className="channel-workspace-row compact" key={String(row.job_id)}>
            <strong>{String(row.judge_type)}</strong>
            <span>{evaluationStatusLabel(row)}</span>
            {row.verdict && String(row.verdict) !== String(row.status) ? <span>{String(row.verdict)}</span> : null}
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
