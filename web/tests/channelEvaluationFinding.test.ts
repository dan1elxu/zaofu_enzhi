import { channelEvaluationFinding } from "../src/components/channel/channelEvaluationFinding.js";

function assert(condition: unknown): void {
  if (!condition) throw new Error("finding display assertion failed");
}

  for (const field of ["finding", "reason", "message", "description", "title"]) {
    assert(JSON.stringify(channelEvaluationFinding({ [field]: " 漏了失败回滚 ", severity: "high",
      criterion_id: "C45", source_location: "SOURCE:L4", target_location: "PRD:验收" })) === JSON.stringify({
      body: "漏了失败回滚", severity: "high", criterion: "C45", source: "SOURCE:L4", target: "PRD:验收",
    }));
  }

assert(channelEvaluationFinding({ finding: {}, reason: "", message: "可读正文" }).body === "可读正文");
assert(channelEvaluationFinding({}).body === "未提供发现正文，请查看原始数据。");
assert(channelEvaluationFinding({}).severity === "未标注严重度");
assert(channelEvaluationFinding({ criterion_ids: [" C106 ", null, "", "C113"] }).criterion === "C106, C113");
assert(channelEvaluationFinding({ criterion_id: "C45", criterion_ids: ["C46"] }).criterion === "C45");
console.log("channelEvaluationFinding.test.ts OK");
