"""Judge output schemas and deterministic score computation.

Agents only produce semantic judgments (per-dimension scores, rationales,
evidence refs). Every aggregate number is computed here, deterministically,
per the Channel/PRD evaluation contract. Degradation
rules (§11): invalid JSON -> invalid (one bounded retry happens upstream);
evidence refs outside the allowlist invalidate the item; confidence < 0.60
converts the item to abstain; missing required evidence -> evidence_missing.

Result structures are now represented by frozen dataclasses
(``zf.runtime.evaluation_types``). They serialize to the same JSON shape as
before, so existing sidecars and events remain readable.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from zf.runtime.evaluation_types import (
    AgentFinding,
    NECESSITY_UNDETERMINED,
    NECESSITY_VALUES,
    DiscussionResult,
    ConclusionResult,
    DimensionScore,
    JudgeResult,
    ContributionResult,
    PrdFidelityResult,
    TaskTypeResult,
    canonical_necessity,
)

SCHEMA_VERSIONS: dict[str, str] = {
    "task_type": "task_type_result.v1",
    "contribution": "contribution_result.v2",
    "discussion": "discussion_result.v2",
    "prd_fidelity": "prd_fidelity_result.v2",
    "conclusion": "conclusion_result.v2",
}

# Named registry for callers that distinguish role-bound stages from the task
# classifier. There is no retired-stage fallback.
ROLE_SCHEMA_VERSIONS: dict[str, str] = {
    key: value for key, value in SCHEMA_VERSIONS.items() if key != "task_type"
}
SCORE_SCALE_RANGES: dict[str, tuple[int, int]] = {"0-4": (0, 4)}
TASK_TYPES = frozenset({"bugfix", "refactor", "greenfield", "ambiguous"})

DISCUSSION_DIMENSIONS: tuple[str, ...] = (
    "information_exchange",
    "clarity",
    "task_assistance",
    "efficiency",
    "necessity",
)

PRD_FIDELITY_DIMENSIONS: tuple[str, ...] = (
    "original_to_channel_prd",
    "channel_to_workflow_prd",
)

# Stable necessity vocabulary. ``undetermined`` is reserved for a finding
# whose evidence cannot establish whether the role was needed. Its
# contribution can still be scored independently.
NECESSITY_VOCAB = NECESSITY_VALUES
NECESSITY_ALLOWED = NECESSITY_VALUES | {NECESSITY_UNDETERMINED}

_CONFIDENCE_FLOOR = 0.60


def mean_scores(
    scores: list[float | None], *, score_scale: str = "0-4",
) -> float | None:
    """Round(mean(valid scores), 2); None when any required score is absent."""

    minimum, maximum = _score_bounds(score_scale)
    if not scores or any(not _valid_number(score, minimum, maximum) for score in scores):
        return None
    return round(sum(scores) / len(scores), 2)  # type: ignore[arg-type]


def discussion_score(dimensions: list[Any], *, score_scale: str | None = None) -> float | None:
    """Deterministic team discussion score (PRD §4.3)."""

    scale = score_scale or _score_scale_from_items(dimensions)
    by_id = _dimension_index(dimensions, score_scale=scale)
    if set(by_id) != set(DISCUSSION_DIMENSIONS):
        return None
    return mean_scores([
        _dimension_score_dataclass(by_id.get(dimension_id), score_scale=scale)
        for dimension_id in DISCUSSION_DIMENSIONS
    ], score_scale=scale)


def prd_fidelity_score(
    dimensions: list[Any], *, workflow_bound: bool | None = None,
    score_scale: str | None = None,
) -> float | None:
    """Deterministic PRD fidelity score (PRD §4.4).

    The PRD fidelity judge runs at freeze time and scores
    ``original_to_channel_prd`` immediately; the
    ``channel_to_workflow_prd`` dimension is scored only after a workflow
    binds. Every applicable dimension must be present and scored; missing
    or abstained workflow comparisons must never become a partial full score.
    """

    scale = score_scale or _score_scale_from_items(dimensions)
    by_id = _dimension_index(dimensions, score_scale=scale)
    if workflow_bound is None:
        workflow_bound = "channel_to_workflow_prd" in by_id
    required = PRD_FIDELITY_DIMENSIONS if workflow_bound else PRD_FIDELITY_DIMENSIONS[:1]
    if set(by_id) != set(required):
        return None
    return mean_scores(
        [_dimension_score_dataclass(by_id[key], score_scale=scale) for key in required],
        score_scale=scale,
    )


def normalize_judge_output(
    *,
    judge_type: str,
    raw: dict[str, Any],
    allowed_evidence_refs: list[str],
    expected_agent_ids: list[str] | None = None,
    workflow_bound: bool | None = None,
    expected_schema_version: str | None = None,
    role_mode: bool = False,
) -> tuple[JudgeResult | None, str]:
    """Validate one judge JSON object against the canonical stage schema.

    Returns (normalized_result, "") on success or (None, error_reason).
    Normalization applies §11 item-level rules: unbound evidence refs mark
    the item invalid; confidence below the floor converts to abstain.
    """

    if not isinstance(raw, dict):
        return None, "judge output must be an object"
    schema_version = SCHEMA_VERSIONS.get(judge_type)
    if not schema_version:
        return None, f"unknown judge_type: {judge_type}"
    # ``role_mode`` is retained as an inert keyword for callers that pass it;
    # quality stages are always role-bound and alternate schemas are rejected.
    if expected_schema_version and expected_schema_version != schema_version:
        return None, (
            f"unsupported schema_version for {judge_type}: "
            f"expected canonical {schema_version}, got {expected_schema_version!r}"
        )
    if str(raw.get("schema_version") or "") != schema_version:
        return None, (
            f"schema_version mismatch: expected {schema_version}, "
            f"got {raw.get('schema_version')!r}"
        )
    # ``judge_type`` is advisory — the schema_version already binds the type;
    # reject only an explicit contradiction, not an absent field.
    declared = str(raw.get("judge_type") or "")
    if declared and declared != judge_type:
        return None, f"judge_type mismatch: expected {judge_type}"

    allowed = {str(ref).strip() for ref in allowed_evidence_refs if str(ref).strip()}
    score_scale = _score_scale_for_schema(schema_version)
    declared_scale = str(raw.get("score_scale") or "").strip()
    if declared_scale and declared_scale != score_scale:
        return None, (
            f"score_scale mismatch: expected {score_scale}, got {declared_scale!r}"
        )
    try:
        if judge_type == "task_type":
            result, norm_error = _normalize_task_type(raw, allowed)
        elif judge_type == "contribution":
            result, norm_error = _normalize_contribution(
                raw, allowed, expected_agent_ids, score_scale=score_scale,
            )
        elif judge_type == "discussion":
            result, norm_error = _normalize_discussion(
                raw, allowed, score_scale=score_scale,
            )
        elif judge_type == "prd_fidelity":
            result, norm_error = _normalize_prd_fidelity(
                raw, allowed, workflow_bound, score_scale=score_scale,
            )
        elif judge_type == "conclusion":
            result, norm_error = _normalize_conclusion(raw, score_scale=score_scale)
        else:
            return None, f"unknown judge_type: {judge_type}"
    except (ValueError, TypeError, OverflowError) as exc:
        return None, str(exc)

    if result is None:
        return None, norm_error
    return result, ""


def _dimension_index(
    dimensions: list[Any], *, score_scale: str | None = None,
) -> dict[str, DimensionScore]:
    """Build a dimension_id -> DimensionScore map from dataclasses or dicts."""

    by_id: dict[str, DimensionScore] = {}
    for item in dimensions:
        if isinstance(item, DimensionScore):
            dim = item
        elif isinstance(item, dict):
            try:
                _score_value(item.get("score"), *_score_bounds(
                    score_scale or str(item.get("score_scale") or "0-4")
                ))
            except ValueError:
                return {}
            dim = DimensionScore.from_dict(item)
        else:
            return {}
        if not dim.dimension_id or dim.dimension_id in by_id:
            return {}
        by_id[dim.dimension_id] = dim
    return by_id


def _dimension_score_dataclass(
    item: DimensionScore | None, *, score_scale: str | None = None,
) -> float | None:
    if not item or item.status != "scored":
        return None
    try:
        return _score_value(
            item.score,
            *_score_bounds(score_scale or item.score_scale),
        )
    except ValueError:
        return None


def _dimension_entries(value: Any) -> list[dict[str, Any]]:
    """Accept the v1 list form and the dict form judges actually emit
    (``{"information_exchange": {"score": 5, ...}, ...}`` — verified in the
    2026-09-17 live run). Dict keys become ``dimension_id`` when the entry
    does not declare one."""

    if isinstance(value, list) and value:
        if not all(isinstance(item, dict) for item in value):
            raise ValueError("every dimension must be an object")
        return [dict(item) for item in value]
    if isinstance(value, dict) and value:
        entries: list[dict[str, Any]] = []
        for key, item in value.items():
            if not isinstance(item, dict) or item.get("dimension_id", key) != key:
                raise ValueError("dimension mapping key and entry must agree")
            entry = dict(item)
            entry.setdefault("dimension_id", key)
            entries.append(entry)
        return entries
    return []


def _normalize_contribution(
    raw: dict[str, Any],
    allowed: set[str],
    expected_agent_ids: list[str] | None = None,
    *,
    score_scale: str = "0-4",
) -> tuple[ContributionResult | None, str]:
    findings = raw.get("agent_findings")
    if not isinstance(findings, list) or not findings:
        return None, "agent_findings must be a non-empty list"
    _require_ids(findings, "agent_id", expected_agent_ids)
    default_confidence = raw.get("confidence")
    normalized: list[AgentFinding] = []
    for item in findings:
        if not isinstance(item, dict) or not str(item.get("agent_id") or "").strip():
            return None, "every agent_finding needs agent_id"
        _validate_item(item, default_confidence, score_scale=score_scale)
        score = _score_value(item.get("score"), *_score_bounds(score_scale))
        necessity = canonical_necessity(item.get("necessity"))
        if necessity not in NECESSITY_ALLOWED:
            return None, f"necessity out of vocab: {necessity!r}"
        necessity_status = str(item.get("necessity_status") or "").strip()
        if not necessity_status:
            necessity_status = (
                "incomplete" if necessity == NECESSITY_UNDETERMINED else "complete"
            )
        if necessity_status not in {"complete", "incomplete"}:
            return None, "necessity_status must be complete or incomplete"
        if necessity_status == "incomplete" and necessity != NECESSITY_UNDETERMINED:
            return None, "incomplete necessity_status requires necessity=undetermined"
        if necessity == NECESSITY_UNDETERMINED and necessity_status != "incomplete":
            return None, "undetermined necessity requires necessity_status=incomplete"
        finding = AgentFinding(
            agent_id=str(item.get("agent_id")).strip(),
            score=score,
            necessity=necessity,
            role_summary=str(item.get("role_summary") or "").strip(),
            evidence_refs=_string_list(item.get("evidence_refs")),
            rationale=str(item.get("rationale") or "").strip(),
            confidence=_confidence(
                item.get("confidence")
                if item.get("confidence") is not None
                else default_confidence
            ),
            necessity_status=necessity_status,
            score_scale=score_scale,
        )
        status = _item_status(finding, allowed, str(item.get("status") or ""))
        normalized.append(replace(finding, status=status, score=score if status == "scored" else None))
    missing = _string_list(
        raw.get("missing_evidence") or raw.get("missing_evidence_keys")
    )
    # Necessity is deliberately independent from contribution quality.  An
    # Agent may have a well-evidenced contribution score while its necessity
    # remains undetermined; only that finding's ``necessity_status`` is
    # incomplete.  A missing contribution score still follows the ordinary
    # item/result evidence gate below.
    result_status = _result_status(normalized, missing)
    return ContributionResult(
        schema_version=str(raw["schema_version"]),
        judge_type="contribution",
        status=result_status,
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        agent_findings=normalized,
        missing_evidence=missing,
        score_scale=score_scale,
        **_result_metadata(raw),
    ), ""


def _normalize_discussion(
    raw: dict[str, Any],
    allowed: set[str],
    *,
    score_scale: str = "0-4",
) -> tuple[DiscussionResult | None, str]:
    dimensions = _dimension_entries(
        raw.get("dimensions") or raw.get("scores") or raw.get("dimension_scores")
    )
    if not dimensions:
        return None, "dimensions must be a non-empty list"
    _require_ids(dimensions, "dimension_id", DISCUSSION_DIMENSIONS)
    default_confidence = raw.get("confidence")
    dims = [
        _normalize_dimension(item, allowed, default_confidence, score_scale=score_scale)
        for item in dimensions
    ]
    if any(d is None for d in dims):
        return None, "dimension entry malformed (needs dimension_id)"
    dims = [d for d in dims if d is not None]
    missing = _string_list(
        raw.get("missing_evidence") or raw.get("missing_evidence_keys")
    )
    status = _result_status(dims, missing)
    human_count = raw.get("human_intervention_count", 0)
    if type(human_count) is not int or human_count < 0:
        return None, "human_intervention_count must be a non-negative integer"
    return DiscussionResult(
        schema_version=str(raw["schema_version"]),
        judge_type="discussion",
        status=status,
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        dimensions=dims,
        human_intervention_count=human_count,
        discussion_score=discussion_score(dims, score_scale=score_scale) if status == "scored" else None,
        missing_evidence=missing,
        score_scale=score_scale,
        **_result_metadata(raw),
    ), ""


def _normalize_prd_fidelity(
    raw: dict[str, Any],
    allowed: set[str],
    workflow_bound: bool | None = None,
    *,
    score_scale: str = "0-4",
) -> tuple[PrdFidelityResult | None, str]:
    dimensions = _dimension_entries(raw.get("dimensions"))
    if not dimensions:
        return None, "dimensions must be a non-empty list"
    if workflow_bound is None:
        workflow_bound = "artifact/prd/workflow-bound.json" in allowed
    required = PRD_FIDELITY_DIMENSIONS if workflow_bound else PRD_FIDELITY_DIMENSIONS[:1]
    _require_ids(dimensions, "dimension_id", required)
    default_confidence = raw.get("confidence")
    dims = [
        _normalize_dimension(item, allowed, default_confidence, score_scale=score_scale)
        for item in dimensions
    ]
    if any(d is None for d in dims):
        return None, "dimension entry malformed (needs dimension_id)"
    dims = [d for d in dims if d is not None]
    missing = _string_list(
        raw.get("missing_evidence") or raw.get("missing_evidence_keys")
    )
    status = _result_status(dims, missing)
    return PrdFidelityResult(
        schema_version=str(raw["schema_version"]),
        judge_type="prd_fidelity",
        status=status,
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        dimensions=dims,
        differences=_string_list(raw.get("differences")),
        prd_fidelity_score=prd_fidelity_score(
            dims, workflow_bound=workflow_bound, score_scale=score_scale,
        ) if status == "scored" else None,
        missing_evidence=missing,
        score_scale=score_scale,
        **_result_metadata(raw),
    ), ""


def _normalize_conclusion(
    raw: dict[str, Any],
    *,
    score_scale: str = "0-4",
) -> tuple[ConclusionResult | None, str]:
    team = raw.get("team_findings")
    if not isinstance(team, dict):
        return None, "team_findings must be an object"
    agent_scores = raw.get("agent_scores")
    if not isinstance(agent_scores, list):
        return None, "agent_scores must be a list"
    if _has_adjusted_score(agent_scores) or any(
        key in raw for key in {
            "judge_adjusted_score", "adjusted_score", "score_override",
            "raw_subagent_score", "subagent_score", "original_score",
            "final_score", "adjustment_reason",
            "discussion_score", "prd_fidelity_score", "overall_score",
        }
    ):
        return None, "main judge may summarize subagent scores but cannot adjust or aggregate them"
    normalized_agent_scores, scores_error = _normalize_agent_scores(
        agent_scores, score_scale=score_scale,
    )
    if scores_error:
        return None, scores_error
    status = str(raw.get("status") or "complete").strip() or "complete"
    if status.lower() in {"ok", "done", "finished"}:
        status = "complete"
    if status not in {"complete", "incomplete", "inconclusive"}:
        return None, "conclusion status must be complete, incomplete or inconclusive"
    narrative = raw.get("conclusion_zh")
    if not isinstance(narrative, str) or not narrative.strip():
        return None, "conclusion_zh must be non-empty"
    for field in ("discussion_meaningful", "discussion_progressed", "prd_ready_for_next_stage"):
        if type(team.get(field)) is not bool:
            return None, f"team_findings.{field} must be a boolean"
    for field in ("problems", "underperformed_roles", "unnecessary_roles"):
        if not isinstance(team.get(field), list):
            return None, f"team_findings.{field} must be a list"
        _string_list(team[field])
    if status != "complete" and team["prd_ready_for_next_stage"]:
        return None, "an incomplete conclusion cannot declare PRD ready"
    return ConclusionResult(
        schema_version=str(raw["schema_version"]),
        judge_type="conclusion",
        status=status,
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        agent_scores=normalized_agent_scores,
        team_findings=team,
        conclusion_zh=narrative.strip(),
        next_actions=_string_list(raw.get("next_actions")),
        judge_refs=_string_list(raw.get("judge_refs")),
        evidence_refs=_string_list(raw.get("evidence_refs")),
        score_scale=score_scale,
        **_result_metadata(raw),
    ), ""


def _result_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract optional task/role identity without changing score semantics.

    Metadata is advisory context for the frozen evaluator role.  It is copied
    into normalized sidecars so a report can be traced to the selected task
    type and role revision, but it never participates in score aggregation.
    A nested ``metadata``/``role`` object is accepted for compatibility with
    role catalogs that already use those shapes.
    """

    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    role = raw.get("role") if isinstance(raw.get("role"), dict) else {}
    role_metadata = raw.get("role_metadata")
    if not isinstance(role_metadata, dict):
        role_metadata = {}
    merged_role_metadata = dict(role)
    merged_role_metadata.update(role_metadata)
    task_type = str(
        raw.get("task_type")
        or metadata.get("task_type")
        or ""
    ).strip()
    role_id = str(
        raw.get("role_id")
        or metadata.get("role_id")
        or merged_role_metadata.get("role_id")
        or merged_role_metadata.get("id")
        or ""
    ).strip()
    role_version = str(
        raw.get("role_version")
        or metadata.get("role_version")
        or merged_role_metadata.get("role_version")
        or merged_role_metadata.get("version")
        or ""
    ).strip()
    return {
        "task_type": task_type,
        "role_id": role_id,
        "role_version": role_version,
        "role_metadata": merged_role_metadata,
    }


