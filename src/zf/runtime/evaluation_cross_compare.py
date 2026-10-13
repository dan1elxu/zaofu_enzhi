"""Cross-lane comparison judge: schema normalization and deterministic rulings.

The ``cross_compare`` stage (three-lane PRD eval) is the only judge that reads
several de-identified PRDs at once. The agent produces semantic pairwise
calls; this module owns the mechanical contract: matrix completeness,
decisiveness, pairwise/ranking consistency, mandatory two-sided difference
attribution, blind-score conflict recording, and the vote-based recommendation
rule. Shape violations return an error (bounded schema retry upstream);
unbound evidence references produce a terminal ``invalid`` result, matching
the channel evaluation's item-level rule.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from zf.runtime.evaluation_types import (
    LANE_PRD_FIDELITY_DIMENSIONS,
    CrossCompareResult,
    DifferenceAttribution,
    DimensionRanking,
    PairwiseComparison,
    RankingConflict,
)

CROSS_COMPARE_SCHEMA_VERSION = "cross_compare_result.v1"
SIMILAR = "similar"
MARGIN_VALUES = frozenset({"large", "medium", "small", "none"})
ATTRIBUTION_CATEGORIES = frozenset({
    "coverage_gap",
    "scope_drift",
    "acceptance_missing",
    "ambiguity",
})
CANDIDATE_PRD_REF_PREFIX = "eval/prd/"


def pair_label(first: str, second: str) -> str:
    return f"{first}-{second}"


def expected_pairs(candidates: list[str]) -> list[str]:
    return [pair_label(a, b) for a, b in combinations(candidates, 2)]


def candidate_prd_ref(label: str) -> str:
    return f"{CANDIDATE_PRD_REF_PREFIX}{label}"


def blind_mean_scores(
    blind_scores: dict[str, dict[str, Any]],
) -> dict[str, float | None]:
    """Per-candidate mean over the four blind dimensions; None when incomplete."""

    means: dict[str, float | None] = {}
    for label, dims in blind_scores.items():
        values = [dims.get(dimension) for dimension in LANE_PRD_FIDELITY_DIMENSIONS]
        if any(type(value) not in {int, float} for value in values):
            means[label] = None
        else:
            means[label] = round(sum(values) / len(values), 2)  # type: ignore[arg-type]
    return means


def blind_favorites(blind_scores: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Unique blind-score winner per dimension; ties or gaps yield no favorite."""

    favorites: dict[str, str] = {}
    for dimension in LANE_PRD_FIDELITY_DIMENSIONS:
        scored = {
            label: dims[dimension]
            for label, dims in blind_scores.items()
            if type(dims.get(dimension)) in {int, float}
        }
        if not scored:
            continue
        best = max(scored.values())
        winners = [label for label, value in scored.items() if value == best]
        if len(winners) == 1:
            favorites[dimension] = winners[0]
    return favorites


def expected_vote_ranking(
    candidates: list[str],
    dimension_rankings: list[DimensionRanking],
    blind_scores: dict[str, dict[str, Any]],
) -> list[str]:
    """Deterministic recommendation order: dimension wins, blind mean, label."""

    votes = {label: 0 for label in candidates}
    for entry in dimension_rankings:
        if entry.ranking:
            votes[entry.ranking[0]] += 1
    means = blind_mean_scores(blind_scores)
    return sorted(
        candidates,
        key=lambda label: (
            -votes[label],
            -(means[label] if means[label] is not None else -1.0),
            label,
        ),
    )


