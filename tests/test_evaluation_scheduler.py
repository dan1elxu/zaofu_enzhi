"""Durable queue contracts for the canonical five-stage evaluator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, EvaluationScheduler
from zf.runtime.evaluation_packets import evaluation_request_id


def _job(stage: str) -> str:
    return evaluation_request_id(
        workflow_run_id="wf", trial_id="trial", judge_type=stage,
        source_snapshot_digest="d" * 64,
    )


def _entry(stage: str, *, deps: list[str] | None = None) -> EvaluationQueueEntry:
    trial_id = "trial"
    digest = "d" * 64
    job_id = _job(stage)
    return EvaluationQueueEntry(
        job_id=job_id, channel_id="ch", workflow_run_id="wf", trial_id=trial_id,
        judge_type=stage, source_snapshot_digest=digest,
        input_ref="artifacts/benchmark-evaluations/queue-inputs/input.json",
        input_digest="x", depends_on=deps or [],
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