def _has_adjusted_score(agent_scores: list[Any]) -> bool:
    """Reject explicit score overrides in the main-Judge summary contract."""

    forbidden = {
        "adjusted_score", "judge_adjusted_score", "override_score",
        "score_override", "raw_subagent_score", "subagent_score",
        "original_score", "final_score", "adjustment_reason",
    }
    return any(
        isinstance(item, dict) and forbidden.intersection(item)
        for item in agent_scores
    )


def _normalize_agent_scores(
    values: list[Any], *, score_scale: str,
) -> tuple[list[dict[str, Any]], str]:
    """Validate the main Judge's copied per-Agent summaries.

    This function only validates copied values; it never computes or alters a
    score.
    """

    minimum, maximum = _score_bounds(score_scale)
    normalized: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            return [], "every agent_scores entry must be an object"
        agent_id = str(item.get("agent_id") or "").strip()
        if not agent_id:
            return [], "every agent_scores entry needs agent_id"
        try:
            score = _score_value(item.get("score"), minimum, maximum)
        except ValueError as exc:
            return [], str(exc)
        output = dict(item)
        output["agent_id"] = agent_id
        output["score"] = score
        if "necessity" not in item:
            return [], "every agent_scores entry needs necessity"
        necessity = canonical_necessity(item.get("necessity"))
        if necessity not in NECESSITY_ALLOWED:
            return [], f"necessity out of vocab: {necessity!r}"
        output["necessity"] = necessity
        necessity_status = str(item.get("necessity_status") or "").strip()
        if not necessity_status:
            necessity_status = (
                "incomplete" if necessity == NECESSITY_UNDETERMINED else "complete"
            )
        if necessity_status not in {"complete", "incomplete"}:
            return [], "necessity_status must be complete or incomplete"
        if necessity_status == "incomplete" and necessity != NECESSITY_UNDETERMINED:
            return [], "incomplete necessity_status requires necessity=undetermined"
        output["necessity_status"] = necessity_status
        normalized.append(output)
    return normalized, ""


