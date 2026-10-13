"""Three-lane eval kernel: lane blind scoring and cross_compare judge chain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.evaluation_artifacts import read_judge_result
from zf.runtime.evaluation_cross_compare import (
    blind_favorites,
    blind_mean_scores,
    candidate_prd_ref,
    expected_pairs,
    expected_vote_ranking,
)
from zf.runtime.evaluation_lane_packets import (
    EVAL_PRD_REF,
    EVAL_PROMPT_REF,
    assign_candidate_labels,
    blind_evidence_ref,
    blind_summary_from_record,
    build_cross_compare_packet,
    build_lane_prd_fidelity_packet,
    build_lane_task_type_packet,
    cross_compare_context_from_packet,
    missing_cross_compare_inputs,
    missing_lane_prd_inputs,
)
from zf.runtime.evaluation_packets import (
    evaluation_request_id,
    packet_for_prompt,
    write_immutable_packet,
)
from zf.runtime.evaluation_prompts import (
    CROSS_COMPARE_PROMPT_VERSION,
    EVALUATION_PROMPTS,
    EVALUATION_PROMPT_VERSION,
    LANE_EVALUATION_CONTRACT,
    LANE_EVALUATION_PROMPTS,
    LANE_PRD_FIDELITY_PROMPT_VERSION,
    evaluation_contract_for_stage,
    prompt_version_for_stage,
)
from zf.runtime.evaluation_report import render_report
from zf.runtime.evaluation_runner import run_judge
from zf.runtime.evaluation_schemas import (
    LANE_PRD_FIDELITY_DIMENSIONS,
    ROLE_SCHEMA_VERSIONS,
    SCHEMA_VERSIONS,
    normalize_judge_output,
)
from zf.runtime.evaluation_scheduler import (
    EvaluationQueueEntry,
    EvaluationScheduler,
)
from tests.evaluation_fixtures import (
    _cross_compare_blind,
    _cross_compare_reply,
    _judge_reply,
    _lane_prd_fidelity_reply,
)

PROMPT_TEXT = "做一个命令行待办事项工具，支持增删改查与到期提醒，数据本地存储。"
PRD_BODY = (
    "# 待办工具 PRD\n\n## 背景\n命令行待办工具。\n\n## 范围\n增删改查、到期提醒。\n\n"
    "## 验收\n- 新增待办 -> verify: 列表出现该事项\n"
)

LANE_NAMES = ("terminal", "kanban", "channel-auto")
CANDIDATES = ["A", "B", "C"]
BLIND_SCORES = {
    label: {dimension: 4 - index for dimension in LANE_PRD_FIDELITY_DIMENSIONS}
    for index, label in enumerate(CANDIDATES)
}
COMPARE_ALLOWED = (
    [EVAL_PROMPT_REF]
    + [candidate_prd_ref(label) for label in CANDIDATES]
    + [blind_evidence_ref(label) for label in CANDIDATES]
)


def _lane_packet(**overrides):
    kwargs = {
        "run_id": "run-1",
        "workflow_run_id": "evalrun-1-trial-1-lane-0",
        "trial_id": "trial-1",
        "prompt_text": PROMPT_TEXT,
        "prd_body": PRD_BODY,
    }
    kwargs.update(overrides)
    return build_lane_prd_fidelity_packet(**kwargs)


def _compare_packet(**overrides):
    kwargs = {
        "run_id": "run-1",
        "workflow_run_id": "evalrun-1-trial-1",
        "trial_id": "trial-1",
        "prompt_text": PROMPT_TEXT,
        "candidates": _cross_compare_blind(),
    }
    kwargs.update(overrides)
    return build_cross_compare_packet(**kwargs)


def _lane_raw(**overrides):
    raw = json.loads(_lane_prd_fidelity_reply())
    raw.update(overrides)
    return raw


def _normalize_lane(raw):
    return normalize_judge_output(
        judge_type="lane_prd_fidelity", raw=raw,
        allowed_evidence_refs=[EVAL_PROMPT_REF, EVAL_PRD_REF],
    )


def _compare_raw(**overrides):
    raw = json.loads(_cross_compare_reply())
    raw.update(overrides)
    return raw


def _normalize_compare(raw, *, blind_scores=None, candidates=CANDIDATES):
    return normalize_judge_output(
        judge_type="cross_compare", raw=raw,
        allowed_evidence_refs=COMPARE_ALLOWED,
        cross_compare_context={
            "candidates": candidates,
            "blind_scores": blind_scores if blind_scores is not None else BLIND_SCORES,
        },
    )


def _writer(state_dir: Path) -> EventWriter:
    return EventWriter(EventLog(state_dir / "events.jsonl"))


def _queue_entry(
    judge_type: str, workflow: str, trial: str, digest: str,
    deps: list[str] | None = None, channel: str = "",
) -> EvaluationQueueEntry:
    return EvaluationQueueEntry(
        job_id=evaluation_request_id(
            workflow_run_id=workflow, trial_id=trial, judge_type=judge_type,
            source_snapshot_digest=digest,
            prompt_version=prompt_version_for_stage(judge_type),
        ),
        channel_id=channel or judge_type,
        workflow_run_id=workflow,
        trial_id=trial,
        judge_type=judge_type,
        source_snapshot_digest=digest,
        depends_on=list(deps or []),
    )


# --- registry isolation -----------------------------------------------------


def test_channel_prompt_registry_unchanged_by_lane_stages() -> None:
    assert set(EVALUATION_PROMPTS) == {
        "task_type", "contribution", "discussion", "prd_fidelity", "conclusion",
    }
    assert set(LANE_EVALUATION_PROMPTS) == {"lane_prd_fidelity", "cross_compare"}


def test_role_schema_registry_excludes_unbound_lane_stages() -> None:
    assert "lane_prd_fidelity" not in ROLE_SCHEMA_VERSIONS
    assert "cross_compare" not in ROLE_SCHEMA_VERSIONS
    assert SCHEMA_VERSIONS["lane_prd_fidelity"] == "lane_prd_fidelity_result.v1"
    assert SCHEMA_VERSIONS["cross_compare"] == "cross_compare_result.v1"


def test_prompt_versions_and_contracts_are_decoupled() -> None:
    assert prompt_version_for_stage("prd_fidelity") == EVALUATION_PROMPT_VERSION
    assert prompt_version_for_stage("conclusion") == EVALUATION_PROMPT_VERSION
    assert prompt_version_for_stage("lane_prd_fidelity") == LANE_PRD_FIDELITY_PROMPT_VERSION
    assert prompt_version_for_stage("cross_compare") == CROSS_COMPARE_PROMPT_VERSION
    assert LANE_PRD_FIDELITY_PROMPT_VERSION != EVALUATION_PROMPT_VERSION
    assert CROSS_COMPARE_PROMPT_VERSION != EVALUATION_PROMPT_VERSION
    assert evaluation_contract_for_stage("cross_compare") == LANE_EVALUATION_CONTRACT
    assert evaluation_contract_for_stage("conclusion") == "channel-prd-eval.v1"


def test_lane_request_identity_departs_from_channel_prompt_version() -> None:
    digest = "d" * 64
    lane = evaluation_request_id(
        workflow_run_id="wf", trial_id="t", judge_type="lane_prd_fidelity",
        source_snapshot_digest=digest, prompt_version=LANE_PRD_FIDELITY_PROMPT_VERSION,
    )
    channel_version = evaluation_request_id(
        workflow_run_id="wf", trial_id="t", judge_type="lane_prd_fidelity",
        source_snapshot_digest=digest,
    )
    assert lane != channel_version


# --- lane packets ------------------------------------------------------------


def test_lane_prd_fidelity_packet_is_blind_and_channel_free() -> None:
    packet = _lane_packet()
    assert packet.stage == "lane_prd_fidelity"
    assert packet.channel_messages == []
    assert packet.channel_frozen_prd == {}
    assert packet.workflow_bound_prd == {}
    assert packet.agent_profiles == []
    assert packet.agent_relationships == []
    assert packet.allowed_evidence_refs == [EVAL_PROMPT_REF, EVAL_PRD_REF]
    assert packet.prompt_version == LANE_PRD_FIDELITY_PROMPT_VERSION
    view = packet_for_prompt(packet)
    details = view["task_details"]
    assert details["prd"]["body"] == PRD_BODY
    assert details["prd"]["digest"]
    payload = json.dumps(view, ensure_ascii=False)
    for marker in LANE_NAMES:
        assert marker not in payload


def test_lane_packets_rebuild_byte_identical(tmp_path: Path) -> None:
    first, second = _lane_packet(), _lane_packet()
    assert first.to_dict() == second.to_dict()
    path_first, digest_first = write_immutable_packet(tmp_path, first)
    path_second, digest_second = write_immutable_packet(tmp_path, second)
    assert (path_first, digest_first) == (path_second, digest_second)


def test_assign_candidate_labels_is_deterministic_and_blind() -> None:
    mapping = assign_candidate_labels(["terminal", "kanban", "channel"], seed="run-1:trial-1")
    assert mapping == assign_candidate_labels(
        ["channel", "terminal", "kanban"], seed="run-1:trial-1",
    )
    assert set(mapping) == {"terminal", "kanban", "channel"}
    assert sorted(mapping.values()) == ["A", "B", "C"]
    with pytest.raises(ValueError, match="at most"):
        assign_candidate_labels(["a", "b", "c", "d"], seed="s")


def test_cross_compare_packet_strips_lane_identity() -> None:
    mapping = assign_candidate_labels(["terminal", "kanban", "channel"], seed="run-1:trial-1")
    blind = _cross_compare_blind()
    by_label = {item["label"]: item for item in blind}
    candidates = [
        {**by_label[label], "lane": lane}  # caller-side lane tag must be rejected
        for lane, label in mapping.items()
    ]
    with pytest.raises(ValueError, match="unknown keys"):
        build_cross_compare_packet(
            run_id="r", workflow_run_id="wf", trial_id="t",
            prompt_text=PROMPT_TEXT, candidates=candidates,
        )
    packet = _compare_packet()
    payload = json.dumps(packet_for_prompt(packet), ensure_ascii=False)
    for marker in LANE_NAMES:
        assert marker not in payload
    assert packet.prompt_version == CROSS_COMPARE_PROMPT_VERSION
    assert set(packet.allowed_evidence_refs) == set(COMPARE_ALLOWED)
    with pytest.raises(ValueError, match="at least two"):
        build_cross_compare_packet(
            run_id="r", workflow_run_id="wf", trial_id="t",
            prompt_text=PROMPT_TEXT, candidates=_cross_compare_blind(("A",)),
        )


def test_shared_task_type_packet_classifies_the_prompt() -> None:
    packet = build_lane_task_type_packet(
        run_id="run-1", workflow_run_id="evalrun-1", trial_id="trial-1",
        prompt_text=PROMPT_TEXT,
    )
    assert packet.stage == "task_type"
    assert packet.prompt_version == EVALUATION_PROMPT_VERSION
    assert packet.allowed_evidence_refs == [EVAL_PROMPT_REF]
    assert packet.channel_messages == []
    raw = json.loads(_judge_reply("task_type", EVAL_PROMPT_REF))
    result, error = normalize_judge_output(
        judge_type="task_type", raw=raw,
        allowed_evidence_refs=packet.allowed_evidence_refs,
    )
    assert error == ""
    assert result is not None and result.status == "scored"
    assert result.task_type == "bugfix"


# --- lane prd fidelity normalization -----------------------------------------


def test_lane_fidelity_scored_computes_equal_weight_mean() -> None:
    result, error = _normalize_lane(_lane_raw())
    assert error == ""
    assert result is not None
    assert result.status == "scored"
    assert result.judge_type == "lane_prd_fidelity"
    assert [item.dimension_id for item in result.dimensions] == list(
        LANE_PRD_FIDELITY_DIMENSIONS
    )
    assert result.lane_prd_fidelity_score == 3.0


def test_lane_fidelity_mean_weights_all_four_dimensions() -> None:
    raw = _lane_raw()
    scores = {"coverage": 4, "fidelity": 2, "actionability": 2, "structure": 0}
    for item in raw["dimensions"]:
        item["score"] = scores[item["dimension_id"]]
    result, error = _normalize_lane(raw)
    assert error == ""
    assert result is not None and result.status == "scored"
    assert result.lane_prd_fidelity_score == 2.0


@pytest.mark.parametrize("score", [5, -1, 2.5])
def test_lane_fidelity_rejects_out_of_range_scores(score: float) -> None:
    raw = _lane_raw()
    raw["dimensions"][0]["score"] = score
    result, error = _normalize_lane(raw)
    assert result is None
    assert "score" in error


def test_lane_fidelity_requires_exactly_the_four_dimensions() -> None:
    raw = _lane_raw()
    raw["dimensions"] = raw["dimensions"][:3]
    result, error = _normalize_lane(raw)
    assert result is None
    assert "dimension_id" in error


def test_lane_fidelity_unbound_evidence_marks_result_invalid() -> None:
    raw = _lane_raw()
    raw["dimensions"][0]["evidence_refs"] = ["eval/prd/ZZZ"]
    result, error = _normalize_lane(raw)
    assert error == ""
    assert result is not None and result.status == "invalid"
    assert result.lane_prd_fidelity_score is None


def test_lane_fidelity_low_confidence_abstains() -> None:
    raw = _lane_raw(confidence=0.4)
    for item in raw["dimensions"]:
        item["confidence"] = 0.4
        item.pop("status", None)
    result, error = _normalize_lane(raw)
    assert error == ""
    assert result is not None and result.status == "abstain"


def test_lane_fidelity_evidence_missing_dimension() -> None:
    raw = _lane_raw()
    raw["dimensions"][0].update(status="evidence_missing", score=None)
    result, error = _normalize_lane(raw)
    assert error == ""
    assert result is not None and result.status == "evidence_missing"
    assert result.lane_prd_fidelity_score is None


def test_lane_fidelity_rejects_object_differences() -> None:
    raw = _lane_raw(differences=[{"dimension_id": "coverage", "description": "x"}])
    result, error = _normalize_lane(raw)
    assert result is None
    assert "differences" in error


# --- cross_compare deterministic helpers --------------------------------------


def test_blind_favorites_require_a_unique_winner() -> None:
    assert blind_favorites(BLIND_SCORES) == {
        dimension: "A" for dimension in LANE_PRD_FIDELITY_DIMENSIONS
    }
    tied = {label: {"coverage": 3} for label in ("A", "B")}
    assert blind_favorites(tied) == {}
    assert blind_mean_scores({"A": {"coverage": 4}, "B": {"coverage": None}}) == {
        "A": None, "B": None,
    }


def test_expected_pairs_and_vote_ranking() -> None:
    assert expected_pairs(["A", "B", "C"]) == ["A-B", "A-C", "B-C"]
    reply = json.loads(_cross_compare_reply())
    rankings = [
        type("R", (), {"ranking": entry["ranking"]})
        for entry in reply["dimension_rankings"]
    ]
    assert expected_vote_ranking(CANDIDATES, rankings, BLIND_SCORES) == ["A", "B", "C"]


# --- cross_compare normalization ----------------------------------------------


def test_cross_compare_valid_output_is_scored() -> None:
    result, error = _normalize_compare(_compare_raw())
    assert error == ""
    assert result is not None
    assert result.status == "scored"
    assert result.candidates == CANDIDATES
    assert len(result.pairwise) == 12
    assert len(result.dimension_rankings) == 4
    assert len(result.attributions) == 12
    assert result.conflicts == []
    assert result.recommendation["top"] == "A"
    assert [gap["candidate"] for gap in result.recommendation["key_gaps"]] == ["B", "C"]


def test_cross_compare_supports_two_candidates() -> None:
    result, error = _normalize_compare(
        _compare_raw(
            candidates=["A", "B"],
            dimension_rankings=json.loads(_cross_compare_reply(
                candidates=("A", "B"),
            ))["dimension_rankings"],
            pairwise=json.loads(_cross_compare_reply(candidates=("A", "B")))["pairwise"],
            attributions=json.loads(_cross_compare_reply(candidates=("A", "B")))["attributions"],
            recommendation=json.loads(_cross_compare_reply(candidates=("A", "B")))["recommendation"],
        ),
        blind_scores={label: BLIND_SCORES[label] for label in ("A", "B")},
        candidates=["A", "B"],
    )
    assert error == ""
    assert result is not None and result.status == "scored"
    assert len(result.pairwise) == 4


def test_cross_compare_rejects_incomplete_pairwise_matrix() -> None:
    raw = _compare_raw()
    dropped = raw["pairwise"].pop(0)
    raw["attributions"] = [
        item for item in raw["attributions"]
        if not (item["pair"] == dropped["pair"] and item["dimension_id"] == dropped["dimension_id"])
    ]
    result, error = _normalize_compare(raw)
    assert result is None
    assert "incomplete" in error


def test_cross_compare_decisiveness_rules() -> None:
    raw = _compare_raw()
    raw["pairwise"][0].update(winner="similar", margin="small")
    result, error = _normalize_compare(raw)
    assert result is None and "margin" in error

    raw = _compare_raw()
    raw["pairwise"][0].update(winner="A", margin="none")
    result, error = _normalize_compare(raw)
    assert result is None and "margin" in error

    raw = _compare_raw()
    raw["pairwise"][0]["confidence"] = 0
    result, error = _normalize_compare(raw)
    assert result is None and "confidence 0" in error

    raw = _compare_raw()
    raw["pairwise"][0].update(winner="similar", margin="none", confidence=1)
    result, error = _normalize_compare(raw)
    assert error == ""
    assert result is not None and result.status == "scored"


def test_cross_compare_ranking_must_be_a_permutation() -> None:
    raw = _compare_raw()
    raw["dimension_rankings"][0]["ranking"] = ["A", "A", "B"]
    result, error = _normalize_compare(raw)
    assert result is None
    assert "permutation" in error


def test_cross_compare_pairwise_must_agree_with_ranking() -> None:
    raw = _compare_raw()
    raw["dimension_rankings"][0]["ranking"] = ["B", "A", "C"]
    result, error = _normalize_compare(raw)
    assert result is None
    assert "contradicts the dimension ranking" in error


def test_cross_compare_decided_pair_requires_two_sided_attribution() -> None:
    raw = _compare_raw()
    raw["attributions"] = raw["attributions"][1:]
    result, error = _normalize_compare(raw)
    assert result is None
    assert "attribution" in error

    raw = _compare_raw()
    raw["attributions"][0]["evidence_refs"] = [candidate_prd_ref("A")]
    result, error = _normalize_compare(raw)
    assert result is None
    assert "cite both PRDs" in error

    raw = _compare_raw()
    raw["attributions"][0]["category"] = "style"
    result, error = _normalize_compare(raw)
    assert result is None
    assert "category" in error


def test_cross_compare_blind_conflict_must_be_recorded() -> None:
    blind = {
        label: dict(dimensions) for label, dimensions in BLIND_SCORES.items()
    }
    blind["B"]["coverage"] = 9  # blind favorite on coverage flips to B
    result, error = _normalize_compare(_compare_raw(), blind_scores=blind)
    assert result is None
    assert "coverage" in error

    conflict = {
        "dimension_id": "coverage", "blind_favorite": "B", "pairwise_favorite": "A",
        "resolution": "逐段对比显示 A 覆盖了全部显式需求，blind 单项分高估了 B。",
        "evidence_refs": [candidate_prd_ref("A"), candidate_prd_ref("B")],
    }
    result, error = _normalize_compare(_compare_raw(conflicts=[conflict]), blind_scores=blind)
    assert error == ""
    assert result is not None and result.status == "scored"
    assert result.conflicts[0].blind_favorite == "B"

    result, error = _normalize_compare(
        _compare_raw(conflicts=[{**conflict, "blind_favorite": "C"}]),
        blind_scores=blind,
    )
    assert result is None
    assert "blind_favorite" in error


def test_cross_compare_recommendation_follows_votes_or_explains() -> None:
    tied_blind = {
        label: {dimension: 3 for dimension in LANE_PRD_FIDELITY_DIMENSIONS}
        for label in CANDIDATES
    }
    divergent = {
        "ranking": ["B", "A", "C"], "top": "B", "confidence": 2,
        "rationale": "B 在关键需求上更完整。",
        "key_gaps": [
            {"candidate": "A", "gap": "A 的验收标准缺失。",
             "evidence_refs": [candidate_prd_ref("A"), candidate_prd_ref("B")]},
            {"candidate": "C", "gap": "C 的范围偏移。",
             "evidence_refs": [candidate_prd_ref("C"), candidate_prd_ref("B")]},
        ],
    }
    result, error = _normalize_compare(
        _compare_raw(recommendation=divergent), blind_scores=tied_blind,
    )
    assert result is None
    assert "divergence_explanation" in error

    result, error = _normalize_compare(
        _compare_raw(recommendation={
            **divergent,
            "divergence_explanation": "票选维度差距过小，可执行性权重更高。",
        }),
        blind_scores=tied_blind,
    )
    assert error == ""
    assert result is not None and result.status == "scored"
    assert result.recommendation["top"] == "B"


def test_cross_compare_recommendation_shape_rules() -> None:
    raw = _compare_raw()
    raw["recommendation"]["ranking"] = ["A", "B"]
    result, error = _normalize_compare(raw)
    assert result is None and "permutation" in error

    raw = _compare_raw()
    raw["recommendation"]["top"] = "B"
    result, error = _normalize_compare(raw)
    assert result is None and "top" in error

    raw = _compare_raw()
    raw["recommendation"]["key_gaps"] = raw["recommendation"]["key_gaps"][:1]
    result, error = _normalize_compare(raw)
    assert result is None and "key gaps" in error

    raw = _compare_raw()
    raw["recommendation"]["key_gaps"][0]["evidence_refs"] = []
    result, error = _normalize_compare(raw)
    assert result is None and "evidence_refs" in error


def test_cross_compare_unbound_evidence_is_invalid_not_error() -> None:
    raw = _compare_raw()
    raw["pairwise"][0]["evidence_refs"] = ["eval/prd/ZZZ"]
    result, error = _normalize_compare(raw)
    assert error == ""
    assert result is not None and result.status == "invalid"


def test_cross_compare_missing_evidence_status() -> None:
    raw = _compare_raw(missing_evidence=["eval/blind/C"])
    result, error = _normalize_compare(raw)
    assert error == ""
    assert result is not None and result.status == "evidence_missing"

    raw = _compare_raw()
    raw["status"] = "evidence_missing"
    result, error = _normalize_compare(raw)
    assert result is None
    assert "missing_evidence" in error


def test_cross_compare_requires_candidate_order() -> None:
    raw = _compare_raw(candidates=["B", "A", "C"])
    result, error = _normalize_compare(raw)
    assert result is None
    assert "candidates" in error


# --- packet helpers ------------------------------------------------------------


def test_blind_summary_and_context_projection() -> None:
    record = {
        "status": "scored",
        "result": json.loads(_lane_prd_fidelity_reply()) | {"lane_prd_fidelity_score": 3.0},
    }
    summary = blind_summary_from_record(record)
    assert summary["status"] == "scored"
    assert summary["mean"] == 3.0
    assert [item["dimension_id"] for item in summary["dimensions"]] == list(
        LANE_PRD_FIDELITY_DIMENSIONS
    )

    packet = _compare_packet()
    context = cross_compare_context_from_packet(packet)
    assert context["candidates"] == CANDIDATES
    assert context["blind_scores"]["A"]["coverage"] == 3


def test_missing_input_guards() -> None:
    assert missing_lane_prd_inputs(_lane_packet()) == []
    assert missing_lane_prd_inputs(_lane_packet(prd_body="")) == ["task_details.prd.body"]
    assert missing_lane_prd_inputs(_lane_packet(prompt_text="")) == ["task.statement"]

    assert missing_cross_compare_inputs(_compare_packet()) == []
    blind = _cross_compare_blind()
    blind[1]["blind"] = {}
    missing = missing_cross_compare_inputs(_compare_packet(candidates=blind))
    assert missing == ["task_details.candidates[B].blind"]


# --- runner integration -------------------------------------------------------


def test_run_judge_lane_fidelity_scored_and_deduped(tmp_path: Path) -> None:
    packet = _lane_packet()
    calls = []

    def backend(**kwargs):
        calls.append(kwargs)
        return _lane_prd_fidelity_reply(), ""

    record = run_judge(
        state_dir=tmp_path, project_root=None, config=None, writer=_writer(tmp_path),
        workflow_run_id="evalrun-1-trial-1-lane-0", trial_id="trial-1",
        judge_type="lane_prd_fidelity", packet=packet, backend=backend,
    )
    assert record["status"] == "scored"
    assert record["result"]["lane_prd_fidelity_score"] == 3.0
    assert record["prompt_version"] == LANE_PRD_FIDELITY_PROMPT_VERSION
    assert record["evaluation_contract"] == LANE_EVALUATION_CONTRACT
    events = EventLog(tmp_path / "events.jsonl").read_all()
    completed = [e for e in events if e.type == "benchmark.evaluation.judge.completed"]
    assert completed
    assert all(
        e.payload.get("evaluation_contract") == LANE_EVALUATION_CONTRACT
        and e.payload.get("prompt_version") == LANE_PRD_FIDELITY_PROMPT_VERSION
        for e in completed
    )
    again = run_judge(
        state_dir=tmp_path, project_root=None, config=None, writer=_writer(tmp_path),
        workflow_run_id="evalrun-1-trial-1-lane-0", trial_id="trial-1",
        judge_type="lane_prd_fidelity", packet=packet, backend=backend,
    )
    assert again["request_id"] == record["request_id"]
    assert len(calls) == 1
    assert read_judge_result(
        tmp_path, workflow_run_id="evalrun-1-trial-1-lane-0",
        judge_type="lane_prd_fidelity", request_id=record["request_id"],
    ) is not None
    sidecar = next(
        (tmp_path / "artifacts" / "benchmark-evaluations" / "judges").rglob("*.packet.txt")
    )
    rendered = sidecar.read_text(encoding="utf-8")
    assert PRD_BODY.splitlines()[0] in rendered
    for marker in LANE_NAMES:
        assert marker not in rendered


def test_run_judge_lane_fidelity_missing_prd_is_deterministic(tmp_path: Path) -> None:
    packet = _lane_packet(prd_body="")

    def backend(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("provider must not run for missing inputs")

    record = run_judge(
        state_dir=tmp_path, project_root=None, config=None, writer=_writer(tmp_path),
        workflow_run_id="evalrun-1-trial-1-lane-0", trial_id="trial-1",
        judge_type="lane_prd_fidelity", packet=packet, backend=backend,
    )
    assert record["status"] == "evidence_missing"
    assert record["attempt"] == 0
    assert "task_details.prd.body" in record["error"]


def test_run_judge_rejects_foreign_prompt_version(tmp_path: Path) -> None:
    packet = _lane_packet().to_dict()
    packet["prompt_version"] = EVALUATION_PROMPT_VERSION
    with pytest.raises(ValueError, match="prompt_version"):
        run_judge(
            state_dir=tmp_path, project_root=None, config=None, writer=_writer(tmp_path),
            workflow_run_id="evalrun-1", trial_id="trial-1",
            judge_type="lane_prd_fidelity", packet=packet,
            backend=lambda **kwargs: (_lane_prd_fidelity_reply(), ""),
        )


def test_run_judge_cross_compare_full_chain(tmp_path: Path) -> None:
    packet = _compare_packet()
    good = _cross_compare_reply()
    calls = []

    def backend(**kwargs):
        calls.append(kwargs)
        return good, ""

    record = run_judge(
        state_dir=tmp_path, project_root=None, config=None, writer=_writer(tmp_path),
        workflow_run_id="evalrun-1-trial-1", trial_id="trial-1",
        judge_type="cross_compare", packet=packet, backend=backend,
    )
    assert record["status"] == "scored"
    assert record["prompt_version"] == CROSS_COMPARE_PROMPT_VERSION
    assert record["evaluation_contract"] == LANE_EVALUATION_CONTRACT
    result = record["result"]
    assert result["recommendation"]["top"] == "A"
    assert len(result["pairwise"]) == 12
    assert len(result["attributions"]) == 12
    assert read_judge_result(
        tmp_path, workflow_run_id="evalrun-1-trial-1",
        judge_type="cross_compare", request_id=record["request_id"],
    ) is not None
    assert len(calls) == 1


def test_run_judge_cross_compare_schema_repair_once(tmp_path: Path) -> None:
    packet = _compare_packet()
    broken = json.loads(_cross_compare_reply())
    broken["attributions"] = []  # decided pairs without attribution: shape error
    replies = [json.dumps(broken, ensure_ascii=False), _cross_compare_reply()]

    def backend(**kwargs):
        return replies.pop(0), ""

    record = run_judge(
        state_dir=tmp_path, project_root=None, config=None, writer=_writer(tmp_path),
        workflow_run_id="evalrun-1-trial-1", trial_id="trial-1",
        judge_type="cross_compare", packet=packet, backend=backend,
    )
    assert record["status"] == "scored"
    assert replies == []


# --- scheduler ----------------------------------------------------------------


def test_scheduler_accepts_lane_judge_types(tmp_path: Path) -> None:
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert scheduler.enqueue(_queue_entry(
            "lane_prd_fidelity", "wf-lane", "trial-1", "a" * 64, channel="lane-a",
        ))
        assert scheduler.enqueue(_queue_entry(
            "cross_compare", "wf-lane", "trial-1", "b" * 64, channel="compare",
        ))
    finally:
        scheduler.close()
    payloads = [
        event.payload for event in EventLog(tmp_path / "events.jsonl").read_all()
        if event.type == "benchmark.evaluation.progress"
    ]
    lane_payloads = [p for p in payloads if p.get("judge_type") == "lane_prd_fidelity"]
    compare_payloads = [p for p in payloads if p.get("judge_type") == "cross_compare"]
    assert lane_payloads and compare_payloads
    assert all(p["evaluation_contract"] == LANE_EVALUATION_CONTRACT for p in payloads)
    assert {p["prompt_version"] for p in payloads} == {
        LANE_PRD_FIDELITY_PROMPT_VERSION, CROSS_COMPARE_PROMPT_VERSION,
    }


def test_cross_compare_tolerates_inconclusive_lane_dependency(tmp_path: Path) -> None:
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        lane_a = _queue_entry("lane_prd_fidelity", "wf", "t", "a" * 64, channel="lane-a")
        assert scheduler.enqueue(lane_a)
        cross = _queue_entry(
            "cross_compare", "wf", "t", "c" * 64, deps=[lane_a.job_id], channel="compare",
        )
        assert scheduler.enqueue(cross)
        lane_b = _queue_entry(
            "lane_prd_fidelity", "wf", "t", "b" * 64, deps=[lane_a.job_id], channel="lane-b",
        )
        assert scheduler.enqueue(lane_b)
        ran: list[str] = []

        def runner(entry: EvaluationQueueEntry, _slot: int) -> dict:
            ran.append(entry.job_id)
            if entry.job_id == lane_a.job_id:
                return {"status": "inconclusive", "error": "provider down"}
            return {"status": "completed"}

        scheduler.tick(runner)
        entries = scheduler.entries
        assert entries[lane_a.job_id].status == "inconclusive"
        assert entries[cross.job_id].status == "completed"
        assert cross.job_id in ran
        # Non-aggregator dependents still fail closed on an inconclusive dep.
        assert entries[lane_b.job_id].status == "inconclusive"
        assert lane_b.job_id not in ran
    finally:
        scheduler.close()


def test_scheduler_rejects_lane_job_with_channel_versioned_id(tmp_path: Path) -> None:
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        entry = _queue_entry("cross_compare", "wf", "t", "c" * 64, channel="compare")
        entry.job_id = evaluation_request_id(
            workflow_run_id="wf", trial_id="t", judge_type="cross_compare",
            source_snapshot_digest="c" * 64,
        )
        assert not scheduler.enqueue(entry)
    finally:
        scheduler.close()


# --- report -------------------------------------------------------------------


def test_report_without_lane_stages_is_unchanged() -> None:
    judges = {
        stage: {"status": "scored", "result": {}}
        for stage in ("task_type", "contribution", "discussion", "prd_fidelity")
    }
    report = render_report(
        workflow_run_id="wf", trial_id="t", channel_id="ch-1", freeze={}, judges=judges,
    )
    assert "Cross Compare" not in report
    assert "Lane PRD Fidelity" not in report
    assert "lane_prd_fidelity" not in report


def test_report_renders_lane_fidelity_and_cross_compare_sections() -> None:
    lane_result, error = _normalize_lane(_lane_raw())
    assert error == "" and lane_result is not None
    compare_result, error = _normalize_compare(_compare_raw())
    assert error == "" and compare_result is not None
    judges = {
        "lane_prd_fidelity": {
            "status": lane_result.status, "result": lane_result.to_dict(),
        },
        "cross_compare": {
            "status": compare_result.status, "result": compare_result.to_dict(),
        },
    }
    report = render_report(
        workflow_run_id="evalrun-1", trial_id="trial-1", channel_id="",
        freeze={}, judges=judges,
    )
    assert "Lane PRD fidelity (`lane_prd_fidelity`): **scored**" in report
    assert "Cross compare (`cross_compare`): **scored**" in report
    assert "## Lane PRD Fidelity（blind 四维度）" in report
    assert "综合参考分（四维度算术平均）: 3.0" in report
    assert "## Cross Compare 横向对比" in report
    assert "推荐排名: A > B > C" in report
    assert "关键差距（B）" in report
    assert "归因 `coverage`" in report
