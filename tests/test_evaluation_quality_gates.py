"""评分必须有完整证据，失败项不能通过聚合或报告变成有效总分。"""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer, evaluation_runner as runner
from zf.runtime.evaluation_schemas import (
    COMMUNICATION_DIMENSIONS,
    PRD_RESULT_DIMENSIONS,
    communication_score,
    normalize_judge_output,
    prd_result_score,
)
from tests.test_evaluation_prd import _judge_reply, _packet, _seed_channel_state, _team_findings

REFS = ["channel/message/evt-1", "artifact/prd/channel-frozen.json"]
BOUND_REF = "artifact/prd/workflow-bound.json"


def raw_result(stage="communication", *, bound=False):
    item = {"score": 4, "confidence": 0.9, "evidence_refs": [REFS[0]],
            "rationale": "消息明确列出了验收条件，并被后续 PRD 采用。"}
    if stage == "milestone":
        return {"schema_version": "milestone_result.v1", "judge_type": stage,
                "agent_findings": [{**item, "agent_id": "m1", "necessity": "necessary"}],
                "missing_evidence": []}
    ids = (COMMUNICATION_DIMENSIONS if stage == "communication"
           else PRD_RESULT_DIMENSIONS if bound else PRD_RESULT_DIMENSIONS[:1])
    return {"schema_version": "communication_result.v1" if stage == "communication" else "prd_result.v1",
            "judge_type": stage,
            "dimensions": [{**copy.deepcopy(item), "dimension_id": key} for key in ids],
            "missing_evidence": []}


def normalize(raw, **kwargs):
    return normalize_judge_output(judge_type=raw["judge_type"], raw=raw,
                                  allowed_evidence_refs=REFS, **kwargs)


@pytest.mark.parametrize("stage", ["milestone", "communication", "prd_result"])
@pytest.mark.parametrize("field,value", [
    ("evidence_refs", []), ("evidence_refs", "channel/message/evt-1"),
    ("evidence_refs", [None]), ("rationale", ""), ("rationale", "  "),
    ("rationale", 123),
])
def test_scored_items_require_own_evidence_and_explanation(stage, field, value):
    raw = raw_result(stage)
    items = raw["agent_findings"] if stage == "milestone" else raw["dimensions"]
    items[0][field] = value
    # A top-level explanation/reference cannot fill missing item-level proof.
    raw.update(evidence_refs=REFS, rationale="整体评价良好")
    normalized, error = normalize(raw)
    assert normalized is None and error


@pytest.mark.parametrize("score", [True, False, "4", 3.5, 0, 6, float("nan"), float("inf")])
def test_score_requires_an_integer_in_the_rubric(score):
    raw = raw_result()
    raw["dimensions"][0]["score"] = score
    normalized, error = normalize(raw)
    assert normalized is None and error


@pytest.mark.parametrize("confidence", [True, "0.9", -0.1, 1.1, float("nan"), float("inf")])
def test_confidence_requires_a_finite_json_number(confidence):
    raw = raw_result()
    raw["dimensions"][0]["confidence"] = confidence
    normalized, error = normalize(raw)
    assert normalized is None and error


def test_confidence_below_floor_cannot_round_up_to_a_score():
    raw = raw_result()
    raw["dimensions"][0]["confidence"] = 0.599
    normalized, error = normalize(raw)
    assert not error and normalized is not None
    assert normalized.dimensions[0].status == "abstain"
    assert normalized.status != "scored"
    assert normalized.communication_score is None


def test_unknown_evidence_never_produces_a_score():
    raw = raw_result()
    raw["dimensions"][0]["evidence_refs"] = ["channel/message/invented"]
    normalized, error = normalize(raw)
    if normalized is None:
        assert error
    else:
        assert normalized.status == "invalid"
        assert normalized.communication_score is None


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown", "malformed"])
def test_communication_requires_exact_dimensions(change):
    raw = raw_result()
    if change == "missing":
        raw["dimensions"].pop()
    elif change == "duplicate":
        raw["dimensions"].append(copy.deepcopy(raw["dimensions"][0]))
    elif change == "unknown":
        raw["dimensions"][-1]["dimension_id"] = "invented"
    else:
        raw["dimensions"].append("not a dimension")
    normalized, error = normalize(raw)
    assert normalized is None and error


def test_dimension_map_cannot_silently_discard_bad_entries():
    raw = raw_result()
    raw["dimensions"] = {item["dimension_id"]: item for item in raw["dimensions"]}
    raw["dimensions"]["extra"] = None
    normalized, error = normalize(raw)
    assert normalized is None and error


def test_unbound_prd_rejects_an_extra_null_workflow_dimension():
    raw = raw_result("prd_result", bound=True)
    raw["dimensions"][1].update(score=None, evidence_refs=[])
    normalized, error = normalize(raw, workflow_bound=False)
    assert normalized is None and error