def _normalize_dimension(
    item: dict[str, Any],
    allowed: set[str],
    default_confidence: Any = None,
    *,
    score_scale: str = "0-4",
) -> DimensionScore | None:
    dimension_id = str(item.get("dimension_id") or "").strip()
    if not dimension_id:
        return None
    _validate_item(item, default_confidence, score_scale=score_scale)
    dim = DimensionScore(
        dimension_id=dimension_id,
        score=_score_value(item.get("score"), *_score_bounds(score_scale)),
        verdict=str(item.get("verdict") or "").strip(),
        evidence_refs=_string_list(item.get("evidence_refs")),
        failure_explanation=str(item.get("failure_explanation") or "").strip(),
        rationale=str(item.get("rationale") or "").strip(),
        confidence=_confidence(
            item.get("confidence")
            if item.get("confidence") is not None
            else default_confidence
        ),
        score_scale=score_scale,
    )
    status = _item_status(dim, allowed, str(item.get("status") or ""))
    return replace(dim, status=status, score=dim.score if status == "scored" else None)


def _item_status(entry: AgentFinding | DimensionScore, allowed: set[str], declared: str = "") -> str:
    refs = entry.evidence_refs
    if any(not _evidence_bound(ref, allowed) for ref in refs):
        return "invalid"
    if declared == "abstain":
        return "abstain"
    if declared == "incomplete":
        return "evidence_missing"
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


