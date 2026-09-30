"""Durable queue contracts for the canonical five-stage evaluator."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from zf.core.events import EventLog
from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, EvaluationScheduler
from zf.runtime.evaluation_packets import evaluation_request_id


def _job(stage: str) -> str:
    return evaluation_request_id(
        workflow_run_id="wf", trial_id="trial", judge_type=stage,
        source_snapshot_digest="d" * 64,
    )


def _entry(stage: str, *, deps: list[str] | None = None,
           trial_id: str = "trial", digest: str = "d" * 64,
           channel_id: str = "ch", workflow_run_id: str = "wf",
           freeze_event_id: str = "freeze") -> EvaluationQueueEntry:
    job_id = _job(stage)
    if trial_id != "trial" or digest != "d" * 64:
        job_id = evaluation_request_id(
            workflow_run_id=workflow_run_id, trial_id=trial_id,
            judge_type=stage, source_snapshot_digest=digest,
        )
    return EvaluationQueueEntry(
        job_id=job_id, channel_id=channel_id, workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        judge_type=stage, source_snapshot_digest=digest,
        input_ref="artifacts/benchmark-evaluations/queue-inputs/input.json",
        input_digest="x", freeze_event_id=freeze_event_id, depends_on=deps or [],
    )


def test_queue_accepts_only_canonical_stages(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert not scheduler.enqueue(_entry("retired_stage_a"))
        assert not scheduler.enqueue(_entry("retired_stage_b"))
        assert scheduler.enqueue(_entry("task_type"))
        assert set(scheduler.entries) == {_job("task_type")}
    finally:
        scheduler.close()


def test_dependencies_are_ordered_and_classifier_fail_closed(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    calls: list[str] = []
    try:
        assert scheduler.enqueue(_entry("task_type"))
        assert scheduler.enqueue(_entry("contribution", deps=[_job("task_type")]))
        assert scheduler.enqueue(_entry("discussion", deps=[_job("task_type")]))
        assert scheduler.enqueue(_entry("conclusion", deps=[_job("contribution"), _job("discussion")]))
        scheduler.tick(lambda entry, slot: calls.append(entry.judge_type) or {
            "status": "inconclusive" if entry.judge_type == "task_type" else "completed"
        })
        assert calls == ["task_type", "conclusion"]
        entries = scheduler.entries
        assert entries[_job("contribution")].status == "inconclusive"
        assert entries[_job("discussion")].status == "inconclusive"
        assert entries[_job("conclusion")].status == "completed"
    finally:
        scheduler.close()


def test_missing_and_cyclic_dependencies_settle_on_restart(tmp_path: Path):
    queue = tmp_path / "artifacts/benchmark-evaluations/scheduler.json"
    queue.parent.mkdir(parents=True)
    queue.write_text(json.dumps({
        "schema_version": "benchmark.evaluation.scheduler.v1",
        "entries": [
            {"job_id": "a", "channel_id": "ch", "workflow_run_id": "wf",
             "judge_type": "task_type", "depends_on": ["missing"]},
            {"job_id": "b", "channel_id": "ch", "workflow_run_id": "wf",
             "judge_type": "contribution", "depends_on": ["c"]},
            {"job_id": "c", "channel_id": "ch", "workflow_run_id": "wf",
             "judge_type": "discussion", "depends_on": ["b"]},
        ],
    }), encoding="utf-8")
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert scheduler.entries["a"].status == "inconclusive"
        assert scheduler.entries["b"].status == "inconclusive"
        assert scheduler.entries["c"].status == "inconclusive"
        calls: list[str] = []
        scheduler.tick(lambda entry, slot: calls.append(entry.judge_type) or {"status": "completed"})
        assert calls == []
    finally:
        scheduler.close()


def test_corrupt_queue_fails_closed(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    path = scheduler.queue_path
    scheduler.close()
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        EvaluationScheduler(tmp_path, inline=True)


def test_manual_queue_does_not_share_canonical_owner_or_dispatch(tmp_path: Path):
    canonical = EvaluationScheduler(tmp_path, inline=True)
    manual = EvaluationScheduler(
        tmp_path, inline=True,
        queue_path=tmp_path / "artifacts/benchmark-evaluations/regrades/manual/scheduler.json",
    )
    try:
        original = _entry("task_type")
        regrade = _entry("task_type")
        regrade.evaluation_origin = "manual_regrade"
        assert not canonical.enqueue(regrade)
        assert not manual.enqueue(original)
        assert canonical.enqueue(original)
        assert manual.enqueue(regrade)
        manual.tick(lambda entry, slot: {"status": "completed"})
        assert canonical.entries[original.job_id].status == "queued"
        assert manual.entries[regrade.job_id].status == "completed"
    finally:
        manual.close()
        canonical.close()


def test_canonical_restart_never_dispatches_manual_rows(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    path = scheduler.queue_path
    scheduler.close()
    entry = _entry("task_type")
    entry.evaluation_origin = "manual_regrade"
    path.write_text(json.dumps({
        "schema_version": "benchmark.evaluation.scheduler.v1", "entries": [asdict(entry)],
    }), encoding="utf-8")
    resumed = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert resumed.entries[entry.job_id].status == "inconclusive"
        calls = []
        resumed.tick(lambda entry, slot: calls.append(entry.job_id) or {"status": "completed"})
        assert calls == []
    finally:
        resumed.close()


def test_supersede_jobs_preserves_result_and_is_idempotent(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        canonical = _entry("task_type", trial_id="canonical", digest="a" * 64)
        duplicate = _entry("task_type", trial_id="duplicate", digest="b" * 64)
        assert scheduler.enqueue(canonical)
        assert scheduler.enqueue(duplicate)
        scheduler.tick(lambda entry, slot: {"status": "completed"})
        assert scheduler.entries[duplicate.job_id].status == "completed"
        before = len(EventLog(tmp_path / "events.jsonl").read_all())
        assert scheduler.supersede_jobs({duplicate.job_id: canonical.job_id})
        row = scheduler.entries[duplicate.job_id]
        assert row.superseded_by == canonical.job_id
        assert row.status == "completed"
        after = len(EventLog(tmp_path / "events.jsonl").read_all())
        assert after == before + 1
        assert not scheduler.supersede_jobs({duplicate.job_id: canonical.job_id})
        assert len(EventLog(tmp_path / "events.jsonl").read_all()) == after
    finally:
        scheduler.close()


def test_superseded_row_cannot_be_repaired_or_dispatched_after_restart(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    canonical = _entry("task_type", trial_id="canonical", digest="a" * 64)
    duplicate = _entry("task_type", trial_id="duplicate", digest="b" * 64)
    try:
        assert scheduler.enqueue(canonical)
        assert scheduler.enqueue(duplicate)
        assert scheduler.supersede_jobs({duplicate.job_id: canonical.job_id})
        replacement = EvaluationQueueEntry(**asdict(duplicate))
        replacement.status = "completed"
        assert not scheduler.enqueue(replacement, repair=True)
    finally:
        scheduler.close()
    resumed = EvaluationScheduler(tmp_path, inline=True)
    try:
        calls: list[str] = []
        resumed.tick(lambda entry, slot: calls.append(entry.job_id) or {"status": "completed"})
        assert duplicate.job_id not in calls
        assert resumed.entries[duplicate.job_id].superseded_by == canonical.job_id
    finally:
        resumed.close()


def test_supersede_jobs_rejects_cross_freeze_origin_and_chains(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        a = _entry("task_type", trial_id="a", digest="a" * 64)
        b = _entry("task_type", trial_id="b", digest="b" * 64)
        c = _entry("task_type", trial_id="c", digest="c" * 64, freeze_event_id="other")
        assert scheduler.enqueue(a)
        assert scheduler.enqueue(b)
        assert scheduler.enqueue(c)
        assert not scheduler.supersede_jobs({b.job_id: c.job_id})
        assert not scheduler.supersede_jobs({a.job_id: b.job_id, b.job_id: a.job_id})
        assert not scheduler.entries[a.job_id].superseded_by
        assert not scheduler.entries[b.job_id].superseded_by
    finally:
        scheduler.close()


def test_superseded_dependency_settles_without_provider_call(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        canonical = _entry("task_type", trial_id="canonical", digest="a" * 64)
        duplicate = _entry("task_type", trial_id="duplicate", digest="b" * 64)
        dependent = _entry("contribution", deps=[duplicate.job_id], trial_id="dep", digest="c" * 64)
        assert scheduler.enqueue(canonical)
        assert scheduler.enqueue(duplicate)
        assert scheduler.enqueue(dependent)
        assert scheduler.supersede_jobs({duplicate.job_id: canonical.job_id})
        calls: list[str] = []
        scheduler.tick(lambda entry, slot: calls.append(entry.job_id) or {"status": "completed"})
        assert duplicate.job_id not in calls
        assert dependent.job_id not in calls
        assert scheduler.entries[dependent.job_id].status == "inconclusive"
    finally:
        scheduler.close()


def test_supersede_running_row_keeps_provider_result(tmp_path: Path):
    scheduler = EvaluationScheduler(tmp_path, inline=False, max_slots=1)
    started = threading.Event()
    finished = threading.Event()
    release = threading.Event()
    duplicate = _entry("task_type", trial_id="duplicate", digest="b" * 64)
    canonical = _entry("task_type", trial_id="canonical", digest="a" * 64)
    try:
        # Insert the duplicate first so it is the row that receives the slot.
        assert scheduler.enqueue(duplicate)
        assert scheduler.enqueue(canonical)

        def runner(entry, slot):
            started.set()
            release.wait(timeout=5)
            finished.set()
            return {"status": "completed", "evidence": "provider-finished"}

        scheduler.tick(runner)
        assert started.wait(timeout=5)
        assert scheduler.entries[duplicate.job_id].status == "running"
        assert scheduler.supersede_jobs({duplicate.job_id: canonical.job_id})
        release.set()
        assert finished.wait(timeout=5)
        results: list[dict[str, object]] = []
        for _ in range(50):
            results = scheduler.tick(runner)
            if any(result.get("job_id") == duplicate.job_id for result in results):
                break
            threading.Event().wait(0.01)
        assert any(result.get("job_id") == duplicate.job_id for result in results)
        assert scheduler.entries[duplicate.job_id].status == "completed"
        assert scheduler.entries[duplicate.job_id].superseded_by == canonical.job_id
    finally:
        release.set()
        scheduler.close()