def test_bound_prd_requires_both_boundaries_from_explicit_flag_or_ref():
    raw = raw_result("prd_result")
    normalized, error = normalize(raw, workflow_bound=True)
    assert normalized is None and error
    normalized, error = normalize_judge_output(
        judge_type="prd_result", raw=raw, allowed_evidence_refs=[*REFS, BOUND_REF])
    assert normalized is None and error


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("failure", ["missing", "abstain"])
def test_bound_prd_cannot_aggregate_one_surviving_boundary(index, failure):
    raw = raw_result("prd_result", bound=True)
    if failure == "missing":
        raw["dimensions"][index].update(score=None, evidence_refs=[])
    else:
        raw["dimensions"][index]["confidence"] = 0.1
    normalized, error = normalize(raw, workflow_bound=True)
    assert not error and normalized is not None
    assert normalized.status != "scored"
    assert normalized.prd_result_score is None
    assert prd_result_score(normalized.dimensions) is None


def test_null_missing_evidence_remains_an_explicit_unscored_result():
    raw = raw_result()
    raw["dimensions"][0].update(score=None, evidence_refs=[], rationale="未提供相关发言证据")
    normalized, error = normalize(raw)
    assert not error and normalized is not None
    assert normalized.status == "evidence_missing"
    assert normalized.communication_score is None


def test_explicit_abstention_remains_unscored_with_no_numeric_score():
    raw = raw_result()
    raw["dimensions"][0].update(status="abstain", score=None, confidence=0.2)
    normalized, error = normalize(raw)
    assert not error and normalized is not None
    assert normalized.dimensions[0].status == "abstain"
    assert normalized.communication_score is None


@pytest.mark.parametrize("status,score", [("scored", None), ("abstain", 4), ("evidence_missing", 4)])
def test_explicit_item_status_cannot_contradict_the_score(status, score):
    raw = raw_result()
    raw["dimensions"][0].update(status=status, score=score)
    normalized, error = normalize(raw)
    assert normalized is None and error


def test_declared_missing_prd_evidence_suppresses_aggregate():
    raw = raw_result("prd_result", bound=True)
    raw["missing_evidence"] = [REFS[0]]
    normalized, error = normalize(raw, workflow_bound=True)
    assert not error and normalized is not None
    assert normalized.status == "evidence_missing"
    assert normalized.prd_result_score is None


@pytest.mark.parametrize("agent_ids", [["m1"], ["m1", "m1"], ["m1", "unknown"]])
def test_milestone_requires_every_expected_agent_exactly_once(agent_ids):
    raw = raw_result("milestone")
    item = raw["agent_findings"][0]
    raw["agent_findings"] = [{**item, "agent_id": agent_id} for agent_id in agent_ids]
    normalized, error = normalize(raw, expected_agent_ids=["m1", "m2"])
    assert normalized is None and error


def test_milestone_accepts_complete_participant_set_in_any_order():
    raw = raw_result("milestone")
    item = raw["agent_findings"][0]
    raw["agent_findings"] = [{**item, "agent_id": agent_id} for agent_id in ["m2", "m1"]]
    normalized, error = normalize(raw, expected_agent_ids=["m1", "m2"])
    assert not error and normalized is not None and normalized.status == "scored"


@pytest.mark.parametrize("stage,aggregate", [("communication", communication_score), ("prd_result", prd_result_score)])
def test_aggregate_cannot_hide_duplicate_dimensions(stage, aggregate):
    dims = raw_result(stage)["dimensions"]
    for item in dims:
        item["status"] = "scored"
    dims.append(copy.deepcopy(dims[0]))
    assert aggregate(dims) is None


def valid_judges():
    judges = {}
    for stage in ["milestone", "communication", "prd_result"]:
        normalized, error = normalize(raw_result(stage))
        assert not error and normalized is not None
        judges[stage] = {"status": normalized.status, "result": normalized.to_dict()}
    return judges


@pytest.mark.parametrize("stage", ["milestone", "communication", "prd_result"])
@pytest.mark.parametrize("status", ["invalid", "evidence_missing", "abstain", "conflict", "inconclusive", "judge_timeout"])
def test_conclusion_requires_all_judges_scored_without_calling_provider(tmp_path, stage, status):
    judges = valid_judges()
    judges[stage]["status"] = status
    def forbidden(**kwargs):
        pytest.fail("unscored input must use a deterministic conclusion")
    result = runner.run_conclusion(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
        workflow_run_id="wf-quality", trial_id="t", judges=judges, backend=forbidden)
    assert result["status"] in {"incomplete", "inconclusive"}
    assert result["result"]["overall_score"] is None
    normalized, error = normalize_judge_output(
        judge_type="conclusion", raw=result["result"], allowed_evidence_refs=REFS)
    assert normalized is not None and not error
    assert normalized.team_findings["prd_ready_for_next_stage"] is False


