"""Kanban -> Channel -> PRD evaluation chain (benchmark PRD contract tests).

Covers: packet identity/immutability (§5/§8.2/§8.3), deterministic scoring
(§4), §11 degradation rules, the judge runner (bounded retry, timeout class,
event emission), conclusion deterministic overrides, and the observer
trigger/dedup path with a fake backend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer, evaluation_runner
from zf.runtime.evaluation_packets import (
    build_packet,
    evaluation_request_id,
    evaluation_run_id,
    message_evidence_ref,
    stable_digest,
    write_immutable_packet,
)
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_schemas import (
    COMMUNICATION_DIMENSIONS,
    PRD_RESULT_DIMENSIONS,
    communication_score,
    normalize_judge_output,
    overall_score,
    prd_result_score,
)

ALLOWED = ["channel/message/evt-1", "artifact/prd/channel-frozen.json"]


def _team_findings() -> dict:
    return {"discussion_meaningful": True, "discussion_progressed": True,
            "prd_ready_for_next_stage": True, "problems": [],
            "underperformed_roles": [], "unnecessary_roles": []}


# ---------------------------------------------------------------- packets


def test_stable_digest_is_order_insensitive_and_stable() -> None:
    a = stable_digest({"x": 1, "y": [2, 3]})
    b = stable_digest({"y": [2, 3], "x": 1})
    assert a == b and len(a) == 64


def test_evaluation_request_id_identity_is_tuple_bound() -> None:
    kwargs = {
        "workflow_run_id": "wf-1",
        "trial_id": "trial-1",
        "judge_type": "milestone",
        "source_snapshot_digest": "d" * 64,
    }
    assert evaluation_request_id(**kwargs) == evaluation_request_id(**kwargs)
    changed = {**kwargs, "judge_type": "communication"}
    assert evaluation_request_id(**changed) != evaluation_request_id(**kwargs)
    bumped = {
        **kwargs,
        "prompt_version": f"{EVALUATION_PROMPT_VERSION}-next",
    }
    assert evaluation_request_id(**bumped) != evaluation_request_id(**kwargs)


def test_evaluation_run_id_is_deterministic_per_revision() -> None:
    assert evaluation_run_id(
        channel_id="ch-a", thread_id="main", prd_revision=2,
    ) == "channel-ch-a-main-r2"


def test_build_packet_derives_allowed_evidence_refs() -> None:
    packet = build_packet(
        stage="milestone",
        run_id="run-1",
        workflow_run_id="wf-1",
        trial_id="t-1",
        source_snapshot_digest="d" * 64,
        task={"task_id": "task-1", "statement": "s"},
        task_details={"evidence_keys": ["artifact/task/details.json"]},
        channel_messages=[
            {"event_id": "evt-1", "member_id": "m1", "role": "r", "text": "hi"},
        ],
        channel_frozen_prd={"prd_ref": "p", "readiness_ref": "r"},
        workflow_bound_prd={"prd_ref": "bp", "plan_ref": "pl"},
    )
    refs = packet["allowed_evidence_refs"]
    assert message_evidence_ref("evt-1") in refs
    assert "artifact/prd/channel-frozen.json" in refs
    assert "artifact/prd/channel-readiness.json" in refs
    assert "artifact/prd/workflow-bound.json" in refs
    assert "artifact/prd/workflow-plan.json" in refs
    assert "artifact/task/details.json" in refs
    # dedup: no duplicates even if a message ref repeats
    assert len(refs) == len(set(refs))
    assert packet["prompt_version"] == EVALUATION_PROMPT_VERSION


def test_write_immutable_packet_never_overwrites(tmp_path: Path) -> None:
    from dataclasses import replace

    packet = build_packet(
        stage="milestone",
        run_id="run-1",
        workflow_run_id="wf-1",
        trial_id="t-1",
        source_snapshot_digest="d" * 64,
        task={"task_id": "task-1"},
        channel_messages=[],
    )
    path, digest = write_immutable_packet(tmp_path, packet)
    assert path.exists()
    first = path.read_text(encoding="utf-8")
    # Same identity rewritten with extra unrelated mutations -> same path,
    # original bytes preserved.
    packet = replace(packet, forbidden_refs=["sneaky"])
    path2, digest2 = write_immutable_packet(tmp_path, packet)
    assert digest != digest2  # different digest -> different identity
    assert path2 != path
    assert path.read_text(encoding="utf-8") == first


# ---------------------------------------------------------------- scoring


def _dims(ids: list[str], scores: list[float]) -> list[dict]:
    return [
        {"dimension_id": dim_id, "score": score, "status": "scored",
         "evidence_refs": ALLOWED[:1], "rationale": "具体证据支持该分数", "confidence": 0.9}
        for dim_id, score in zip(ids, scores)
    ]


def test_communication_score_is_mean_of_five_dims() -> None:
    dims = _dims(list(COMMUNICATION_DIMENSIONS), [5, 4, 3, 2, 1])
    assert communication_score(dims) == 3.0


def test_score_is_none_when_any_required_dimension_missing() -> None:
    dims = _dims(list(COMMUNICATION_DIMENSIONS)[:4], [5, 4, 3, 2])
    assert communication_score(dims) is None
    dims = _dims(list(COMMUNICATION_DIMENSIONS), [5, 4, 3, 2, None])
    assert communication_score(dims) is None


def test_prd_result_score_is_mean_of_two_dims() -> None:
    dims = _dims(list(PRD_RESULT_DIMENSIONS), [4, 2])
    assert prd_result_score(dims) == 3.0


def test_prd_result_score_uses_original_dim_when_workflow_not_bound() -> None:
    """At freeze time only original_to_channel_prd is scored; the score
    should still be usable for the overall conclusion."""
    dims = _dims(["original_to_channel_prd"], [4])
    assert prd_result_score(dims) == 4.0


def test_prd_result_score_returns_none_when_no_dimension_scored() -> None:
    dims = _dims(["channel_to_workflow_prd"], [None])
    assert prd_result_score(dims) is None


def test_normalize_milestone_accepts_agent_reports_alias() -> None:
    """2026-09-17 v3 live run: the judge invented ``agent_reports`` for
    ``agent_findings``; the semantic content was fine, only the name drifted."""
    raw = {
        "schema_version": "milestone_result.v1",
        "judge_type": "milestone",
        "agent_reports": [
            {
                "agent_id": "a1",
                "score": 4,
                "necessity": "necessary",
                "what_it_did": "drove the design",
                "evidence_refs": [ALLOWED[0]],
                "confidence": 0.9,
                "rationale": "明确提出并推动了设计决策。",
            }
        ],
        "missing_evidence": [],
    }
    normalized, error = normalize_judge_output(
        judge_type="milestone", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert error == ""
    assert normalized is not None
    assert normalized.status == "scored"
    assert normalized.agent_findings[0].agent_id == "a1"


def test_normalize_communication_accepts_dimension_scores_alias() -> None:
    """2026-09-17 v3 live run: the judge used ``dimension_scores`` for the
    dimensions list."""
    raw = {
        "schema_version": "communication_result.v1",
        "judge_type": "communication",
        "dimension_scores": [
            {
                "dimension_id": dim_id,
                "score": 4,
                "confidence": 0.9,
                "evidence_refs": [ALLOWED[0]],
                "rationale": "clear exchange",
            }
            for dim_id in COMMUNICATION_DIMENSIONS
        ],
        "missing_evidence": [],
    }
    normalized, error = normalize_judge_output(
        judge_type="communication", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert error == ""
    assert normalized is not None
    assert normalized.status == "scored"
    assert normalized.communication_score == 4.0


def test_normalize_prd_result_unbound_accepts_original_dimension_only() -> None:
    """Before a workflow binds, the judge must omit the workflow boundary.

    A second null dimension is ambiguous and cannot silently turn a partial
    result into a valid score.
    """
    raw = {
        "schema_version": "prd_result.v1",
        "judge_type": "prd_result",
        "dimensions": [
            {
                "dimension_id": "original_to_channel_prd",
                "score": 4,
                "confidence": 0.9,
                "evidence_refs": ["artifact/prd/channel-frozen.json"],
                "rationale": "requirements preserved",
            },
        ],
        "missing_evidence": [],
    }
    # No workflow bound: workflow-bound.json NOT in allowed refs.
    normalized, error = normalize_judge_output(
        judge_type="prd_result", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert error == ""
    assert normalized is not None
    assert normalized.status == "scored" and normalized.prd_result_score == 4.0

    # Workflow bound: both dimensions are required.
    raw["dimensions"].append({
        "dimension_id": "channel_to_workflow_prd",
        "score": None,
        "confidence": 0.9,
        "evidence_refs": [],
        "rationale": "workflow evidence unavailable",
    })
    extra, extra_error = normalize_judge_output(
        judge_type="prd_result", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert extra is None and extra_error
    normalized_bound, error_bound = normalize_judge_output(
        judge_type="prd_result",
        raw=raw,
        allowed_evidence_refs=ALLOWED + ["artifact/prd/workflow-bound.json"],
    )
    assert not error_bound and normalized_bound is not None
    assert normalized_bound.status == "evidence_missing"
    assert normalized_bound.prd_result_score is None


def test_overall_score_applies_prd_weights() -> None:
    assert overall_score(communication=5.0, prd_result=2.0) == 3.2
    assert overall_score(communication=None, prd_result=2.0) is None
    assert overall_score(communication=5.0, prd_result=None) is None


# ------------------------------------------------------------- normalize


def _communication_raw() -> dict:
    dims = []
    for dim_id in COMMUNICATION_DIMENSIONS:
        dims.append({
            "dimension_id": dim_id,
            "score": 4,
            "confidence": 0.9,
            "evidence_refs": [ALLOWED[0]],
            "rationale": "r",
        })
    return {
        "schema_version": "communication_result.v1",
        "judge_type": "communication",
        "dimensions": dims,
        "missing_evidence": [],
    }


def test_normalize_unbound_evidence_ref_invalidates_item() -> None:
    raw = _communication_raw()
    raw["dimensions"][0]["evidence_refs"] = ["channel/message/evt-999"]
    normalized, error = normalize_judge_output(
        judge_type="communication", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert error == ""
    assert normalized is not None
    assert normalized.dimensions[0].status == "invalid"
    assert normalized.status == "invalid"


def test_normalize_low_confidence_abstains() -> None:
    raw = _communication_raw()
    raw["dimensions"][0]["confidence"] = 0.4
    normalized, _ = normalize_judge_output(
        judge_type="communication", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert normalized is not None
    assert normalized.dimensions[0].status == "abstain"
    # one abstain + rest scored -> conflict at result level
    assert normalized.status == "conflict"


def test_normalize_all_abstain_is_abstain_not_conflict() -> None:
    raw = _communication_raw()
    for dim in raw["dimensions"]:
        dim["confidence"] = 0.1
    normalized, _ = normalize_judge_output(
        judge_type="communication", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert normalized is not None
    assert normalized.status == "abstain"


def test_normalize_missing_evidence_marks_result() -> None:
    raw = _communication_raw()
    raw["missing_evidence"] = [ALLOWED[0]]
    normalized, _ = normalize_judge_output(
        judge_type="communication", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert normalized is not None
    assert normalized.status == "evidence_missing"


def test_normalize_out_of_range_score_requires_schema_repair() -> None:
    raw = _communication_raw()
    raw["dimensions"][0]["score"] = 9
    normalized, error = normalize_judge_output(
        judge_type="communication", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert normalized is None and error


def test_normalize_rejects_wrong_schema_version() -> None:
    raw = _communication_raw()
    raw["schema_version"] = "communication_result.v2"
    normalized, error = normalize_judge_output(
        judge_type="communication", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert normalized is None
    assert "schema_version" in error


def test_normalize_milestone_requires_necessity_vocab() -> None:
    raw = {
        "schema_version": "milestone_result.v1",
        "judge_type": "milestone",
        "agent_findings": [
            {
                "agent_id": "a1",
                "score": 4,
                "necessity": "indispensable",  # not in vocab
                "confidence": 0.9,
                "evidence_refs": [ALLOWED[0]],
                "rationale": "架构建议被采用并转化为实现方案。",
            }
        ],
        "missing_evidence": [],
    }
    normalized, error = normalize_judge_output(
        judge_type="milestone", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert normalized is None
    assert "necessity" in error


def test_normalize_accepts_missing_judge_type_but_rejects_contradiction() -> None:
    """Regression 2026-09-17 live run: the judge returned a valid
    milestone_result.v1 without a ``judge_type`` field — schema_version
    already binds the type, so absence is fine, contradiction is not."""
    raw = {
        "schema_version": "milestone_result.v1",
        "agent_findings": [
            {
                "agent_id": "a1",
                "score": 4,
                "necessity": "necessary",
                "summary": "contributed design",  # judges say "summary"
                "confidence": 0.9,
                "evidence_refs": [ALLOWED[0]],
                "rationale": "指出了关键缺口并给出修复方向。",
            }
        ],
        "missing_evidence": [],
    }
    normalized, error = normalize_judge_output(
        judge_type="milestone", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert error == ""
    assert normalized is not None
    assert normalized.agent_findings[0].role_summary == "contributed design"

    raw["judge_type"] = "communication"  # explicit contradiction
    normalized, error = normalize_judge_output(
        judge_type="milestone", raw=raw, allowed_evidence_refs=ALLOWED,
    )
    assert normalized is None
    assert "judge_type mismatch" in error


def test_normalize_accepts_live_model_output_shapes() -> None:
    """Regression 2026-09-17 live run (kimi via cc-switch): judges emit
    ``evaluations`` for agent_findings, ``scores`` as a dimension-keyed
    dict, top-level ``confidence``, and ``missing_evidence_keys``."""
    milestone = {
        "schema_version": "milestone_result.v1",
        "evaluations": [
            {
                "agent_id": "a1",
                "role": "arch",
                "score": 5,
                "necessity": "necessary",
                "summary": "deep design",
                "adopted": True,
                "gap": "",
                "evidence_refs": [ALLOWED[0]],
                "rationale": "设计结论被消息证据直接支持。",
            },
            {
                "agent_id": "a2",
                "role": "critic",
                "score": 4,
                "necessity": "necessary",
                "summary": "gap analysis",
                "adopted": True,
                "gap": "minor",
                "evidence_refs": [ALLOWED[0]],
                "rationale": "指出了实现风险并给出改进建议。",
            },
        ],
        "confidence": 0.92,
        "missing_evidence_keys": [],
    }
    normalized, error = normalize_judge_output(
        judge_type="milestone", raw=milestone, allowed_evidence_refs=ALLOWED,
    )
    assert error == "" and normalized is not None
    assert normalized.status == "scored"
    assert all(a.confidence == 0.92 for a in normalized.agent_findings)
    assert normalized.agent_findings[0].role_summary == "deep design"

    communication = {
        "schema_version": "communication_result.v1",
        "scores": {
            "information_exchange": {"score": 5, "rationale": "rich exchange", "evidence_refs": ALLOWED[:1]},
            "clarity": {"score": 5, "rationale": "clear", "evidence_refs": ALLOWED[:1]},
            "task_assistance": {"score": 5, "rationale": "on task", "evidence_refs": ALLOWED[:1]},
            "efficiency": {"score": 4, "rationale": "some rounds", "evidence_refs": ALLOWED[:1]},
            "necessity": {"score": 4, "rationale": "one quiet spell", "evidence_refs": ALLOWED[:1]},
        },
        "confidence": 0.92,
        "evidence_refs": ALLOWED[:3],
        "overall_rationale": "strong",
        "missing_evidence_keys": [],
    }
    normalized, error = normalize_judge_output(
        judge_type="communication", raw=communication,
        allowed_evidence_refs=ALLOWED,
    )
    assert error == "" and normalized is not None
    assert normalized.status == "scored"
    assert normalized.communication_score == 4.6
    ids = {d.dimension_id for d in normalized.dimensions}
    assert ids == set(COMMUNICATION_DIMENSIONS)


# ---------------------------------------------------------------- runner


def _packet(judge_type: str = "milestone", run_id: str = "wf-x") -> dict:
    return build_packet(
        stage=judge_type,
        run_id=run_id,
        workflow_run_id=run_id,
        trial_id="t-1",
        source_snapshot_digest="d" * 64,
        task={"task_id": "task-1", "statement": "do X"},
        task_details={},
        channel_messages=[
            {
                "event_id": "evt-1",
                "member_id": "m1",
                "role": "dev",
                "text": "proposal",
            }
        ],
        channel_frozen_prd={"prd_ref": "p", "body": "# PRD\n\nDo X."},
    )


def _milestone_reply() -> str:
    return json.dumps(
        {
            "schema_version": "milestone_result.v1",
            "judge_type": "milestone",
            "agent_findings": [
                {
                    "agent_id": "m1",
                    "score": 4,
                    "necessity": "necessary",
                    "role_summary": "drove the design",
                    "evidence_refs": ["channel/message/evt-1"],
                    "rationale": "cited concrete arguments",
                    "confidence": 0.9,
                }
            ],
            "missing_evidence": [],
        }
    )


def _scored_record(run_id: str = "wf-x") -> dict:
    return {
        "status": "scored",
        "result": {
            "schema_version": "milestone_result.v1",
            "judge_type": "milestone",
            "agent_findings": [
                {
                    "agent_id": "m1",
                    "score": 4,
                    "necessity": "necessary",
                    "status": "scored",
                    "confidence": 0.9,
                    "evidence_refs": ["channel/message/evt-1"],
                    "rationale": "cited concrete arguments",
                }
            ],
            "missing_evidence": [],
        },
    }


def test_run_judge_scores_and_emits_event(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    calls: list[str] = []

    def backend(**kwargs: object) -> tuple[str, str]:
        calls.append(str(kwargs["judge_type"]))
        return _milestone_reply(), ""

    record = evaluation_runner.run_judge(
        state_dir=tmp_path,
        project_root=None,
        config=None,
        writer=writer,
        workflow_run_id="wf-x",
        trial_id="t-1",
        judge_type="milestone",
        packet=_packet(),
        backend=backend,
    )
    assert record["status"] == "scored"
    assert calls == ["milestone"]
    assert evaluation_runner.read_judge_result(
        tmp_path,
        workflow_run_id="wf-x",
        judge_type="milestone",
        request_id=record["request_id"],
    ) is not None
    # human-readable audit trail next to the record
    sidecar_dir = tmp_path / "artifacts/benchmark-evaluations/judges/wf-x/milestone"
    prompt_md = sidecar_dir / f"{record['request_id']}.prompt.md"
    raw_md = sidecar_dir / f"{record['request_id']}.raw.md"
    packet_txt = sidecar_dir / f"{record['request_id']}.packet.txt"
    assert prompt_md.exists() and raw_md.exists() and packet_txt.exists()
    prompt_text = prompt_md.read_text(encoding="utf-8")
    assert "read-only evaluator" in prompt_text  # system prompt included
    # the TUI backend receives a short FILE REFERENCE (codex folds any large
    # pasted input into a "[Pasted Content]" placeholder); the full rendered
    # packet lives in the sidecar the judge reads.
    assert str(packet_txt) in prompt_text
    packet_text = packet_txt.read_text(encoding="utf-8")
    assert "task-1" in packet_text  # rendered packet included
    assert "milestone_result.v1" in packet_text  # schema instruction included
    assert _milestone_reply() in raw_md.read_text(encoding="utf-8")
    events = list(writer.event_log.read_all())
    judge_events = [
        event for event in events
        if event.type == "benchmark.evaluation.judge.completed"
    ]
    assert len(judge_events) == 1
    assert judge_events[0].payload["status"] == "scored"
    assert judge_events[0].payload["prompt_version"] == EVALUATION_PROMPT_VERSION
    assert judge_events[0].payload["judge_type"] == "milestone"


def test_run_judge_dedups_on_identity(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    calls: list[str] = []

    def backend(**kwargs: object) -> tuple[str, str]:
        calls.append("hit")
        return _milestone_reply(), ""

    kwargs = dict(
        state_dir=tmp_path,
        project_root=None,
        config=None,
        writer=writer,
        workflow_run_id="wf-x",
        trial_id="t-1",
        judge_type="milestone",
        packet=_packet(),
        backend=backend,
    )
    first = evaluation_runner.run_judge(**kwargs)
    second = evaluation_runner.run_judge(**kwargs)
    assert first["request_id"] == second["request_id"]
    assert calls == ["hit"]  # sidecar dedup, no second backend turn
    events = [
        event for event in writer.event_log.read_all()
        if event.type == "benchmark.evaluation.judge.completed"
    ]
    assert len(events) == 1  # dedup also means no duplicate event


def test_run_judge_transport_error_exhausts_bounded_retry(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    calls = []

    def backend(**kwargs: object) -> tuple[str, str]:
        calls.append(kwargs["attempt"])
        return "", "provider exploded"

    record = evaluation_runner.run_judge(
        state_dir=tmp_path,
        project_root=None,
        config=None,
        writer=writer,
        workflow_run_id="wf-x",
        trial_id="t-1",
        judge_type="milestone",
        packet=_packet(),
        backend=backend,
    )
    assert record["status"] == "inconclusive"
    assert calls == [1, 2]
    assert record["attempt"] == record["max_attempts"] == 2
    assert record["retryable"] is False


def test_run_judge_garbage_json_marks_invalid(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))

    def backend(**kwargs: object) -> tuple[str, str]:
        return "not json at all", ""

    record = evaluation_runner.run_judge(
        state_dir=tmp_path,
        project_root=None,
        config=None,
        writer=writer,
        workflow_run_id="wf-x",
        trial_id="t-1",
        judge_type="milestone",
        packet=_packet(),
        backend=backend,
    )
    assert record["status"] == "invalid"
    assert record["error"]


def test_conclusion_overall_is_deterministically_recomputed(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    comm_record = {
        "status": "scored",
        "result": {
            "dimensions": [
                {
                    "dimension_id": dim_id,
                    "score": 5,
                    "status": "scored",
                    "confidence": 0.9,
                    "evidence_refs": [ALLOWED[0]],
                    "rationale": "明确交换了验收信息",
                }
                for dim_id in COMMUNICATION_DIMENSIONS
            ]
        },
    }
    prd_record = {
        "status": "scored",
        "result": {
            "dimensions": [
                {"dimension_id": dim_id, "score": 2, "status": "scored",
                 "confidence": 0.9, "evidence_refs": [ALLOWED[1]],
                 "rationale": "遗漏了原始需求的关键条件"}
                for dim_id in PRD_RESULT_DIMENSIONS
            ]
        },
    }
    conclusion_reply = json.dumps(
        {
            "schema_version": "conclusion_result.v1",
            "judge_type": "conclusion",
            "status": "complete",
            # agent tries to fudge the numbers — runner must override
            "communication_score": 1.0,
            "prd_result_score": 1.0,
            "overall_score": 1.0,
            "agent_scores": [],
            "team_findings": _team_findings(),
            "conclusion_zh": "结论：可用",
            "next_actions": [],
        }
    )

    def backend(**kwargs: object) -> tuple[str, str]:
        return conclusion_reply, ""

    record = evaluation_runner.run_conclusion(
        state_dir=tmp_path,
        project_root=None,
        config=None,
        writer=writer,
        workflow_run_id="wf-x",
        trial_id="t-1",
        judges={
            "milestone": _scored_record(),
            "communication": comm_record,
            "prd_result": prd_record,
        },
        backend=backend,
    )
    result = record["result"]
    assert result["communication_score"] == 5.0
    assert result["prd_result_score"] == 2.0
    assert result["overall_score"] == 3.2  # 0.4*5 + 0.6*2
    events = [
        event for event in writer.event_log.read_all()
        if event.type == "benchmark.evaluation.conclusion.completed"
    ]
    assert len(events) == 1
    assert events[0].payload["overall_score"] == 3.2


# --------------------------------------------------------------- observer


def test_evaluation_window_env_kill_switch(
    tmp_path: Path, monkeypatch,
) -> None:
    from zf.runtime.evaluation_tmux import (
        evaluation_window_enabled,
        spawn_evaluation_window,
    )

    monkeypatch.delenv("ZF_EVALUATION_WINDOW", raising=False)
    assert evaluation_window_enabled(None) is True
    monkeypatch.setenv("ZF_EVALUATION_WINDOW", "0")
    assert evaluation_window_enabled(None) is False
    monkeypatch.setenv("ZF_EVALUATION_WINDOW", "off")
    assert evaluation_window_enabled(None) is False
    # dry_run never touches tmux; disabled env only gates the start.py caller
    assert spawn_evaluation_window(
        tmux_session="zf-no-such-session",
        state_dir=tmp_path,
        project_root=tmp_path,
        dry_run=True,
    ) is True


def test_evaluation_window_spawn_fails_closed_without_session(
    tmp_path: Path,
) -> None:
    from zf.runtime.evaluation_tmux import spawn_evaluation_window

    assert spawn_evaluation_window(
        tmux_session="zf-no-such-session-definitely",
        state_dir=tmp_path,
        project_root=tmp_path,
        dry_run=False,
    ) is False


# --------------------------------------------------------- visible judges


def test_judge_argv_is_orchestrator_bypass_spawn() -> None:
    """Judges spawn like orchestrator bypass workers, not raw claude."""
    from zf.runtime.evaluation_tmux import (
        build_judge_argv,
        build_judge_role,
        judge_session_uuid,
    )

    role = build_judge_role()
    assert role.backend == "claude-code"
    assert role.permission_mode == "bypass"  # --dangerously-skip-permissions

    argv = build_judge_argv(session_uuid="s-1", system_prompt="SYS")
    assert argv[0] == "claude"
    assert "--dangerously-skip-permissions" in argv
    i = argv.index("--session-id")
    assert argv[i + 1] == "s-1"
    i = argv.index("--append-system-prompt")
    assert argv[i + 1] == "SYS"
    assert "--verbose" in argv
    assert "--tools" not in argv  # bypass mode: full autonomy by contract

    codex_role = build_judge_role("codex")
    assert codex_role.backend == "codex"
    codex_argv = build_judge_argv(
        session_uuid="s-1", system_prompt="SYS", backend="codex",
    )
    assert codex_argv[0] == "codex"
    assert "--dangerously-bypass-approvals-and-sandbox" in codex_argv
    assert "--append-system-prompt" not in codex_argv

    # deterministic identity: same request -> same provider session
    a = judge_session_uuid(Path("/p"), "wf", "milestone", "evalreq-x")
    b = judge_session_uuid(Path("/p"), "wf", "milestone", "evalreq-x")
    assert (
        a == b
        and a != judge_session_uuid(Path("/p"), "wf", "communication", "evalreq-x")
    )


def test_judge_backend_follows_orchestrator_then_provider_role() -> None:
    from types import SimpleNamespace

    from zf.runtime.evaluation_tmux import resolve_judge_backend

    config = SimpleNamespace(
        orchestrator=SimpleNamespace(backend="codex"),
        roles=[SimpleNamespace(backend="claude-code")],
    )
    assert resolve_judge_backend(config) == "codex"
    fallback = SimpleNamespace(
        orchestrator=SimpleNamespace(backend="python"),
        roles=[SimpleNamespace(backend="claude-headless")],
    )
    assert resolve_judge_backend(fallback) == "claude-code"


def test_visible_judge_passes_project_backend_to_tmux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from zf.runtime import evaluation_runner, evaluation_tmux
    from zf.runtime.evaluation_turns import JudgeTurnObservation
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-home"))

    calls: dict[str, object] = {}
    monkeypatch.setattr(
        evaluation_tmux, "start_judge_agent",
        lambda *args, **kwargs: (calls.update(start=kwargs), (True, ""))[1],
    )
    monkeypatch.setattr(
        evaluation_tmux, "send_prompt_to_window",
        lambda _session, prompt, **kwargs: calls.update(prompt=prompt, send=kwargs),
    )
    monkeypatch.setattr(
        evaluation_tmux, "read_judge_reply",
        lambda *args, **kwargs: calls.update(read=kwargs) or JudgeTurnObservation(status="completed", reply='{"ok":true}'),
    )
    config = SimpleNamespace(
        session=SimpleNamespace(tmux_session="zf-test"),
        orchestrator=SimpleNamespace(backend="codex"),
        roles=[],
    )
    observed = evaluation_runner._visible_tmux_backend(
        state_dir=tmp_path,
        project_root=Path("/tmp/project"),
        config=config,
        workflow_run_id="wf",
        judge_type="milestone",
        request_id="req",
        system_prompt="PERSONA",
        prompt="PACKET",
    )
    assert (observed.reply, observed.error) == ('{"ok":true}', "")
    assert calls["start"]["backend"] == "codex"
    # codex quirks (2026-09-21): multi-line drafts never submit on plain
    # Enter, and >~1000 chars gets folded into a "[Pasted Content]"
    # placeholder — so the turn prompt is ONE SHORT LINE with the compact
    # persona, not the caller's (possibly long) system prompt.
    from zf.runtime.evaluation_runner import _CODEX_COMPACT_PERSONA
    assert calls["prompt"].endswith(f"{_CODEX_COMPACT_PERSONA} PACKET")
    assert calls["prompt"].startswith("evaluation-turn:")
    assert calls["read"]["request_marker"] in calls["prompt"]
    assert calls["read"]["codex_sessions_root"] == calls["start"]["codex_home"] / "sessions"
    assert calls["send"]["backend"] == "codex"
    assert calls["read"]["backend"] == "codex"


def test_judge_readiness_waits_for_tui_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression 2026-09-17: readiness must be pane-based. Waiting for the
    provider transcript file before pasting the prompt kills every judge
    (interactive claude creates the file only on first submit)."""
    from zf.runtime import evaluation_tmux

    states = iter([
        ("bash", ""),                      # still booting
        ("claude", "starting up..."),      # TUI without prompt glyph yet
        ("claude", "some output\n❯ "),     # ready
    ])

    monkeypatch.setattr(
        evaluation_tmux, "_pane_current_command",
        lambda session, window: next(states)[0],
    )
    monkeypatch.setattr(
        evaluation_tmux, "_capture_pane",
        lambda session, window, lines=60: next(states)[1],
    )
    monkeypatch.setattr(evaluation_tmux.time, "sleep", lambda _s: None)

    ready, blocker = evaluation_tmux.wait_judge_ready(
        "sess", timeout_s=5.0,
    )
    assert ready and blocker == ""