def normalize_cross_compare_output(
    raw: dict[str, Any],
    *,
    allowed_evidence_refs: list[str],
    expected_candidates: list[str],
    blind_scores: dict[str, dict[str, Any]],
) -> tuple[CrossCompareResult | None, str]:
    """Validate one cross_compare JSON object; (result, "") or (None, error)."""

    candidates = [str(label).strip() for label in expected_candidates if str(label).strip()]
    if len(candidates) < 2 or len(set(candidates)) != len(candidates):
        return None, "cross_compare requires at least two distinct expected candidates"
    declared_candidates = raw.get("candidates")
    if (
        not isinstance(declared_candidates, list)
        or any(not isinstance(label, str) or not label.strip() for label in declared_candidates)
        or [label.strip() for label in declared_candidates] != candidates
    ):
        return None, f"candidates must be exactly {candidates} in the supplied order"

    try:
        rankings, error = _normalize_rankings(raw.get("dimension_rankings"), candidates)
        if rankings is None:
            return None, error
        pairwise, error = _normalize_pairwise(raw.get("pairwise"), candidates)
        if pairwise is None:
            return None, error
        consistency_error = _ranking_pairwise_consistency(rankings, pairwise)
        if consistency_error:
            return None, consistency_error
        attributions, error = _normalize_attributions(
            raw.get("attributions"), candidates, pairwise,
        )
        if attributions is None:
            return None, error
        conflicts, error = _normalize_conflicts(
            raw.get("conflicts"), rankings, blind_scores,
        )
        if conflicts is None:
            return None, error
        recommendation, error = _normalize_recommendation(
            raw.get("recommendation"), candidates, rankings, blind_scores,
        )
        if recommendation is None:
            return None, error
    except ValueError as exc:
        return None, str(exc)
    declared_status = str(raw.get("status") or "scored").strip()
    if declared_status not in {"scored", "evidence_missing"}:
        return None, "cross_compare status must be scored or evidence_missing"
    try:
        missing = _strict_string_list(raw.get("missing_evidence") or [])
    except ValueError as exc:
        return None, str(exc)
    if declared_status == "evidence_missing" and not missing:
        return None, "declared evidence_missing requires non-empty missing_evidence"

    allowed = {str(ref).strip() for ref in allowed_evidence_refs if str(ref).strip()}
    bound_entries = (
        list(rankings) + list(pairwise) + list(attributions) + list(conflicts)
    )
    key_gaps = recommendation.get("key_gaps") or []
    unbound = any(
        ref not in allowed
        for entry in bound_entries
        for ref in entry.evidence_refs
    ) or any(
        str(ref).strip() not in allowed
        for gap in key_gaps
        for ref in (gap.get("evidence_refs") or [])
    )
    if unbound:
        status = "invalid"
    elif missing:
        status = "evidence_missing"
    else:
        status = "scored"
    return CrossCompareResult(
        schema_version=str(raw.get("schema_version") or CROSS_COMPARE_SCHEMA_VERSION),
        judge_type="cross_compare",
        status=status,
        run_id=str(raw.get("run_id") or ""),
        source_snapshot_digest=str(raw.get("source_snapshot_digest") or ""),
        candidates=candidates,
        dimension_rankings=rankings,
        pairwise=pairwise,
        attributions=attributions,
        conflicts=conflicts,
        recommendation=recommendation,
        missing_evidence=missing,
        task_type=str(raw.get("task_type") or "").strip(),
    ), ""


def _normalize_rankings(
    value: Any, candidates: list[str],
) -> tuple[list[DimensionRanking] | None, str]:
    entries = value if isinstance(value, list) else None
    if not entries:
        return None, "dimension_rankings must be a non-empty list"
    rankings: list[DimensionRanking] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            return None, "every dimension_rankings entry must be an object"
        dimension_id = str(item.get("dimension_id") or "").strip()
        if dimension_id not in LANE_PRD_FIDELITY_DIMENSIONS or dimension_id in seen:
            return None, (
                "dimension_rankings must cover "
                f"{list(LANE_PRD_FIDELITY_DIMENSIONS)} exactly once"
            )
        seen.add(dimension_id)
        ranking = item.get("ranking")
        if (
            not isinstance(ranking, list)
            or any(not isinstance(label, str) or not label.strip() for label in ranking)
            or sorted(label.strip() for label in ranking) != sorted(candidates)
        ):
            return None, f"ranking for {dimension_id} must be a permutation of {candidates}"
        confidence = _confidence_0_4(item.get("confidence"))
        rationale = str(item.get("rationale") or "").strip()
        if not rationale:
            return None, f"ranking for {dimension_id} requires a non-empty rationale"
        refs = _evidence_list(item.get("evidence_refs"), f"ranking {dimension_id}")
        if refs is None:
            return None, f"ranking {dimension_id} requires non-empty evidence_refs"
        rankings.append(DimensionRanking(
            dimension_id=dimension_id,
            ranking=[label.strip() for label in ranking],
            confidence=confidence,
            evidence_refs=refs,
            rationale=rationale,
        ))
    if seen != set(LANE_PRD_FIDELITY_DIMENSIONS):
        return None, (
            "dimension_rankings must cover "
            f"{list(LANE_PRD_FIDELITY_DIMENSIONS)} exactly once"
        )
    return rankings, ""


