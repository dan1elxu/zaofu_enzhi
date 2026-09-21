"""Judge output schemas and deterministic score computation.

Agents only produce semantic judgments (per-dimension scores, rationales,
evidence refs). Every aggregate number is computed here, deterministically,
per the external PRD (kanban-channel-prd-evaluation.md §4.3-4.5). Degradation
rules (§11): invalid JSON -> invalid (one bounded retry happens upstream);
evidence refs outside the allowlist invalidate the item; confidence < 0.60
converts the item to abstain; missing required evidence -> evidence_missing.

Result structures are now represented by frozen dataclasses
(``zf.runtime.evaluation_types``). They serialize to the same JSON shape as
before, so existing sidecars and events remain readable.
"""

from __future__ import annotations

from typing import Any

from zf.runtime.evaluation_types import (
    AgentFinding,
    CommunicationResult,
    ConclusionResult,
    DimensionScore,
    JudgeResult,
    MilestoneResult,
    PrdResult,
)

SCHEMA_VERSIONS: dict[str, str] = {
    "milestone": "milestone_result.v1",
    "communication": "communication_result.v1",
    "prd_result": "prd_result.v1",
    "conclusion": "conclusion_result.v1",
}

COMMUNICATION_DIMENSIONS: tuple[str, ...] = (
    "information_exchange",
    "clarity",
    "task_assistance",
    "efficiency",
    "necessity",
)

PRD_RESULT_DIMENSIONS: tuple[str, ...] = (
    "original_to_channel_prd",
    "channel_to_workflow_prd",
)

NECESSITY_VOCAB = frozenset({
    "necessary",
    "optional",
    "unnecessary",
    "misfit",
})

OVERALL_COMMUNICATION_WEIGHT = 0.40
OVERALL_PRD_RESULT_WEIGHT = 0.60

_CONFIDENCE_FLOOR = 0.60


def mean_scores(scores: list[float | None]) -> float | None:
    """Round(mean(valid scores), 2); None when any required score is absent."""

    if not scores or any(score is None for score in scores):
        return None
    return round(sum(scores) / len(scores), 2)  # type: ignore[arg-type]


def communication_score(dimensions: list[Any]) -> float | None:
    """Deterministic team communication score (PRD §4.3)."""

    by_id = _dimension_index(dimensions)
    return mean_scores([
        _dimension_score_dataclass(by_id.get(dimension_id))
        for dimension_id in COMMUNICATION_DIMENSIONS
    ])


def prd_result_score(dimensions: list[Any]) -> float | None:
    """Deterministic PRD result score (PRD §4.4).

    The PRD result judge now runs at freeze time and scores
    ``original_to_channel_prd`` immediately; the
    ``channel_to_workflow_prd`` dimension is scored only after a workflow
    binds. When both dimensions are scored we return their mean; when only
    one is scored we return that dimension's score so the freeze-time
    evaluation still produces a meaningful result.
    """

    by_id = _dimension_index(dimensions)
    original = _dimension_score_dataclass(by_id.get("original_to_channel_prd"))
    workflow = _dimension_score_dataclass(by_id.get("channel_to_workflow_prd"))
    if original is not None and workflow is not None:
        return mean_scores([original, workflow])
    if original is not None:
        return original
    if workflow is not None:
        return workflow
    return None


def overall_score(
    *,
    communication: float | None,
    prd_result: float | None,
) -> float | None:
    """Deterministic overall score (PRD §4.5); None unless both inputs scored."""

    if communication is None or prd_result is None:
        return None
    return round(
        OVERALL_COMMUNICATION_WEIGHT * communication
        + OVERALL_PRD_RESULT_WEIGHT * prd_result,
        2,
    )