def test_judge_readiness_surfaces_interactive_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zf.runtime import evaluation_tmux
    import zf.runtime.provider_interactive_prompt as prompts

    monkeypatch.setattr(
        evaluation_tmux, "_pane_current_command", lambda s, w: "claude",
    )
    monkeypatch.setattr(
        evaluation_tmux, "_capture_pane", lambda s, w, lines=60: "screen",
    )
    monkeypatch.setattr(
        prompts, "provider_interactive_prompt_marker", lambda screen: "trust_prompt",
    )
    monkeypatch.setattr(evaluation_tmux.time, "sleep", lambda _s: None)

    ready, blocker = evaluation_tmux.wait_judge_ready("sess", timeout_s=5.0)
    assert not ready and blocker == "trust_prompt"


def test_wait_pane_quiet_waits_for_paste_to_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zf.runtime import evaluation_tmux

    screens = iter(["growing", "growing..", "growing...", "settled", "settled"])
    monkeypatch.setattr(
        evaluation_tmux, "_capture_pane",
        lambda s, w, lines=60: next(screens),
    )
    slept: list[float] = []
    monkeypatch.setattr(evaluation_tmux.time, "sleep", slept.append)
    evaluation_tmux._wait_pane_quiet("sess", "judges")
    # polled until two consecutive captures matched, then returned
    assert slept[-1] == 0.7 or len(slept) >= 2