def _normalize_pairwise(
    value: Any, candidates: list[str],
) -> tuple[list[PairwiseComparison] | None, str]:
    entries = value if isinstance(value, list) else None
    if not entries:
        return None, "pairwise must be a non-empty list"
    expected = {
        (pair, dimension)
        for pair in expected_pairs(candidates)
        for dimension in LANE_PRD_FIDELITY_DIMENSIONS
    }
    pairwise: list[PairwiseComparison] = []
    seen: set[tuple[str, str]] = set()
    for item in entries:
        if not isinstance(item, dict):
            return None, "every pairwise entry must be an object"
        pair = str(item.get("pair") or "").strip()
        dimension_id = str(item.get("dimension_id") or "").strip()
        if (pair, dimension_id) not in expected or (pair, dimension_id) in seen:
            return None, (
                "pairwise must cover every pair x dimension exactly once; "
                f"unexpected or duplicate entry: {pair!r}/{dimension_id!r}"
            )
        seen.add((pair, dimension_id))
        members = pair.split("-", 1)
        winner = str(item.get("winner") or "").strip()
        if winner != SIMILAR and winner not in members:
            return None, f"winner of {pair} must be one of {members} or {SIMILAR!r}"
        margin = str(item.get("margin") or "").strip()
        if margin not in MARGIN_VALUES:
            return None, f"margin of {pair} must be one of {sorted(MARGIN_VALUES)}"
        if (winner == SIMILAR) != (margin == "none"):
            return None, f"{pair}: winner {SIMILAR!r} requires margin 'none' and vice versa"
        confidence = _confidence_0_4(item.get("confidence"))
        if confidence == 0 and winner != SIMILAR:
            return None, f"{pair}: confidence 0 (cannot compare) requires winner {SIMILAR!r}"
        rationale = str(item.get("rationale") or "").strip()
        if not rationale:
            return None, f"pairwise {pair}/{dimension_id} requires a non-empty rationale"
        refs = _evidence_list(item.get("evidence_refs"), f"pairwise {pair}/{dimension_id}")
        if refs is None:
            return None, f"pairwise {pair}/{dimension_id} requires non-empty evidence_refs"
        pairwise.append(PairwiseComparison(
            pair=pair,
            dimension_id=dimension_id,
            winner=winner,
            margin=margin,
            confidence=confidence,
            evidence_refs=refs,
            rationale=rationale,
        ))
    if seen != expected:
        missing = sorted(expected - seen)
        return None, f"pairwise matrix is incomplete; missing: {missing}"
    return pairwise, ""


def _ranking_pairwise_consistency(
    rankings: list[DimensionRanking], pairwise: list[PairwiseComparison],
) -> str:
    by_dimension = {entry.dimension_id: entry.ranking for entry in rankings}
    for entry in pairwise:
        if entry.winner == SIMILAR:
            continue
        loser = next(label for label in entry.pair.split("-", 1) if label != entry.winner)
        ranking = by_dimension[entry.dimension_id]
        if ranking.index(entry.winner) > ranking.index(loser):
            return (
                f"pairwise {entry.pair}/{entry.dimension_id} winner {entry.winner} "
                "contradicts the dimension ranking"
            )
    return ""


