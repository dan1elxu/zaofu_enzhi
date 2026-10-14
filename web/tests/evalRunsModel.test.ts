import {
  errorMessage,
  evalDimensionLabel,
  evalLaneLabel,
  evalLaneStatusLabel,
  evalLaneTone,
  evalRunStatusLabel,
  evalRunTone,
  evalScoreDimensions,
  evalTaskTypeLabel,
  formatEvalConfidence,
  formatEvalScore,
  formatEvalTimestamp,
  groupLaneAggregates,
  laneStatusOf,
  orderedEvalLanes,
  readInitialEvalRunId,
  revealCandidateLabel,
  shortEvalDigest,
  sortEvalGroups,
  sortEvalTrials,
  writeEvalRunSelection,
} from "../src/components/eval/evalRunsModel.js";
import type { EvalRunGroup, EvalRunTrialSummary } from "../src/api/types.js";

function assert(value: unknown, message: string): asserts value {
  if (!value) throw new Error(message);
}

function trial(partial: Partial<EvalRunTrialSummary>): EvalRunTrialSummary {
  return {
    run_id: "run-x",
    trial: 1,
    status: "running",
    lanes: {},
    ...partial,
  };
}

assert(evalLaneLabel("terminal") === "Terminal", "terminal lane label");
assert(evalLaneLabel("kanban") === "Kanban", "kanban lane label");
assert(evalLaneLabel("channel") === "Channel Auto", "channel lane label");
assert(evalLaneLabel("mystery") === "mystery", "unknown lane falls back to the raw id");
assert(evalLaneLabel("") === "未知 lane", "empty lane is explicit");

assert(evalLaneStatusLabel("prd_frozen") === "PRD 已冻结", "frozen lane label");
assert(evalLaneStatusLabel("failed", "timeout") === "超时", "timeout failure is a timeout");
assert(evalLaneStatusLabel("timeout", "timeout") === "超时", "timeout status is a timeout");
assert(evalLaneStatusLabel("failed", "prd_missing") === "失败", "non-timeout failure is a failure");
assert(evalLaneStatusLabel("running") === "运行中", "running lane label");
assert(evalLaneStatusLabel("scored") === "已评分", "judge scored label");
assert(evalLaneStatusLabel("absent") === "未参与", "absent lane label");
assert(evalLaneStatusLabel("evidence_missing") === "缺少证据", "judge evidence label");
assert(evalLaneStatusLabel("") === "等待中", "empty status is pending");

assert(evalRunStatusLabel("running") === "运行中", "run running label");
assert(evalRunStatusLabel("completed") === "已完成", "run completed label");
assert(evalRunStatusLabel("") === "排队中", "empty run status is queued");

assert(evalLaneTone("prd_frozen") === "ok", "frozen lane is ok");
assert(evalLaneTone("scored") === "ok", "scored judge is ok");
assert(evalLaneTone("running") === "info", "running lane is info");
assert(evalLaneTone("failed") === "err", "failed lane is err");
assert(evalLaneTone("evidence_missing") === "warn", "evidence gap is warn");
assert(evalLaneTone("absent") === "muted", "absent lane is muted");
assert(evalLaneTone("pending") === "muted", "pending lane is muted");
assert(evalRunTone("completed") === "ok" && evalRunTone("running") === "info" && evalRunTone("x") === "muted", "run tones");

assert(evalDimensionLabel("coverage") === "需求覆盖", "coverage label");
assert(evalDimensionLabel("fidelity") === "需求忠实", "fidelity label");
assert(evalDimensionLabel("actionability") === "可执行性", "actionability label");
assert(evalDimensionLabel("structure") === "完整性结构", "structure label");
assert(evalDimensionLabel("other") === "other", "unknown dimension falls back");

assert(evalTaskTypeLabel("bugfix") === "修复缺陷", "bugfix label");
assert(evalTaskTypeLabel("greenfield") === "从零开发", "greenfield label");
assert(evalTaskTypeLabel("") === "未分类", "empty task type");

assert(formatEvalScore(4) === "4", "integer score stays integer");
assert(formatEvalScore(3.5) === "3.5", "composite keeps one decimal");
assert(formatEvalScore(0) === "0", "zero score is visible");
for (const value of [null, undefined, Number.NaN, "3", -1]) {
  assert(formatEvalScore(value) === "—", `invalid score ${String(value)} is hidden`);
}
assert(formatEvalConfidence(0.97) === "0.97", "confidence keeps two decimals");
assert(formatEvalConfidence(null) === "", "missing confidence is empty");

