"""Typed evaluation data models.

These frozen dataclasses replace bare ``dict[str, Any]`` for the structured
judge results. They serialize to the same JSON shape used by the sidecars and
events, so existing artifact readers remain compatible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
            necessity=str(value.get("necessity") or ""),
            role_summary=str(value.get("role_summary") or ""),
            evidence_refs=_string_list(value.get("evidence_refs")),
            rationale=str(value.get("rationale") or ""),
            confidence=_float_or_zero(value.get("confidence")),
            status=str(value.get("status") or ""),
        )


@dataclass(frozen=True)
class MilestoneResult:
    schema_version: str
    judge_type: str = "milestone"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    agent_findings: list[AgentFinding] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MilestoneResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "milestone"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            agent_findings=[
                AgentFinding.from_dict(item)
                for item in value.get("agent_findings") or []
                if isinstance(item, dict)
            ],
            missing_evidence=_string_list(value.get("missing_evidence")),
        )


@dataclass(frozen=True)
class CommunicationResult:
    schema_version: str
    judge_type: str = "communication"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    dimensions: list[DimensionScore] = field(default_factory=list)
    human_intervention_count: int = 0
    communication_score: float | None = None
    missing_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CommunicationResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "communication"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            dimensions=[
                DimensionScore.from_dict(item)
                for item in value.get("dimensions") or []
                if isinstance(item, dict)
            ],
            human_intervention_count=int(value.get("human_intervention_count") or 0),
            communication_score=_float_or_none(value.get("communication_score")),
            missing_evidence=_string_list(value.get("missing_evidence")),
        )


@dataclass(frozen=True)
class PrdResult:
    schema_version: str
    judge_type: str = "prd_result"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    dimensions: list[DimensionScore] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    prd_result_score: float | None = None
    missing_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PrdResult":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            judge_type=str(value.get("judge_type") or "prd_result"),
            status=str(value.get("status") or ""),
            run_id=str(value.get("run_id") or ""),
            source_snapshot_digest=str(value.get("source_snapshot_digest") or ""),
            dimensions=[
                DimensionScore.from_dict(item)
                for item in value.get("dimensions") or []
                if isinstance(item, dict)
            ],
            differences=_string_list(value.get("differences")),
            prd_result_score=_float_or_none(value.get("prd_result_score")),
            missing_evidence=_string_list(value.get("missing_evidence")),
        )


@dataclass(frozen=True)
class ConclusionResult:
    schema_version: str
    judge_type: str = "conclusion"
    status: str = ""
    run_id: str = ""
    source_snapshot_digest: str = ""
    communication_score: float | None = None
    prd_result_score: float | None = None
    overall_score: float | None = None
    agent_scores: list[dict[str, Any]] = field(default_factory=list)
    team_findings: dict[str, Any] = field(default_factory=dict)
    conclusion_zh: str = ""
    next_actions: list[str] = field(default_factory=list)
    judge_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

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
            communication_score=_float_or_none(value.get("communication_score")),
            prd_result_score=_float_or_none(value.get("prd_result_score")),
            overall_score=_float_or_none(value.get("overall_score")),
            agent_scores=_any_list(value.get("agent_scores")),
            team_findings=value.get("team_findings") or {},
            conclusion_zh=str(value.get("conclusion_zh") or ""),
            next_actions=_string_list(value.get("next_actions")),
            judge_refs=_string_list(value.get("judge_refs")),
            evidence_refs=_string_list(value.get("evidence_refs")),
        )


JudgeResult = MilestoneResult | CommunicationResult | PrdResult | ConclusionResult


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


__all__ = [
    "AgentFinding",
    "CommunicationResult",
    "ConclusionResult",
    "DimensionScore",
    "EvaluationPacket",
    "JudgeRecord",
    "JudgeResult",
    "MilestoneResult",
    "PrdResult",
]