def _normalize_attributions(
    value: Any,
    candidates: list[str],
    pairwise: list[PairwiseComparison],
) -> tuple[list[DifferenceAttribution] | None, str]:
    entries = value if isinstance(value, list) else []
    attributions: list[DifferenceAttribution] = []
    covered: set[tuple[str, str]] = set()
    valid_pairs = set(expected_pairs(candidates))
    for item in entries:
        if not isinstance(item, dict):
            return None, "every attribution entry must be an object"
        dimension_id = str(item.get("dimension_id") or "").strip()
        pair = str(item.get("pair") or "").strip()
        if dimension_id not in LANE_PRD_FIDELITY_DIMENSIONS or pair not in valid_pairs:
            return None, f"attribution has unknown dimension/pair: {dimension_id!r}/{pair!r}"
        category = str(item.get("category") or "").strip()
        if category not in ATTRIBUTION_CATEGORIES:
            return None, f"attribution category must be one of {sorted(ATTRIBUTION_CATEGORIES)}"
        summary = str(item.get("summary") or "").strip()
        if not summary:
            return None, f"attribution {pair}/{dimension_id} requires a non-empty summary"
        refs = _evidence_list(item.get("evidence_refs"), f"attribution {pair}/{dimension_id}")
        if refs is None:
            return None, f"attribution {pair}/{dimension_id} requires non-empty evidence_refs"
        required_refs = {candidate_prd_ref(label) for label in pair.split("-", 1)}
        if not required_refs.issubset(refs):
            return None, (
                f"attribution {pair}/{dimension_id} must cite both PRDs: "
                f"{sorted(required_refs)}"
            )
        attributions.append(DifferenceAttribution(
            dimension_id=dimension_id,
            pair=pair,
            category=category,
            summary=summary,
            evidence_refs=refs,
        ))
        covered.add((pair, dimension_id))
    decided = {
        (entry.pair, entry.dimension_id)
        for entry in pairwise
        if entry.winner != SIMILAR
    }
    missing = sorted(decided - covered)
    if missing:
        return None, f"decided pairs require difference attribution; missing: {missing}"
    return attributions, ""


def _normalize_conflicts(
    value: Any,
    rankings: list[DimensionRanking],
    blind_scores: dict[str, dict[str, Any]],
) -> tuple[list[RankingConflict] | None, str]:
    entries = value if isinstance(value, list) else []
    favorites = blind_favorites(blind_scores)
    tops = {entry.dimension_id: entry.ranking[0] for entry in rankings if entry.ranking}
    expected = {
        dimension
        for dimension, favorite in favorites.items()
        if tops.get(dimension) and tops[dimension] != favorite
    }
    conflicts: list[RankingConflict] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            return None, "every conflict entry must be an object"
        dimension_id = str(item.get("dimension_id") or "").strip()
        if dimension_id not in expected or dimension_id in seen:
            return None, (
                "conflicts must exactly cover dimensions where the ranking top "
                f"contradicts a unique blind favorite; unexpected: {dimension_id!r}"
            )
        seen.add(dimension_id)
        blind_favorite = str(item.get("blind_favorite") or "").strip()
        pairwise_favorite = str(item.get("pairwise_favorite") or "").strip()
        if blind_favorite != favorites[dimension_id]:
            return None, (
                f"conflict {dimension_id} blind_favorite must be "
                f"{favorites[dimension_id]!r} per the packet blind scores"
            )
        if pairwise_favorite != tops[dimension_id]:
            return None, (
                f"conflict {dimension_id} pairwise_favorite must be "
                f"{tops[dimension_id]!r} per the dimension ranking"
            )
        resolution = str(item.get("resolution") or "").strip()
        if not resolution:
            return None, f"conflict {dimension_id} requires a non-empty resolution"
        refs = _evidence_list(item.get("evidence_refs"), f"conflict {dimension_id}")
        if refs is None:
            return None, f"conflict {dimension_id} requires non-empty evidence_refs"
        conflicts.append(RankingConflict(
            dimension_id=dimension_id,
            blind_favorite=blind_favorite,
            pairwise_favorite=pairwise_favorite,
            resolution=resolution,
            evidence_refs=refs,
        ))
    missing = sorted(expected - seen)
    if missing:
        return None, (
            "dimension ranking contradicts the blind scores without a recorded "
            f"conflict arbitration: {missing}"
        )
    return conflicts, ""