def test_stop_judge_agent_noops_without_live_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zf.runtime import evaluation_tmux

    called: list[str] = []
    monkeypatch.setattr(
        evaluation_tmux, "_pane_current_command", lambda s, w: "bash",
    )
    monkeypatch.setattr(
        evaluation_tmux.subprocess, "run",
        lambda *a, **k: called.append("run"),
    )
    evaluation_tmux.stop_judge_agent("sess")
    assert called == []  # nothing sent to the pane


def test_stop_judge_agent_exits_gracefully_without_pane_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-17 regression: /exit must be given a grace period and only a
    truly stuck claude escalates to respawn-pane -k."""
    from zf.runtime import evaluation_tmux

    states = iter(["claude", "claude", "bash"])  # entry check, poll, poll
    sent: list[str] = []
    monkeypatch.setattr(
        evaluation_tmux, "_pane_current_command", lambda s, w: next(states),
    )
    monkeypatch.setattr(
        evaluation_tmux, "_send_line",
        lambda s, w, text: sent.append(text),
    )
    monkeypatch.setattr(evaluation_tmux.time, "sleep", lambda _s: None)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        evaluation_tmux.subprocess, "run",
        lambda *a, **k: ran.append(list(a[0]) if a else []),
    )
    evaluation_tmux.stop_judge_agent("sess")
    assert "/exit" in sent
    assert not any("respawn-pane" in cmd for cmd in ran)


def test_stop_judge_agent_escalates_to_pane_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zf.runtime import evaluation_tmux

    monkeypatch.setattr(
        evaluation_tmux, "_pane_current_command", lambda s, w: "claude",
    )
    monkeypatch.setattr(
        evaluation_tmux, "_send_line", lambda s, w, text: None,
    )
    monkeypatch.setattr(evaluation_tmux.time, "sleep", lambda _s: None)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        evaluation_tmux.subprocess, "run",
        lambda *a, **k: ran.append(list(a[0]) if a else []),
    )
    evaluation_tmux.stop_judge_agent("sess", timeout_s=0.2)
    assert any("respawn-pane" in cmd and "-k" in cmd for cmd in ran)


def test_start_judge_agent_leaves_startup_retry_to_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Startup failure consumes a durable runner attempt, without hidden retries."""
    from zf.runtime import evaluation_tmux

    monkeypatch.setattr(
        evaluation_tmux, "ensure_judges_window", lambda s, p, **k: True,
    )
    monkeypatch.setattr(
        evaluation_tmux, "stop_judge_agent", lambda s, **k: None,
    )
    monkeypatch.setattr(
        evaluation_tmux, "ensure_workspace_trusted", lambda p: None,
    )
    sent: list[str] = []
    monkeypatch.setattr(
        evaluation_tmux, "_send_line",
        lambda s, w, text, **k: sent.append(text),
    )
    monkeypatch.setattr(evaluation_tmux.time, "sleep", lambda _s: None)
    monkeypatch.setattr(evaluation_tmux.subprocess, "run", lambda *a, **k: None)
    readiness = iter([
        (False, "provider_launch_not_submitted"),
        (True, ""),
    ])
    monkeypatch.setattr(
        evaluation_tmux, "wait_judge_ready",
        lambda s, **k: next(readiness),
    )
    ok, error = evaluation_tmux.start_judge_agent(
        "sess", tmp_path, session_uuid="u-1", system_prompt="S",
    )
    assert not ok and "provider_launch_not_submitted" in error
    assert len(sent) == 1