def normalize_judge_output(
    *,
    judge_type: str,
    raw: dict[str, Any],
    allowed_evidence_refs: list[str],
) -> tuple[JudgeResult | None, str]:
    """Validate one judge JSON object against its v1 schema.

    Returns (normalized_result, "") on success or (None, error_reason).
    Normalization applies §11 item-level rules: unbound evidence refs mark
    the item invalid; confidence below the floor converts to abstain.
    """

    schema_version = SCHEMA_VERSIONS.get(judge_type)
    if not schema_version:
        return None, f"unknown judge_type: {judge_type}"
    if str(raw.get("schema_version") or "") != schema_version:
        return None, (
            f"schema_version mismatch: expected {schema_version}, "
            f"got {raw.get('schema_version')!r}"
        )
    # ``judge_type`` is advisory — the schema_version already binds the type
    # ("milestone_result.v1" can only be milestone). Judges prompted for the
    # v1 schemas routinely omit it (2026-09-17 live run); reject only an
    # explicit contradiction, not an absent field.
    declared = str(raw.get("judge_type") or "")
    if declared and declared != judge_type:
        return None, f"judge_type mismatch: expected {judge_type}"

    allowed = {str(ref).strip() for ref in allowed_evidence_refs if str(ref).strip()}
    if judge_type == "milestone":
        result, norm_error = _normalize_milestone(raw, allowed)
    elif judge_type == "communication":
        result, norm_error = _normalize_communication(raw, allowed)
    elif judge_type == "prd_result":
        result, norm_error = _normalize_prd_result(raw, allowed)
    elif judge_type == "conclusion":
        result, norm_error = _normalize_conclusion(raw)
    else:
        return None, f"unknown judge_type: {judge_type}"

    if result is None:
        return None, norm_error
    return result, ""


def _dimension_index(dimensions: list[Any]) -> dict[str, DimensionScore]:
    """Build a dimension_id -> DimensionScore map from dataclasses or dicts."""

    by_id: dict[str, DimensionScore] = {}
    for item in dimensions:
        if isinstance(item, DimensionScore):
            by_id[item.dimension_id] = item
        elif isinstance(item, dict):
            dim = DimensionScore.from_dict(item)
            if dim.dimension_id:
                by_id[dim.dimension_id] = dim
    return by_id


def _dimension_score_dataclass(item: DimensionScore | None) -> float | None:
    if not item or item.status != "scored":
        return None
    return item.score


