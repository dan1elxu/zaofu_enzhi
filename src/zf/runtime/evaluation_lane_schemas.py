"""Blind lane PRD fidelity (three-lane eval) output normalization.

Split out of ``evaluation_schemas.py`` when the v3 checklist/concerns
validation pushed that module past the 1000-line soft limit; mirrors
``evaluation_cross_compare.py`` owning the other lane-stage schema. The
shared item-level machinery (dimension entries, evidence binding, status
rules) stays in ``evaluation_schemas`` and is imported here — the import
direction is one-way because ``evaluation_schemas`` only references this
module lazily inside ``normalize_judge_output``.
"""

from __future__ import annotations

from typing import Any

from zf.runtime.evaluation_schemas import (
    PRD_DIFFERENCES_SHAPE_ERROR,
    _dimension_entries,
    _dimension_index,
    _dimension_score_dataclass,
    _evidence_bound,
    _normalize_dimension,
    _require_ids,
    _result_metadata,
    _result_status,
    _score_scale_from_items,
    _string_list,
    mean_scores,
)
from zf.runtime.evaluation_types import (
    LANE_PRD_FIDELITY_DIMENSIONS,
    LanePrdFidelityResult,
)

LANE_CHECKLIST_VERDICTS = frozenset({"pass", "partial", "missing"})
LANE_CHECKLIST_KINDS = frozenset({"explicit", "implicit"})
# v3 forces the judge beyond paraphrasing the prompt: the checklist must
# contain at least this many implicit (unstated but expected) requirements.
LANE_CHECKLIST_MIN_IMPLICIT = 3


def lane_prd_fidelity_score(
    dimensions: list[Any], *, score_scale: str | None = None,
) -> float | None:
    """Deterministic blind lane score: the equal-weight mean of all four
    dimensions; None unless every dimension is scored."""

    scale = score_scale or _score_scale_from_items(dimensions)
    by_id = _dimension_index(dimensions, score_scale=scale)
    if set(by_id) != set(LANE_PRD_FIDELITY_DIMENSIONS):
        return None
    return mean_scores(
        [
            _dimension_score_dataclass(by_id.get(dimension_id), score_scale=scale)
            for dimension_id in LANE_PRD_FIDELITY_DIMENSIONS
        ],
        score_scale=scale,
    )


def normalize_lane_prd_fidelity(
    raw: dict[str, Any],
    allowed: set[str],
    *,
    score_scale: str = "0-4",
) -> tuple[LanePrdFidelityResult | None, str]:
    """Blind lane PRD scoring: fixed four dimensions, no role/workflow gate."""

    dimensions = _dimension_entries(raw.get("dimensions"))
    if not dimensions:
        return None, "dimensions must be a non-empty list"
    _require_ids(dimensions, "dimension_id", LANE_PRD_FIDELITY_DIMENSIONS)
    default_confidence = raw.get("confidence")
    dims = [
        _normalize_dimension(item, allowed, default_confidence, score_scale=score_scale)
        for item in dimensions
    ]
    if any(d is None for d in dims):
        return None, "dimension entry malformed (needs dimension_id)"
    dims = [d for d in dims if d is not None]
    checklist, checklist_error = _normalize_lane_checklist(
        raw.get("checklist"), allowed,
    )
    if checklist_error:
        return None, checklist_error
    concerns, concerns_error = _normalize_lane_concerns(dimensions, allowed)
    if concerns_error:
        return None, concerns_error
    missing = _string_list(
        raw.get("missing_evidence") or raw.get("missing_evidence_keys")
    )
    status = _result_status(dims, missing)
    try:
        differences = _string_list(raw.get("differences") or [])
    except (TypeError, ValueError):
        return None, PRD_DIFFERENCES_SHAPE_ERROR
    return LanePrdFidelityResult(
        schema_version=str(raw["schema_version"]),
        judge_type="lane_prd_fidelity",
        status=status,
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        dimensions=dims,
        differences=differences,
        checklist=checklist,
        concerns=concerns,
        lane_prd_fidelity_score=lane_prd_fidelity_score(
            dims, score_scale=score_scale,
        ) if status == "scored" else None,
        missing_evidence=missing,
        score_scale=score_scale,
        **_result_metadata(raw),
    ), ""


