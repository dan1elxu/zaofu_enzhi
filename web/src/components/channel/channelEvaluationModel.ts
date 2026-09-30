import type { ChannelEvaluationStage } from "../../api/types";

export const EVALUATION_STAGE_ORDER = [
  "task_type", "contribution", "discussion", "prd_fidelity", "conclusion",
] as const;

const STAGE_LABELS: Record<string, string> = {
  task_type: "任务类型",
  contribution: "Agent 贡献",
  discussion: "讨论有效性",
  prd_fidelity: "PRD 忠实度",
  conclusion: "汇总结论",
};
const DIMENSION_LABELS: Record<string, string> = {
  information_exchange: "信息交换", clarity: "表达清晰度", task_assistance: "任务协助",
  efficiency: "讨论效率", necessity: "讨论必要性", original_to_channel_prd: "原始需求到 Channel PRD",
  channel_to_workflow_prd: "Channel PRD 到工作流 PRD", requirement_coverage: "需求覆盖",
  requirement_accuracy: "需求准确性", scope_alignment: "范围对齐", acceptance_criteria: "验收标准",
};
const PHASE_LABELS: Record<string, string> = {
  assistant: "生成回复", reasoning: "分析中", tool: "调用工具", tool_result: "处理工具结果",
};
const TASK_TYPE_LABELS: Record<string, string> = {
  bugfix: "修复缺陷", refactor: "重构", greenfield: "从零开发",
};

const STATUS_LABELS: Record<string, string> = {
  pending: "等待中",
  queued: "排队中",
  running: "评估中",
  retryable_failure: "等待重试",
  scored: "已评分",
  complete: "已完成",
  completed: "已完成",
  incomplete: "未完成",
  evidence_missing: "缺少证据",
  inconclusive: "无法确定",
  invalid: "无效",
  ambiguous: "类型不明确",
  abstain: "弃权",
  conflict: "冲突",
  judge_timeout: "评估超时",
  unavailable: "暂无评测",
};

export function orderedEvaluationStages(stages: readonly ChannelEvaluationStage[]): ChannelEvaluationStage[] {
  return [...stages].sort((left, right) => (
    EVALUATION_STAGE_ORDER.indexOf(left.judge_type as typeof EVALUATION_STAGE_ORDER[number])
    - EVALUATION_STAGE_ORDER.indexOf(right.judge_type as typeof EVALUATION_STAGE_ORDER[number])
  ));
}

export function evaluationStageLabel(stage: ChannelEvaluationStage): string {
  return STAGE_LABELS[stage.judge_type] || stage.label || stage.judge_type;
}

export function evaluationDimensionLabel(value: unknown): string {
  const key = String(value ?? "");
  return DIMENSION_LABELS[key] || key || "未命名维度";
}

export function evaluationPhaseLabel(value: unknown): string {
  const key = String(value ?? "");
  return PHASE_LABELS[key] || key;
}

export function evaluationTaskTypeLabel(value: unknown): string {
  const key = String(value ?? "");
  return TASK_TYPE_LABELS[key] || key || "未分类";
}

export function evaluationScore(value: unknown, status: unknown): string {
  if (String(status ?? "") !== "scored") return "—";
  if (typeof value !== "number" || !Number.isInteger(value) || !Number.isFinite(value)) return "—";
  return value >= 0 && value <= 4 ? `${value}/4` : "—";
}

export function evaluationStatusLabel(status: unknown): string {
  const value = String(status ?? "");
  return STATUS_LABELS[value] || value || "等待中";
}

export function necessityLabel(value: unknown): string {
  return ({ must: "必须", optional: "可选", not_needed: "不需要", undetermined: "未确定" } as Record<string, string>)[String(value ?? "")]
    || "未确定";
}

export function stageTone(status: unknown): "active" | "complete" | "warning" | "idle" {
  const value = String(status ?? "");
  if (["running", "retryable_failure"].includes(value)) return "active";
  if (["scored", "complete", "completed"].includes(value)) return "complete";
  if (["incomplete", "evidence_missing", "inconclusive", "invalid", "ambiguous", "abstain", "conflict", "judge_timeout"].includes(value)) return "warning";
  return "idle";
}

export function evaluationResultUnavailable(stage: ChannelEvaluationStage | undefined): boolean {
  if (!stage || stage.result_available !== false) return false;
  return ["scored", "complete", "completed", "incomplete", "evidence_missing", "inconclusive", "invalid", "abstain", "conflict"].includes(String(stage.status || stage.verdict || ""));
}
