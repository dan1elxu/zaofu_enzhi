"""Pin automatic evaluations to their first admission and retire rubric drift."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from zf.core.events import EventLog
from zf.runtime.evaluation_packets import evaluation_request_id, stable_digest
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, EvaluationScheduler


def admitted_context(
    state_dir: Path, entries: dict[str, EvaluationQueueEntry],
    events: list[Any], freeze: dict[str, Any],
) -> dict[str, Any] | None:
    from zf.runtime.evaluation_observer import _load_queue_context

    classifiers = {
        key: entry for key, entry in entries.items()
        if entry.channel_id == freeze["channel_id"] and entry.freeze_event_id == freeze["event_id"]
        and entry.judge_type == "task_type" and not entry.evaluation_origin
    }
    if not classifiers:
        return None
    # Ledger order, not queue order, completion time or score, owns admission.
    for event in events:
        if event.type != "benchmark.evaluation.progress":
            continue
        payload = event.payload
        entry = classifiers.get(payload.get("job_id"))
        if entry is None or payload.get("evaluation_origin"):
            continue
        fields = ("channel_id", "workflow_run_id", "freeze_event_id", "judge_type",
                  "trial_id", "source_snapshot_digest", "input_digest")
        if any(payload.get(field) != getattr(entry, field) for field in fields):
            continue
        context = _load_queue_context(state_dir, entry)
        if entry.superseded_by or context["freeze"] != freeze:
            raise ValueError("first evaluation admission has conflicting freeze identity")
        return context
    raise ValueError("evaluation classifier has no matching admission receipt")


def require_admitted_trial(state_dir: Path, context: dict[str, Any]) -> None:
    """Guard auto-dispatched workers even if discovery could not repair a row."""
    packet = context["packets"]["task_type"]
    expected = evaluation_request_id(
        workflow_run_id=context["workflow_run_id"], trial_id=packet["trial_id"],
        judge_type="task_type", source_snapshot_digest=packet["source_snapshot_digest"],
    )
    for event in EventLog(state_dir / "events.jsonl").read_all():
        payload = event.payload
        if (event.type != "benchmark.evaluation.progress" or payload.get("evaluation_origin")
                or payload.get("channel_id") != context["channel_id"]
                or payload.get("workflow_run_id") != context["workflow_run_id"]
                or payload.get("freeze_event_id") != context["freeze"]["event_id"]
                or payload.get("judge_type") != "task_type"
                or payload.get("prompt_version") != EVALUATION_PROMPT_VERSION):
            continue
        if (payload.get("job_id") != expected
                or payload.get("trial_id") != packet.get("trial_id")
                or payload.get("source_snapshot_digest") != packet.get("source_snapshot_digest")):
            raise ValueError("evaluation trial is not the admitted freeze trial; not dispatched")
        return
    raise ValueError("evaluation trial has no admission receipt; not dispatched")


def binding_changed(context: dict[str, Any], bound_workflows: list[dict[str, Any]]) -> bool:
    """Tell discovery whether a legal Workflow bind created a new generation."""
    freeze = context.get("freeze") or {}
    expected = context.get("packets", {}).get("prd_fidelity", {}).get("workflow_bound_prd") or {}
    relevant = [
        item for item in bound_workflows
        if str(item.get("channel_thread_id") or "") == str(freeze.get("thread_id") or "")
        and str(item.get("channel_prd_revision") or "") == str(freeze.get("prd_revision") or "")
        and str(item.get("channel_prd_ref") or "") == str(freeze.get("prd_ref") or "")
        and str(item.get("channel_prd_digest") or "").removeprefix("sha256:")
            == str(freeze.get("prd_digest") or "").removeprefix("sha256:")
    ]
    if not expected:
        return bool(relevant)
    expected_identity = tuple(str(expected.get(key) or "") for key in (
        "workflow_run_id", "task_id", "prd_ref", "prd_digest",
    ))
    identities = {
        tuple(str(item.get(key) or "") for key in (
            "workflow_run_id", "task_id", "prd_ref", "prd_digest",
        ))
        for item in relevant
    }
    return identities != {expected_identity}


def _evidence_fingerprint(context: dict[str, Any]) -> str:
    # Remove only fields at the queue/packet rubric boundary.  In particular,
    # keep digests inside PRD/source snapshots and all workflow-bound bodies.
    comparable = dict(context)
    for field in ("trial_id", "source_snapshot_digest"):
        comparable.pop(field, None)
    packets = {}
    for stage, source in context["packets"].items():
        packet = dict(source)
        for field in ("trial_id", "source_snapshot_digest"):
            packet.pop(field, None)
        details = dict(packet.get("task_details") or {})
        details.pop("evaluation_role_catalog", None)
        details.pop("evaluation_role_catalog_ref", None)
        packet["task_details"] = details
        packets[stage] = packet
    comparable["packets"] = packets
    return stable_digest(comparable)


def supersede_rubric_duplicates(
    state_dir: Path, scheduler: EvaluationScheduler, current: dict[str, Any],
) -> None:
    """Recover only equivalent material under the first admitted rubric.

    Workflow bindings retain their own fidelity/conclusion identities.  If a
    duplicate differs in anything beyond rubric-derived identity, recovery
    fails closed and the observer's trial gate prevents provider dispatch.
    """
    from zf.runtime.evaluation_observer import _load_queue_context

    rows = [
        entry for entry in scheduler.entries.values()
        if entry.channel_id == current["channel_id"]
        and entry.freeze_event_id == current["freeze"]["event_id"]
        and not entry.evaluation_origin
    ]
    # Normal scans (including legitimate workflow binds) have one classifier.
    if sum(entry.judge_type == "task_type" for entry in rows) < 2:
        return
    # Recovery may have persisted the classifier marker before a crash.
    # Keep using the original admission when only its remaining stages need
    # convergence; counting only live classifiers would miss those rows.
    rows = [entry for entry in rows if not entry.superseded_by]
    contexts: dict[str, dict[str, Any]] = {}
    for entry in rows:
        contexts[entry.job_id] = _load_queue_context(state_dir, entry)
    fingerprints = {
        ref: _evidence_fingerprint(context)
        for ref, context in {entry.input_ref: contexts[entry.job_id] for entry in rows}.items()
    }
    canonical = {
        (entry.judge_type, fingerprints[entry.input_ref]): entry.job_id
        for entry in rows if contexts[entry.job_id]["trial_id"] == current["trial_id"]
    }
    # Binding reuses three jobs whose input_ref still names the pre-bind
    # context. The freshly validated context proves their unchanged identity.
    current_fingerprint = _evidence_fingerprint(current)
    for stage in (*current["packets"], "conclusion"):
        packet = current["packets"].get(stage, current)
        canonical[(stage, current_fingerprint)] = evaluation_request_id(
            workflow_run_id=current["workflow_run_id"], trial_id=packet["trial_id"],
            judge_type=stage, source_snapshot_digest=packet["source_snapshot_digest"],
        )
    mapping = {}
    for entry in rows:
        context = contexts[entry.job_id]
        if context["trial_id"] == current["trial_id"]:
            continue
        target = canonical.get((entry.judge_type, fingerprints[entry.input_ref]))
        if target is None:
            raise ValueError("duplicate evaluation differs in frozen evidence or workflow binding")
        mapping[entry.job_id] = target
    if mapping and not scheduler.supersede_jobs(mapping):
        raise ValueError("evaluation duplicate recovery rejected conflicting queue identity")
