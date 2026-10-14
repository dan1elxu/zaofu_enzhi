import type {
  EvalCrossCompareGap,
  EvalLaneChecklist,
  EvalLaneStatus,
  EvalLaneScore,
  EvalRunGroup,
  EvalRunTrialSummary,
} from "../../api/types";
import type { UiTone } from "../../app/sharedTypes";

export const EVAL_LANE_IDS = ["terminal", "kanban", "channel"] as const;

const LANE_LABELS: Record<string, string> = {
  terminal: "Terminal",
  kanban: "Kanban",
  channel: "Channel Auto",
};

const LANE_STATUS_LABELS: Record<string, string> = {
  pending: "等待中",
  queued: "排队中",
  running: "运行中",
  prd_frozen: "PRD 已冻结",
  scored: "已评分",
  complete: "已完成",
  completed: "已完成",
  failed: "失败",
  absent: "未参与",
  evidence_missing: "缺少证据",
  inconclusive: "无法确定",
  invalid: "无效",
  judge_timeout: "评估超时",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  running: "运行中",
  completed: "已完成",
};

const DIMENSION_ORDER = ["coverage", "fidelity", "actionability", "structure"] as const;

const DIMENSION_LABELS: Record<string, string> = {
  coverage: "需求覆盖",
  fidelity: "需求忠实",
  actionability: "可执行性",
  structure: "完整性结构",
};

const TASK_TYPE_LABELS: Record<string, string> = {
  bugfix: "修复缺陷",
  refactor: "重构",
  greenfield: "从零开发",
  ambiguous: "类型不明确",
};

export function evalLaneLabel(lane: unknown): string {
  const key = String(lane ?? "");
  return LANE_LABELS[key] || key || "未知 lane";
}

export function evalLaneStatusLabel(status: unknown, error?: unknown): string {
  const value = String(status ?? "");
  if ((value === "failed" || value === "timeout") && String(error ?? "") === "timeout") {
    return "超时";
  }
  return LANE_STATUS_LABELS[value] || value || "等待中";
}

export function evalRunStatusLabel(status: unknown): string {
  const value = String(status ?? "");
  return RUN_STATUS_LABELS[value] || value || "排队中";
}

export function evalLaneTone(status: unknown): UiTone {
  const value = String(status ?? "");
  if (value === "prd_frozen" || value === "completed" || value === "scored" || value === "complete") {
    return "ok";
  }
  if (value === "running") return "info";
  if (value === "failed" || value === "timeout") return "err";
  if (["evidence_missing", "inconclusive", "invalid", "judge_timeout"].includes(value)) return "warn";
  return "muted";
}

export function evalRunTone(status: unknown): UiTone {
  const value = String(status ?? "");
  if (value === "completed") return "ok";
  if (value === "running") return "info";
  return "muted";
}

export function evalDimensionLabel(dimensionId: unknown): string {
  const key = String(dimensionId ?? "");
  return DIMENSION_LABELS[key] || key || "未命名维度";
}

export function evalTaskTypeLabel(value: unknown): string {
  const key = String(value ?? "");
  return TASK_TYPE_LABELS[key] || key || "未分类";
}

export function formatEvalScore(value: unknown): string {
  // Scores live on the 0–4 judge scale; an out-of-range or non-numeric value
  // must not render as a misleading number (mirrors channelEvaluationModel).
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value < 0 || value > 4) return "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

export function formatEvalConfidence(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  return value.toFixed(2);
}