def _normalize_lane_checklist(
    value: Any, allowed: set[str],
) -> tuple[list[dict[str, Any]], str]:
    """Validate the v3 requirement checklist.

    The checklist is the deduction evidence base of the v3 rubric: it must be
    a non-empty list of unique items, every item must declare a kind
    (``explicit``/``implicit``) with at least LANE_CHECKLIST_MIN_IMPLICIT
    implicit items, every verdict must be in vocabulary, every cited ref must
    stay inside the evidence allowlist, and a pass or partial verdict must
    carry at least one ref (a claim without evidence is rejected, never
    silently trusted).
    """

    if not isinstance(value, list) or not value:
        return [], "checklist must be a non-empty list"
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    implicit_count = 0
    for entry in value:
        if not isinstance(entry, dict):
            return [], "every checklist item must be an object"
        item_id = str(entry.get("item_id") or "").strip()
        if not item_id or item_id != str(entry.get("item_id") or ""):
            return [], "every checklist item needs a non-empty item_id"
        if item_id in seen:
            return [], f"duplicate checklist item_id: {item_id!r}"
        seen.add(item_id)
        kind = str(entry.get("kind") or "").strip()
        if kind not in LANE_CHECKLIST_KINDS:
            return [], f"checklist item {item_id!r} kind must be explicit or implicit"
        implicit_count += kind == "implicit"
        requirement = str(entry.get("requirement") or "").strip()
        if not requirement:
            return [], f"checklist item {item_id!r} needs a non-empty requirement"
        verdict = str(entry.get("verdict") or "").strip()
        if verdict not in LANE_CHECKLIST_VERDICTS:
            return [], f"checklist verdict out of vocab: {verdict!r}"
        try:
            refs = _string_list(entry.get("evidence_refs"))
        except (TypeError, ValueError):
            return [], f"checklist item {item_id!r} evidence_refs must be a list of non-empty strings"
        if any(not _evidence_bound(ref, allowed) for ref in refs):
            return [], f"checklist item {item_id!r} cites evidence outside the allowlist"
        if verdict in {"pass", "partial"} and not refs:
            return [], (
                f"checklist item {item_id!r} verdict {verdict} requires "
                "evidence_refs from the allowlist"
            )
        items.append({
            "item_id": item_id,
            "kind": kind,
            "requirement": requirement,
            "verdict": verdict,
            "evidence_refs": refs,
        })
    if implicit_count < LANE_CHECKLIST_MIN_IMPLICIT:
        return [], (
            f"checklist requires at least {LANE_CHECKLIST_MIN_IMPLICIT} "
            f"implicit items; got {implicit_count}"
        )
    return items, ""


def _normalize_lane_concerns(
    dimensions: list[dict[str, Any]], allowed: set[str],
) -> tuple[list[dict[str, Any]], str]:
    """Validate per-dimension adversarial concerns and flatten them.

    Every v3 dimension must carry a ``concerns`` array. Each concern needs a
    non-empty Chinese text and evidence refs inside the allowlist. An empty
    array is legal only together with a non-empty ``no_concern_note``
    describing the checking process; both present at once is contradictory
    and rejected. Returns the flat ``[{dimension_id, text, evidence_refs}]``
    list stored on the result.
    """

    flattened: list[dict[str, Any]] = []
    for entry in dimensions:
        dimension_id = str(entry.get("dimension_id") or "")
        concerns = entry.get("concerns")
        if not isinstance(concerns, list):
            return [], f"dimension {dimension_id!r} requires a concerns list"
        note = str(entry.get("no_concern_note") or "").strip()
        if concerns and note:
            return [], (
                f"dimension {dimension_id!r} cannot carry both concerns and "
                "no_concern_note"
            )
        if not concerns and not note:
            return [], (
                f"dimension {dimension_id!r} with empty concerns requires a "
                "non-empty no_concern_note"
            )
        for concern in concerns:
            if not isinstance(concern, dict):
                return [], f"dimension {dimension_id!r} concern must be an object"
            text = str(concern.get("text") or "").strip()
            if not text:
                return [], f"dimension {dimension_id!r} concern needs a non-empty text"
            try:
                refs = _string_list(concern.get("evidence_refs"))
            except (TypeError, ValueError):
                return [], (
                    f"dimension {dimension_id!r} concern evidence_refs must be "
                    "a list of non-empty strings"
                )
            if not refs or any(not _evidence_bound(ref, allowed) for ref in refs):
                return [], (
                    f"dimension {dimension_id!r} concern cites evidence "
                    "outside the allowlist"
                )
            flattened.append({
                "dimension_id": dimension_id,
                "text": text,
                "evidence_refs": refs,
            })
    return flattened, ""


__all__ = [
    "LANE_CHECKLIST_KINDS",
    "LANE_CHECKLIST_MIN_IMPLICIT",
    "LANE_CHECKLIST_VERDICTS",
    "lane_prd_fidelity_score",
    "normalize_lane_prd_fidelity",
]
