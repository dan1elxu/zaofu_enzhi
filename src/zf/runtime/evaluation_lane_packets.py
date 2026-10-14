"""Immutable packet builders for the three-lane PRD eval (eval-run side).

The channel observer remains the only producer for channel-frozen packets; the
eval-run driver freezes lane material through these builders into the same
packet machinery. Lane identity is deliberately absent from every packet: the
blind judge reads the prompt plus exactly one PRD, and the cross-compare judge
reads de-identified candidates labelled A/B/C. Candidate assignment is a
deterministic shuffle seeded by the run identity, so rescanning a trial
rebuilds byte-identical packets (idempotence) while judges cannot infer lanes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from zf.runtime.evaluation_cross_compare import candidate_prd_ref
from zf.runtime.evaluation_packets import build_packet, stable_digest
from zf.runtime.evaluation_prompts import (
    CROSS_COMPARE_JUDGE_TYPE,
    LANE_PRD_FIDELITY_JUDGE_TYPE,
    prompt_version_for_stage,
)
from zf.runtime.evaluation_types import EvaluationPacket

EVAL_PROMPT_REF = "eval/prompt"
EVAL_PRD_REF = "eval/prd"
EVAL_BLIND_REF_PREFIX = "eval/blind/"
CANDIDATE_LABELS: tuple[str, ...] = ("A", "B", "C")

_CANDIDATE_KEYS = frozenset({"label", "prd_body", "prd_digest", "blind"})


def blind_evidence_ref(label: str) -> str:
    return f"{EVAL_BLIND_REF_PREFIX}{label}"


def assign_candidate_labels(lanes: list[str], *, seed: str) -> dict[str, str]:
    """Deterministically map lane names to blind labels A/B/C.

    The mapping must be stable across driver restarts (packet digests bind it)
    while remaining unrelated to the lane order, so the judge cannot read lane
    identity out of the label assignment.
    """

    ordered = sorted(
        (str(lane) for lane in lanes),
        key=lambda lane: hashlib.sha256(f"{seed}:{lane}".encode("utf-8")).hexdigest(),
    )
    if len(ordered) > len(CANDIDATE_LABELS):
        raise ValueError(f"at most {len(CANDIDATE_LABELS)} lanes are supported")
    return {lane: CANDIDATE_LABELS[index] for index, lane in enumerate(ordered)}


def build_lane_task_type_packet(
    *,
    run_id: str,
    workflow_run_id: str,
    trial_id: str,
    prompt_text: str,
    source_snapshot_digest: str = "",
) -> EvaluationPacket:
    """Run-level shared classifier input: the entry prompt and nothing else."""

    prompt_text = str(prompt_text or "")
    digest = source_snapshot_digest or stable_digest({"prompt": prompt_text})
    return build_packet(
        stage="task_type",
        run_id=run_id,
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        source_snapshot_digest=digest,
        task={"statement": prompt_text},
        task_details={
            "statement": prompt_text,
            "source": "eval-run-prompt",
            "evidence_keys": [EVAL_PROMPT_REF],
        },
        evaluator_profile="task_type",
        subagent_scope="task_type",
    )


def build_lane_prd_fidelity_packet(
    *,
    run_id: str,
    workflow_run_id: str,
    trial_id: str,
    prompt_text: str,
    prd_body: str,
    prd_digest: str = "",
    task_type: str = "",
    task_type_confidence: float | None = None,
    source_snapshot_digest: str = "",
) -> EvaluationPacket:
    """Blind single-PRD scoring input; no channel fields, no lane identity."""

    prompt_text = str(prompt_text or "")
    prd_body = str(prd_body or "")
    prd_digest = str(prd_digest or "") or hashlib.sha256(
        prd_body.encode("utf-8")
    ).hexdigest()
    digest = source_snapshot_digest or stable_digest(
        {"prompt": prompt_text, "prd_digest": prd_digest}
    )
    return build_packet(
        stage=LANE_PRD_FIDELITY_JUDGE_TYPE,
        run_id=run_id,
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        source_snapshot_digest=digest,
        task={"statement": prompt_text},
        task_details={
            "statement": prompt_text,
            "source": "eval-run-lane-prd",
            "prd": {"body": prd_body, "digest": prd_digest},
            "evidence_keys": [EVAL_PROMPT_REF, EVAL_PRD_REF],
        },
        task_type=task_type,
        task_type_confidence=task_type_confidence,
        evaluator_profile=LANE_PRD_FIDELITY_JUDGE_TYPE,
        subagent_scope=LANE_PRD_FIDELITY_JUDGE_TYPE,
        prompt_version=prompt_version_for_stage(LANE_PRD_FIDELITY_JUDGE_TYPE),
    )


def build_cross_compare_packet(
    *,
    run_id: str,
    workflow_run_id: str,
    trial_id: str,
    prompt_text: str,
    candidates: list[dict[str, Any]],
    task_type: str = "",
    source_snapshot_digest: str = "",
) -> EvaluationPacket:
    """De-identified multi-PRD comparison input (labels A/B/C only)."""

    prompt_text = str(prompt_text or "")
    normalized: list[dict[str, Any]] = []
    labels: list[str] = []
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("cross_compare candidate must be an object")
        unknown = set(item) - _CANDIDATE_KEYS
        if unknown:
            # Lane identity must never leak into a blind packet; fail closed
            # instead of silently dropping caller fields.
            raise ValueError(f"cross_compare candidate carries unknown keys: {sorted(unknown)}")
        label = str(item.get("label") or "").strip()
        if not label or label in labels:
            raise ValueError("cross_compare candidates need unique non-empty labels")
        labels.append(label)
        prd_body = str(item.get("prd_body") or "")
        prd_digest = str(item.get("prd_digest") or "") or hashlib.sha256(
            prd_body.encode("utf-8")
        ).hexdigest()
        blind = item.get("blind") if isinstance(item.get("blind"), dict) else {}
        normalized.append({
            "label": label,
            "prd": {"body": prd_body, "digest": prd_digest},
            "blind": blind,
        })
    if len(normalized) < 2:
        raise ValueError("cross_compare requires at least two candidates")
    evidence_keys = [EVAL_PROMPT_REF]
    for label in labels:
        evidence_keys.append(candidate_prd_ref(label))
        evidence_keys.append(blind_evidence_ref(label))
    digest = source_snapshot_digest or stable_digest({
        "prompt": prompt_text,
        "candidates": normalized,
    })
    return build_packet(
        stage=CROSS_COMPARE_JUDGE_TYPE,
        run_id=run_id,
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        source_snapshot_digest=digest,
        task={"statement": prompt_text},
        task_details={
            "statement": prompt_text,
            "source": "eval-run-cross-compare",
            "candidates": normalized,
            "evidence_keys": evidence_keys,
        },
        task_type=task_type,
        evaluator_profile=CROSS_COMPARE_JUDGE_TYPE,
        subagent_scope=CROSS_COMPARE_JUDGE_TYPE,
        prompt_version=prompt_version_for_stage(CROSS_COMPARE_JUDGE_TYPE),
    )


def blind_summary_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project a lane_prd_fidelity judge record into a de-identified summary."""

    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    dimensions = []
    for item in result.get("dimensions") or []:
        if not isinstance(item, dict):
            continue
        dimensions.append({
            "dimension_id": str(item.get("dimension_id") or ""),
            "score": item.get("score"),
            "status": str(item.get("status") or ""),
            "verdict": str(item.get("verdict") or ""),
            "rationale": str(item.get("rationale") or ""),
        })
    concerns = result.get("concerns")
    # v3 records expose the adversarial-concern count so the cross-compare
    # judge can weigh how much doubt each blind review surfaced.
    concern_count = len(concerns) if isinstance(concerns, list) else 0
    return {
        "status": str(record.get("status") or ""),
        "mean": result.get("lane_prd_fidelity_score"),
        "dimensions": dimensions,
        "concern_count": concern_count,
    }