export function formatEvalTimestamp(value: unknown): string {
  const raw = String(value ?? "");
  const time = Date.parse(raw);
  if (!Number.isFinite(time)) return "";
  const date = new Date(time);
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getMonth() + 1}-${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function shortEvalDigest(digest: unknown): string {
  const value = String(digest ?? "");
  return value.length > 12 ? value.slice(0, 12) : value;
}

export function orderedEvalLanes<T>(
  lanes: Record<string, T> | null | undefined,
): Array<{ lane: string; state: T }> {
  const source = lanes ?? {};
  const ordered: Array<{ lane: string; state: T }> = [];
  for (const lane of EVAL_LANE_IDS) {
    if (lane in source) ordered.push({ lane, state: source[lane] });
  }
  for (const lane of Object.keys(source).sort()) {
    if (!(EVAL_LANE_IDS as readonly string[]).includes(lane)) {
      ordered.push({ lane, state: source[lane] });
    }
  }
  return ordered;
}

export function sortEvalTrials(trials: readonly EvalRunTrialSummary[]): EvalRunTrialSummary[] {
  return [...trials].sort((left, right) => right.trial - left.trial);
}

export function sortEvalGroups(groups: readonly EvalRunGroup[]): EvalRunGroup[] {
  return [...groups].sort((left, right) => {
    const delta = groupNewestTimestamp(right) - groupNewestTimestamp(left);
    return delta || left.prompt_digest.localeCompare(right.prompt_digest);
  });
}

export interface EvalLaneAggregate {
  lane: string;
  count: number;
  min: number;
  max: number;
  mean: number;
}

export function groupLaneAggregates(group: EvalRunGroup): EvalLaneAggregate[] {
  const buckets = new Map<string, number[]>();
  for (const trial of group.trials ?? []) {
    for (const { lane, state } of orderedEvalLanes(trial.composite_scores ?? {})) {
      if (typeof state !== "number" || !Number.isFinite(state)) continue;
      const bucket = buckets.get(lane) ?? [];
      bucket.push(state);
      buckets.set(lane, bucket);
    }
  }
  return [...buckets.entries()].map(([lane, values]) => ({
    lane,
    count: values.length,
    min: Math.min(...values),
    max: Math.max(...values),
    mean: values.reduce((total, value) => total + value, 0) / values.length,
  }));
}

export function evalScoreDimensions(
  laneScores: Record<string, EvalLaneScore | null> | null | undefined,
): string[] {
  const seen = new Set<string>();
  for (const score of Object.values(laneScores ?? {})) {
    for (const key of Object.keys(score?.dimensions ?? {})) seen.add(key);
  }
  const ordered = DIMENSION_ORDER.filter((key) => seen.has(key)) as string[];
  const extras = [...seen].filter((key) => !(DIMENSION_ORDER as readonly string[]).includes(key)).sort();
  return [...ordered, ...extras];
}

export function revealCandidateLabel(label: unknown, labelMap?: Record<string, string>): string {
  const key = String(label ?? "");
  const lane = labelMap?.[key];
  return lane ? evalLaneLabel(lane) : key;
}

const RANKING_CONFIDENCE_LABELS: Record<number, string> = {
  4: "极高",
  3: "高",
  2: "中",
  1: "低",
  0: "无法比较",
};

export function evalRankingConfidenceLabel(value: unknown): string {
  // Dimension-ranking confidence is a 0–4 judge scale (may arrive as a
  // string); anything unparsable or out of range renders as "no label".
  const numeric =
    typeof value === "number" ? value : typeof value === "string" ? Number(value.trim()) : Number.NaN;
  if (!Number.isFinite(numeric)) return "";
  return RANKING_CONFIDENCE_LABELS[Math.round(numeric)] ?? "";
}

export function formatEvalChecklist(checklist: EvalLaneChecklist | null | undefined): string {
  if (!checklist) return "";
  const { total, pass, partial, missing } = checklist;
  const counts = [total, pass, partial, missing];
  if (!counts.every((value) => typeof value === "number" && Number.isFinite(value) && value >= 0)) {
    return "";
  }
  if (total <= 0) return "";
  const parts = [`需求清单 ${pass}/${total} 通过`];
  if (partial > 0) parts.push(`部分 ${partial}`);
  if (missing > 0) parts.push(`缺失 ${missing}`);
  return parts.join(" · ");
}

export interface EvalKeyGapGroup {
  lane: string;
  title: string;
  blindLabel: string;
  gaps: EvalCrossCompareGap[];
}

export function groupEvalKeyGaps(
  gaps: readonly EvalCrossCompareGap[] | null | undefined,
  labelMap?: Record<string, string>,
): EvalKeyGapGroup[] {
  const byLane = new Map<string, EvalKeyGapGroup>();
  for (const gap of gaps ?? []) {
    const candidate = String(gap?.candidate ?? "");
    const mapped = labelMap?.[candidate];
    const lane = mapped || candidate || "unknown";
    let group = byLane.get(lane);
    if (!group) {
      group = {
        lane,
        title: mapped ? evalLaneLabel(mapped) : candidate || "未知 lane",
        blindLabel: mapped ? candidate : "",
        gaps: [],
      };
      byLane.set(lane, group);
    }
    group.gaps.push(gap);
  }
  const rank = (lane: string) => {
    const index = (EVAL_LANE_IDS as readonly string[]).indexOf(lane);
    return index === -1 ? EVAL_LANE_IDS.length : index;
  };
  // Stable sort: canonical lane order first, unmapped lanes in first-seen order.
  return [...byLane.values()].sort((left, right) => rank(left.lane) - rank(right.lane));
}

export function laneStatusOf(
  lanes: Record<string, EvalLaneStatus> | null | undefined,
  lane: string,
): EvalLaneStatus {
  return lanes?.[lane] ?? { status: "pending", error: "" };
}

export function readInitialEvalRunId(): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get("run_id") ?? "";
}

export function writeEvalRunSelection(runId: string): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  params.set("page", "eval");
  if (runId.trim()) params.set("run_id", runId.trim());
  else params.delete("run_id");
  window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}${window.location.hash}`);
}

function groupNewestTimestamp(group: EvalRunGroup): number {
  let newest = 0;
  for (const trial of group.trials ?? []) {
    const time = Date.parse(trial.started_at || "");
    if (Number.isFinite(time) && time > newest) newest = time;
  }
  return newest;
}