def _dimension_entries(value: Any) -> list[dict[str, Any]]:
    """Accept the v1 list form and the dict form judges actually emit
    (``{"information_exchange": {"score": 5, ...}, ...}`` — verified in the
    2026-09-17 live run). Dict keys become ``dimension_id`` when the entry
    does not declare one."""

    if isinstance(value, list) and value:
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict) and value:
        entries: list[dict[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("dimension_id", key)
                entries.append(entry)
        return entries
    return []


def _normalize_milestone(
    raw: dict[str, Any],
    allowed: set[str],
) -> tuple[MilestoneResult | None, str]:
    # Live judges (2026-09-17) emit ``evaluations`` and put confidence at
    # the top level; the v1 schema says ``agent_findings`` + per-item
    # confidence. A v3 live run invented ``agent_reports``. Accept all —
    # schema_version already bound the type.
    findings = (
        raw.get("agent_findings")
        or raw.get("evaluations")
        or raw.get("agent_reports")
    )
    if not isinstance(findings, list) or not findings:
        return None, "agent_findings must be a non-empty list"
    default_confidence = raw.get("confidence")
    normalized: list[AgentFinding] = []
    for item in findings:
        if not isinstance(item, dict) or not str(item.get("agent_id") or "").strip():
            return None, "every agent_finding needs agent_id"
        score = _score_value(item.get("score"))
        necessity = str(item.get("necessity") or "").strip()
        if necessity not in NECESSITY_VOCAB:
            return None, f"necessity out of vocab: {necessity!r}"
        finding = AgentFinding(
            agent_id=str(item.get("agent_id")).strip(),
            score=score,
            necessity=necessity,
            role_summary=str(
                item.get("role_summary") or item.get("summary") or ""
            ).strip(),
            evidence_refs=_string_list(item.get("evidence_refs")),
            rationale=str(item.get("rationale") or "").strip(),
            confidence=_confidence(
                item.get("confidence")
                if item.get("confidence") is not None
                else default_confidence
            ),
        )
        normalized.append(finding.with_status(_item_status(finding, allowed)))
    missing = _string_list(
        raw.get("missing_evidence") or raw.get("missing_evidence_keys")
    )
    if any(not _evidence_bound(ref, allowed) for ref in missing):
        return None, "missing_evidence refs must come from allowed_evidence_refs"
    return MilestoneResult(
        schema_version=str(raw["schema_version"]),
        judge_type="milestone",
        status=_result_status(normalized, missing),
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        agent_findings=normalized,
        missing_evidence=missing,
    ), ""


def _normalize_communication(
    raw: dict[str, Any],
    allowed: set[str],
) -> tuple[CommunicationResult | None, str]:
    dimensions = _dimension_entries(
        raw.get("dimensions") or raw.get("scores") or raw.get("dimension_scores")
    )
    if not dimensions:
        return None, "dimensions must be a non-empty list"
    default_confidence = raw.get("confidence")
    dims = [
        _normalize_dimension(item, allowed, default_confidence)
        for item in dimensions
    ]
    if any(d is None for d in dims):
        return None, "dimension entry malformed (needs dimension_id)"
    dims = [d for d in dims if d is not None]
    missing = _string_list(
        raw.get("missing_evidence") or raw.get("missing_evidence_keys")
    )
    return CommunicationResult(
        schema_version=str(raw["schema_version"]),
        judge_type="communication",
        status=_result_status(dims, missing),
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        dimensions=dims,
        human_intervention_count=int(raw.get("human_intervention_count") or 0),
        communication_score=communication_score(dims),
        missing_evidence=missing,
    ), ""


def _normalize_prd_result(
    raw: dict[str, Any],
    allowed: set[str],
) -> tuple[PrdResult | None, str]:
    # Live judges (2026-09-17) also emit the two boundaries as top-level keys
    # ("original_to_channel_prd": {...}, "channel_to_workflow_prd": {...})
    # with no "dimensions"/"scores" wrapper — lift those as dimension entries.
    # A v3 live run used "dimension_scores" as the wrapper.
    dimensions = _dimension_entries(
        raw.get("dimensions")
        or raw.get("scores")
        or raw.get("dimension_scores")
    )
    if not dimensions:
        dimensions = _dimension_entries(
            {key: raw[key] for key in PRD_RESULT_DIMENSIONS if key in raw}
        )
    if not dimensions:
        return None, "dimensions must be a non-empty list"
    default_confidence = raw.get("confidence")
    dims = [
        _normalize_dimension(item, allowed, default_confidence)
        for item in dimensions
    ]
    if any(d is None for d in dims):
        return None, "dimension entry malformed (needs dimension_id)"
    dims = [d for d in dims if d is not None]
    missing = _string_list(
        raw.get("missing_evidence") or raw.get("missing_evidence_keys")
    )
    return PrdResult(
        schema_version=str(raw["schema_version"]),
        judge_type="prd_result",
        status=_prd_result_status(dims, missing, allowed),
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        dimensions=dims,
        differences=_string_list(raw.get("differences")),
        prd_result_score=prd_result_score(dims),
        missing_evidence=missing,
    ), ""


def _normalize_conclusion(
    raw: dict[str, Any],
) -> tuple[ConclusionResult | None, str]:
    # Live judges (2026-09-17) emit ``role_analysis`` for team_findings,
    # put per-agent scores under ``scores.*.per_agent``, and write the
    # narrative under ``conclusion`` — all aliases of the v1 field names.
    team = raw.get("team_findings") or raw.get("role_analysis")
    if not isinstance(team, dict):
        return None, "team_findings must be an object"
    agent_scores = raw.get("agent_scores")
    if not isinstance(agent_scores, list):
        per_agent = (
            (raw.get("scores") or {}).get("milestone") or {}
        ).get("per_agent")
        if isinstance(per_agent, dict) and per_agent:
            agent_scores = [
                {"agent_id": str(agent_id), "score": _float_or_none(score)}
                for agent_id, score in per_agent.items()
            ]
    if not isinstance(agent_scores, list):
        return None, "agent_scores must be a list"
    status = str(raw.get("status") or "complete").strip() or "complete"
    if status.lower() in {"ok", "done", "finished"}:
        status = "complete"
    return ConclusionResult(
        schema_version=str(raw["schema_version"]),
        judge_type="conclusion",
        status=status,
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        communication_score=_float_or_none(raw.get("communication_score")),
        prd_result_score=_float_or_none(raw.get("prd_result_score")),
        overall_score=_float_or_none(raw.get("overall_score")),
        agent_scores=list(agent_scores),
        team_findings=team,
        conclusion_zh=str(
            raw.get("conclusion_zh")
            or raw.get("conclusion")
            or raw.get("summary")
            or raw.get("overall_rationale")
            or ""
        ).strip(),
        next_actions=_string_list(raw.get("next_actions")),
        judge_refs=_string_list(raw.get("judge_refs")),
        evidence_refs=_string_list(raw.get("evidence_refs")),
    ), ""


def _normalize_dimension(
    item: dict[str, Any],
    allowed: set[str],
    default_confidence: Any = None,
) -> DimensionScore | None:
    dimension_id = str(item.get("dimension_id") or "").strip()
    if not dimension_id:
        return None
    dim = DimensionScore(
        dimension_id=dimension_id,
        score=_score_value(item.get("score")),
        verdict=str(item.get("verdict") or "").strip(),
        evidence_refs=_string_list(item.get("evidence_refs")),
        failure_explanation=str(item.get("failure_explanation") or "").strip(),
        rationale=str(item.get("rationale") or "").strip(),
        confidence=_confidence(
            item.get("confidence")
            if item.get("confidence") is not None
            else default_confidence
        ),
    )
    return dim.with_status(_item_status(dim, allowed))


def _item_status(entry: AgentFinding | DimensionScore, allowed: set[str]) -> str:
    refs = entry.evidence_refs
    if any(not _evidence_bound(ref, allowed) for ref in refs):
        return "invalid"
    if entry.score is None:
        return "evidence_missing"
    if entry.confidence < _CONFIDENCE_FLOOR:
        return "abstain"
    return "scored"


def _result_status(items: list[Any], missing: list[str]) -> str:
    statuses = {str(getattr(item, "status", "")) for item in items}
    if "invalid" in statuses:
        return "invalid"
    if missing or "evidence_missing" in statuses:
        return "evidence_missing"
    if statuses == {"abstain"}:
        return "abstain"
    if "abstain" in statuses:
        return "conflict"
    return "scored"


def _prd_result_status(
    dims: list[DimensionScore],
    missing: list[str],
    allowed: set[str],
) -> str:
    """Result status for prd_result with not-yet-applicable dimensions.

    ``channel_to_workflow_prd`` can only be judged after a workflow binds.
    The packet carries ``artifact/prd/workflow-bound.json`` in
    allowed_evidence_refs exactly when a workflow is bound (see
    ``build_packet``); before that, a ``channel_to_workflow_prd`` entry the
    judge left unscored (score null -> item evidence_missing) means "not
    applicable yet", not "evidence missing". v3's prompt asked judges to
    return such a null entry, which otherwise downgraded the whole result to
    evidence_missing and hid the scored original_to_channel_prd dimension.
    """

    workflow_bound = "artifact/prd/workflow-bound.json" in allowed
    if workflow_bound:
        return _result_status(dims, missing)
    active = [
        d for d in dims
        if d.dimension_id != "channel_to_workflow_prd"
    ]
    if not active:
        return "evidence_missing"
    return _result_status(active, missing)


def _score_value(value: Any) -> float | None:
    score = _float_or_none(value)
    if score is None:
        return None
    if not 1.0 <= score <= 5.0:
        return None
    return score


def _confidence(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _evidence_bound(ref: str, allowed: set[str]) -> bool:
    return ref in allowed


__all__ = [
    "COMMUNICATION_DIMENSIONS",
    "NECESSITY_VOCAB",
    "OVERALL_COMMUNICATION_WEIGHT",
    "OVERALL_PRD_RESULT_WEIGHT",
    "PRD_RESULT_DIMENSIONS",
    "SCHEMA_VERSIONS",
    "communication_score",
    "mean_scores",
    "normalize_judge_output",
    "overall_score",
    "prd_result_score",
]