def test_start_judge_agent_fails_closed_without_tmux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from zf.runtime import evaluation_tmux

    monkeypatch.setattr(
        evaluation_tmux, "_window_exists", lambda s, w: False,
    )
    monkeypatch.setattr(
        evaluation_tmux, "_spawn_window", lambda s, w, root: False,
    )
    ok, error = evaluation_tmux.start_judge_agent(
        "no-such-session", tmp_path,
        session_uuid="u", system_prompt="S",
    )
    assert not ok and "judges window" in error


def test_extract_assistant_reply_only_completes_turns(tmp_path: Path) -> None:
    from zf.runtime.evaluation_tmux import extract_assistant_reply

    session = tmp_path / "s.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "draft in progress"}],
            "stop_reason": None,
        }},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Read"}],
            "stop_reason": "tool_use",
        }},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "final answer"}],
            "stop_reason": "end_turn",
        }},
    ]
    session.write_text(
        "\n".join(json.dumps(e) for e in entries), encoding="utf-8",
    )
    assert extract_assistant_reply(session) == "final answer"
    session.write_text(json.dumps(entries[0]), encoding="utf-8")
    assert extract_assistant_reply(session) == ""
    assert extract_assistant_reply(tmp_path / "missing.jsonl") == ""


def test_extract_codex_reply_only_completes_turns(tmp_path: Path) -> None:
    from zf.runtime.evaluation_tmux import extract_assistant_reply

    session = tmp_path / "rollout.jsonl"
    entries = [
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "final answer"}],
        }},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    session.write_text(
        "\n".join(json.dumps(e) for e in entries), encoding="utf-8",
    )
    assert extract_assistant_reply(session, backend="codex") == "final answer"


