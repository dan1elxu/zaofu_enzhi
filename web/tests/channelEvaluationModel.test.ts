import {
  evaluationStageLabel,
  evaluationDimensionLabel,
  evaluationPhaseLabel,
  evaluationScore,
  evaluationTaskTypeLabel,
  evaluationResultUnavailable,
  evaluationStatusLabel,
  necessityLabel,
  orderedEvaluationStages,
  stageTone,
} from "../src/components/channel/channelEvaluationModel.js";

function assert(value: unknown, message: string): asserts value {
  if (!value) throw new Error(message);
}

const stages = orderedEvaluationStages([
  { judge_type: "conclusion" }, { judge_type: "discussion" }, { judge_type: "task_type" },
]);
assert(stages.map((stage) => stage.judge_type).join(",") === "task_type,discussion,conclusion", "stages use the fixed evaluation order");
assert(evaluationStageLabel({ judge_type: "prd_fidelity" }) === "PRD 忠实度", "stage label is presentable");
assert(evaluationDimensionLabel("information_exchange") === "信息交换", "dimension label is Chinese");
assert(evaluationDimensionLabel("original_to_channel_prd") === "原始需求到 Channel PRD", "PRD dimension label is Chinese");
assert(evaluationPhaseLabel("reasoning") === "分析中", "phase label is Chinese");
assert(evaluationTaskTypeLabel("bugfix") === "修复缺陷", "task type is Chinese");
assert(evaluationScore(0, "scored") === "0/4" && evaluationScore(4, "scored") === "4/4", "zero and max scores are visible");
for (const value of [null, undefined, false, "", 1.5, Number.NaN, 5]) assert(evaluationScore(value, "scored") === "—", `invalid score ${String(value)} is hidden`);
assert(evaluationScore(3, "running") === "—", "running score is hidden");
assert(evaluationResultUnavailable({ judge_type: "discussion", status: "complete", result_available: false }), "finished missing result is explicit");
assert(evaluationStatusLabel("evidence_missing") === "缺少证据", "semantic status is translated");
assert(necessityLabel("not_needed") === "不需要", "necessity remains distinct");
assert(stageTone("scored") === "complete" && stageTone("inconclusive") === "warning", "status colors distinguish outcome");
console.log("channelEvaluationModel tests passed");
