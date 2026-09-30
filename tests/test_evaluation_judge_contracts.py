"""Focused contracts for role-bound Channel/PRD Judge results."""

from __future__ import annotations

from zf.runtime.evaluation_prompts import (
    DISCUSSION_PROMPT,
    EVALUATION_PROMPTS,
)
from zf.runtime.evaluation_schemas import (
    ROLE_SCHEMA_VERSIONS,
    normalize_judge_output,
)
from zf.runtime.evaluation_types import necessity_label


REFS = ["channel/message/m1", "artifact/prd/channel-frozen.json"]


def _contribution_raw(**finding):
    item = {
        "agent_id": "agent-a",
        "score": 3,
        "necessity": "must",
        "necessity_status": "complete",
        "evidence_refs": [REFS[0]],
        "rationale": "提出并推动了可追溯的修复决策。",
        "confidence": 0.9,
    }
    item.update(finding)
    return {
        "schema_version": "contribution_result.v2",
        "judge_type": "contribution",
        "task_type": "bugfix",
        "role_id": "bugfix",
        "role_version": "1",
        "role_metadata": {"source": "roles/bugfix.md"},
        "agent_findings": [item],
        "missing_evidence": [],
    }


def test_role_result_uses_zero_to_four_and_persists_role_metadata() -> None:
    result, error = normalize_judge_output(
        judge_type="contribution",
        raw=_contribution_raw(),
        allowed_evidence_refs=REFS,
        expected_agent_ids=["agent-a"],
        expected_schema_version=ROLE_SCHEMA_VERSIONS["contribution"],
    )
    assert error == ""
    assert result is not None
    assert result.score_scale == "0-4"
    assert result.task_type == "bugfix"
    assert result.role_id == "bugfix"
    assert result.role_metadata["source"] == "roles/bugfix.md"
    assert result.agent_findings[0].score == 3
    assert result.agent_findings[0].necessity == "must"


def test_canonical_necessity_is_kept_on_v2() -> None:
    raw = _contribution_raw(necessity="must")
    raw["schema_version"] = "contribution_result.v2"
    result, error = normalize_judge_output(
        judge_type="contribution",
        raw=raw,
        allowed_evidence_refs=REFS,
        expected_agent_ids=["agent-a"],
    )
    assert error == ""
    assert result is not None
    assert result.agent_findings[0].necessity == "must"
    assert result.score_scale == "0-4"


def test_undetermined_necessity_is_explicitly_incomplete() -> None:
    result, error = normalize_judge_output(
        judge_type="contribution",
        raw=_contribution_raw(
            necessity="undetermined",
            necessity_status="incomplete",
            score=None,
            status="evidence_missing",
        ),
        allowed_evidence_refs=REFS,
        expected_agent_ids=["agent-a"],
        expected_schema_version=ROLE_SCHEMA_VERSIONS["contribution"],
    )
    assert error == ""
    assert result is not None
    assert result.status == "evidence_missing"
    assert result.agent_findings[0].necessity == "undetermined"
    assert result.agent_findings[0].necessity_status == "incomplete"
    assert result.agent_findings[0].score is None


def test_necessity_uncertainty_does_not_erase_evidenced_contribution_score() -> None:
    result, error = normalize_judge_output(
        judge_type="contribution",
        raw=_contribution_raw(
            necessity="undetermined",
            necessity_status="incomplete",
        ),
        allowed_evidence_refs=REFS,
        expected_agent_ids=["agent-a"],
        expected_schema_version=ROLE_SCHEMA_VERSIONS["contribution"],
    )
    assert error == ""
    assert result is not None
    assert result.status == "scored"
    assert result.agent_findings[0].score == 3
    assert result.agent_findings[0].necessity_status == "incomplete"


def test_role_score_rejects_legacy_five() -> None:
    result, error = normalize_judge_output(
        judge_type="contribution",
        raw=_contribution_raw(score=5),
        allowed_evidence_refs=REFS,
        expected_agent_ids=["agent-a"],
        expected_schema_version=ROLE_SCHEMA_VERSIONS["contribution"],
    )
    assert result is None
    assert "0 to 4" in error


def test_task_classifier_is_bounded_and_low_confidence_becomes_ambiguous() -> None:
    raw = {
        "schema_version": "task_type_result.v1",
        "judge_type": "task_type",
        "task_type": "refactor",
        "confidence": 0.59,
        "evidence_refs": [REFS[0]],
        "rationale": "存在迁移和边界调整，但证据不足。",
    }
    result, error = normalize_judge_output(
        judge_type="task_type", raw=raw, allowed_evidence_refs=REFS,
    )
    assert error == ""
    assert result is not None
    assert result.status == "abstain"
    assert result.task_type == "ambiguous"


def test_main_judge_cannot_submit_adjusted_agent_score() -> None:
    raw = {
        "schema_version": "conclusion_result.v2",
        "judge_type": "conclusion",
        "status": "complete",
        "agent_scores": [
            {"agent_id": "agent-a", "score": 3, "adjusted_score": 4}
        ],
        "team_findings": {
            "discussion_meaningful": True,
            "discussion_progressed": True,
            "problems": [],
            "underperformed_roles": [],
            "unnecessary_roles": [],
            "prd_ready_for_next_stage": True,
        },
        "conclusion_zh": "汇总完成。",
    }
    result, error = normalize_judge_output(
        judge_type="conclusion",
        raw=raw,
        allowed_evidence_refs=REFS,
        expected_schema_version=ROLE_SCHEMA_VERSIONS["conclusion"],
    )
    assert result is None
    assert "cannot adjust" in error


def test_discussion_role_prompt_does_not_read_contribution_result() -> None:
    assert "Milestone results" not in DISCUSSION_PROMPT
    assert EVALUATION_PROMPTS["discussion"] == DISCUSSION_PROMPT


def test_only_unified_prompt_profiles_are_registered() -> None:
    assert set(EVALUATION_PROMPTS) == {
        "task_type", "contribution", "discussion", "prd_fidelity", "conclusion",
    }


def test_necessity_report_labels_are_chinese() -> None:
    assert necessity_label("must") == "必须"
    assert necessity_label("optional") == "可选"
    assert necessity_label("not_needed") == "不需要"
    assert necessity_label("undetermined") == "未确定"