def _require_ids(items: list[dict[str, Any]], field: str, expected: Any = None) -> None:
    ids = [item.get(field) if isinstance(item, dict) else None for item in items]
    if any(not isinstance(value, str) or not value or value != value.strip() for value in ids):
        raise ValueError(f"every entry needs a non-empty {field}")
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate {field}")
    if expected is not None and set(ids) != set(expected):
        raise ValueError(f"{field} must contain exactly {sorted(expected)}; got {sorted(ids)}")


def _validate_item(
    item: dict[str, Any], default_confidence: Any, *, score_scale: str = "0-4",
) -> None:
    if "score" not in item:
        raise ValueError("score is required; use null when evidence is missing")
    score = _score_value(item["score"], *_score_bounds(score_scale))
    confidence = _confidence(item.get("confidence", default_confidence))
    if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
        raise ValueError("each item requires a non-empty rationale")
    refs = _string_list(item.get("evidence_refs"))
    declared = item.get("status")
    if declared is not None and declared not in {
        "scored", "abstain", "evidence_missing", "incomplete",
    }:
        raise ValueError("item status must be scored, abstain, evidence_missing or incomplete")
    if declared in {"abstain", "evidence_missing", "incomplete"} and score is not None:
        raise ValueError("unscored items must have score=null")
    if declared == "scored" and (score is None or confidence < _CONFIDENCE_FLOOR):
        raise ValueError("scored status requires a valid score and confidence >= 0.60")
    if (score is not None or declared == "abstain") and not refs:
        raise ValueError("scored/abstained items require non-empty evidence_refs")


