import {
  canonicalEvaluationStages,
  evaluationStatusLabel,
} from "../src/components/channel/channelEvaluationStatusModel.js";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const terminalLabels: Record<string, string> = {
  scored: "已评分",
  incomplete: "未完成",
  evidence_missing: "缺少证据",
  abstain: "弃权",
  conflict: "冲突",
  invalid: "无效",
};

for (const [status, label] of Object.entries(terminalLabels)) {
  assert(evaluationStatusLabel({ status }) === label, `${status} should be ${label}`);
}

assert(
  evaluationStatusLabel({ status: "", verdict: "scored" }) === "已评分",
  "verdict should provide a semantic fallback when status is empty",
);
assert(
  evaluationStatusLabel({ status: "provider_custom" }) === "provider_custom",
  "unknown status should remain visible for diagnosis",
);

const stages = canonicalEvaluationStages([
  { judge_type: "contribution", status: "scored" },
  { judge_type: "retired_stage", status: "completed" },
  { judge_type: "discussion", status: "incomplete" },
]);
assert(stages.length === 2, "retired evaluation stages must stay hidden");
assert(
  stages.every((row) => ["contribution", "discussion"].includes(String(row.judge_type))),
  "only canonical evaluation stages should remain",
);

console.log("channelEvaluationStatusModel tests passed");