def cross_compare_context_from_packet(packet: Any) -> dict[str, Any]:
    """Extract the mechanical normalization context from a cross-compare packet."""

    get = packet.get if hasattr(packet, "get") else lambda _key, default=None: default
    details = get("task_details")
    details = details if isinstance(details, dict) else {}
    labels: list[str] = []
    blind_scores: dict[str, dict[str, Any]] = {}
    for item in details.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        labels.append(label)
        blind = item.get("blind") if isinstance(item.get("blind"), dict) else {}
        scores: dict[str, Any] = {}
        for dimension in blind.get("dimensions") or []:
            if isinstance(dimension, dict) and str(dimension.get("dimension_id") or ""):
                scores[str(dimension["dimension_id"])] = dimension.get("score")
        blind_scores[label] = scores
    return {"candidates": labels, "blind_scores": blind_scores}


def missing_lane_prd_inputs(packet: Any) -> list[str]:
    """Deterministic pre-flight check for the blind lane fidelity stage."""

    get = packet.get if hasattr(packet, "get") else lambda _key, default=None: default
    details = get("task_details")
    details = details if isinstance(details, dict) else {}
    task = get("task")
    task = task if isinstance(task, dict) else {}
    missing: list[str] = []
    statement = details.get("statement") or task.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        missing.append("task.statement")
    prd = details.get("prd")
    prd = prd if isinstance(prd, dict) else {}
    body = prd.get("body")
    if not isinstance(body, str) or not body.strip():
        missing.append("task_details.prd.body")
    return missing