def test_conclusion_agent_scores_and_aggregates_come_from_judges(tmp_path):
    reply = {"schema_version": "conclusion_result.v1", "judge_type": "conclusion",
             "status": "complete", "team_findings": _team_findings(), "conclusion_zh": "结论",
             "agent_scores": [{"agent_id": "invented", "score": 5}],
             "communication_score": 1, "prd_result_score": 1, "overall_score": 1}
    result = runner.run_conclusion(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
        workflow_run_id="wf-quality", trial_id="t", judges=valid_judges(),
        backend=lambda **kwargs: (json.dumps(reply), ""))
    assert result["status"] == "complete"
    body = result["result"]
    assert {item["agent_id"]: item["score"] for item in body["agent_scores"]} == {"m1": 4}
    assert [body[key] for key in ["communication_score", "prd_result_score", "overall_score"]] == [4, 4, 4]


def test_conclusion_preserves_provider_incomplete_status(tmp_path):
    reply = {"schema_version": "conclusion_result.v1", "judge_type": "conclusion",
             "status": "incomplete", "team_findings": _team_findings(), "conclusion_zh": "尚有证据争议",
             "agent_scores": [], "overall_score": 5}
    reply["team_findings"]["prd_ready_for_next_stage"] = False
    result = runner.run_conclusion(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
        workflow_run_id="wf-quality", trial_id="t", judges=valid_judges(),
        backend=lambda **kwargs: (json.dumps(reply), ""))
    assert result["status"] == "incomplete"
    assert result["result"]["overall_score"] is None


def test_runner_repairs_missing_item_evidence_before_accepting_score(tmp_path):
    bad = raw_result("milestone")
    bad["agent_findings"][0]["evidence_refs"] = []
    replies = iter([bad, raw_result("milestone")])
    calls = []
    def backend(**kwargs):
        calls.append((kwargs["attempt"], kwargs["schema_attempt"]))
        return json.dumps(next(replies)), ""
    result = runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")), workflow_run_id="wf-quality",
        trial_id="t", judge_type="milestone", packet=_packet(), backend=backend)
    assert result["status"] == "scored"
    assert calls == [(1, 1), (1, 2)]


def test_runner_checks_packet_participants_before_accepting_milestone(tmp_path):
    packet = replace(_packet(), agent_profiles=[{"agent_id": "m1"}, {"agent_id": "m2"}])
    result = runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")), workflow_run_id="wf-quality",
        trial_id="t", judge_type="milestone", packet=packet,
        backend=lambda **kwargs: (json.dumps(raw_result("milestone")), ""))
    assert result["status"] == "invalid"
    assert result["error"]


def test_runner_checks_packet_workflow_binding_before_accepting_prd(tmp_path):
    packet = replace(_packet("prd_result"), workflow_bound_prd={
        "workflow_run_id": "bound-wf", "body": "# Workflow PRD\n\nDo X."})
    result = runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")), workflow_run_id="wf-quality",
        trial_id="t", judge_type="prd_result", packet=packet,
        backend=lambda **kwargs: (json.dumps(raw_result("prd_result")), ""))
    assert result["status"] == "invalid"
    assert result["error"]


@pytest.mark.parametrize("failed", [False, True])
def test_report_recomputes_or_suppresses_untrusted_total(failed):
    judges = valid_judges()
    if failed:
        judges["milestone"]["status"] = "invalid"
    report = observer.render_report(
        workflow_run_id="wf-quality", trial_id="t", channel_id="ch", freeze={}, judges=judges,
        conclusion={"status": "complete", "result": {"overall_score": 99, "conclusion_zh": "结论"}})
    assert "overall_score: **99**" not in report
    assert f"overall_score: **{'None' if failed else '4.0'}**" in report


@pytest.mark.parametrize("body", [
    '{"score": 4, "score": 5}',
    '{"item": {"score": 4, "score": 5}}',
    '{"score": NaN}', '{"score": Infinity}', '{"score": -Infinity}',
    '{"score": 1e999}', '{"score": -1e999}',
    '{} {}', '说明：\n```json\n{}\n```',
])
def test_ambiguous_or_nonfinite_json_is_rejected(body):
    assert runner._parse_json_object(body) is None


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```\n{}\n```"])
def test_single_json_object_can_be_plain_or_fenced(wrapper):
    raw = raw_result()
    assert runner._parse_json_object(wrapper.format(json.dumps(raw))) == raw


@pytest.mark.parametrize("invalid", [None, "missing", [None]])
def test_conclusion_requires_typed_team_findings(invalid):
    raw = {"schema_version": "conclusion_result.v1", "judge_type": "conclusion",
           "agent_scores": [], "team_findings": _team_findings(), "conclusion_zh": "结论"}
    raw["team_findings"]["problems"] = invalid
    result, error = normalize(raw)
    assert result is None and error