def test_observer_rerun_after_runtime_noise_still_dedups(
    tmp_path: Path,
) -> None:
    """Regression: runtime events (member connect, typing) must not change
    the source snapshot digest — otherwise every pass re-runs the judges
    forever (2026-09-16 live incident on ch-auto-82198e0f5f)."""
    posted_ids = _seed_channel_state(tmp_path)
    message_ref = message_evidence_ref(posted_ids[0])
    replies = {
        judge_type: _judge_reply(judge_type, message_ref)
        for judge_type in ("milestone", "communication", "prd_result", "conclusion")
    }
    backend = _fake_backend(replies)
    first = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert first["judges_run"] == 4  # 3 judges at freeze + conclusion
    # runtime-only traffic after the freeze: changes live projection state
    # but nothing the packet is built from
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit(
        "channel.member.connected", actor="test",
        payload={"channel_id": "ch-1", "member_id": "m1"},
    )
    writer.emit(
        "channel.typing.started", actor="test",
        payload={"channel_id": "ch-1", "member_id": "m2", "thread_id": "main"},
    )
    writer.emit(
        "channel.message.read", actor="test",
        payload={"channel_id": "ch-1", "member_id": "m1", "thread_id": "main"},
    )
    second = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert second["errors"] == []
    assert second["judges_run"] == 0
    assert backend.calls == [  # type: ignore[attr-defined]
        "milestone", "communication", "prd_result", "conclusion",
    ]


