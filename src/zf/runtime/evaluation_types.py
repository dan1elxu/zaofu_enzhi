"""Typed evaluation data models.

These frozen dataclasses replace bare ``dict[str, Any]`` for the structured
judge results. They serialize to the canonical sidecar and event JSON shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# ``must``/``optional``/``not_needed`` are the stable vocabulary exposed by
# the Channel/PRD evaluation contract.  A result that uses another label is
# invalid; keeping one vocabulary is necessary for comparable reports.
NECESSITY_VALUES = frozenset({"must", "optional", "not_needed"})
NECESSITY_UNDETERMINED = "undetermined"

# Blind per-lane PRD rubric (three-lane eval): the same four dimensions are
# scored per lane and reused as the cross-compare comparison axes.
LANE_PRD_FIDELITY_DIMENSIONS: tuple[str, ...] = (
    "coverage",
    "fidelity",
    "actionability",
    "structure",
)


def canonical_necessity(value: Any) -> str:
    """Return the stable necessity value for a judge-provided value.

    Unknown or empty values are returned unchanged (usually as an empty
    string). Validation of whether ``undetermined`` is appropriate for an
    item belongs to the schema normalizer, not this small data-model helper.
    """

    text = str(value or "").strip()
    return text


def necessity_label(value: Any) -> str:
    """Human-readable Chinese label used by the compact Markdown report."""

    labels = {
        "must": "必须",
        "optional": "可选",
        "not_needed": "不需要",
        "undetermined": "未确定",
    }
    canonical = canonical_necessity(value)
    return labels.get(canonical, canonical or "未确定")


@dataclass(frozen=True)
class DimensionScore:
    dimension_id: str
    score: float | None
    verdict: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    failure_explanation: str = ""
    rationale: str = ""
    confidence: float = 0.0
    status: str = ""
    score_scale: str = "0-4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_status(self, status: str) -> "DimensionScore":
        from dataclasses import replace

        return replace(self, status=status)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DimensionScore":
        return cls(
            dimension_id=str(value.get("dimension_id") or ""),
            score=_float_or_none(value.get("score")),
            verdict=str(value.get("verdict") or ""),
            evidence_refs=_string_list(value.get("evidence_refs")),
            failure_explanation=str(value.get("failure_explanation") or ""),
            rationale=str(value.get("rationale") or ""),
            confidence=_float_or_zero(value.get("confidence")),
            status=str(value.get("status") or ""),
            score_scale=str(value.get("score_scale") or "0-4"),
        )


@dataclass(frozen=True)
class AgentFinding:
    agent_id: str
    score: float | None
    necessity: str
    role_summary: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    rationale: str = ""
    confidence: float = 0.0
    status: str = ""
    necessity_status: str = "complete"
    score_scale: str = "0-4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_status(self, status: str) -> "AgentFinding":
        from dataclasses import replace

        return replace(self, status=status)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentFinding":
        return cls(
            agent_id=str(value.get("agent_id") or ""),
            score=_float_or_none(value.get("score")),
            necessity=canonical_necessity(value.get("necessity")),
            role_summary=str(value.get("role_summary") or ""),
            evidence_refs=_string_list(value.get("evidence_refs")),
            rationale=str(value.get("rationale") or ""),
            confidence=_float_or_zero(value.get("confidence")),
            status=str(value.get("status") or ""),
            necessity_status=str(value.get("necessity_status") or "complete"),
            score_scale=str(value.get("score_scale") or "0-4"),
        )


@dataclass(frozen=True)
class ContributionResult:
    schema_version: str
    judge_type: str = "contribution"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    agent_findings: list[AgentFinding] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    task_type: str = ""
    role_id: str = ""
    role_version: str = ""
    role_metadata: dict[str, Any] = field(default_factory=dict)
    score_scale: str = "0-4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContributionResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "contribution"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            agent_findings=[
                AgentFinding.from_dict(item)
                for item in value.get("agent_findings") or []
                if isinstance(item, dict)
            ],
            missing_evidence=_string_list(value.get("missing_evidence")),
            task_type=str(value.get("task_type") or ""),
            role_id=str(value.get("role_id") or ""),
            role_version=str(value.get("role_version") or ""),
            role_metadata=_dict_value(value.get("role_metadata")),
            score_scale=str(value.get("score_scale") or "0-4"),
        )


@dataclass(frozen=True)
class DiscussionResult:
    schema_version: str
    judge_type: str = "discussion"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    dimensions: list[DimensionScore] = field(default_factory=list)
    human_intervention_count: int = 0
    discussion_score: float | None = None
    discussion_meaningful: bool | None = None
    discussion_progressed: bool | None = None
    missing_evidence: list[str] = field(default_factory=list)
    task_type: str = ""
    role_id: str = ""
    role_version: str = ""
    role_metadata: dict[str, Any] = field(default_factory=dict)
    score_scale: str = "0-4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscussionResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "discussion"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            dimensions=[
                DimensionScore.from_dict(item)
                for item in value.get("dimensions") or []
                if isinstance(item, dict)
            ],
            human_intervention_count=int(value.get("human_intervention_count") or 0),
            discussion_score=_float_or_none(value.get("discussion_score")),
            discussion_meaningful=(
                value.get("discussion_meaningful")
                if type(value.get("discussion_meaningful")) is bool else None
            ),
            discussion_progressed=(
                value.get("discussion_progressed")
                if type(value.get("discussion_progressed")) is bool else None
            ),
            missing_evidence=_string_list(value.get("missing_evidence")),
            task_type=str(value.get("task_type") or ""),
            role_id=str(value.get("role_id") or ""),
            role_version=str(value.get("role_version") or ""),
            role_metadata=_dict_value(value.get("role_metadata")),
            score_scale=str(value.get("score_scale") or "0-4"),
        )


@dataclass(frozen=True)
class PrdFidelityResult:
    schema_version: str
    judge_type: str = "prd_fidelity"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    dimensions: list[DimensionScore] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    prd_fidelity_score: float | None = None
    prd_ready_for_next_stage: bool | None = None
    missing_evidence: list[str] = field(default_factory=list)
    task_type: str = ""
    role_id: str = ""
    role_version: str = ""
    role_metadata: dict[str, Any] = field(default_factory=dict)
    score_scale: str = "0-4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PrdFidelityResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "prd_fidelity"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            dimensions=[
                DimensionScore.from_dict(item)
                for item in value.get("dimensions") or []
                if isinstance(item, dict)
            ],
            differences=_string_list(value.get("differences")),
            prd_fidelity_score=_float_or_none(value.get("prd_fidelity_score")),
            prd_ready_for_next_stage=(
                value.get("prd_ready_for_next_stage")
                if type(value.get("prd_ready_for_next_stage")) is bool else None
            ),
            missing_evidence=_string_list(value.get("missing_evidence")),
            task_type=str(value.get("task_type") or ""),
            role_id=str(value.get("role_id") or ""),
            role_version=str(value.get("role_version") or ""),
            role_metadata=_dict_value(value.get("role_metadata")),
            score_scale=str(value.get("score_scale") or "0-4"),
        )


@dataclass(frozen=True)
class ConclusionResult:
    schema_version: str
    judge_type: str = "conclusion"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    agent_scores: list[dict[str, Any]] = field(default_factory=list)
    team_findings: dict[str, Any] = field(default_factory=dict)
    conclusion_zh: str = ""
    next_actions: list[str] = field(default_factory=list)
    judge_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    task_type: str = ""
    role_id: str = ""
    role_version: str = ""
    role_metadata: dict[str, Any] = field(default_factory=dict)
    score_scale: str = "0-4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConclusionResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "conclusion"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            agent_scores=_any_list(value.get("agent_scores")),
            team_findings=value.get("team_findings") or {},
            conclusion_zh=str(value.get("conclusion_zh") or ""),
            next_actions=_string_list(value.get("next_actions")),
            judge_refs=_string_list(value.get("judge_refs")),
            evidence_refs=_string_list(value.get("evidence_refs")),
            task_type=str(value.get("task_type") or ""),
            role_id=str(value.get("role_id") or ""),
            role_version=str(value.get("role_version") or ""),
            role_metadata=_dict_value(value.get("role_metadata")),
            score_scale=str(value.get("score_scale") or "0-4"),
        )


@dataclass(frozen=True)
class TaskTypeResult:
    """Bounded task classifier result used before role-specific judges run."""

    schema_version: str
    judge_type: str = "task_type"
    status: str = ""
    task_type: str = "ambiguous"
    confidence: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)
    rationale: str = ""
    alternative: str = ""
    missing_evidence: list[str] = field(default_factory=list)
    role_id: str = ""
    role_version: str = ""
    role_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskTypeResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "task_type"),
            status=str(value.get("status") or ""),
            task_type=str(value.get("task_type") or "ambiguous"),
            confidence=_float_or_zero(value.get("confidence")),
            evidence_refs=_string_list(value.get("evidence_refs")),
            rationale=str(value.get("rationale") or ""),
            alternative=str(value.get("alternative") or ""),
            missing_evidence=_string_list(value.get("missing_evidence")),
            role_id=str(value.get("role_id") or ""),
            role_version=str(value.get("role_version") or ""),
            role_metadata=_dict_value(value.get("role_metadata")),
        )


@dataclass(frozen=True)
class LanePrdFidelityResult:
    """Blind four-dimension score of one de-identified lane PRD."""

    schema_version: str
    judge_type: str = "lane_prd_fidelity"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    dimensions: list[DimensionScore] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    checklist: list[dict[str, Any]] = field(default_factory=list)
    concerns: list[dict[str, Any]] = field(default_factory=list)
    lane_prd_fidelity_score: float | None = None
    missing_evidence: list[str] = field(default_factory=list)
    task_type: str = ""
    role_id: str = ""
    role_version: str = ""
    role_metadata: dict[str, Any] = field(default_factory=dict)
    score_scale: str = "0-4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LanePrdFidelityResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "lane_prd_fidelity"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            dimensions=[
                DimensionScore.from_dict(item)
                for item in value.get("dimensions") or []
                if isinstance(item, dict)
            ],
            differences=_string_list(value.get("differences")),
            # ``checklist`` exists on v2+ and ``concerns`` on v3+ records;
            # older sidecars simply deserialize with empty lists.
            checklist=[
                dict(item)
                for item in value.get("checklist") or []
                if isinstance(item, dict)
            ],
            concerns=[
                dict(item)
                for item in value.get("concerns") or []
                if isinstance(item, dict)
            ],
            lane_prd_fidelity_score=_float_or_none(value.get("lane_prd_fidelity_score")),
            missing_evidence=_string_list(value.get("missing_evidence")),
            task_type=str(value.get("task_type") or ""),
            role_id=str(value.get("role_id") or ""),
            role_version=str(value.get("role_version") or ""),
            role_metadata=_dict_value(value.get("role_metadata")),
            score_scale=str(value.get("score_scale") or "0-4"),
        )


@dataclass(frozen=True)
class PairwiseComparison:
    pair: str
    dimension_id: str
    winner: str
    margin: str
    confidence: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PairwiseComparison":
        return cls(
            pair=str(value.get("pair") or ""),
            dimension_id=str(value.get("dimension_id") or ""),
            winner=str(value.get("winner") or ""),
            margin=str(value.get("margin") or ""),
            confidence=_float_or_zero(value.get("confidence")),
            evidence_refs=_string_list(value.get("evidence_refs")),
            rationale=str(value.get("rationale") or ""),
        )


@dataclass(frozen=True)
class DimensionRanking:
    dimension_id: str
    ranking: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DimensionRanking":
        return cls(
            dimension_id=str(value.get("dimension_id") or ""),
            ranking=_string_list(value.get("ranking")),
            confidence=_float_or_zero(value.get("confidence")),
            evidence_refs=_string_list(value.get("evidence_refs")),
            rationale=str(value.get("rationale") or ""),
        )


@dataclass(frozen=True)
class DifferenceAttribution:
    dimension_id: str
    pair: str
    category: str
    summary: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DifferenceAttribution":
        return cls(
            dimension_id=str(value.get("dimension_id") or ""),
            pair=str(value.get("pair") or ""),
            category=str(value.get("category") or ""),
            summary=str(value.get("summary") or ""),
            evidence_refs=_string_list(value.get("evidence_refs")),
        )


@dataclass(frozen=True)
class RankingConflict:
    dimension_id: str
    blind_favorite: str
    pairwise_favorite: str
    resolution: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RankingConflict":
        return cls(
            dimension_id=str(value.get("dimension_id") or ""),
            blind_favorite=str(value.get("blind_favorite") or ""),
            pairwise_favorite=str(value.get("pairwise_favorite") or ""),
            resolution=str(value.get("resolution") or ""),
            evidence_refs=_string_list(value.get("evidence_refs")),
        )


@dataclass(frozen=True)
class CrossCompareResult:
    """De-identified cross-lane comparison over frozen blind scores."""

    schema_version: str
    judge_type: str = "cross_compare"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    candidates: list[str] = field(default_factory=list)
    dimension_rankings: list[DimensionRanking] = field(default_factory=list)
    pairwise: list[PairwiseComparison] = field(default_factory=list)
    attributions: list[DifferenceAttribution] = field(default_factory=list)
    conflicts: list[RankingConflict] = field(default_factory=list)
    recommendation: dict[str, Any] = field(default_factory=dict)
    missing_evidence: list[str] = field(default_factory=list)
    task_type: str = ""
    score_scale: str = "0-4"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CrossCompareResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "cross_compare"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            candidates=_string_list(value.get("candidates")),
            dimension_rankings=[
                DimensionRanking.from_dict(item)
                for item in value.get("dimension_rankings") or []
                if isinstance(item, dict)
            ],
            pairwise=[
                PairwiseComparison.from_dict(item)
                for item in value.get("pairwise") or []
                if isinstance(item, dict)
            ],
            attributions=[
                DifferenceAttribution.from_dict(item)
                for item in value.get("attributions") or []
                if isinstance(item, dict)
            ],
            conflicts=[
                RankingConflict.from_dict(item)
                for item in value.get("conflicts") or []
                if isinstance(item, dict)
            ],
            recommendation=_dict_value(value.get("recommendation")),
            missing_evidence=_string_list(value.get("missing_evidence")),
            task_type=str(value.get("task_type") or ""),
            score_scale=str(value.get("score_scale") or "0-4"),
        )


JudgeResult = (
    ContributionResult | DiscussionResult | PrdFidelityResult | ConclusionResult
    | TaskTypeResult | LanePrdFidelityResult | CrossCompareResult
)


@dataclass(frozen=True)
class EvaluationPacket:
    schema_version: str
    run_id: str
    workflow_run_id: str
    trial_id: str
    stage: str
    source_snapshot_digest: str
    prompt_version: str
    task: dict[str, Any]
    task_details: dict[str, Any] = field(default_factory=dict)
    agent_profiles: list[dict[str, Any]] = field(default_factory=list)
    agent_relationships: list[dict[str, Any]] = field(default_factory=list)
    iteration_summary: dict[str, Any] = field(default_factory=dict)
    channel_messages: list[dict[str, Any]] = field(default_factory=list)
    channel_frozen_prd: dict[str, Any] = field(default_factory=dict)
    workflow_bound_prd: dict[str, Any] = field(default_factory=dict)
    prior_judges: list[dict[str, Any]] = field(default_factory=list)
    allowed_evidence_refs: list[str] = field(default_factory=list)
    forbidden_refs: list[str] = field(default_factory=list)
    # Frozen evaluator-selection metadata.  These optional fields keep
    # pre-role packets readable while allowing the classifier and isolated
    # subagents to bind one immutable task type/role contract.
    task_type: str = ""
    task_type_confidence: float | None = None
    role_ref: str = ""
    role_digest: str = ""
    evaluator_profile: str = ""
    subagent_scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvaluationPacket":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            run_id=str(value.get("run_id") or ""),
            workflow_run_id=str(value.get("workflow_run_id") or ""),
            trial_id=str(value.get("trial_id") or ""),
            stage=str(value.get("stage") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            prompt_version=str(value.get("prompt_version") or ""),
            task=value.get("task") or {},
            task_details=value.get("task_details") or {},
            agent_profiles=_any_list(value.get("agent_profiles")),
            agent_relationships=_any_list(value.get("agent_relationships")),
            iteration_summary=value.get("iteration_summary") or {},
            channel_messages=_any_list(value.get("channel_messages")),
            channel_frozen_prd=value.get("channel_frozen_prd") or {},
            workflow_bound_prd=value.get("workflow_bound_prd") or {},
            prior_judges=_any_list(value.get("prior_judges")),
            allowed_evidence_refs=_string_list(value.get("allowed_evidence_refs")),
            forbidden_refs=_string_list(value.get("forbidden_refs")),
            task_type=str(value.get("task_type") or ""),
            task_type_confidence=_float_or_none(value.get("task_type_confidence")),
            role_ref=str(value.get("role_ref") or ""),
            role_digest=str(value.get("role_digest") or ""),
            evaluator_profile=str(value.get("evaluator_profile") or ""),
            subagent_scope=str(value.get("subagent_scope") or ""),
        )


@dataclass(frozen=True)
class JudgeRecord:
    schema_version: str
    judge_type: str
    status: str
    request_id: str
    run_id: str
    workflow_run_id: str
    trial_id: str
    source_snapshot_digest: str
    prompt_version: str
    prompt_digest: str
    provider: str
    model: str
    created_at: str
    error: str
    result: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    attempt: int = 1
    max_attempts: int = 2
    retry_of: str = ""
    retryable: bool = False
    inconclusive_after: int | None = None
    turn_id: str = ""
    transcript_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JudgeRecord":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or ""),
            status=str(value.get("status") or ""),
            request_id=str(value.get("request_id") or ""),
            run_id=str(value.get("run_id") or ""),
            workflow_run_id=str(value.get("workflow_run_id") or ""),
            trial_id=str(value.get("trial_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            prompt_version=str(value.get("prompt_version") or ""),
            prompt_digest=str(value.get("prompt_digest") or ""),
            provider=str(value.get("provider") or ""),
            model=str(value.get("model") or ""),
            created_at=str(value.get("created_at") or ""),
            error=str(value.get("error") or ""),
            result=value.get("result") or {},
            evidence_refs=_string_list(value.get("evidence_refs")),
            attempt=int(value.get("attempt") or 1),
            max_attempts=int(value.get("max_attempts") or 2),
            retry_of=str(value.get("retry_of") or ""),
            retryable=bool(value.get("retryable")),
            inconclusive_after=(
                int(value.get("inconclusive_after"))
                if value.get("inconclusive_after") is not None else None
            ),
            turn_id=str(value.get("turn_id") or ""),
            transcript_ref=str(value.get("transcript_ref") or ""),
        )


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _float_or_zero(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _any_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return list(value)


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "AgentFinding",
    "NECESSITY_UNDETERMINED",
    "NECESSITY_VALUES",
    "CrossCompareResult",
    "DifferenceAttribution",
    "DimensionRanking",
    "DimensionScore",
    "DiscussionResult",
    "ConclusionResult",
    "EvaluationPacket",
    "JudgeRecord",
    "JudgeResult",
    "LANE_PRD_FIDELITY_DIMENSIONS",
    "LanePrdFidelityResult",
    "PairwiseComparison",
    "RankingConflict",
    "ContributionResult",
    "canonical_necessity",
    "necessity_label",
    "PrdFidelityResult",
    "TaskTypeResult",
]