@pytest.mark.parametrize("scores", [1, ["invalid"], {"milestone": 1}])
def test_malformed_conclusion_returns_schema_error_without_crashing(scores):
    raw = {"schema_version": "conclusion_result.v1", "judge_type": "conclusion",
           "scores": scores, "team_findings": _team_findings(), "conclusion_zh": "结论"}
    result, error = normalize(raw)
    assert result is None and error


@pytest.mark.parametrize("missing", ["task.statement", "channel_frozen_prd.body", "workflow_bound_prd.body"])
@pytest.mark.parametrize("empty", [None, "  ", {"body": ""}])
def test_missing_prd_inputs_finish_without_provider_or_total(tmp_path, missing, empty):
    packet = _packet("prd_result")
    if missing == "task.statement":
        packet = replace(packet, task={"statement": empty}, task_details={})
    else:
        key = missing.split(".")[0]
        packet = replace(packet, **{key: {"prd_ref": "missing", "body": empty}})
    def forbidden(**kwargs):
        pytest.fail("missing comparison input must not start a provider")
    result = runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")), workflow_run_id="wf-quality",
        trial_id="t", judge_type="prd_result", packet=packet, backend=forbidden)
    assert result["status"] == "evidence_missing" and result["attempt"] == 0
    assert result["result"]["missing_evidence"] == [missing]
    assert result["result"]["prd_result_score"] is None


@pytest.mark.parametrize("stage", ["milestone", "communication", "prd_result"])
def test_provider_prompt_sidecar_contains_the_full_rubric(tmp_path, stage):
    from zf.runtime.evaluation_prompts import SCORE_RUBRIC

    result = runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")), workflow_run_id="wf-quality",
        trial_id="t", judge_type=stage, packet=_packet(stage),
        backend=lambda **kwargs: (json.dumps(raw_result(stage)), ""))
    assert result["status"] == "scored"
    prompt = runner.judge_result_path(tmp_path, workflow_run_id="wf-quality",
                                      judge_type=stage, request_id=result["request_id"])
    rendered = prompt.with_suffix(".packet.txt").read_text(encoding="utf-8")
    assert SCORE_RUBRIC in rendered


@pytest.mark.parametrize("readable", [False, True])
@pytest.mark.parametrize("binding", ["direct", "source_refs", "task_source_refs", "accepted"])
def test_observer_reads_workflow_prd_body_or_records_missing_input(tmp_path, readable, binding):
    _seed_channel_state(tmp_path)
    ref = "workflow-prd.md"
    body = "# Workflow PRD\n\nChanged requirement: do Y."
    if readable:
        (tmp_path / ref).write_text(body, encoding="utf-8")
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    payload = {"channel_id": "ch-1", "workflow_run_id": "bound-wf", "prd_ref": ref}
    if binding == "source_refs":
        payload = {"workflow_run_id": "bound-wf",
                   "source_refs": {"channel_id": "ch-1", "channel_prd_ref": ref}}
    elif binding == "task_source_refs":
        payload = {"workflow_run_id": "bound-wf", "source_refs": {
            "channel_id": "ch-1", "task_source_ref": ref,
            "channel_prd_ref": "channel-artifacts/ch-1/prd.md"}}
    requested = writer.emit("workflow.invoke.requested", actor="test", payload=payload)
    if binding == "accepted":
        writer.emit("workflow.invoke.accepted", actor="test", payload={
            "channel_id": "ch-1", "workflow_run_id": "bound-wf", "source_event_id": requested.id})
    calls = []
    def backend(**kwargs):
        calls.append(kwargs["judge_type"])
        return _judge_reply(kwargs["judge_type"], with_bound=True), ""
    observed = observer.observe_once(state_dir=tmp_path, backend=backend)
    if readable:
        assert observed["errors"] == []
    else:
        assert observed["errors"] and all("workflow_bound_prd.body" in error for error in observed["errors"])
    packets = [json.loads(path.read_text(encoding="utf-8")) for path in
               (tmp_path / "artifacts" / "benchmark-evaluations" / "inputs").glob("*.json")]
    prd_packet = next(packet for packet in packets if packet["stage"] == "prd_result")
    assert prd_packet["workflow_bound_prd"]["body"] == ({"body": body} if readable else None)
    assert ("prd_result" in calls) is readable
    completions = [event for event in EventLog(tmp_path / "events.jsonl").read_all()
                   if event.type == runner.JUDGE_COMPLETED_EVENT and event.payload["judge_type"] == "prd_result"]
    assert completions[-1].payload["status"] == ("scored" if readable else "evidence_missing")