def _valid_number(value: Any, minimum: float, maximum: float) -> bool:
    return type(value) in {int, float} and minimum <= value <= maximum and math.isfinite(value)


def _score_value(value: Any, minimum: int = 0, maximum: int = 4) -> float | None:
    if value is None:
        return None
    if not _valid_number(value, minimum, maximum) or not float(value).is_integer():
        raise ValueError(
            f"score must be an integer from {minimum} to {maximum}, or null"
        )
    return float(value)


def _confidence(value: Any) -> float:
    if not _valid_number(value, 0, 1):
        raise ValueError("confidence must be a finite JSON number from 0 to 1")
    return float(value)  # Never round 0.599 up across the abstention threshold.


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("expected a list of non-empty strings")
    return [item.strip() for item in value]


def _evidence_bound(ref: str, allowed: set[str]) -> bool:
    return ref in allowed


def _score_bounds(score_scale: str) -> tuple[int, int]:
    try:
        return SCORE_SCALE_RANGES[score_scale]
    except KeyError as exc:
        raise ValueError(f"unknown score_scale: {score_scale!r}") from exc


def _score_scale_for_schema(schema_version: str) -> str:
    # The classifier has no score field; quality results use one canonical
    # role scale. Returning 0–4 keeps helper defaults uniform.
    return "0-4"


