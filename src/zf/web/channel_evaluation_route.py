"""Read-only, bounded Channel/PRD evaluation API."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from zf.core.events import EventLog
from zf.core.security.redaction import redact_obj
from zf.runtime.channel_projection import project_channel
from zf.runtime.evaluation_inputs import _freeze_points
from zf.runtime.evaluation_runner import (
    conclusion_result_path,
    judge_result_path,
    read_conclusion_result,
    read_judge_result,
    report_path,
)
from zf.runtime.evaluation_packets import stable_digest
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_report import render_report


SCHEMA_VERSION = "channel.evaluation.v1"
STAGES = ("task_type", "contribution", "discussion", "prd_fidelity", "conclusion")
STAGE_LABELS = {
    "task_type": "Task Type",
    "contribution": "Agent Contribution",
    "discussion": "Discussion Effectiveness",
    "prd_fidelity": "PRD Fidelity",
    "conclusion": "Conclusion",
}
REPORT_MAX_BYTES = 256 * 1024
_INTERNAL_PATH_RE = re.compile(
    r"(?<![\w/:])(?:(?:file://)?/(?:home|Users|tmp|private|var|root|mnt|opt|etc|usr|workspace)/"
    r"|[A-Za-z]:\\|artifacts/benchmark-evaluations/)[^\s`<>\"'，。；：)]+"
)


def build_channel_evaluation(state_dir: Path, channel_id: str) -> dict[str, Any] | None:
    """Return only the current public evaluation generation for one Channel."""

    channel = project_channel(state_dir, channel_id)
    if channel is None:
        return None
    public = channel.get("evaluation")
    if not isinstance(public, dict):
        return _empty_response(channel_id)
    for candidate in _completed_regrades(state_dir, channel_id, public):
        response = _build_generation(state_dir, channel_id, candidate)
        if response["report"]["available"]:
            response["evaluation_origin"] = "manual_regrade"
            return response
    return _build_generation(state_dir, channel_id, public)


def _completed_regrades(
    state_dir: Path, channel_id: str, current: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Newest completed replays of the exact current input, in ledger order."""
    from zf.runtime.evaluation_projection import EVALUATION_EVENTS, fold_evaluation, public_evaluation
    from zf.runtime.evaluation_regrade import _load_source_context, _validate_regrade_chain

    rows = {row["judge_type"]: row for row in current.get("stages", [])}
    freeze_id = str(rows.get("prd_fidelity", {}).get("freeze_event_id") or "")
    if not freeze_id or not current.get("workflow_run_id"):
        return
    events = [
        event for event in EventLog(Path(state_dir) / "events.jsonl").read_all()
        if event.type in EVALUATION_EVENTS
        and event.payload.get("evaluation_origin") == "manual_regrade"
        and event.payload.get("channel_id") == channel_id
        and event.payload.get("freeze_event_id") == freeze_id
    ]
    # First completion establishes report order; a duplicate late receipt
    # must not make an older replay current again.
    completed_runs = dict.fromkeys(
        str(event.payload.get("workflow_run_id") or "") for event in events
        if event.type == "benchmark.evaluation.conclusion.completed"
    )
    for run_id in reversed(completed_runs):
        if not run_id:
            continue
        audit_path = _safe_bound_path(state_dir, report_path(state_dir, run_id).with_name("regrade.json"))
        if audit_path is None:
            continue
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            # The terminal event/report may precede the final audit bookkeeping.
            # The verified full report below is the publication gate.
            if (not isinstance(audit, dict) or audit.get("status") not in {"running", "completed"}
                    or audit.get("workflow_run_id") != run_id):
                continue
            source_ref = str(audit.get("original_context_ref") or "")
            source, digest = _load_source_context(state_dir, source_ref)
            if source.get("channel_id") != channel_id or source.get("workflow_run_id") != current["workflow_run_id"]:
                continue
            # A later Workflow PRD binding changes fidelity's input identity.
            # Regrading an older binding must not replace that newer view.
            if any(
                packet.get(key) != rows.get(stage, {}).get(key)
                for stage, packet in source["packets"].items()
                for key in ("workflow_run_id", "trial_id", "source_snapshot_digest")
            ):
                continue
            clone = _validate_regrade_chain(
                state_dir, source=source, source_ref=source_ref, source_digest=digest,
                regrade_id=str(audit.get("regrade_id") or ""), audit=audit,
            )
            freeze = clone["freeze"]
            if freeze.get("event_id") != freeze_id:
                continue
            replay: dict[str, Any] = {"_evaluation_freeze_identities": {
                freeze_id: {**freeze, "seq": 1, "workflow_run_id": run_id},
            }}
            for seq, item in enumerate(events, 1):
                if item.payload.get("workflow_run_id") == run_id:
                    # Only this validated replay gets an isolated projection;
                    # the canonical Channel projection and ledger stay intact.
                    fold_evaluation(
                        replay, {**item.payload, "evaluation_origin": ""},
                        event_type=item.type, ts=item.ts, seq=seq,
                    )
            public = public_evaluation(replay)
            stages = {row["judge_type"]: row for row in public.get("stages", [])}
            if set(stages) != set(STAGES) or stages["conclusion"].get("status") not in {"complete", "incomplete"}:
                continue
            if any(
                stages[stage].get("status") not in {"scored", "evidence_missing", "abstain", "conflict", "ambiguous"}
                or any(stages[stage].get(key) != packet.get(key) for key in (
                    "workflow_run_id", "trial_id", "source_snapshot_digest",
                ))
                for stage, packet in clone["packets"].items()
            ):
                continue
        except (OSError, ValueError, TypeError, KeyError):
            continue
        yield public


