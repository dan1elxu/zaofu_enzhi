"""Existing rubric-drift queues converge without deleting scoring evidence."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer
from zf.runtime.evaluation_admission import require_admitted_trial
from zf.runtime.channel_projection import project_channel
from zf.runtime.evaluation_roles import EvaluationRoleCatalog
from zf.runtime.evaluation_scheduler import EvaluationScheduler
from tests.evaluation_fixtures import _judge_reply, _seed_channel_state, _workflow_binding_payload


def _backend(**kwargs):
    prompt = Path(kwargs["packet_path"]).read_text(encoding="utf-8")
    ref = re.search(r"channel/message/[A-Za-z0-9._:-]+", prompt).group(0)
    return _judge_reply(kwargs["judge_type"], ref,
                        with_bound="artifact/prd/workflow-bound.json" in prompt), ""


def _enqueue_old_rubric(state_dir, scheduler, version):
    """Use the old admission path to reproduce a deployment-time catalog edit."""
    role_dir = state_dir / "test-roles"
    role_dir.mkdir(exist_ok=True)
    catalog = EvaluationRoleCatalog()
    for name in catalog.task_types:
        (role_dir / f"{name}.md").write_text(
            catalog.load(name).body + f"\n评分边界补充 {version}\n", encoding="utf-8",
        )
    events = EventLog(state_dir / "events.jsonl").read_all()
    freeze = next(iter(observer._freeze_points(events).values()))
    context = observer._prepare_channel(
        state_dir=state_dir, events=events, channel_id=freeze["channel_id"], freeze=freeze,
        bound_workflows=observer._bound_workflows(events).get(freeze["channel_id"], []),
        role_catalog=EvaluationRoleCatalog(role_dir).snapshot(),
    )
    assert observer._enqueue_channel(state_dir, scheduler, context) == 5
    return context


def test_existing_five_batches_recover_once_and_preserve_all_results(tmp_path):
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        first = observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
        assert first["errors"] == []
        original_ids = set(scheduler.entries)
        original_view = project_channel(tmp_path, "ch-1")["evaluation"]
        original_report = next((tmp_path / "artifacts/benchmark-evaluations/reports").rglob("report.md"))
        original_report_bytes = original_report.read_bytes()
        _enqueue_old_rubric(tmp_path, scheduler, 1)
        # One accidental batch has also completed and overwritten the report.
        scheduler.tick(lambda entry, slot: observer._evaluate_stage(
            state_dir=tmp_path, project_root=None, config=None, entry=entry,
            slot_id=slot, backend=_backend, log=lambda _: None,
        ))
        for version in range(2, 5):
            _enqueue_old_rubric(tmp_path, scheduler, version)
        assert len(scheduler.entries) == 25
        evidence = {
            path: path.read_bytes()
            for folder in ("judges", "conclusions", "queue-inputs")
            for path in (tmp_path / "artifacts/benchmark-evaluations" / folder).rglob("*.json")
        }
        old_states = {key: entry.status for key, entry in scheduler.entries.items()}
    finally:
        scheduler.close()

    for _ in range(2):
        resumed = EvaluationScheduler(tmp_path, inline=True)
        try:
            before = EventLog(tmp_path / "events.jsonl").read_all()
            calls = []
            stats = observer.observe_once(
                state_dir=tmp_path, scheduler=resumed,
                backend=lambda **kwargs: calls.append(kwargs["judge_type"]) or _backend(**kwargs),
            )
            assert stats["errors"] == []
            assert stats["queued"] == 0 and calls == []
            entries = resumed.entries
            assert set(entries) == set(old_states)
            assert {key: entry.status for key, entry in entries.items()} == old_states
            assert {key for key, entry in entries.items() if not entry.superseded_by} == original_ids
            assert all(entry.superseded_by in original_ids for key, entry in entries.items()
                       if key not in original_ids)
            assert {path: path.read_bytes() for path in evidence} == evidence
            assert original_report.read_bytes() == original_report_bytes
            assert project_channel(tmp_path, "ch-1")["evaluation"] == original_view
            events = EventLog(tmp_path / "events.jsonl").read_all()
            assert sum(bool(event.payload.get("superseded_by")) for event in events) == 20
            if _:
                assert events == before
        finally:
            resumed.close()


@pytest.mark.parametrize("drift_after_bind", [False, True])
def test_recovery_preserves_later_workflow_binding(tmp_path, drift_after_bind):
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
        original_ids = set(scheduler.entries)
        if not drift_after_bind:
            _enqueue_old_rubric(tmp_path, scheduler, 1)
        writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
        request = writer.emit("workflow.invoke.requested", actor="test",
                              payload=_workflow_binding_payload(tmp_path))
        writer.emit("workflow.invoke.accepted", actor="kernel", payload={"source_event_id": request.id})
        if drift_after_bind:
            _enqueue_old_rubric(tmp_path, scheduler, 1)
        calls = []
        stats = observer.observe_once(state_dir=tmp_path, scheduler=scheduler,
            backend=lambda **kwargs: calls.append(kwargs["judge_type"]) or _backend(**kwargs))
        assert stats["errors"] == [] and stats["queued"] == 2
        assert calls == ["prd_fidelity", "conclusion"]
        assert len(scheduler.entries) == 12
        assert len([entry for entry in scheduler.entries.values() if entry.superseded_by]) == 5
        assert all(not scheduler.entries[key].superseded_by for key in original_ids)
    finally:
        scheduler.close()


def test_recovery_finishes_partial_supersession_after_classifier_was_retired(tmp_path):
    """A crash after classifier supersession must still retire quality duplicates."""
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
        original_ids = set(scheduler.entries)
        _enqueue_old_rubric(tmp_path, scheduler, 1)
        duplicate_classifier = next(
            entry for job_id, entry in scheduler.entries.items()
            if job_id not in original_ids and entry.judge_type == "task_type"
        )
        original_classifier = next(
            entry for job_id, entry in scheduler.entries.items()
            if job_id in original_ids and entry.judge_type == "task_type"
        )
        assert scheduler.supersede_jobs({duplicate_classifier.job_id: original_classifier.job_id})
        assert any(
            job_id not in original_ids and entry.judge_type == "contribution"
            and not entry.superseded_by
            for job_id, entry in scheduler.entries.items()
        )

        stats = observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
        assert stats["errors"] == []
        assert all(
            entry.superseded_by
            for job_id, entry in scheduler.entries.items()
            if job_id not in original_ids
        )
    finally:
        scheduler.close()


def test_admitted_packet_resumes_when_live_freeze_sidecar_is_unreadable(tmp_path):
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        first = observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
        assert first["errors"] == []
        (tmp_path / "channel-artifacts/ch-1/readiness.json").unlink()
        resumed = observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
        assert resumed["errors"] == []
        assert resumed["queued"] == 0
    finally:
        scheduler.close()


@pytest.mark.parametrize("field, value", [
    ("workflow_run_id", "wrong-run"),
    ("trial_id", "wrong-trial"),
    ("source_snapshot_digest", "f" * 64),
    ("job_id", "wrong-job"),
])
def test_admission_receipt_rejects_identity_mismatch(tmp_path, field, value):
    """A same-freeze ledger receipt cannot admit a different evaluator trial."""
    _seed_channel_state(tmp_path)
    event_log = EventLog(tmp_path / "events.jsonl")
    events = event_log.read_all()
    freeze = next(iter(observer._freeze_points(events).values()))
    context = observer._prepare_channel(
        state_dir=tmp_path,
        events=events,
        channel_id=freeze["channel_id"],
        freeze=freeze,
        bound_workflows=[],
    )
    assert context is not None
    packet = context["packets"]["task_type"]
    job_id = observer._request_id(
        context["workflow_run_id"], packet["trial_id"], "task_type",
        packet["source_snapshot_digest"],
    )
    payload = {
        "job_id": job_id,
        "channel_id": context["channel_id"],
        "workflow_run_id": context["workflow_run_id"],
        "freeze_event_id": context["freeze"]["event_id"],
        "judge_type": "task_type",
        "trial_id": packet["trial_id"],
        "source_snapshot_digest": packet["source_snapshot_digest"],
        "prompt_version": observer.EVALUATION_PROMPT_VERSION,
    }
    payload[field] = value
    EventWriter(event_log).emit("benchmark.evaluation.progress", actor="test", payload=payload)

    with pytest.raises(ValueError, match="not dispatched"):
        require_admitted_trial(tmp_path, context)