def _score_scale_from_items(items: list[Any]) -> str:
    scales = {
        str(item.get("score_scale") or "0-4") if isinstance(item, dict)
        else str(getattr(item, "score_scale", "0-4") or "0-4")
        for item in items
    }
    if len(scales) == 1:
        return next(iter(scales))
    return "0-4"


def _normalize_task_type(
    raw: dict[str, Any], allowed: set[str],
) -> tuple[TaskTypeResult | None, str]:
    """Normalize the bounded Judge classifier output."""

    task_type = str(raw.get("task_type") or "ambiguous").strip().lower()
    if task_type not in TASK_TYPES:
        return None, "task_type must be bugfix, refactor, greenfield or ambiguous"
    confidence = _confidence(raw.get("confidence"))
    refs = _string_list(raw.get("evidence_refs"))
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        return None, "task_type requires a non-empty rationale"
    if any(not _evidence_bound(ref, allowed) for ref in refs):
        status = "invalid"
    elif not refs:
        status = "evidence_missing"
    elif task_type == "ambiguous":
        status = "ambiguous"
    elif confidence < _CONFIDENCE_FLOOR:
        status = "abstain"
        task_type = "ambiguous"
    else:
        status = "scored"
    missing = _string_list(raw.get("missing_evidence") or raw.get("missing_evidence_keys"))
    if missing and status == "scored":
        status = "evidence_missing"
    return TaskTypeResult(
        schema_version=str(raw["schema_version"]),
        judge_type="task_type",
        status=status,
        task_type=task_type,
        confidence=confidence,
        evidence_refs=refs,
        rationale=rationale,
        alternative=str(raw.get("alternative") or "").strip(),
        missing_evidence=missing,
        **{key: value for key, value in _result_metadata(raw).items() if key != "task_type"},
    ), ""


__all__ = [
    "DISCUSSION_DIMENSIONS",
    "NECESSITY_VOCAB",
    "NECESSITY_ALLOWED",
    "PRD_FIDELITY_DIMENSIONS",
    "ROLE_SCHEMA_VERSIONS",
    "TASK_TYPES",
    "SCHEMA_VERSIONS",
    "discussion_score",
    "mean_scores",
    "normalize_judge_output",
    "prd_fidelity_score",
]