def _build_generation(state_dir: Path, channel_id: str, public: dict[str, Any]) -> dict[str, Any]:
    workflow_run_id = str(public.get("workflow_run_id") or "")
    public_stages = public.get("stages")
    if not workflow_run_id or not isinstance(public_stages, list):
        return _empty_response(channel_id)
    rows = {
        str(row.get("judge_type") or ""): row
        for row in public_stages
        if isinstance(row, dict) and str(row.get("judge_type") or "") in STAGES
    }
    freeze_ids = {
        str(row.get("freeze_event_id") or "")
        for row in rows.values()
        if str(row.get("freeze_event_id") or "")
    }
    # A public generation is useful only when its rows agree on its immutable
    # freeze occurrence.  Do not join sidecars across a mixed projection.
    freeze_event_id = next(iter(freeze_ids)) if len(freeze_ids) == 1 else ""
    stages: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, Any]] = {}
    records: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        row = rows.get(stage)
        item = _stage_row(stage, row)
        record = _read_bound_record(
            state_dir, workflow_run_id=workflow_run_id,
            freeze_event_id=freeze_event_id, stage=stage, row=row,
        )
        if record is not None:
            summary = _business_summary(stage, record.get("result"))
            item["result_available"] = True
            item["result"] = summary
            summaries[stage] = summary
            records[stage] = record
        stages.append(item)
    packet = _current_contribution_packet(
        state_dir, workflow_run_id=workflow_run_id,
        freeze_event_id=freeze_event_id, records=records,
    )
    names = {
        str(profile.get("agent_id") or ""): str(profile.get("agent_name") or "")
        for profile in (packet or {}).get("agent_profiles", [])
    }
    for agent in summaries.get("contribution", {}).get("agents", []):
        agent["agent_name"] = names.get(agent.get("agent_id")) or agent.get("agent_id")
    response = {
        "schema_version": SCHEMA_VERSION,
        "channel_id": channel_id,
        "workflow_run_id": workflow_run_id,
        "freeze_event_id": freeze_event_id,
        "status": _evaluation_status(stages),
        "stages": stages,
        "scores": {
            key: summaries.get(key, {})
            for key in ("contribution", "discussion", "prd_fidelity")
        },
        "conclusion": summaries.get("conclusion", {}),
        "report": _read_current_report(
            state_dir,
            workflow_run_id,
            channel_id=channel_id,
            freeze=_freeze_from_ledger(state_dir, channel_id, freeze_event_id),
            records=records,
            packet=packet,
        ),
    }
    return _public_value(redact_obj(response))


def _empty_response(channel_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "channel_id": channel_id,
        "workflow_run_id": "",
        "freeze_event_id": "",
        "status": "unavailable",
        "stages": [_stage_row(stage, None) for stage in STAGES],
        "scores": {key: {} for key in ("contribution", "discussion", "prd_fidelity")},
        "conclusion": {},
        "report": {"available": False},
    }


