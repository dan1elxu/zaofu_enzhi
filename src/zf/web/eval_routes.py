"""Read-only, bounded three-lane PRD eval run API (docs/ideas/three-lane-prd-eval.md).

The projections below read only the eval-run artifact tree
(``artifacts/benchmark-evaluations/runs/`` under the project state dir): the
``run.json`` manifest is the canonical run state, the trial directory doubles
as the evaluation state dir (scheduler queue + judge sidecars + reports), and
lane PRDs are served only when their bytes still match the manifest digest.
Every payload is redacted and scrubbed of internal runtime paths, mirroring
``channel_evaluation_route``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from zf.core.security.redaction import redact_obj
from zf.runtime import eval_run
from zf.runtime.eval_run import EvalRunManifest, LaneState
from zf.runtime.evaluation_artifacts import read_judge_result
from zf.runtime.evaluation_lane_packets import blind_summary_from_record
from zf.runtime.evaluation_packets import EVALUATION_ARTIFACT_ROOT, stable_digest
from zf.runtime.evaluation_prompts import (
    CROSS_COMPARE_JUDGE_TYPE,
    LANE_PRD_FIDELITY_JUDGE_TYPE,
)
from zf.runtime.evaluation_scheduler import QUEUE_RELATIVE_PATH
from zf.web.channel_evaluation_route import _public_value

INDEX_SCHEMA_VERSION = "eval.runs.v1"
DETAIL_SCHEMA_VERSION = "eval.run.v1"
TASK_TYPE_JUDGE_TYPE = "task_type"
ABSENT_LANE_STATUS = "absent"

LANE_IDS: tuple[str, ...] = eval_run.DEFAULT_LANES

CONTENT_MAX_BYTES = 256 * 1024
QUEUE_MAX_BYTES = 1024 * 1024
PACKET_MAX_BYTES = 1024 * 1024
MAX_INPUT_PACKETS = 256

_QUEUE_SCHEMA_VERSION = "benchmark.evaluation.scheduler.v1"


# --------------------------------------------------------------------- reads


def _bounded_bytes(path: Path, root: Path, max_bytes: int) -> bytes | None:
    """Bounded read that rejects links escaping ``root`` and oversized files."""

    try:
        resolved = path.resolve()
        resolved.relative_to(Path(root).resolve())
        with resolved.open("rb") as stream:
            body = stream.read(max_bytes + 1)
    except (OSError, ValueError):
        return None
    if len(body) > max_bytes:
        return None
    return body


def _bounded_text(path: Path, root: Path, max_bytes: int) -> str | None:
    body = _bounded_bytes(path, root, max_bytes)
    if body is None:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _queue_entries(run_dir: Path) -> list[dict[str, Any]]:
    """Trial-local scheduler queue rows; an unreadable queue means no progress."""

    text = _bounded_text(Path(run_dir) / QUEUE_RELATIVE_PATH, run_dir, QUEUE_MAX_BYTES)
    if text is None:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != _QUEUE_SCHEMA_VERSION:
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _pick_entry(
    entries: list[dict[str, Any]], *, judge_type: str, workflow_run_id: str = "",
) -> dict[str, Any] | None:
    candidates = [
        entry
        for entry in entries
        if str(entry.get("judge_type") or "") == judge_type
        and not str(entry.get("superseded_by") or "")
        and str(entry.get("job_id") or "").strip()
        and (not workflow_run_id or str(entry.get("workflow_run_id") or "") == workflow_run_id)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: str(entry.get("updated_at") or ""))


def _bound_record(run_dir: Path, entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Judge sidecar bound to its queue identity and the trial-local ledger."""

    if entry is None:
        return None
    request_id = str(entry.get("job_id") or "")
    workflow_run_id = str(entry.get("workflow_run_id") or "")
    trial_id = str(entry.get("trial_id") or "")
    source_snapshot_digest = str(entry.get("source_snapshot_digest") or "")
    if not (request_id and workflow_run_id and trial_id and source_snapshot_digest):
        return None
    try:
        return read_judge_result(
            Path(run_dir),
            workflow_run_id=workflow_run_id,
            judge_type=str(entry.get("judge_type") or ""),
            request_id=request_id,
            expected_trial_id=trial_id,
            expected_source_snapshot_digest=source_snapshot_digest,
        )
    except (OSError, ValueError, TypeError):
        return None


