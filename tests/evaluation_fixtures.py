"""Canonical fixtures for Channel/PRD evaluation tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_prd_freeze import emit_prd_frozen
from zf.runtime.evaluation_cross_compare import (
    candidate_prd_ref,
    expected_pairs,
)
from zf.runtime.evaluation_packets import message_evidence_ref, build_packet
from zf.runtime.evaluation_schemas import (
    DISCUSSION_DIMENSIONS,
    LANE_PRD_FIDELITY_DIMENSIONS,
    PRD_FIDELITY_DIMENSIONS,
)


def _team_findings() -> dict[str, Any]:
    return {
        "discussion_meaningful": True,
        "discussion_progressed": True,
        "prd_ready_for_next_stage": True,
        "problems": [],
        "underperformed_roles": [],
        "unnecessary_roles": [],
    }


def _seed_channel_state(
    tmp_path: Path,
    *,
    with_workflow_bind: bool = False,
    receipt_kind: str = "prd_confirmed",
    freeze: bool = True,
    signers: tuple[str, ...] = ("m1", "m2"),
    readiness_verdict: str = "ready",
    product_mode: str = "multi_lens",
    owner_confirmed: bool = False,
    emit_prd_freeze: bool = True,
) -> list[str]:
    """Seed a canonical, digest-bound frozen Channel PRD."""
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit(
        "channel.created", actor="test",
        payload={"channel_id": "ch-1", "name": "ch-1", "task_id": "task-1"},
    )
    for member_id in ("m1", "m2"):
        writer.emit(
            "channel.member.added", actor="test",
            payload={"channel_id": "ch-1", "member_id": member_id,
                     "member_type": "provider_agent", "backend": "claude-code",
                     "name": f"Agent {member_id}"},
        )
    posted_event_ids: list[str] = []
    for idx, member_id in enumerate(("m1", "m2"), start=1):
        posted = writer.emit(
            "channel.message.posted", actor="test",
            payload={
                "channel_id": "ch-1", "message_id": f"msg-{idx}",
                "member_id": member_id, "role": "dev", "thread_id": "main",
                "text": f"proposal {idx}",
            },
        )
        posted_event_ids.append(posted.id)

    prd_ref = "channel-artifacts/ch-1/prd.md"
    (tmp_path / prd_ref).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / prd_ref).write_text("# Frozen PRD\n\nDo X.\n", encoding="utf-8")
    prd_digest = hashlib.sha256((tmp_path / prd_ref).read_bytes()).hexdigest()
    readiness_ref = "channel-artifacts/ch-1/readiness.json"
    readiness_body = {
        "channel_id": "ch-1", "thread_id": "main", "revision": 1,
        "verdict": readiness_verdict, "gaps": [], "risks": [],
    }
    (tmp_path / readiness_ref).write_text(
        json.dumps(readiness_body, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    readiness_digest = hashlib.sha256((tmp_path / readiness_ref).read_bytes()).hexdigest()
    snapshot_ref = "channel-artifacts/ch-1/source-snapshot.json"
    snapshot_body = {
        "channel_id": "ch-1", "thread_id": "main", "revision": 1,
        "messages": ["proposal 1", "proposal 2"],
    }
    (tmp_path / snapshot_ref).write_text(
        json.dumps(snapshot_body, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    snapshot_digest = hashlib.sha256((tmp_path / snapshot_ref).read_bytes()).hexdigest()
    artifact = {
        "channel_id": "ch-1", "thread_id": "main",
        "artifact_ref": prd_ref, "artifact_digest": prd_digest,
        "prd_ref": prd_ref, "prd_digest": prd_digest, "prd_revision": 1,
        "readiness_ref": readiness_ref, "readiness_digest": readiness_digest,
        "readiness_verdict": readiness_verdict,
        "source_snapshot_ref": snapshot_ref, "source_snapshot_digest": snapshot_digest,
        "required_signers": ["m1", "m2"] if product_mode == "multi_lens" else [],
        "product_mode": product_mode, "task_id": "task-1",
    }
    writer.emit("channel.synthesis.proposed", actor="test", payload=artifact)
    writer.emit("channel.consensus.proposed", actor="test", payload=artifact)
    for signer in signers:
        writer.emit("channel.consensus.signed", actor=signer,
                    payload={**artifact, "member_id": signer})
    if freeze:
        reached = writer.emit(
            "channel.consensus.reached", actor="kernel",
            payload={**artifact, **({"confirmed_by": "owner"} if owner_confirmed else {})},
        )
        if emit_prd_freeze:
            emit_prd_frozen(
                writer,
                consensus_payload={
                    **artifact,
                    **({"confirmed_by": "owner"} if owner_confirmed else {}),
                },
                source_event=reached,
                freeze_reason="test",
                actor="kernel",
            )
    if receipt_kind != "prd_confirmed":
        writer.emit(
            "channel.result.receipt.recorded", actor="test",
            payload={"channel_id": "ch-1", "receipt_kind": receipt_kind},
        )
    if with_workflow_bind:
        requested = writer.emit(
            "workflow.invoke.requested", actor="test",
            payload=_workflow_binding_payload(tmp_path),
        )
        writer.emit(
            "workflow.invoke.accepted", actor="kernel",
            payload={"source_event_id": requested.id},
        )
    return posted_event_ids


def _workflow_binding_payload(state_dir: Path) -> dict[str, Any]:
    ref = "channel-artifacts/ch-1/prd.md"
    digest = hashlib.sha256((state_dir / ref).read_bytes()).hexdigest()
    readiness_ref = "channel-artifacts/ch-1/readiness.json"
    readiness_digest = hashlib.sha256((state_dir / readiness_ref).read_bytes()).hexdigest()
    return {
        "channel_id": "ch-1", "workflow_run_id": "wf-bound-1", "task_id": "task-1",
        "prd_ref": ref,
        "source_refs": {
            "channel_id": "ch-1", "channel_thread_id": "main",
            "channel_prd_ref": ref, "channel_prd_revision": "1",
            "channel_prd_digest": digest,
            "channel_prd_readiness_ref": readiness_ref,
            "channel_prd_readiness_digest": readiness_digest,
        },
        "prd_digest": digest,
    }


def _packet(judge_type: str = "contribution", run_id: str = "wf-x") -> dict[str, Any]:
    return build_packet(
        stage=judge_type, run_id=run_id, workflow_run_id=run_id,
        trial_id="t-1", source_snapshot_digest="d" * 64,
        task={"task_id": "task-1", "statement": "do X"},
        task_details={"discussion_events": []},
        agent_profiles=[{"agent_id": "m1", "agent_name": "Agent m1"}],
        channel_messages=[{
            "event_id": "evt-1", "evidence_ref": "channel/message/evt-1",
            "member_id": "m1", "role": "dev", "text": "proposal",
        }],
        channel_frozen_prd={"prd_ref": "p", "body": "# PRD\n\nDo X."},
    ).to_dict()


def _judge_reply(judge_type: str, message_ref: str = "", with_bound: bool = False) -> str:
    ref = message_ref or "channel/message/evt-1"
    if judge_type == "task_type":
        return json.dumps({
            "schema_version": "task_type_result.v1", "judge_type": "task_type",
            "status": "scored", "task_type": "bugfix", "confidence": 0.9,
            "evidence_refs": [ref], "rationale": "需求包含明确的现有行为修复。",
            "missing_evidence": [],
        }, ensure_ascii=False)
    if judge_type == "contribution":
        return json.dumps({
            "schema_version": "contribution_result.v2", "judge_type": "contribution",
            "task_type": "bugfix", "role_id": "bugfix", "role_version": "evaluation-role-bugfix.v3",
            "role_metadata": {}, "score_scale": "0-4",
            "agent_findings": [{
                "agent_id": agent_id, "status": "scored", "score": 3,
                "necessity": "must", "necessity_status": "complete",
                "role_summary": "提出并验证了修复方案。", "evidence_refs": [ref],
                "rationale": "有明确的诊断和验证证据。", "confidence": 0.9,
            } for agent_id in ("m1", "m2")],
            "missing_evidence": [],
        }, ensure_ascii=False)
    if judge_type == "discussion":
        return json.dumps({
            "schema_version": "discussion_result.v2", "judge_type": "discussion",
            "task_type": "bugfix", "role_id": "bugfix", "role_version": "evaluation-role-bugfix.v3",
            "role_metadata": {}, "score_scale": "0-4",
            "dimensions": [{
                "dimension_id": dimension, "status": "scored", "score": 3,
                "confidence": 0.9, "evidence_refs": [ref],
                "rationale": "讨论有明确的信息交换和决策。", "verdict": "aligned",
            } for dimension in DISCUSSION_DIMENSIONS],
            "human_intervention_count": 0,
            "discussion_meaningful": True,
            "discussion_progressed": True,
            "missing_evidence": [],
        }, ensure_ascii=False)
    if judge_type == "prd_fidelity":
        dimensions = ["original_to_channel_prd"]
        if with_bound:
            dimensions = list(PRD_FIDELITY_DIMENSIONS)
        refs = ["artifact/prd/channel-frozen.json"]
        if with_bound:
            refs.append("artifact/prd/workflow-bound.json")
        return json.dumps({
            "schema_version": "prd_fidelity_result.v2", "judge_type": "prd_fidelity",
            "task_type": "bugfix", "role_id": "bugfix", "role_version": "evaluation-role-bugfix.v3",
            "role_metadata": {}, "score_scale": "0-4",
            "dimensions": [{
                "dimension_id": dimension, "status": "scored", "score": 3,
                "confidence": 0.9, "evidence_refs": refs,
                "rationale": "需求边界在 PRD 中保持一致。", "verdict": "aligned",
            } for dimension in dimensions],
            "differences": [], "prd_ready_for_next_stage": True,
            "missing_evidence": [],
        }, ensure_ascii=False)
    return json.dumps({
        "schema_version": "conclusion_result.v2", "judge_type": "conclusion",
        "status": "complete", "score_scale": "0-4", "agent_scores": [
            {"agent_id": agent_id, "score": 3, "necessity": "must", "necessity_status": "complete"}
            for agent_id in ("m1", "m2")
        ], "team_findings": _team_findings(), "conclusion_zh": "评估完成。",
        "next_actions": [], "judge_refs": [], "evidence_refs": [],
    }, ensure_ascii=False)


LANE_EVAL_REFS = ["eval/prompt", "eval/prd"]


def _lane_prd_fidelity_reply(
    *, refs: list[str] | None = None, score: int | None = 3,
    confidence: float = 0.9, status: str = "scored",
    checklist: list[dict[str, Any]] | None = None,
) -> str:
    refs = list(refs or LANE_EVAL_REFS)
    if checklist is None:
        checklist = [
            {"item_id": "req-1", "requirement": "支持待办事项增删改查。",
             "verdict": "pass", "evidence_refs": ["eval/prd"]},
            {"item_id": "req-2", "requirement": "支持到期提醒。",
             "verdict": "partial", "evidence_refs": ["eval/prd"]},
            {"item_id": "req-3", "requirement": "数据本地存储。",
             "verdict": "missing", "evidence_refs": ["eval/prompt"]},
        ]
    return json.dumps({
        "schema_version": "lane_prd_fidelity_result.v2",
        "judge_type": "lane_prd_fidelity",
        "score_scale": "0-4",
        "checklist": checklist,
        "dimensions": [{
            "dimension_id": dimension, "status": status, "score": score,
            "confidence": confidence, "evidence_refs": refs,
            "rationale": f"{dimension} 有明确证据支撑。", "verdict": "aligned",
        } for dimension in LANE_PRD_FIDELITY_DIMENSIONS],
        "differences": [], "missing_evidence": [],
    }, ensure_ascii=False)


def _cross_compare_reply(
    *,
    candidates: tuple[str, ...] = ("A", "B", "C"),
    ranking: tuple[str, ...] | None = None,
    confidence: int = 3,
    conflicts: list[dict[str, Any]] | None = None,
    divergence_explanation: str = "",
    missing_evidence: list[str] | None = None,
) -> str:
    """Deterministic cross-compare reply consistent with the vote order."""
    ranking = list(ranking or candidates)
    pairwise = []
    attributions = []
    for pair in expected_pairs(list(candidates)):
        first, second = pair.split("-", 1)
        winner = first if ranking.index(first) < ranking.index(second) else second
        for dimension in LANE_PRD_FIDELITY_DIMENSIONS:
            pairwise.append({
                "pair": pair, "dimension_id": dimension, "winner": winner,
                "margin": "medium", "confidence": confidence,
                "evidence_refs": [candidate_prd_ref(first), candidate_prd_ref(second)],
                "rationale": f"{dimension} 维度 {winner} 更完整。",
            })
            attributions.append({
                "dimension_id": dimension, "pair": pair, "category": "coverage_gap",
                "summary": f"{first} 与 {second} 在 {dimension} 的章节摘录差异。",
                "evidence_refs": [candidate_prd_ref(first), candidate_prd_ref(second)],
            })
    reply: dict[str, Any] = {
        "schema_version": "cross_compare_result.v1",
        "judge_type": "cross_compare",
        "status": "scored" if not missing_evidence else "evidence_missing",
        "score_scale": "0-4",
        "candidates": list(candidates),
        "dimension_rankings": [{
            "dimension_id": dimension, "ranking": ranking,
            "confidence": confidence,
            "evidence_refs": [candidate_prd_ref(label) for label in candidates],
            "rationale": f"{dimension} 排名与逐段对比一致。",
        } for dimension in LANE_PRD_FIDELITY_DIMENSIONS],
        "pairwise": pairwise,
        "attributions": attributions,
        "conflicts": conflicts if conflicts is not None else [],
        "recommendation": {
            "ranking": ranking, "top": ranking[0], "confidence": confidence,
            "rationale": "维度票选领先且差距稳定。", "divergence_explanation": divergence_explanation,
            "key_gaps": [{
                "candidate": label, "gap": f"{label} 相对 {ranking[0]} 的覆盖缺口。",
                "evidence_refs": [candidate_prd_ref(label), candidate_prd_ref(ranking[0])],
            } for label in ranking[1:]],
        },
        "missing_evidence": list(missing_evidence or []),
    }
    return json.dumps(reply, ensure_ascii=False)


def _cross_compare_blind(
    candidates: tuple[str, ...] = ("A", "B", "C"),
    scores: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Candidate dicts for build_cross_compare_packet with blind summaries."""
    scores = scores or {
        label: len(candidates) - index for index, label in enumerate(candidates)
    }
    return [{
        "label": label,
        "prd_body": f"# PRD {label}\n\n满足提示词需求的方案 {label}。\n",
        "blind": {
            "status": "scored",
            "mean": float(scores[label]),
            "dimensions": [{
                "dimension_id": dimension, "score": scores[label],
                "status": "scored", "verdict": "aligned",
                "rationale": f"{dimension} 摘要。",
            } for dimension in LANE_PRD_FIDELITY_DIMENSIONS],
        },
    } for label in candidates]


__all__ = [
    "_cross_compare_blind",
    "_cross_compare_reply",
    "_judge_reply",
    "_lane_prd_fidelity_reply",
    "_packet",
    "_seed_channel_state",
    "_team_findings",
    "_workflow_binding_payload",
]