def _stage_row(stage: str, row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    return {
        "judge_type": stage,
        "label": STAGE_LABELS[stage],
        "status": str(row.get("status") or "pending"),
        "verdict": str(row.get("verdict") or ""),
        "attempt": _nonnegative_int(row.get("attempt")),
        "max_attempts": _nonnegative_int(row.get("max_attempts")),
        "phase": str(row.get("phase") or ""),
        "slot_id": _slot_id(row.get("slot_id")),
        # Opaque generation fields let the client discard a response for an
        # older fidelity rebind without exposing a filesystem/audit reference.
        "job_id": str(row.get("job_id") or ""),
        "request_id": str(row.get("request_id") or ""),
        "trial_id": str(row.get("trial_id") or ""),
        "freeze_event_id": str(row.get("freeze_event_id") or ""),
        "source_snapshot_digest": str(row.get("source_snapshot_digest") or ""),
        "workflow_run_id": str(row.get("workflow_run_id") or ""),
        "error": str(row.get("error") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "result_available": False,
    }


def _read_bound_record(
    state_dir: Path,
    *,
    workflow_run_id: str,
    freeze_event_id: str,
    stage: str,
    row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if row is None or not freeze_event_id:
        return None
    if str(row.get("workflow_run_id") or "") != workflow_run_id:
        return None
    if str(row.get("freeze_event_id") or "") != freeze_event_id:
        return None
    request_id = str(row.get("request_id") or "")
    trial_id = str(row.get("trial_id") or "")
    if not request_id or not trial_id:
        return None
    try:
        if stage == "conclusion":
            if _safe_bound_path(
                state_dir, conclusion_result_path(state_dir, workflow_run_id, request_id)
            ) is None:
                return None
            record = read_conclusion_result(state_dir, workflow_run_id, request_id)
            if (
                record is None
                or str(record.get("trial_id") or "") != trial_id
                or not _conclusion_completion_bound(
                    state_dir,
                    workflow_run_id=workflow_run_id,
                    request_id=request_id,
                    trial_id=trial_id,
                    freeze_event_id=freeze_event_id,
                    record=record,
                )
            ):
                return None
        else:
            source_snapshot_digest = str(row.get("source_snapshot_digest") or "")
            if not source_snapshot_digest:
                return None
            if _safe_bound_path(
                state_dir,
                judge_result_path(
                    state_dir, workflow_run_id=workflow_run_id,
                    judge_type=stage, request_id=request_id,
                ),
            ) is None:
                return None
            record = read_judge_result(
                state_dir,
                workflow_run_id=workflow_run_id,
                judge_type=stage,
                request_id=request_id,
                expected_trial_id=trial_id,
                expected_source_snapshot_digest=source_snapshot_digest,
            )
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("status") != row.get("status") or row.get("status") in {
        "pending", "queued", "running", "retryable_failure",
    }:
        return None
    if stage != "conclusion" and str(record.get("freeze_event_id") or "") != freeze_event_id:
        return None
    return record


def _safe_bound_path(state_dir: Path, path: Path) -> Path | None:
    """Resolve a sidecar path and reject links escaping the state root."""

    try:
        resolved = path.resolve()
        resolved.relative_to(Path(state_dir).resolve())
        return resolved
    except (OSError, ValueError):
        return None


def _freeze_from_ledger(state_dir: Path, channel_id: str, freeze_event_id: str) -> dict[str, Any]:
    """Return the exact frozen handoff body, never a later Channel state."""

    if not freeze_event_id:
        return {}
    try:
        matches = [
            freeze for freeze in _freeze_points(
                EventLog(Path(state_dir) / "events.jsonl").read_all()
            ).values()
            if freeze["event_id"] == freeze_event_id and freeze["channel_id"] == channel_id
        ]
    except (OSError, ValueError, TypeError):
        return {}
    return dict(matches[0]) if len(matches) == 1 else {}


def _conclusion_completion_bound(
    state_dir: Path,
    *,
    workflow_run_id: str,
    request_id: str,
    trial_id: str,
    freeze_event_id: str,
    record: dict[str, Any],
) -> bool:
    """Confirm the conclusion sidecar through its exact ledger completion.

    Historical conclusion bodies omit ``freeze_event_id``.  The append-only
    completion event is therefore the binding from the validated body to the
    current frozen Channel identity.
    """

    path = _safe_bound_path(
        state_dir, conclusion_result_path(state_dir, workflow_run_id, request_id)
    )
    if path is None:
        return False
    try:
        artifact_ref = str(path.relative_to(Path(state_dir).resolve()))
        expected_digest = stable_digest(record)
        for event in EventLog(Path(state_dir) / "events.jsonl").read_all():
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict) or event.type != "benchmark.evaluation.conclusion.completed":
                continue
            if (
                str(payload.get("artifact_ref") or "") != artifact_ref
                or str(payload.get("artifact_digest") or "") != expected_digest
                or str(payload.get("request_id") or "") != request_id
                or str(payload.get("workflow_run_id") or "") != workflow_run_id
                or str(payload.get("trial_id") or "") != trial_id
                or str(payload.get("freeze_event_id") or "") != freeze_event_id
                or str(payload.get("evaluation_contract") or "") != "channel-prd-eval.v1"
                or str(payload.get("prompt_version") or "") != EVALUATION_PROMPT_VERSION
            ):
                continue
            # For the conclusion job, the event source digest is the digest
            # of upstream judge records.  It intentionally differs from the
            # frozen PRD-fidelity queue digest held in the projection row.
            return True
    except (OSError, ValueError, TypeError):
        return False
    return False


def _business_summary(stage: str, value: Any) -> dict[str, Any]:
    result = value if isinstance(value, dict) else {}
    missing = {"missing_evidence": _string_list(result.get("missing_evidence"))}
    if stage == "task_type":
        return {**_pick(result, "task_type", "confidence", "status"), **missing}
    if stage == "contribution":
        agents = []
        for item in result.get("agent_findings") or []:
            if isinstance(item, dict):
                agents.append(_pick(
                    item, "agent_id", "agent_name", "score", "necessity",
                    "necessity_status", "role_summary", "rationale", "status",
                ))
        return {"status": str(result.get("status") or ""), "agents": agents, **missing}
    if stage in {"discussion", "prd_fidelity"}:
        dimensions = []
        for item in result.get("dimensions") or []:
            if isinstance(item, dict):
                dimensions.append(_pick(
                    item, "dimension_id", "score", "verdict", "rationale", "status",
                ))
        summary = {"status": str(result.get("status") or ""), "dimensions": dimensions, **missing}
        if stage == "prd_fidelity":
            summary["differences"] = _string_list(result.get("differences"))
        else:
            summary.update(_pick(result, "discussion_meaningful", "discussion_progressed"))
        return summary
    team = result.get("team_findings") if isinstance(result.get("team_findings"), dict) else {}
    return {
        "conclusion_zh": str(result.get("conclusion_zh") or ""),
        "next_actions": _string_list(result.get("next_actions")),
        "team_findings": _pick(
            team, "prd_ready_for_next_stage", "discussion_meaningful", "discussion_progressed",
            "problems", "underperformed_roles", "unnecessary_roles",
        ),
        "problems": _string_list(team.get("problems")),
        "underperformed_roles": _string_list(team.get("underperformed_roles")),
        "unnecessary_roles": _string_list(team.get("unnecessary_roles")),
    }


def _read_current_report(
    state_dir: Path,
    workflow_run_id: str,
    *,
    channel_id: str,
    freeze: dict[str, Any],
    records: dict[str, dict[str, Any]],
    packet: dict[str, Any] | None,
) -> dict[str, Any]:
    """Expose a report only when it is exactly the verified current body."""

    try:
        if set(records) != set(STAGES) or not freeze or packet is None:
            return {"available": False}
        conclusion = records["conclusion"]
        trial_id = str(conclusion.get("trial_id") or "")
        if not trial_id:
            return {"available": False}
        path = report_path(state_dir, workflow_run_id)
        if _safe_bound_path(state_dir, path) is None:
            return {"available": False}
        with path.open("rb") as stream:
            body = stream.read(REPORT_MAX_BYTES + 1)
    except (OSError, ValueError):
        return {"available": False}
    if len(body) > REPORT_MAX_BYTES:
        return {"available": False}
    try:
        markdown = body.decode("utf-8")
    except UnicodeDecodeError:
        return {"available": False}
    expected = render_report(
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        channel_id=channel_id,
        freeze=freeze,
        judges={key: value for key, value in records.items() if key != "conclusion"},
        conclusion=conclusion,
        agent_profiles=packet.get("agent_profiles") or [],
    )
    # Re-rendering from the verified current records prevents a stale body
    # with copied identity headers from masquerading as the current report.
    if markdown != expected or "## 6. 原始 Judge JSON" not in markdown:
        return {"available": False}
    return {"available": True, "markdown": _public_report(markdown)}


def _public_report(markdown: str) -> str:
    """Remove internal artifact navigation and the PRD filesystem reference."""

    lines = markdown.splitlines()
    visible: list[str] = []
    for line in lines:
        if line == "## 6. 原始 Judge JSON":
            break
        if line.startswith("- channel: "):
            visible.append(line.split("  prd: `", 1)[0])
            continue
        visible.append(line)
    return "\n".join(visible).rstrip() + "\n"


def _public_value(value: Any) -> Any:
    """Hide runtime paths echoed in result prose, errors or Markdown."""
    if isinstance(value, str):
        return _INTERNAL_PATH_RE.sub("[运行路径]", value)
    if isinstance(value, dict):
        return {key: _public_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


def _current_contribution_packet(
    state_dir: Path,
    *,
    workflow_run_id: str,
    freeze_event_id: str,
    records: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Read the immutable queue context used by the current generation."""

    input_digest = ""
    for stage in ("contribution", "task_type", "discussion"):
        input_digest = _input_digest_for_record(
            state_dir,
            workflow_run_id=workflow_run_id,
            freeze_event_id=freeze_event_id,
            stage=stage,
            record=records.get(stage) or {},
        )
        if input_digest:
            break
    if not input_digest:
        return None
    base = Path(state_dir).resolve()
    path = base / "artifacts" / "benchmark-evaluations" / "queue-inputs" / f"{input_digest}.json"
    resolved = _safe_bound_path(state_dir, path)
    if resolved is None or resolved.suffix != ".json":
        return None
    try:
        context = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(context, dict) or stable_digest(context) != input_digest:
        return None
    freeze = context.get("freeze") if isinstance(context.get("freeze"), dict) else {}
    if (
        context.get("evaluation_contract") != "channel-prd-eval.v1"
        or str(context.get("workflow_run_id") or "") != workflow_run_id
        or str(freeze.get("event_id") or "") != freeze_event_id
    ):
        return None
    packets = context.get("packets")
    if not isinstance(packets, dict):
        return None
    packet = packets.get("contribution")
    if not isinstance(packet, dict) or packet.get("stage") != "contribution":
        return None
    record = records.get("contribution") or {}
    if any(packet.get(key) != record.get(key) for key in (
        "workflow_run_id", "trial_id", "source_snapshot_digest", "prompt_version",
    )):
        return None
    profiles = packet.get("agent_profiles")
    if not isinstance(profiles, list) or any(not isinstance(item, dict) for item in profiles):
        return None
    return {"agent_profiles": profiles}


def _input_digest_for_record(
    state_dir: Path,
    *,
    workflow_run_id: str,
    freeze_event_id: str,
    stage: str,
    record: dict[str, Any],
) -> str:
    request_id = str(record.get("request_id") or "")
    trial_id = str(record.get("trial_id") or "")
    if not request_id or not trial_id:
        return ""
    try:
        events = EventLog(Path(state_dir) / "events.jsonl").read_all()
    except (OSError, ValueError, TypeError):
        return ""
    for event in events:
        if event.type != "benchmark.evaluation.progress" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if (
            str(payload.get("workflow_run_id") or "") == workflow_run_id
            and str(payload.get("freeze_event_id") or "") == freeze_event_id
            and str(payload.get("judge_type") or "") == stage
            and str(payload.get("request_id") or payload.get("job_id") or "") == request_id
            and str(payload.get("trial_id") or "") == trial_id
            and payload.get("source_snapshot_digest") == record.get("source_snapshot_digest")
            and payload.get("evaluation_origin") in {None, "", "manual_regrade"}
            and payload.get("evaluation_contract") == "channel-prd-eval.v1"
            and payload.get("prompt_version") == EVALUATION_PROMPT_VERSION
        ):
            value = str(payload.get("input_digest") or "")
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
    return ""


def _evaluation_status(stages: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in stages}
    conclusion = next((item for item in stages if item["judge_type"] == "conclusion"), {})
    conclusion_status = str(conclusion.get("status") or "")
    if conclusion_status in {"complete", "completed"}:
        return "complete" if all(item["result_available"] for item in stages) else "incomplete"
    if statuses.intersection({"incomplete", "inconclusive", "invalid", "evidence_missing", "abstain", "conflict"}):
        return "incomplete"
    if "running" in statuses:
        return "running"
    return "queued"


def _pick(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value}


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _slot_id(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def build_channel_evaluation_router(resolve_ctx: Callable[[str], Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project_id}/channels/{channel_id}/evaluation")
    def channel_evaluation(project_id: str, channel_id: str) -> JSONResponse:
        context = resolve_ctx(project_id)
        payload = build_channel_evaluation(context.state_dir, channel_id)
        if payload is None:
            raise HTTPException(404, f"channel {channel_id!r} not found")
        return JSONResponse(payload)

    return router


__all__ = ["build_channel_evaluation", "build_channel_evaluation_router"]