def _seed_channel_state(
    tmp_path: Path,
    *,
    with_workflow_bind: bool = False,
    receipt_kind: str = "prd_confirmed",
) -> list[str]:
    """Seed a frozen channel; returns the posted message event ids."""
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit(
        "channel.created",
        actor="test",
        payload={"channel_id": "ch-1", "name": "ch-1", "task_id": "task-1"},
    )
    for member_id in ("m1", "m2"):
        writer.emit(
            "channel.member.added",
            actor="test",
            payload={"channel_id": "ch-1", "member_id": member_id,
                     "member_type": "agent", "backend": "claude-headless"},
        )
    posted_event_ids: list[str] = []
    for idx, member_id in enumerate(("m1", "m2"), start=1):
        posted = writer.emit(
            "channel.message.posted",
            actor="test",
            payload={
                "channel_id": "ch-1",
                "message_id": f"msg-{idx}",
                "member_id": member_id,
                "role": "dev",
                "thread_id": "main",
                "text": f"proposal {idx}",
            },
        )
        posted_event_ids.append(posted.id)
    prd_ref = "channel-artifacts/ch-1/prd.md"
    (tmp_path / prd_ref).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / prd_ref).write_text("# Frozen PRD\n\nDo X.\n", encoding="utf-8")
    writer.emit(
        "channel.consensus.reached",
        actor="test",
        payload={
            "channel_id": "ch-1",
            "thread_id": "main",
            "prd_ref": prd_ref,
            "prd_digest": "a" * 64,
            "prd_revision": 1,
            "readiness_ref": "channel-artifacts/ch-1/readiness.json",
            "task_id": "task-1",
        },
    )
    if receipt_kind != "prd_confirmed":
        writer.emit(
            "channel.result.receipt.recorded",
            actor="test",
            payload={"channel_id": "ch-1", "receipt_kind": receipt_kind},
        )
    if with_workflow_bind:
        writer.emit(
            "workflow.invoke.requested",
            actor="test",
            payload={
                "channel_id": "ch-1",
                "workflow_run_id": "wf-bound-1",
                "task_id": "task-1",
                "prd_ref": prd_ref,
            },
        )
    return posted_event_ids


def _fake_backend(replies: dict[str, str]):
    calls: list[str] = []

    def backend(**kwargs: object) -> tuple[str, str]:
        judge_type = str(kwargs["judge_type"])
        calls.append(judge_type)
        return replies[judge_type], ""

    backend.calls = calls  # type: ignore[attr-defined]
    return backend