def missing_cross_compare_inputs(packet: Any) -> list[str]:
    """Deterministic pre-flight check for the cross-compare stage."""

    missing: list[str] = []
    context = cross_compare_context_from_packet(packet)
    candidates = context["candidates"]
    get = packet.get if hasattr(packet, "get") else lambda _key, default=None: default
    details = get("task_details")
    details = details if isinstance(details, dict) else {}
    task = get("task")
    task = task if isinstance(task, dict) else {}
    statement = details.get("statement") or task.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        missing.append("task.statement")
    if len(candidates) < 2:
        missing.append("task_details.candidates")
        return missing
    for item in details.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip() or "?"
        prd = item.get("prd") if isinstance(item.get("prd"), dict) else {}
        body = prd.get("body")
        if not isinstance(body, str) or not body.strip():
            missing.append(f"task_details.candidates[{label}].prd.body")
        blind = item.get("blind") if isinstance(item.get("blind"), dict) else {}
        if not blind.get("dimensions"):
            missing.append(f"task_details.candidates[{label}].blind")
    return missing


def missing_stage_inputs(judge_type: str, packet: Any) -> list[str]:
    """Deterministic pre-flight input check, dispatched by stage."""

    if judge_type == "prd_fidelity":
        from zf.runtime.evaluation_inputs import missing_prd_fidelity_inputs

        return missing_prd_fidelity_inputs(packet)
    if judge_type == LANE_PRD_FIDELITY_JUDGE_TYPE:
        return missing_lane_prd_inputs(packet)
    if judge_type == CROSS_COMPARE_JUDGE_TYPE:
        return missing_cross_compare_inputs(packet)
    return []


def missing_input_result(
    judge_type: str, *, schema_version: str, missing: list[str],
) -> dict[str, Any]:
    """Deterministic evidence_missing result body for an incomplete packet."""

    result: dict[str, Any] = {
        "schema_version": schema_version,
        "judge_type": judge_type,
        "status": "evidence_missing",
        "missing_evidence": list(missing),
    }
    if judge_type in {"prd_fidelity", LANE_PRD_FIDELITY_JUDGE_TYPE}:
        result["dimensions"] = []
    if judge_type == CROSS_COMPARE_JUDGE_TYPE:
        result.update(
            candidates=[], dimension_rankings=[], pairwise=[],
            attributions=[], conflicts=[], recommendation={},
        )
    return result


def lane_template_values(prompt_view: dict[str, Any]) -> dict[str, str]:
    """Extra prompt-template placeholders understood by lane stage prompts."""

    task = prompt_view.get("task")
    task = task if isinstance(task, dict) else {}
    details = prompt_view.get("task_details")
    details = details if isinstance(details, dict) else {}
    candidates = details.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    return {
        "prompt_statement": json.dumps(
            task.get("statement") or details.get("statement") or "",
            ensure_ascii=False, default=str,
        ),
        "prd_document": json.dumps(details.get("prd") or {}, ensure_ascii=False, default=str),
        "candidates": json.dumps(candidates, ensure_ascii=False, default=str),
        "candidate_count": str(len(candidates)),
        "candidate_labels": json.dumps(
            [item.get("label") for item in candidates if isinstance(item, dict)],
            ensure_ascii=False, default=str,
        ),
    }


__all__ = [
    "CANDIDATE_LABELS",
    "EVAL_BLIND_REF_PREFIX",
    "EVAL_PRD_REF",
    "EVAL_PROMPT_REF",
    "assign_candidate_labels",
    "blind_evidence_ref",
    "blind_summary_from_record",
    "build_cross_compare_packet",
    "build_lane_prd_fidelity_packet",
    "build_lane_task_type_packet",
    "cross_compare_context_from_packet",
    "lane_template_values",
    "missing_cross_compare_inputs",
    "missing_lane_prd_inputs",
    "missing_input_result",
    "missing_stage_inputs",
]
