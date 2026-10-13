"""Lossless provider-output repair before the canonical scoring gates."""
from __future__ import annotations

from typing import Any

from zf.runtime.evaluation_packets import stable_digest
from zf.runtime.evaluation_schemas import (
    PRD_DIFFERENCES_SHAPE_ERROR,
    PRD_FIDELITY_DIMENSIONS,
    normalize_judge_output,
)
from zf.runtime.evaluation_types import JudgeResult


def normalize_provider_output(
    *, judge_type: str, raw: dict[str, Any], allowed_evidence_refs: list[str],
    expected_agent_ids: list[str] | None = None,
    workflow_bound: bool | None = None, expected_schema_version: str | None = None,
    cross_compare_context: dict[str, Any] | None = None,
) -> tuple[JudgeResult | None, str, dict[str, Any]]:
    """Repair only the known differences shape; never change a judgment."""
    options = dict(
        judge_type=judge_type, allowed_evidence_refs=allowed_evidence_refs,
        expected_agent_ids=expected_agent_ids, workflow_bound=workflow_bound,
        expected_schema_version=expected_schema_version,
        cross_compare_context=cross_compare_context,
    )
    result, error = normalize_judge_output(raw=raw, **options)
    if judge_type != "prd_fidelity" or error != PRD_DIFFERENCES_SHAPE_ERROR:
        return result, error, {}
    differences = raw.get("differences")
    if not isinstance(differences, list):
        return result, error, {}
    allowed = set(allowed_evidence_refs)
    if workflow_bound is None:
        workflow_bound = "artifact/prd/workflow-bound.json" in allowed
    required = PRD_FIDELITY_DIMENSIONS if workflow_bound else PRD_FIDELITY_DIMENSIONS[:1]
    repaired: list[str] = []
    for item in differences:
        if isinstance(item, str) and item.strip():
            repaired.append(item)
            continue
        if not isinstance(item, dict) or set(item) != {"dimension_id", "description", "evidence_refs"}:
            return result, error, {}
        description, refs, dimension = item["description"], item["evidence_refs"], item["dimension_id"]
        if (
            not isinstance(description, str) or not description.strip()
            or not isinstance(dimension, str) or dimension not in required
            or not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or ref not in allowed for ref in refs)
        ):
            return result, error, {}
        repaired.append(f"{description}（维度：{dimension}；证据：{', '.join(refs)}）")
    candidate = {**raw, "differences": repaired}
    normalized, validation_error = normalize_judge_output(raw=candidate, **options)
    if normalized is None or normalized.status == "invalid":
        return result, error, {}
    return normalized, validation_error, {
        "schema_version": "channel-prd-evaluation.output-repair.v1",
        "fields": ["differences"],
        "method": "lossless_difference_objects_to_strings",
        "original_output_digest": stable_digest(raw),
        "canonical_output_digest": stable_digest(candidate),
    }