assert(shortEvalDigest("0123456789abcdef") === "0123456789ab", "digest is shortened to 12");
assert(shortEvalDigest("abc") === "abc", "short digest is kept");

{
  const ordered = orderedEvalLanes({ channel: 1, terminal: 2, zebra: 3 });
  assert(ordered.map((entry) => entry.lane).join(",") === "terminal,channel,zebra", "lanes use the canonical order, extras last");
  assert(orderedEvalLanes(null).length === 0, "missing lanes yield nothing");
}

{
  const trials = sortEvalTrials([trial({ trial: 1 }), trial({ trial: 3 }), trial({ trial: 2 })]);
  assert(trials.map((item) => item.trial).join(",") === "3,2,1", "trials sort newest first");
}

{
  const groups: EvalRunGroup[] = [
    { prompt_digest: "bbb", prompt_version: "v1", trials: [trial({ started_at: "2026-10-01T00:00:00Z" })] },
    { prompt_digest: "aaa", prompt_version: "v1", trials: [trial({ started_at: "2026-10-02T00:00:00Z" })] },
    { prompt_digest: "ccc", prompt_version: "v1", trials: [] },
  ];
  const sorted = sortEvalGroups(groups);
  assert(sorted.map((group) => group.prompt_digest).join(",") === "aaa,bbb,ccc", "groups sort by newest trial, empties last");
  assert(groups[0].prompt_digest === "bbb", "sort does not mutate the input");
}

{
  const group: EvalRunGroup = {
    prompt_digest: "d",
    trials: [
      trial({ composite_scores: { terminal: 3.5, kanban: 2.0, channel: null } }),
      trial({ trial: 2, composite_scores: { terminal: 2.5, kanban: 4 } }),
      trial({ trial: 3 }),
    ],
  };
  const aggregates = groupLaneAggregates(group);
  const terminal = aggregates.find((item) => item.lane === "terminal");
  const kanban = aggregates.find((item) => item.lane === "kanban");
  assert(aggregates.length === 2, "null and missing composites are skipped");
  assert(terminal && terminal.count === 2 && terminal.min === 2.5 && terminal.max === 3.5, "terminal aggregate bounds");
  assert(terminal && Math.abs(terminal.mean - 3.0) < 1e-9, "terminal aggregate mean");
  assert(kanban && kanban.count === 2 && kanban.mean === 3.0, "kanban aggregate mean");
}

{
  const dimensions = evalScoreDimensions({
    terminal: { status: "scored", dimensions: { structure: 4, coverage: 3 } },
    kanban: { status: "scored", dimensions: { fidelity: 2, custom_dim: 1 } },
    channel: null,
  });
  assert(dimensions.join(",") === "coverage,fidelity,structure,custom_dim", "dimensions use the canonical order, extras sorted");
  assert(evalScoreDimensions(undefined).length === 0, "missing scores yield no dimensions");
  assert(evalScoreDimensions({ terminal: { status: "running" } }).length === 0, "score without dimensions yields nothing");
}

{
  const labelMap = { A: "terminal", B: "kanban" };
  assert(revealCandidateLabel("A", labelMap) === "Terminal", "label is revealed to the lane name");
  assert(revealCandidateLabel("C", labelMap) === "C", "unmapped label stays blind");
  assert(revealCandidateLabel("A") === "A", "missing map keeps the label");
}

{
  const lanes = { terminal: { status: "failed", error: "boom" } };
  assert(laneStatusOf(lanes, "terminal").status === "failed", "present lane is returned");
  assert(laneStatusOf(lanes, "kanban").status === "pending", "absent lane defaults to pending");
  assert(laneStatusOf(undefined, "channel").status === "pending", "missing lanes map defaults to pending");
}

assert(formatEvalTimestamp("") === "", "empty timestamp is empty");
assert(formatEvalTimestamp("not-a-date") === "", "invalid timestamp is empty");
assert(/^\d{1,2}-\d{1,2} \d{2}:\d{2}$/.test(formatEvalTimestamp("2026-10-13T08:30:00Z")), "timestamp renders as M-D HH:MM");

assert(errorMessage(new Error("boom")) === "boom", "error message unwraps Error");
assert(errorMessage("raw") === "raw", "error message passes strings through");

assert(readInitialEvalRunId() === "", "node context has no initial run id");
writeEvalRunSelection("run-1");

console.log("evalRunsModel tests passed");
