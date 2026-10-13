"""Immutable evaluation input packets (PRD §5, §8.2, §8.3).

Every judge reads an independent, never-overwritten packet under
``artifacts/benchmark-evaluations/inputs/<digest>.json``. Identity is
``(workflow_run_id, trial_id, judge_type, source_snapshot_digest,
prompt_version)``; two requests with the same identity are the same request,
so observer restarts and duplicate scans never re-submit a completed judge.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_types import EvaluationPacket

PACKET_SCHEMA_VERSION = "zaofu-prd-evaluation-input.v1"
EVALUATION_ARTIFACT_ROOT = Path("artifacts") / "benchmark-evaluations"

MESSAGE_EVIDENCE_PREFIX = "channel/message/"


def stable_digest(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluation_run_id(
    *, channel_id: str, thread_id: str, prd_revision: int,
    occurrence_id: str = "",
) -> str:
    """Deterministic run identity for one frozen PRD occurrence.

    ``channel_id/thread_id/prd_revision`` identifies the logical PRD, while
    ``occurrence_id`` identifies the immutable ``channel.prd.frozen`` event
    that handed it to evaluation.  The suffix is optional for compatibility
    with callers that only have the logical identity; the observer always
    supplies the freeze event id so repeated freezes of one revision cannot
    share report/result directories or scheduler request identities.
    """

    safe_channel = _safe_segment(channel_id)
    safe_thread = _safe_segment(thread_id)
    run_id = f"channel-{safe_channel}-{safe_thread}-r{int(prd_revision)}"
    if str(occurrence_id or "").strip():
        run_id += f"-f{_safe_segment(str(occurrence_id).strip())}"
    return run_id


def evaluation_request_id(
    *,
    workflow_run_id: str,
    trial_id: str,
    judge_type: str,
    source_snapshot_digest: str,
    prompt_version: str = EVALUATION_PROMPT_VERSION,
) -> str:
    """PRD §8.2 identity: same tuple -> same request, forever."""

    digest = hashlib.sha256(
        json.dumps(
            {
                "workflow_run_id": workflow_run_id,
                "trial_id": trial_id,
                "judge_type": judge_type,
                "source_snapshot_digest": source_snapshot_digest,
                "prompt_version": prompt_version,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"evalreq-{digest[:24]}"


def message_evidence_ref(event_id: str) -> str:
    return f"{MESSAGE_EVIDENCE_PREFIX}{event_id}"


def build_packet(
    *,
    stage: str,
    run_id: str,
    workflow_run_id: str,
    trial_id: str,
    source_snapshot_digest: str,
    task: dict[str, Any],
    task_details: dict[str, Any] | None = None,
    agent_profiles: list[dict[str, Any]] | None = None,
    agent_relationships: list[dict[str, Any]] | None = None,
    iteration_summary: dict[str, Any] | None = None,
    channel_messages: list[dict[str, Any]] | None = None,
    channel_frozen_prd: dict[str, Any] | None = None,
    workflow_bound_prd: dict[str, Any] | None = None,
    prior_judges: list[dict[str, Any]] | None = None,
    forbidden_refs: list[str] | None = None,
    task_type: str = "",
    task_type_confidence: float | None = None,
    role_ref: str = "",
    role_digest: str = "",
    evaluator_profile: str = "",
    subagent_scope: str = "",
    prompt_version: str = "",
) -> EvaluationPacket:
    """Assemble one stage packet; allowed evidence refs are derived here.

    Every evidence key is digest-bound at packet build time: judges may only
    cite refs that exist in this packet (PRD §5 constraint 4), and unbound
    refs invalidate the citing item (§11).
    """

    messages = [item for item in (channel_messages or []) if isinstance(item, dict)]
    frozen = channel_frozen_prd if isinstance(channel_frozen_prd, dict) else {}
    bound = workflow_bound_prd if isinstance(workflow_bound_prd, dict) else {}

    allowed: list[str] = []
    for item in messages:
        ref = str(item.get("evidence_ref") or "").strip()
        if not ref and str(item.get("event_id") or "").strip():
            ref = message_evidence_ref(str(item["event_id"]).strip())
        if ref:
            allowed.append(ref)
    discussion_events = (task_details or {}).get("discussion_events") or []
    if isinstance(discussion_events, list):
        for item in discussion_events:
            if isinstance(item, dict):
                ref = str(item.get("evidence_ref") or "").strip()
                if ref:
                    allowed.append(ref)
    for ref, key in (
        (frozen.get("prd_ref"), "artifact/prd/channel-frozen.json"),
        (frozen.get("readiness_ref"), "artifact/prd/channel-readiness.json"),
        (frozen.get("source_snapshot_ref"), "artifact/channel/source-snapshot.json"),
        (bound.get("prd_ref"), "artifact/prd/workflow-bound.json"),
        (bound.get("plan_ref"), "artifact/prd/workflow-plan.json"),
    ):
        if str(ref or "").strip():
            allowed.append(str(key))
            if key == "artifact/channel/source-snapshot.json":
                # Keep the canonical logical key while also allowing the
                # exact frozen source ref used by a producer-side manifest.
                allowed.append(str(ref))
    if isinstance(frozen.get("source_snapshot"), dict) and frozen.get("source_snapshot"):
        allowed.append("artifact/channel/source-snapshot.json")
    for item in (task_details or {}).get("evidence_keys") or []:
        ref = str(item or "").strip()
        if ref:
            allowed.append(ref)

    # Keep metadata available to the typed packet model while retaining it in
    # task_details for audit readers that do not know the optional fields.
    packet_fields = {item.name for item in fields(EvaluationPacket)}
    metadata = {
        "task_type": str(task_type or ""),
        "task_type_confidence": task_type_confidence,
        "role_ref": str(role_ref or ""),
        "role_digest": str(role_digest or ""),
        "evaluator_profile": str(evaluator_profile or ""),
        "subagent_scope": str(subagent_scope or ""),
    }
    details = dict(task_details or {})
    if any(value not in ("", None) for value in metadata.values()):
        details.setdefault("evaluation_metadata", {}).update(metadata)
    kwargs: dict[str, Any] = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "run_id": run_id,
        "workflow_run_id": workflow_run_id,
        "trial_id": trial_id,
        "stage": stage,
        "source_snapshot_digest": source_snapshot_digest,
        "prompt_version": str(prompt_version or "") or EVALUATION_PROMPT_VERSION,
        "task": task,
        "task_details": details,
        "agent_profiles": [item for item in (agent_profiles or []) if isinstance(item, dict)],
        "agent_relationships": [
            item for item in (agent_relationships or []) if isinstance(item, dict)
        ],
        "iteration_summary": iteration_summary or {},
        "channel_messages": messages,
        "channel_frozen_prd": frozen,
        "workflow_bound_prd": bound,
        "prior_judges": [item for item in (prior_judges or []) if isinstance(item, dict)],
        "allowed_evidence_refs": list(dict.fromkeys(allowed)),
        "forbidden_refs": [str(ref) for ref in (forbidden_refs or [])],
    }
    for key, value in metadata.items():
        if key in packet_fields:
            kwargs[key] = value
    return EvaluationPacket(**kwargs)


def write_immutable_packet(
    state_dir: Path,
    packet: EvaluationPacket,
) -> tuple[Path, str]:
    """Persist the packet once; existing files are never overwritten."""

    packet_dict = packet.to_dict()
    digest = stable_digest(packet_dict)
    path = Path(state_dir) / EVALUATION_ARTIFACT_ROOT / "inputs" / f"{digest}.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, _canonical(packet_dict))
    return path, digest


def read_packet(state_dir: Path, digest: str) -> EvaluationPacket | None:
    path = Path(state_dir) / EVALUATION_ARTIFACT_ROOT / "inputs" / f"{digest}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return EvaluationPacket.from_dict(payload)


def packet_for_prompt(packet: EvaluationPacket | dict[str, Any]) -> dict[str, Any]:
    """Project a packet into the prompt-visible view (bodies inline)."""

    if isinstance(packet, EvaluationPacket):
        payload = packet.to_dict()
        metadata = ((payload.get("task_details") or {}).get("evaluation_metadata")
                    if isinstance(payload.get("task_details"), dict) else {})
        if isinstance(metadata, dict):
            for key in ("task_type", "task_type_confidence", "role_ref", "role_digest",
                        "evaluator_profile", "subagent_scope"):
                if not payload.get(key) and metadata.get(key) not in (None, ""):
                    payload[key] = metadata[key]
        return payload
    details = packet.get("task_details") or {}
    metadata = details.get("evaluation_metadata") if isinstance(details, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    value = lambda key: packet.get(key) or metadata.get(key)
    return {
        "run_id": packet.get("run_id"),
        "workflow_run_id": packet.get("workflow_run_id"),
        "trial_id": packet.get("trial_id"),
        "stage": packet.get("stage"),
        "task": packet.get("task"),
        "task_details": packet.get("task_details"),
        "agent_profiles": packet.get("agent_profiles"),
        "agent_relationships": packet.get("agent_relationships"),
        "iteration_summary": packet.get("iteration_summary"),
        "channel_messages": packet.get("channel_messages"),
        "channel_frozen_prd": packet.get("channel_frozen_prd"),
        "workflow_bound_prd": packet.get("workflow_bound_prd"),
        "prior_judges": packet.get("prior_judges"),
        "allowed_evidence_refs": packet.get("allowed_evidence_refs"),
        "forbidden_refs": packet.get("forbidden_refs"),
        "task_type": value("task_type"),
        "task_type_confidence": value("task_type_confidence"),
        "role_ref": value("role_ref"),
        "role_digest": value("role_digest"),
        "evaluator_profile": value("evaluator_profile"),
        "subagent_scope": value("subagent_scope"),
    }


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ) + "\n"


def _safe_segment(value: str) -> str:
    """Encode one identity segment without lossy path sanitization.

    Replacing every separator with a hyphen made a/b and a-b address the
    same report directory. Preserve familiar spelling for safe ASCII
    identifiers and percent-encode every other UTF-8 byte (including percent)
    so the mapping is injective while remaining a portable path segment.
    """

    raw = str(value)
    if not raw:
        return "unknown"
    encoded: list[str] = []
    for byte in raw.encode("utf-8"):
        char = chr(byte)
        if byte < 128 and (char.isalnum() or char in "._-"):
            encoded.append(char)
        else:
            encoded.append(f"%{byte:02X}")
    safe = "".join(encoded)
    if safe in {".", ".."}:
        return "".join(f"%{byte:02X}" for byte in raw.encode("utf-8"))
    return safe or "unknown"


__all__ = [
    "EVALUATION_ARTIFACT_ROOT",
    "MESSAGE_EVIDENCE_PREFIX",
    "PACKET_SCHEMA_VERSION",
    "build_packet",
    "evaluation_request_id",
    "evaluation_run_id",
    "message_evidence_ref",
    "packet_for_prompt",
    "read_packet",
    "stable_digest",
    "write_immutable_packet",
]