def _judge_reply(judge_type: str, message_ref: str = "", with_bound: bool = False) -> str:
    if judge_type == "milestone":
        return json.dumps(
            {
                "schema_version": "milestone_result.v1",
                "judge_type": "milestone",
                "agent_findings": [
                    {
                        "agent_id": agent_id,
                        "score": 4,
                        "necessity": "necessary",
                        "role_summary": "drove the design",
                        "evidence_refs": [message_ref or "artifact/prd/channel-frozen.json"],
                        "rationale": "cited concrete arguments",
                        "confidence": 0.9,
                    } for agent_id in ("m1", "m2")
                ],
                "missing_evidence": [],
            }
        )
    if judge_type == "communication":
        return json.dumps(
            {
                "schema_version": "communication_result.v1",
                "judge_type": "communication",
                "dimensions": [
                    {
                        "dimension_id": dim_id,
                        "score": 4,
                        "confidence": 0.9,
                        "evidence_refs": [message_ref or "artifact/prd/channel-frozen.json"],
                        "rationale": "clear exchange",
                    }
                    for dim_id in COMMUNICATION_DIMENSIONS
                ],
                "human_intervention_count": 0,
                "missing_evidence": [],
            }
        )
    if judge_type == "prd_result":
        # At freeze time only original_to_channel_prd is scored; once a
        # workflow binds the judge also scores channel_to_workflow_prd.
        dim_ids = (
            list(PRD_RESULT_DIMENSIONS)
            if with_bound
            else ["original_to_channel_prd"]
        )
        return json.dumps(
            {
                "schema_version": "prd_result.v1",
                "judge_type": "prd_result",
                "dimensions": [
                    {
                        "dimension_id": dim_id,
                        "score": 3,
                        "confidence": 0.8,
                        "evidence_refs": ["artifact/prd/channel-frozen.json"],
                        "verdict": "aligned",
                        "rationale": "requirements preserved",
                    }
                    for dim_id in dim_ids
                ],
                "differences": [],
                "missing_evidence": [],
            }
        )
    return json.dumps(
        {
            "schema_version": "conclusion_result.v1",
            "judge_type": "conclusion",
            "status": "complete",
            "agent_scores": [],
            "team_findings": _team_findings(),
            "conclusion_zh": "结论：通过",
            "next_actions": [],
        }
    )


def test_observer_triggers_on_consensus_and_runs_two_judges(
    tmp_path: Path,
) -> None:
    posted_ids = _seed_channel_state(tmp_path)
    message_ref = message_evidence_ref(posted_ids[0])
    backend = _fake_backend(
        {judge_type: _judge_reply(judge_type, message_ref)
         for judge_type in ("milestone", "communication", "prd_result", "conclusion")}
    )
    stats = evaluation_observer.observe_once(
        state_dir=tmp_path, backend=backend,
    )
    assert stats["errors"] == []
    # prd_result runs at freeze (original vs channel PRD); conclusion on all
    # three judges — a channel that never binds still concludes.
    assert stats["judges_run"] == 4
    run_id = evaluation_run_id(channel_id="ch-1", thread_id="main", prd_revision=1)
    report = tmp_path / "artifacts" / "benchmark-evaluations" / "reports" / run_id / "report.md"
    assert report.exists()
    body = report.read_text(encoding="utf-8")
    assert "communication_score: 4.0" in body
    assert "## 4. PRD Result Score" in body
    # informational judge events landed in the ledger, plus the conclusion
    events = list(EventLog(tmp_path / "events.jsonl").read_all())
    judge_completed = [
        event for event in events
        if event.type == "benchmark.evaluation.judge.completed"
    ]
    assert len(judge_completed) == 3
    conclusion_completed = [
        event for event in events
        if event.type == "benchmark.evaluation.conclusion.completed"
    ]
    assert len(conclusion_completed) == 1


def test_observer_dedups_on_second_pass(tmp_path: Path) -> None:
    posted_ids = _seed_channel_state(tmp_path, with_workflow_bind=True)
    message_ref = message_evidence_ref(posted_ids[0])
    replies = {
        judge_type: _judge_reply(judge_type, message_ref, with_bound=True)
        for judge_type in ("milestone", "communication", "prd_result", "conclusion")
    }
    backend = _fake_backend(replies)
    first = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert first["errors"] == []
    assert first["judges_run"] == 4  # 3 judges + conclusion
    second = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert second["judges_run"] == 0
    assert backend.calls == ["milestone", "communication", "prd_result", "conclusion"]  # type: ignore[attr-defined]
    events = list(EventLog(tmp_path / "events.jsonl").read_all())
    conclusion_events = [
        event for event in events
        if event.type == "benchmark.evaluation.conclusion.completed"
    ]
    assert len(conclusion_events) == 1


def test_observer_retries_transport_failed_judge_but_not_completed(
    tmp_path: Path,
) -> None:
    """Regression 2026-09-17 test_dev3: a ``judge_timeout`` record (provider
    never booted) must NOT block the request identity forever — the next
    pass re-runs that judge. A genuine verdict still dedups."""
    posted_ids = _seed_channel_state(tmp_path, with_workflow_bind=True)
    message_ref = message_evidence_ref(posted_ids[0])
    replies = {
        judge_type: _judge_reply(judge_type, message_ref, with_bound=True)
        for judge_type in ("milestone", "communication", "prd_result", "conclusion")
    }
    backend = _fake_backend(replies)
    first = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert first["judges_run"] == 4

    # Simulate a transport failure overwriting the milestone record, as
    # "judge claude did not start" did on 2026-09-16.
    run_dir = (
        tmp_path / "artifacts" / "benchmark-evaluations" / "judges"
        / "channel-ch-1-main-r1" / "milestone"
    )
    record_path = next(run_dir.glob("evalreq-*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.update({"status": "judge_timeout", "result": None,
                   "error": "judge claude did not start (no provider session file)"})
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    second = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert second["errors"] == []
    assert second["judges_run"] == 2  # milestone rerun + conclusion rerun
    assert backend.calls == [  # type: ignore[attr-defined]
        "milestone", "communication", "prd_result", "conclusion", "milestone",
        "conclusion",
    ]
    # The retried judge produced a real verdict: third pass dedups everything.
    third = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert third["judges_run"] == 0


def test_observer_reruns_prd_result_after_workflow_bind(
    tmp_path: Path,
) -> None:
    """Freeze triggers prd_result immediately (original vs channel PRD);
    when a workflow later binds, prd_result re-runs with the bound PRD
    while milestone/communication dedup untouched."""
    posted_ids = _seed_channel_state(tmp_path)  # NO bind at freeze time
    message_ref = message_evidence_ref(posted_ids[0])
    calls: list[str] = []

    def backend(**kwargs: object) -> tuple[str, str]:
        judge_type = str(kwargs["judge_type"])
        calls.append(judge_type)
        if judge_type == "prd_result":
            # First call is at freeze (one dimension); after bind the judge
            # also scores channel_to_workflow_prd.
            prd_calls = [c for c in calls if c == "prd_result"]
            return _judge_reply(
                judge_type, message_ref,
                with_bound=len(prd_calls) > 1,
            ), ""
        return _judge_reply(judge_type, message_ref), ""

    first = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert first["errors"] == []
    assert first["judges_run"] == 4  # all 3 judges at freeze + conclusion

    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit(
        "workflow.invoke.requested", actor="test",
        payload={"channel_id": "ch-1", "workflow_run_id": "wf-1",
                 "task_id": "task-1", "prd_ref": "channel-artifacts/ch-1/prd.md"},
    )

    second = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert second["errors"] == []
    # only prd_result re-runs (bound digest changed) + conclusion rerun
    assert second["judges_run"] == 2
    assert calls[-2:] == ["prd_result", "conclusion"]

    third = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert third["judges_run"] == 0


