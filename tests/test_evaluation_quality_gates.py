"""Canonical score and evidence gates for role-bound evaluation."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer, evaluation_runner as runner
from zf.runtime.evaluation_schemas import (
    DISCUSSION_DIMENSIONS,
    PRD_FIDELITY_DIMENSIONS,
    discussion_score,
    normalize_judge_output,
    prd_fidelity_score,
)
from tests.evaluation_fixtures import _packet

REFS = ["channel/message/evt-1", "artifact/prd/channel-frozen.json"]
BOUND_REF = "artifact/prd/workflow-bound.json"


def raw_result(stage="discussion", *, bound=False):
    item = {
        "score": 4, "confidence": 0.9, "evidence_refs": [REFS[0]],
        "rationale": "消息明确列出了验收条件，并被后续 PRD 采用。",
    }
    if stage == "contribution":
        return {
            "schema_version": "contribution_result.v2", "judge_type": stage,
            "agent_findings": [{**item, "agent_id": "m1", "necessity": "must"}],
            "missing_evidence": [],
        }
    ids = DISCUSSION_DIMENSIONS if stage == "discussion" else (
        PRD_FIDELITY_DIMENSIONS if bound else PRD_FIDELITY_DIMENSIONS[:1]
    )
    return {
        "schema_version": "discussion_result.v2" if stage == "discussion" else "prd_fidelity_result.v2",
        "judge_type": stage,
        "dimensions": [{**copy.deepcopy(item), "dimension_id": key} for key in ids],
        "missing_evidence": [],
    }


def normalize(raw, **kwargs):
    return normalize_judge_output(
        judge_type=raw["judge_type"], raw=raw, allowed_evidence_refs=REFS, **kwargs
    )


@pytest.mark.parametrize("stage", ["contribution", "discussion", "prd_fidelity"])
@pytest.mark.parametrize("field,value", [
    ("evidence_refs", []), ("evidence_refs", "channel/message/evt-1"),
    ("evidence_refs", [None]), ("rationale", ""), ("rationale", "  "),
    ("rationale", 123),
])
def test_scored_items_require_own_evidence_and_explanation(stage, field, value):
    raw = raw_result(stage)
    items = raw["agent_findings"] if stage == "contribution" else raw["dimensions"]
    items[0][field] = value
    normalized, error = normalize(raw)
    assert normalized is None and error


@pytest.mark.parametrize("score", [True, False, "4", 3.5, -1, 5, 6, float("nan"), float("inf")])
def test_score_requires_integer_in_zero_to_four_rubric(score):
    raw = raw_result()
    raw["dimensions"][0]["score"] = score
    normalized, error = normalize(raw)
    assert normalized is None and error


@pytest.mark.parametrize("confidence", [True, "0.9", -0.1, 1.1, float("nan"), float("inf")])
def test_confidence_requires_finite_number(confidence):
    raw = raw_result()
    raw["dimensions"][0]["confidence"] = confidence
    normalized, error = normalize(raw)
    assert normalized is None and error


def test_confidence_below_floor_becomes_abstain():
    raw = raw_result()
    raw["dimensions"][0]["confidence"] = 0.599
    normalized, error = normalize(raw)
    assert not error and normalized is not None
    assert normalized.dimensions[0].status == "abstain"
    assert normalized.status != "scored"
    assert normalized.discussion_score is None


def test_unknown_evidence_never_produces_score():
    raw = raw_result()
    raw["dimensions"][0]["evidence_refs"] = ["channel/message/invented"]
    normalized, error = normalize(raw)
    if normalized is None:
        assert error
    else:
        assert normalized.status == "invalid"
        assert normalized.discussion_score is None


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown", "malformed"])
def test_discussion_requires_exact_dimensions(change):
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


def test_unbound_prd_rejects_extra_workflow_dimension():
    raw = raw_result("prd_fidelity", bound=True)
    normalized, error = normalize(raw, workflow_bound=False)
    assert normalized is None and error


def test_bound_prd_requires_both_boundaries():
    raw = raw_result("prd_fidelity")
    normalized, error = normalize(raw, workflow_bound=True)
    assert normalized is None and error
    normalized, error = normalize_judge_output(
        judge_type="prd_fidelity", raw=raw,
        allowed_evidence_refs=[*REFS, BOUND_REF], workflow_bound=True,
    )
    assert normalized is None and error


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("failure", ["missing", "abstain"])
def test_bound_prd_cannot_aggregate_one_surviving_boundary(index, failure):
    raw = raw_result("prd_fidelity", bound=True)
    if failure == "missing":
        raw["dimensions"][index].update(score=None, evidence_refs=[])
    else:
        raw["dimensions"][index]["confidence"] = 0.1
    normalized, error = normalize(raw, workflow_bound=True)
    assert not error and normalized is not None
    assert normalized.status != "scored"
    assert normalized.prd_fidelity_score is None
    assert prd_fidelity_score(normalized.dimensions) is None


def test_contribution_requires_exact_frozen_agent_set():
    raw = raw_result("contribution")
    raw["agent_findings"] = [{**raw["agent_findings"][0], "agent_id": "m1"}]
    normalized, error = normalize(raw, expected_agent_ids=["m1", "m2"])
    assert normalized is None and error


def test_contribution_accepts_complete_agent_set():
    raw = raw_result("contribution")
    item = raw["agent_findings"][0]
    raw["agent_findings"] = [{**item, "agent_id": agent_id} for agent_id in ["m2", "m1"]]
    normalized, error = normalize(raw, expected_agent_ids=["m1", "m2"])
    assert not error and normalized is not None and normalized.status == "scored"


def test_main_judge_cannot_submit_aggregate_fields(tmp_path: Path):
    raw = {
        "schema_version": "conclusion_result.v2", "judge_type": "conclusion",
        "status": "complete", "discussion_score": 4, "prd_fidelity_score": 4,
        "agent_scores": [],
        "team_findings": {
            "discussion_meaningful": True, "discussion_progressed": True,
            "prd_ready_for_next_stage": True, "problems": [],
            "underperformed_roles": [], "unnecessary_roles": [],
        }, "conclusion_zh": "汇总完成。",
    }
    result, error = normalize_judge_output(
        judge_type="conclusion", raw=raw, allowed_evidence_refs=REFS,
    )
    assert result is None and "cannot adjust" in error


def test_report_does_not_display_untrusted_aggregate():
    judges = {
        "contribution": {"status": "scored", "result": {"agent_findings": []}},
        "discussion": {"status": "scored", "result": {"dimensions": []}},
        "prd_fidelity": {"status": "scored", "result": {"dimensions": []}},
    }
    report = observer.render_report(
        workflow_run_id="wf-quality", trial_id="t", channel_id="ch", freeze={}, judges=judges,
        conclusion={"status": "complete", "result": {"conclusion_zh": "结论"}},
    )
    assert "overall_score" not in report
    assert "communication_score" not in report
    assert "prd_result_score" not in report