def _normalize_recommendation(
    value: Any,
    candidates: list[str],
    rankings: list[DimensionRanking],
    blind_scores: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(value, dict):
        return None, "recommendation must be an object"
    ranking = value.get("ranking")
    if (
        not isinstance(ranking, list)
        or any(not isinstance(label, str) or not label.strip() for label in ranking)
        or sorted(label.strip() for label in ranking) != sorted(candidates)
    ):
        return None, f"recommendation.ranking must be a permutation of {candidates}"
    ranking = [label.strip() for label in ranking]
    top = str(value.get("top") or "").strip()
    if top != ranking[0]:
        return None, "recommendation.top must equal the first ranked candidate"
    confidence = _confidence_0_4(value.get("confidence"))
    rationale = str(value.get("rationale") or "").strip()
    if not rationale:
        return None, "recommendation requires a non-empty rationale"
    expected = expected_vote_ranking(candidates, rankings, blind_scores)
    divergence = str(value.get("divergence_explanation") or "").strip()
    if ranking != expected and not divergence:
        return None, (
            "recommendation diverges from the dimension-vote order "
            f"{expected}; divergence_explanation is required"
        )
    key_gaps_raw = value.get("key_gaps")
    if not isinstance(key_gaps_raw, list):
        return None, "recommendation.key_gaps must be a list"
    key_gaps: list[dict[str, Any]] = []
    gap_candidates: set[str] = set()
    for item in key_gaps_raw:
        if not isinstance(item, dict):
            return None, "every key_gaps entry must be an object"
        candidate = str(item.get("candidate") or "").strip()
        if candidate not in candidates or candidate == top:
            return None, f"key_gaps candidate must be a non-top candidate: {candidate!r}"
        gap = str(item.get("gap") or "").strip()
        if not gap:
            return None, f"key_gaps entry for {candidate} requires a non-empty gap"
        refs = _evidence_list(item.get("evidence_refs"), f"key_gaps {candidate}")
        if refs is None:
            return None, f"key_gaps entry for {candidate} requires non-empty evidence_refs"
        key_gaps.append({"candidate": candidate, "gap": gap, "evidence_refs": refs})
        gap_candidates.add(candidate)
    missing_gaps = sorted(set(candidates) - {top} - gap_candidates)
    if missing_gaps:
        return None, f"recommendation must list key gaps for every non-top candidate: {missing_gaps}"
    return {
        "ranking": ranking,
        "top": top,
        "confidence": confidence,
        "rationale": rationale,
        "divergence_explanation": divergence,
        "key_gaps": key_gaps,
    }, ""


def _confidence_0_4(value: Any) -> float:
    if type(value) not in {int, float} or not float(value).is_integer() or not 0 <= value <= 4:
        raise ValueError("confidence must be an integer from 0 to 4")
    return float(value)


def _evidence_list(value: Any, label: str) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(ref, str) or not ref.strip() for ref in value):
        return None
    return [ref.strip() for ref in value]


def _strict_string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError("expected a list of non-empty strings")
    return [item.strip() for item in value]


__all__ = [
    "ATTRIBUTION_CATEGORIES",
    "CANDIDATE_PRD_REF_PREFIX",
    "CROSS_COMPARE_SCHEMA_VERSION",
    "MARGIN_VALUES",
    "SIMILAR",
    "blind_favorites",
    "blind_mean_scores",
    "candidate_prd_ref",
    "expected_pairs",
    "expected_vote_ranking",
    "normalize_cross_compare_output",
    "pair_label",
]
