"""Explicit, isolated regrade of one frozen Channel evaluation input."""
from __future__ import annotations

import fcntl
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from zf.core.events import EventLog
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.evaluation_observer import _enqueue_channel, _evaluate_stage
from zf.runtime.evaluation_packets import (
    EVALUATION_ARTIFACT_ROOT, evaluation_request_id, stable_digest,
)
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_roles import snapshot_catalog
from zf.runtime.evaluation_scheduler import EvaluationScheduler, MAX_SLOT_COUNT
from zf.runtime.evaluation_artifacts import report_path

CONTRACT = "channel-prd-eval.v1"
REGRADES_ROOT = EVALUATION_ARTIFACT_ROOT / "regrades"


def _safe(value: str) -> str:
    from zf.runtime.evaluation_packets import _safe_segment
    return _safe_segment(value)


def _load_source_context(state_dir: Path, input_ref: str) -> tuple[dict[str, Any], str]:
    base = Path(state_dir).resolve()
    ref = Path(input_ref)
    if ref.is_absolute() or ".." in ref.parts or ref.suffix != ".json":
        raise ValueError("regrade input-ref must be a relative queue-input JSON path")
    path = (base / ref).resolve()
    queue_root = (base / EVALUATION_ARTIFACT_ROOT / "queue-inputs").resolve()
    try:
        queue_root.relative_to(base)
        path.relative_to(base)
        path.relative_to(queue_root)
    except ValueError as exc:
        raise ValueError("regrade input-ref must be under queue-inputs") from exc
    if not path.is_file():
        raise ValueError("regrade input-ref is missing or malformed")
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("regrade input is unreadable") from exc
    if not isinstance(context, dict) or stable_digest(context) != path.stem:
        raise ValueError("regrade input digest mismatch")
    if context.get("evaluation_contract") != CONTRACT:
        raise ValueError("unsupported evaluation contract")
    if context.get("evaluation_origin"):
        raise ValueError("manual regrade cannot be used as a source")
    packets = context.get("packets")
    required_stages = {"task_type", "contribution", "discussion", "prd_fidelity"}
    if not isinstance(packets, dict) or set(packets) != required_stages:
        raise ValueError("regrade source must contain the complete four judge packets")
    freeze = context.get("freeze")
    if not isinstance(freeze, dict) or not str(freeze.get("event_id") or ""):
        raise ValueError("regrade source has no freeze identity")
    run_id = str(context.get("workflow_run_id") or "")
    channel_id = str(context.get("channel_id") or "")
    if not run_id or not channel_id or not isinstance(context.get("packets"), dict):
        raise ValueError("regrade source context is incomplete")
    # The canonical scheduler row is the admission receipt for this context.
    scheduler_path = base / EVALUATION_ARTIFACT_ROOT / "scheduler.json"
    try:
        queue = json.loads(scheduler_path.read_text(encoding="utf-8"))
        if not isinstance(queue, dict) or queue.get("schema_version") != "benchmark.evaluation.scheduler.v1":
            raise ValueError("unsupported canonical scheduler schema")
        entries = queue["entries"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("canonical evaluation scheduler is unreadable") from exc
    expected_ref = str(path.relative_to(base))
    if not isinstance(entries, list):
        raise ValueError("canonical evaluation scheduler entries are invalid")
    anchored = True
    for stage, packet in packets.items():
        if not isinstance(packet, dict):
            raise ValueError(f"regrade source packet is invalid: {stage}")
        expected_job = evaluation_request_id(
            workflow_run_id=run_id,
            trial_id=str(packet.get("trial_id") or ""),
            judge_type=stage,
            source_snapshot_digest=str(packet.get("source_snapshot_digest") or ""),
            prompt_version=str(packet.get("prompt_version") or ""),
        )
        anchored = anchored and any(
            isinstance(entry, dict)
            and not entry.get("evaluation_origin")
            and entry.get("job_id") == expected_job
            and entry.get("workflow_run_id") == run_id
            and entry.get("channel_id") == channel_id
            and entry.get("freeze_event_id") == freeze.get("event_id")
            and entry.get("judge_type") == stage
            and entry.get("trial_id") == packet.get("trial_id")
            and entry.get("source_snapshot_digest") == packet.get("source_snapshot_digest")
            for entry in entries
        )
    # A later Workflow binding reuses the first three jobs. Only its new
    # fidelity/conclusion rows point at the newer context; all four stage
    # identities above must still be admitted by the canonical queue.
    if not anchored or not any(
        isinstance(entry, dict) and not entry.get("evaluation_origin")
        and entry.get("input_ref") == expected_ref and entry.get("input_digest") == path.stem
        and entry.get("workflow_run_id") == run_id and entry.get("channel_id") == channel_id
        and entry.get("freeze_event_id") == freeze.get("event_id")
        for entry in entries
    ):
        raise ValueError("regrade input is not anchored by the canonical scheduler")
    # The queue file is a projection; require its append-only progress receipt
    # as well, so a copied/hand-written scheduler cannot admit a regrade.
    progress_anchored = any(
        getattr(event, "type", "") == "benchmark.evaluation.progress"
        and isinstance(getattr(event, "payload", None), dict)
        and not event.payload.get("evaluation_origin")
        and event.payload.get("input_digest") == path.stem
        and event.payload.get("input_ref") in {None, "", expected_ref}
        and event.payload.get("workflow_run_id") == run_id
        and event.payload.get("channel_id") == channel_id
        and event.payload.get("freeze_event_id") == freeze.get("event_id")
        for event in EventLog(base / "events.jsonl").read_all()
    )
    if not progress_anchored:
        raise ValueError("regrade input has no scheduler progress receipt")
    return context, path.stem


def _clone_context(context: dict[str, Any], regrade_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    catalog = snapshot_catalog()
    source_run = str(context["workflow_run_id"])
    new_run = f"regrade-{_safe(regrade_id)}"
    clone = json.loads(json.dumps(context, ensure_ascii=False))
    clone.update({
        "workflow_run_id": new_run,
        "trial_id": f"regrade-{_safe(regrade_id)}-{_safe(str(context['trial_id']))}",
        # Preserve the frozen evidence digest exactly. The new run/trial IDs
        # isolate a manual invocation; catalog and prompt identity belong in
        # the explicit audit record, not in evidence provenance.
        "source_snapshot_digest": str(context["source_snapshot_digest"]),
        "regrade_id": regrade_id,
        "parent_workflow_run_id": source_run,
        "evaluation_origin": "manual_regrade",
    })
    stage_map: dict[str, str] = {}
    for stage, packet in clone["packets"].items():
        if not isinstance(packet, dict):
            raise ValueError(f"invalid source packet: {stage}")
        old_digest = str(packet.get("source_snapshot_digest") or "")
        if not old_digest:
            raise ValueError(f"source packet digest missing: {stage}")
        stage_map[stage] = old_digest
        packet.update({
            "workflow_run_id": new_run, "run_id": new_run,
            "trial_id": f"regrade-{_safe(regrade_id)}-{_safe(str(packet.get('trial_id') or stage))}",
            "source_snapshot_digest": old_digest,
            "prompt_version": EVALUATION_PROMPT_VERSION,
        })
        details = packet.setdefault("task_details", {})
        if stage == "task_type":
            details["evaluation_role_catalog"] = catalog
        else:
            details["evaluation_role_catalog_ref"] = {
                "schema_version": catalog["schema_version"],
                "catalog_version": catalog["catalog_version"],
                "catalog_digest": catalog["catalog_digest"],
            }
    clone["regrade_catalog_digest"] = catalog["catalog_digest"]
    clone["regrade_prompt_version"] = EVALUATION_PROMPT_VERSION
    return clone, stage_map


def _persist_clone_context(state_dir: Path, context: dict[str, Any]) -> tuple[str, str]:
    """Write the first manual clone once; retries must consume these bytes."""
    digest = stable_digest(context)
    path = Path(state_dir) / EVALUATION_ARTIFACT_ROOT / "queue-inputs" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("existing regrade clone is unreadable") from exc
        if not isinstance(existing, dict) or stable_digest(existing) != digest:
            raise ValueError("existing regrade clone digest mismatch")
    else:
        atomic_write_text(path, json.dumps(context, ensure_ascii=False, indent=2) + "\n")
    return str(path.relative_to(state_dir)), digest


def _load_clone_context(state_dir: Path, audit: dict[str, Any]) -> dict[str, Any]:
    ref = str(audit.get("regrade_context_ref") or "")
    digest = str(audit.get("regrade_context_digest") or "")
    base = Path(state_dir).resolve()
    path = (base / ref).resolve()
    root = (base / EVALUATION_ARTIFACT_ROOT / "queue-inputs").resolve()
    if not ref or Path(ref).is_absolute() or ".." in Path(ref).parts:
        raise ValueError("regrade clone ref is unsafe")
    try:
        root.relative_to(base)
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("regrade clone escaped queue-inputs") from exc
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("regrade clone is unreadable") from exc
    if not isinstance(context, dict) or path.stem != digest or stable_digest(context) != digest:
        raise ValueError("regrade clone digest mismatch")
    if (
        context.get("evaluation_origin") != "manual_regrade"
        or context.get("workflow_run_id") != audit.get("workflow_run_id")
    ):
        raise ValueError("regrade clone identity mismatch")
    return context


def _stage_digests(context: dict[str, Any]) -> dict[str, str]:
    packets = context.get("packets")
    if not isinstance(packets, dict):
        raise ValueError("regrade context packets are invalid")
    expected = {"task_type", "contribution", "discussion", "prd_fidelity"}
    if set(packets) != expected:
        raise ValueError("regrade context stages are invalid")
    result: dict[str, str] = {}
    for stage, packet in packets.items():
        if not isinstance(packet, dict) or not str(packet.get("source_snapshot_digest") or ""):
            raise ValueError(f"regrade context stage digest is missing: {stage}")
        result[stage] = str(packet["source_snapshot_digest"])
    return result


def _validate_regrade_chain(
    state_dir: Path,
    *,
    source: dict[str, Any],
    source_ref: str,
    source_digest: str,
    regrade_id: str,
    audit: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed unless the audit, source, and frozen manual clone agree."""

    expected_run = f"regrade-{_safe(regrade_id)}"
    expected_trial = f"regrade-{_safe(regrade_id)}-{_safe(str(source.get('trial_id') or ''))}"
    source_freeze = source.get("freeze")
    if not isinstance(source_freeze, dict):
        raise ValueError("regrade source freeze is invalid")
    source_stages = _stage_digests(source)
    if (
        audit.get("schema_version") != "channel-prd-evaluation.regrade.v1"
        or audit.get("regrade_id") != regrade_id
        or audit.get("original_context_ref") != source_ref
        or audit.get("original_context_digest") != source_digest
        or audit.get("parent_workflow_run_id") != source.get("workflow_run_id")
        or audit.get("workflow_run_id") != expected_run
        or audit.get("trial_id") != expected_trial
        or audit.get("freeze_event_id") != source_freeze.get("event_id")
        or audit.get("prompt_version") != EVALUATION_PROMPT_VERSION
        or audit.get("stage_digests") != source_stages
    ):
        raise ValueError("regrade audit identity mismatch")
    clone = _load_clone_context(state_dir, audit)
    clone_freeze = clone.get("freeze")
    if (
        clone.get("regrade_id") != regrade_id
        or clone.get("evaluation_origin") != "manual_regrade"
        or clone.get("parent_workflow_run_id") != source.get("workflow_run_id")
        or clone.get("workflow_run_id") != expected_run
        or clone.get("trial_id") != expected_trial
        or clone.get("channel_id") != source.get("channel_id")
        or clone.get("source_snapshot_digest") != source.get("source_snapshot_digest")
        or not isinstance(clone_freeze, dict)
        or stable_digest(clone_freeze) != stable_digest(source_freeze)
        or _stage_digests(clone) != source_stages
        or clone.get("regrade_prompt_version") != audit.get("prompt_version")
        or clone.get("regrade_catalog_digest") != audit.get("catalog_digest")
    ):
        raise ValueError("regrade clone identity mismatch")
    for stage, packet in clone["packets"].items():
        expected_packet_trial = (
            f"regrade-{_safe(regrade_id)}-"
            f"{_safe(str(source['packets'][stage].get('trial_id') or stage))}"
        )
        if (
            packet.get("workflow_run_id") != expected_run
            or packet.get("run_id") != expected_run
            or packet.get("trial_id") != expected_packet_trial
            or packet.get("prompt_version") != audit.get("prompt_version")
        ):
            raise ValueError("regrade clone packet identity mismatch")
        details = packet.get("task_details") if isinstance(packet.get("task_details"), dict) else {}
        if stage == "task_type":
            catalog = details.get("evaluation_role_catalog")
            if not isinstance(catalog, dict) or catalog.get("catalog_digest") != audit.get("catalog_digest"):
                raise ValueError("regrade clone catalog mismatch")
        else:
            catalog_ref = details.get("evaluation_role_catalog_ref")
            if not isinstance(catalog_ref, dict) or catalog_ref.get("catalog_digest") != audit.get("catalog_digest"):
                raise ValueError("regrade clone catalog mismatch")
    return clone


@contextmanager
def _manual_slot(state_dir: Path) -> Iterator[None]:
    base = Path(state_dir).resolve()
    lock = base / REGRADES_ROOT / "slot-8.lock"
    try:
        lock.resolve().relative_to(base)
    except ValueError as exc:
        raise ValueError("manual regrade lock escaped state_dir") from exc
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("manual regrade slot 8 is busy") from exc
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def regrade_once(*, state_dir: Path, input_ref: str, config: Any | None = None,
                 project_root: Path | None = None, backend: Any | None = None,
                 regrade_id: str | None = None) -> dict[str, Any]:
    """Run one explicitly requested regrade synchronously; never auto-discovered."""
    state_dir = Path(state_dir).resolve()
    context, source_digest = _load_source_context(state_dir, input_ref)
    rid = str(regrade_id or uuid.uuid4()).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", rid):
        raise ValueError("invalid regrade id")
    base = state_dir.resolve()
    regrade_dir = base / REGRADES_ROOT / rid
    audit_path = report_path(state_dir, f"regrade-{_safe(rid)}").parent / "regrade.json"
    queue_path = regrade_dir / "scheduler.json"
    with _manual_slot(state_dir):
        try:
            regrade_dir.resolve().relative_to(base)
            audit_path.resolve().relative_to(base)
        except ValueError as exc:
            raise ValueError("manual regrade path escaped state_dir") from exc
        prior: dict[str, Any] | None = None
        if audit_path.exists():
            try:
                prior = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("existing regrade audit is unreadable") from exc
            if (
                not isinstance(prior, dict)
                or prior.get("original_context_ref") != input_ref
                or prior.get("original_context_digest") != source_digest
            ):
                raise ValueError("regrade id is already bound to another source context")
            clone = _validate_regrade_chain(
                state_dir,
                source=context,
                source_ref=input_ref,
                source_digest=source_digest,
                regrade_id=rid,
                audit=prior,
            )
            if prior.get("status") == "completed":
                return {
                    **prior, "queued": 0, "results": [],
                    "audit_ref": str(audit_path.relative_to(base)),
                    "report_ref": str(report_path(base, str(prior["workflow_run_id"])).relative_to(base)),
                    "idempotent": True,
                }
            if prior.get("status") not in {"running", "failed"}:
                raise ValueError("existing regrade audit has unsupported status")
        if prior is None:
            clone, stage_map = _clone_context(context, rid)
            clone_ref, clone_digest = _persist_clone_context(state_dir, clone)
            audit = {
                "schema_version": "channel-prd-evaluation.regrade.v1",
                "status": "running", "regrade_id": rid,
                "parent_workflow_run_id": context["workflow_run_id"],
                "workflow_run_id": clone["workflow_run_id"], "trial_id": clone["trial_id"],
                "original_context_ref": input_ref, "original_context_digest": source_digest,
                "regrade_context_ref": clone_ref, "regrade_context_digest": clone_digest,
                "freeze_event_id": clone["freeze"]["event_id"],
                "prompt_version": EVALUATION_PROMPT_VERSION,
                "catalog_digest": clone["regrade_catalog_digest"],
                "stage_digests": stage_map,
            }
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(audit_path, json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
        else:
            audit = dict(prior)
        run_id = str(clone["workflow_run_id"])
        regrade_dir.mkdir(parents=True, exist_ok=True)
        scheduler = EvaluationScheduler(state_dir, max_slots=1, inline=True, queue_path=queue_path)
        try:
            queued = _enqueue_channel(state_dir, scheduler, clone)
            results = scheduler.tick(lambda entry, _slot: _evaluate_stage(
                state_dir=state_dir, project_root=project_root, config=config,
                entry=entry, slot_id=MAX_SLOT_COUNT, backend=backend,
                log=lambda _message: None, stop_event=scheduler.stop_event,
            ))
        finally:
            scheduler.close()
        audit.update({"status": "completed", "queued": queued, "results": results})
        atomic_write_text(audit_path, json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
        report_ref = str(report_path(base, run_id).relative_to(base))
        return {"regrade_id": rid, "workflow_run_id": run_id, "queued": queued,
                "results": results, "audit_ref": str(audit_path.relative_to(base)),
                "report_ref": report_ref}


__all__ = ["regrade_once"]