def _stage_status(entry: dict[str, Any] | None, record: dict[str, Any] | None) -> str:
    if record is not None:
        return str(record.get("status") or "")
    if entry is not None:
        return str(entry.get("status") or "")
    return ""


def _number(value: Any) -> int | float | None:
    return value if type(value) in {int, float} else None


# ------------------------------------------------------------------- stages


def _lane_score(
    run_dir: Path,
    manifest: EvalRunManifest,
    lane: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    workflow_run_id = eval_run.lane_eval_run_id(
        prompt_digest=manifest.prompt_digest, trial=manifest.trial, lane=lane,
    )
    entry = _pick_entry(
        entries, judge_type=LANE_PRD_FIDELITY_JUDGE_TYPE, workflow_run_id=workflow_run_id,
    )
    record = _bound_record(run_dir, entry)
    if entry is None and record is None:
        return None
    summary = blind_summary_from_record(record) if record is not None else {}
    dimensions: dict[str, int | float | None] = {}
    for item in summary.get("dimensions") or []:
        dimension = str(item.get("dimension_id") or "")
        if dimension:
            dimensions[dimension] = _number(item.get("score"))
    composite = _number(summary.get("mean"))
    if composite is None:
        scored = [value for value in dimensions.values() if value is not None]
        composite = round(sum(scored) / len(scored), 1) if scored else None
    return {
        "status": _stage_status(entry, record),
        "dimensions": dimensions,
        "composite": composite,
        "checklist": _checklist_counts(record),
    }


def _checklist_counts(record: dict[str, Any] | None) -> dict[str, int] | None:
    """Verdict counts of the v2 requirement checklist; None for v1 records."""

    result = (record or {}).get("result")
    result = result if isinstance(result, dict) else {}
    checklist = result.get("checklist")
    if not isinstance(checklist, list) or not checklist:
        return None
    counts = {"pass": 0, "partial": 0, "missing": 0}
    total = 0
    for entry in checklist:
        if not isinstance(entry, dict):
            continue
        verdict = str(entry.get("verdict") or "")
        if verdict in counts:
            counts[verdict] += 1
            total += 1
    if not total:
        return None
    return {"total": total, **counts}


def _task_type_section(
    run_dir: Path, entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    entry = _pick_entry(entries, judge_type=TASK_TYPE_JUDGE_TYPE)
    record = _bound_record(run_dir, entry)
    if entry is None and record is None:
        return None
    result = (record or {}).get("result")
    result = result if isinstance(result, dict) else {}
    return {
        "status": _stage_status(entry, record),
        "task_type": str(result.get("task_type") or "") or None,
        "confidence": _number(result.get("confidence")),
    }


def _cross_compare_section(
    run_dir: Path, entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    entry = _pick_entry(entries, judge_type=CROSS_COMPARE_JUDGE_TYPE)
    record = _bound_record(run_dir, entry)
    if entry is None and record is None:
        return None
    result = (record or {}).get("result")
    result = result if isinstance(result, dict) else {}
    recommendation = result.get("recommendation")
    recommendation = recommendation if isinstance(recommendation, dict) else {}
    ranking = recommendation.get("ranking")
    key_gaps = [
        {"candidate": str(gap.get("candidate") or ""), "gap": str(gap.get("gap") or "")}
        for gap in recommendation.get("key_gaps") or []
        if isinstance(gap, dict)
    ]
    dimension_rankings = []
    for item in result.get("dimension_rankings") or []:
        if not isinstance(item, dict):
            continue
        order = item.get("ranking")
        dimension_rankings.append({
            "dimension_id": str(item.get("dimension_id") or ""),
            "ranking": [str(label) for label in order] if isinstance(order, list) else [],
            "confidence": _number(item.get("confidence")),
            "rationale": str(item.get("rationale") or ""),
        })
    conflicts = [
        {
            "dimension_id": str(item.get("dimension_id") or ""),
            "blind_favorite": str(item.get("blind_favorite") or ""),
            "pairwise_favorite": str(item.get("pairwise_favorite") or ""),
            "resolution": str(item.get("resolution") or ""),
        }
        for item in result.get("conflicts") or []
        if isinstance(item, dict)
    ]
    return {
        "status": _stage_status(entry, record),
        "recommendation": (
            [str(label) for label in ranking] if isinstance(ranking, list) else []
        ),
        "key_gaps": key_gaps,
        "dimension_rankings": dimension_rankings,
        "conflicts": conflicts,
    }


# ------------------------------------------------------------- run material


def _prompt_text(state_dir: Path, manifest: EvalRunManifest) -> str:
    """Serve the prompt body only when the immutable packet still verifies."""

    path = eval_run.prompt_packet_path(state_dir, prompt_digest=manifest.prompt_digest)
    text = _bounded_text(path, state_dir, CONTENT_MAX_BYTES)
    if text is None:
        return ""
    try:
        packet = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(packet, dict):
        return ""
    body = str(packet.get("prompt_text") or "")
    version = str(packet.get("prompt_version") or "")
    if packet.get("prompt_digest") != manifest.prompt_digest:
        return ""
    try:
        if eval_run.prompt_digest(body, prompt_version=version) != manifest.prompt_digest:
            return ""
    except ValueError:
        return ""
    return body


def _prd_markdown(run_dir: Path, lane_state: LaneState | None) -> str | None:
    """A lane PRD is served only while its bytes match the manifest digest."""

    if lane_state is None:
        return None
    if lane_state.status != eval_run.LANE_STATUS_PRD_FROZEN or not lane_state.prd_digest:
        return None
    path = eval_run.lane_prd_path(Path(run_dir), lane_state.lane)
    body = _bounded_bytes(path, run_dir, CONTENT_MAX_BYTES)
    if body is None:
        return None
    if hashlib.sha256(body).hexdigest() != lane_state.prd_digest:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _report_markdown(run_dir: Path, manifest: EvalRunManifest) -> str | None:
    # Bind the run-level report by identity; per-lane reports share the
    # reports/ root and must not be confused with it.
    path = (
        Path(run_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "reports"
        / manifest.run_id
        / "report.md"
    )
    if not path.is_file():
        return None
    return _bounded_text(path, run_dir, CONTENT_MAX_BYTES)


def _label_map(run_dir: Path, manifest: EvalRunManifest) -> dict[str, str]:
    """Recover candidate label -> lane from the immutable cross-compare packet.

    The binding is digest-exact: candidate PRD digests in the verified packet
    must map one-to-one onto the manifest's lane PRD digests, otherwise the
    mapping stays empty rather than guessing.
    """

    lane_digests = {
        lane: state.prd_digest
        for lane, state in manifest.lanes.items()
        if state.prd_digest
    }
    if len(lane_digests) < 2:
        return {}
    digest_to_lane: dict[str, str] = {}
    for lane, digest in lane_digests.items():
        if digest in digest_to_lane:
            return {}
        digest_to_lane[digest] = lane
    inputs = Path(run_dir) / EVALUATION_ARTIFACT_ROOT / "inputs"
    if not inputs.is_dir():
        return {}
    mapping: dict[str, str] = {}
    for path in sorted(inputs.glob("*.json"))[:MAX_INPUT_PACKETS]:
        text = _bounded_text(path, run_dir, PACKET_MAX_BYTES)
        if text is None:
            continue
        try:
            packet = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(packet, dict) or path.stem != stable_digest(packet):
            continue
        if str(packet.get("stage") or "") != CROSS_COMPARE_JUDGE_TYPE:
            continue
        details = packet.get("task_details")
        details = details if isinstance(details, dict) else {}
        candidates = details.get("candidates")
        if not isinstance(candidates, list):
            continue
        by_digest: dict[str, str] = {}
        valid = True
        for item in candidates:
            if not isinstance(item, dict):
                valid = False
                break
            label = str(item.get("label") or "").strip()
            prd = item.get("prd") if isinstance(item.get("prd"), dict) else {}
            digest = str(prd.get("digest") or "")
            if not label or not digest or digest in by_digest:
                valid = False
                break
            by_digest[digest] = label
        if not valid or set(by_digest) != set(digest_to_lane):
            continue
        candidate_map = {label: digest_to_lane[digest] for digest, label in by_digest.items()}
        if mapping and mapping != candidate_map:
            return {}
        mapping = candidate_map
    return mapping


# --------------------------------------------------------------- projection


def _trial_summary(state_dir: Path, manifest: EvalRunManifest) -> dict[str, Any]:
    run_dir = eval_run.eval_run_dir(
        state_dir, prompt_digest=manifest.prompt_digest, trial=manifest.trial,
    )
    entries = _queue_entries(run_dir)
    lanes: dict[str, Any] = {}
    composite: dict[str, Any] = {}
    any_score = False
    for lane in LANE_IDS:
        lane_state = manifest.lanes.get(lane)
        lanes[lane] = {
            "status": str(lane_state.status) if lane_state else ABSENT_LANE_STATUS,
            "error": str(lane_state.error) if lane_state else "",
        }
        score = (
            _lane_score(run_dir, manifest, lane, entries)
            if lane_state is not None
            else None
        )
        value = score.get("composite") if score else None
        composite[lane] = value
        any_score = any_score or value is not None
    compare = _cross_compare_section(run_dir, entries)
    recommendation = compare["recommendation"] if compare else []
    return {
        "run_id": manifest.run_id,
        "trial": manifest.trial,
        "status": manifest.status,
        "started_at": manifest.created_at,
        "finished_at": manifest.completed_at or None,
        "lanes": lanes,
        "composite_scores": composite if any_score else None,
        "recommendation": recommendation or None,
        "artifacts_source": manifest.artifacts_source or None,
    }


def build_eval_runs_index(state_dir: Path) -> dict[str, Any]:
    """Run-group index: trials grouped by prompt digest, deterministically sorted."""

    state_dir = Path(state_dir)
    groups: dict[str, list[EvalRunManifest]] = {}
    for manifest in eval_run.list_eval_runs(state_dir):
        groups.setdefault(manifest.prompt_digest, []).append(manifest)
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "groups": [
            {
                "prompt_digest": digest,
                "prompt_version": manifests[0].prompt_version,
                "trials": [
                    _trial_summary(state_dir, manifest) for manifest in manifests
                ],
            }
            for digest, manifests in sorted(groups.items())
        ],
    }
    return _public_value(redact_obj(payload))


def build_eval_run_detail(state_dir: Path, run_id: str) -> dict[str, Any] | None:
    state_dir = Path(state_dir)
    try:
        manifest = eval_run.load_manifest(state_dir, str(run_id or ""))
    except (FileNotFoundError, ValueError):
        return None
    run_dir = eval_run.eval_run_dir(
        state_dir, prompt_digest=manifest.prompt_digest, trial=manifest.trial,
    )
    entries = _queue_entries(run_dir)
    lanes: dict[str, Any] = {}
    lane_scores: dict[str, Any] = {}
    for lane in LANE_IDS:
        lane_state = manifest.lanes.get(lane)
        lanes[lane] = {
            "status": str(lane_state.status) if lane_state else ABSENT_LANE_STATUS,
            "error": str(lane_state.error) if lane_state else "",
            "started_at": str(lane_state.started_at) if lane_state else "",
            "finished_at": str(lane_state.completed_at) if lane_state else "",
            "prd_markdown": _prd_markdown(run_dir, lane_state),
        }
        lane_scores[lane] = (
            _lane_score(run_dir, manifest, lane, entries)
            if lane_state is not None
            else None
        )
    payload = {
        "schema_version": DETAIL_SCHEMA_VERSION,
        "run_id": manifest.run_id,
        "prompt_digest": manifest.prompt_digest,
        "prompt_version": manifest.prompt_version,
        "prompt_text": _prompt_text(state_dir, manifest),
        "trial": manifest.trial,
        "status": manifest.status,
        "artifacts_source": manifest.artifacts_source or None,
        "lanes": lanes,
        "evaluation": {
            "task_type": _task_type_section(run_dir, entries),
            "lane_scores": lane_scores,
            "cross_compare": _cross_compare_section(run_dir, entries),
        },
        "label_map": _label_map(run_dir, manifest),
        "report_markdown": _report_markdown(run_dir, manifest),
    }
    return _public_value(redact_obj(payload))


# -------------------------------------------------------------------- router


def build_eval_router(resolve_ctx: Callable[[str], Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project_id}/eval/runs")
    def eval_runs_index(project_id: str) -> JSONResponse:
        context = resolve_ctx(project_id)
        return JSONResponse(build_eval_runs_index(context.state_dir))

    @router.get("/api/projects/{project_id}/eval/runs/{run_id}")
    def eval_run_detail(project_id: str, run_id: str) -> JSONResponse:
        context = resolve_ctx(project_id)
        payload = build_eval_run_detail(context.state_dir, run_id)
        if payload is None:
            raise HTTPException(404, f"eval run {run_id!r} not found")
        return JSONResponse(payload)

    return router


__all__ = [
    "DETAIL_SCHEMA_VERSION",
    "INDEX_SCHEMA_VERSION",
    "build_eval_run_detail",
    "build_eval_router",
    "build_eval_runs_index",
]
