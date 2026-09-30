export type ChannelEvaluationStage = Record<string, unknown>;

export const EVALUATION_STATUS_LABELS: Readonly<Record<string, string>> = {
  queued: "排队中",
  running: "评估中",
  retryable_failure: "等待重试",
  completed: "已完成",
  complete: "已完成",
  scored: "已评分",
  incomplete: "未完成",
  ambiguous: "类型不明确",
  evidence_missing: "缺少证据",
  abstain: "弃权",
  conflict: "冲突",
  invalid: "无效",
  judge_timeout: "评估超时",
  inconclusive: "无法确定（inconclusive）",
};

export const CANONICAL_EVALUATION_STAGES = new Set([
  "task_type",
  "contribution",
  "discussion",
  "prd_fidelity",
  "conclusion",
]);

export function canonicalEvaluationStages(
  stages: readonly ChannelEvaluationStage[] | null | undefined,
): ChannelEvaluationStage[] {
  return (stages ?? []).filter((row) => (
    CANONICAL_EVALUATION_STAGES.has(String(row.judge_type))
  ));
}

export function evaluationStatusLabel(row: ChannelEvaluationStage): string {
  const status = String(row.status ?? "");
  const verdict = String(row.verdict ?? "");
  return EVALUATION_STATUS_LABELS[status] ?? EVALUATION_STATUS_LABELS[verdict] ?? status;
}