def test_observer_ignores_non_prd_receipts(tmp_path: Path) -> None:
    posted_ids = _seed_channel_state(tmp_path, receipt_kind="handoff_accepted")
    message_ref = message_evidence_ref(posted_ids[0])
    # The receipt after consensus must not create/replace a freeze point:
    # freeze stays the consensus one (channel still evaluated exactly once).
    backend = _fake_backend(
        {judge_type: _judge_reply(judge_type, message_ref)
         for judge_type in ("milestone", "communication", "prd_result", "conclusion")}
    )
    stats = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert stats["channels_seen"] == 1
    assert stats["judges_run"] == 4  # 3 judges at freeze + conclusion


def test_observer_skips_unreadable_prd_artifact(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit(
        "channel.created", actor="test",
        payload={"channel_id": "ch-1"},
    )
    writer.emit(
        "channel.consensus.reached",
        actor="test",
        payload={
            "channel_id": "ch-1",
            "thread_id": "main",
            "prd_ref": "channel-artifacts/ch-1/missing.md",
            "prd_revision": 1,
        },
    )
    backend = _fake_backend({"milestone": _milestone_reply()})
    stats = evaluation_observer.observe_once(state_dir=tmp_path, backend=backend)
    assert stats["judges_run"] == 0
    assert backend.calls == []  # type: ignore[attr-defined]


def test_freeze_point_uses_receipt_revision_fallback(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit(
        "channel.result.receipt.recorded",
        actor="test",
        payload={
            "channel_id": "ch-1",
            "receipt_kind": "prd_confirmed",
            "artifact_ref": "channel-artifacts/ch-1/prd.md",
            "artifact_digest": "b" * 64,
            "revision": 3,
        },
    )
    events = list(writer.event_log.read_all())
    points = evaluation_observer._freeze_points(events)
    assert points["ch-1"]["prd_revision"] == 3
    assert points["ch-1"]["prd_ref"] == "channel-artifacts/ch-1/prd.md"


def test_write_report_archives_per_prompt_version(tmp_path: Path) -> None:
    """2026-09-17: report.md alone was shared by every prompt version, so
    each new evaluation round silently destroyed the previous readable
    report (v1/v2/v3 lost on test_dev3). Each version must keep its own
    archive; report.md stays the latest pointer."""
    body_v1 = "# report v1\n"
    body_v4 = "# report v4\n"
    evaluation_runner.write_report(
        tmp_path, "wf-x", body_v1, prompt_version="prd-eval-prompts.v1",
    )
    evaluation_runner.write_report(
        tmp_path, "wf-x", body_v4, prompt_version="prd-eval-prompts.v4",
    )
    run_dir = tmp_path / "artifacts/benchmark-evaluations/reports/wf-x"
    latest = run_dir / "report.md"
    archived_v1 = run_dir / "report-prd-eval-prompts.v1.md"
    archived_v4 = run_dir / "report-prd-eval-prompts.v4.md"
    assert latest.read_text(encoding="utf-8") == body_v4  # latest pointer
    assert archived_v1.read_text(encoding="utf-8") == body_v1  # v1 survives
    assert archived_v4.read_text(encoding="utf-8") == body_v4
    # without prompt_version: only report.md, no archive
    evaluation_runner.write_report(tmp_path, "wf-x", body_v4)
    assert latest.exists()
    assert not (run_dir / "report-None.md").exists()


def test_render_report_sections_follow_prd_order() -> None:
    judges = {
        "milestone": _scored_record(),
        "communication": {
            "status": "scored",
            "result": {
                "dimensions": [
                    {
                        "dimension_id": dim_id,
                        "score": 4,
                        "status": "scored",
                        "rationale": "r",
                        "confidence": 0.9,
                        "evidence_refs": [ALLOWED[0]],
                    }
                    for dim_id in COMMUNICATION_DIMENSIONS
                ]
            },
        },
        "prd_result": {
            "status": "scored",
            "result": {
                "dimensions": [
                    {
                        "dimension_id": dim_id,
                        "score": 3,
                        "status": "scored",
                        "rationale": "r",
                        "verdict": "aligned",
                        "confidence": 0.9,
                        "evidence_refs": [ALLOWED[1]],
                    }
                    for dim_id in PRD_RESULT_DIMENSIONS
                ],
                "differences": ["d1"],
            },
        },
    }
    body = evaluation_observer.render_report(
        workflow_run_id="wf-x",
        trial_id="t-1",
        channel_id="ch-1",
        freeze={"prd_ref": "p", "prd_revision": 1, "task_id": "task-1"},
        judges=judges,
        conclusion={
            "status": "complete",
            "result": {"overall_score": 3.6, "conclusion_zh": "结论",
                       "next_actions": ["n1"]},
        },
    )
    assert body.index("## 1.") < body.index("## 2.") < body.index("## 3.") < body.index("## 4.") < body.index("## 5.") < body.index("## 6.")
    assert "overall_score: **3.4**" in body
    assert "communication_score: 4.0" in body
    assert "prd_result_score: 3.0" in body


def test_pane_provider_alive_detects_codex_under_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-18 test_dev5: codex runs under node, so the pane leaf is
    ``node`` — liveness must probe the process tree or every codex judge
    turn dies with provider_ready_pattern_timeout."""
    from zf.runtime import evaluation_tmux

    monkeypatch.setattr(
        evaluation_tmux, "_pane_current_command", lambda s, w: "node",
    )
    probe = iter([True, False])
    monkeypatch.setattr(
        evaluation_tmux, "_pane_process_tree_has_executable",
        lambda s, w, exe: next(probe),
    )
    assert evaluation_tmux._pane_provider_alive("sess", "codex") is True
    assert evaluation_tmux._pane_provider_alive("sess", "codex") is False
    # claude still matches by leaf directly
    monkeypatch.setattr(
        evaluation_tmux, "_pane_current_command", lambda s, w: "claude",
    )
    assert evaluation_tmux._pane_provider_alive("sess", "claude-code") is True


def test_stop_judge_agent_codex_resets_pane_without_slash_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex has no /exit: pasted slash-commands land in its input box as
    chat messages, and the pane reset is unconditional — a prompt pasted
    into a dead provider's shell can leave bash in a PS2 continuation that
    swallows the next launch command."""
    from zf.runtime import evaluation_tmux

    monkeypatch.setattr(
        evaluation_tmux, "_pane_provider_alive", lambda s, b, **k: True,
    )
    sent: list[str] = []
    monkeypatch.setattr(
        evaluation_tmux, "_send_line",
        lambda s, w, text, **k: sent.append(text),
    )
    monkeypatch.setattr(evaluation_tmux.time, "sleep", lambda _s: None)
    resets: list[str] = []
    monkeypatch.setattr(
        evaluation_tmux, "_reset_judges_pane", lambda s, **k: resets.append(k["window_name"]),
    )
    monkeypatch.setattr(evaluation_tmux.subprocess, "run", lambda *a, **k: None)
    evaluation_tmux.stop_judge_agent("sess", backend="codex", timeout_s=0.2)
    assert "/exit" not in sent
    assert resets == ["judges-slot-0"]
