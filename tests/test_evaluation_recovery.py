"""Regression coverage for observer-side evaluation recovery."""

from __future__ import annotations

from pathlib import Path
import json
import re
import hashlib

import pytest

from zf.runtime import evaluation_observer as observer
from zf.runtime.evaluation_scheduler import EvaluationScheduler
from zf.runtime.evaluation_artifacts import report_path, write_report
from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_projection import project_channel
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from tests.evaluation_fixtures import (
    _judge_reply,
    _seed_channel_state,
    _workflow_binding_payload,
)


def _context() -> dict:
    packets = {}
    for index, stage in enumerate(("task_type", "contribution", "discussion", "prd_fidelity")):
        digest = f"{index + 1:x}" * 64
        packets[stage] = {
            "stage": stage,
            "workflow_run_id": "run-1",
            "trial_id": f"trial-{stage}",
            "source_snapshot_digest": digest,
            "task_details": {"statement": "Do X."},
            "task": {"statement": "Do X."},
            "agent_profiles": [],
        }
    return {
        "evaluation_contract": "channel-prd-eval.v1",
        "channel_id": "ch-1",
        "workflow_run_id": "run-1",
        "trial_id": "trial-root",
        "source_snapshot_digest": packets["prd_fidelity"]["source_snapshot_digest"],
        "freeze": {"event_id": "freeze-1", "event_type": "channel.prd.frozen"},
        "packets": packets,
    }


def _record(stage: str) -> dict:
    return {
        "status": "scored" if stage != "task_type" else "scored",
        "result": {"task_type": "bugfix"} if stage == "task_type" else {},
        "request_id": f"request-{stage}",
    }


def _complete_entries(scheduler: EvaluationScheduler) -> None:
    for entry in scheduler._entries.values():  # test-only state setup
        entry.status = "completed"


def test_observer_requeues_missing_conclusion_sidecar(tmp_path: Path, monkeypatch) -> None:
    context = _context()
    records = {stage: _record(stage) for stage in context["packets"]}
    monkeypatch.setattr(observer, "read_judge_result", lambda *args, **kwargs: records[
        kwargs["judge_type"]
    ])
    monkeypatch.setattr(observer, "read_conclusion_result", lambda *args, **kwargs: None)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer._enqueue_channel(tmp_path, scheduler, context)
        _complete_entries(scheduler)
        observer._enqueue_channel(tmp_path, scheduler, context)
        conclusion = next(item for item in scheduler.entries.values() if item.judge_type == "conclusion")
        assert conclusion.status == "queued"
    finally:
        scheduler.close()


def test_missing_terminal_upstream_sidecar_uses_inconclusive_conclusion(tmp_path: Path, monkeypatch) -> None:
    context = _context()
    records = {stage: _record(stage) for stage in context["packets"]}
    records["discussion"] = None
    monkeypatch.setattr(observer, "read_judge_result", lambda *args, **kwargs: records[
        kwargs["judge_type"]
    ])
    monkeypatch.setattr(observer, "read_conclusion_result", lambda *args, **kwargs: None)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer._enqueue_channel(tmp_path, scheduler, context)
        _complete_entries(scheduler)
        discussion = next(item for item in scheduler._entries.values() if item.judge_type == "discussion")
        discussion.status = "inconclusive"
        assert observer._enqueue_channel(tmp_path, scheduler, context) == 1
        conclusion = next(item for item in scheduler.entries.values() if item.judge_type == "conclusion")
        assert conclusion.status == "queued"
    finally:
        scheduler.close()


def test_missing_inconclusive_upstream_sidecar_settles_conclusion_without_provider(
    tmp_path: Path,
) -> None:
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=_scoring_backend,
        )["errors"] == []
        discussion = next(entry for entry in scheduler._entries.values() if entry.judge_type == "discussion")
        discussion.status = "inconclusive"
        sidecar = next(
            path for path in (tmp_path / "artifacts/benchmark-evaluations/judges").rglob("*.json")
            if path.parent.name == "discussion"
        )
        sidecar.unlink()
        conclusion_sidecar = next(
            path for path in (tmp_path / "artifacts/benchmark-evaluations/conclusions").rglob("*.json")
            if path.parent.name != "attempts"
        )
        conclusion_sidecar.unlink()
        calls: list[str] = []
        observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler,
            backend=lambda **kwargs: calls.append(kwargs["judge_type"]) or _scoring_backend(**kwargs),
        )
        assert calls == []
        conclusions = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (tmp_path / "artifacts/benchmark-evaluations/conclusions").rglob("*.json")
            if path.parent.name != "attempts"
        ]
        assert any(item["status"] == "inconclusive" for item in conclusions)
    finally:
        scheduler.close()


def test_observer_refreshes_report_without_provider(tmp_path: Path, monkeypatch) -> None:
    context = _context()
    records = {stage: _record(stage) for stage in context["packets"]}
    conclusion = {
        "status": "complete",
        "result": {"conclusion_zh": "完成", "team_findings": {}},
    }
    monkeypatch.setattr(observer, "read_judge_result", lambda *args, **kwargs: records[
        kwargs["judge_type"]
    ])
    monkeypatch.setattr(observer, "read_conclusion_result", lambda *args, **kwargs: conclusion)
    refreshed = []
    monkeypatch.setattr(observer, "_refresh_report", lambda *args, **kwargs: refreshed.append(True))
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer._enqueue_channel(tmp_path, scheduler, context)
        _complete_entries(scheduler)
        refreshed.clear()
        observer._enqueue_channel(tmp_path, scheduler, context)
        assert refreshed == [True]
    finally:
        scheduler.close()


def test_corrupt_report_is_archived_before_rebuild(tmp_path: Path) -> None:
    path = report_path(tmp_path, "run-1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# broken\xff\n")
    write_report(tmp_path, "run-1", "# repaired\n", final=False)
    assert path.read_text(encoding="utf-8") == "# repaired\n"
    snapshots = list(path.parent.glob("report-corrupt-*.bin"))
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == b"# broken\xff\n"


def _scoring_backend(**kwargs):
    prompt = Path(kwargs["packet_path"]).read_text(encoding="utf-8")
    match = re.search(r"channel/message/[A-Za-z0-9._:-]+", prompt)
    message_ref = match.group(0) if match else "channel/message/evt-1"
    return _judge_reply(
        kwargs["judge_type"], message_ref,
        with_bound="artifact/prd/workflow-bound.json" in prompt,
    ), ""


@pytest.mark.parametrize("damage", ["missing", "json", "utf8"])
def test_observe_once_recovers_only_damaged_conclusion_and_rebuilds_report(
    tmp_path: Path, damage: str,
) -> None:
    """Exercise the freeze -> observer -> queue caller, not just helpers."""

    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        first = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=_scoring_backend,
        )
        assert first["errors"] == []
        conclusion_entry = next(
            entry for entry in scheduler.entries.values() if entry.judge_type == "conclusion"
        )
        run_id = conclusion_entry.workflow_run_id
        conclusion_paths = list(
            path for path in (tmp_path / "artifacts/benchmark-evaluations/conclusions").rglob("*.json")
            if path.parent.name != "attempts"
        )
        assert len(conclusion_paths) == 1
        if damage == "missing":
            conclusion_paths[0].unlink()
        else:
            conclusion_paths[0].write_bytes(b"{" if damage == "json" else b"\xff")

        calls: list[str] = []

        def conclusion_only_backend(**kwargs):
            calls.append(kwargs["judge_type"])
            return _scoring_backend(**kwargs)

        recovered = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=conclusion_only_backend,
        )
        assert recovered["errors"] == []
        assert calls == ["conclusion"]
        assert len([
            path for path in (tmp_path / "artifacts/benchmark-evaluations/conclusions").rglob("*.json")
            if path.parent.name != "attempts"
        ]) == 1

        report = report_path(tmp_path, run_id)
        report.unlink()
        calls.clear()
        refreshed = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=conclusion_only_backend,
            dispatch=False,
        )
        assert refreshed["queued"] == 0
        assert calls == []
        assert report.read_text(encoding="utf-8").startswith("# PRD Evaluation Report")

        report.write_bytes(b"# corrupt\xff\n")
        calls.clear()
        rebuilt = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=conclusion_only_backend,
            dispatch=False,
        )
        assert rebuilt["queued"] == 0
        assert calls == []
        assert report.read_text(encoding="utf-8").startswith("# PRD Evaluation Report")
        assert list(report.parent.glob("report-corrupt-*.bin"))
    finally:
        scheduler.close()


def test_workflow_bind_hides_old_conclusion_even_after_late_fidelity_completion(
    tmp_path: Path,
) -> None:
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=_scoring_backend,
        )["errors"] == []
        old_fidelity = next(
            entry for entry in scheduler.entries.values() if entry.judge_type == "prd_fidelity"
        )
        writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
        bound_ref = "workflow-artifacts/ch-1/bound-prd.md"
        bound_body = b"# Workflow-bound PRD\n\nDo X with bound details.\n"
        (tmp_path / bound_ref).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / bound_ref).write_bytes(bound_body)
        bound_payload = _workflow_binding_payload(tmp_path)
        bound_payload.update(
            prd_ref=bound_ref,
            prd_digest=hashlib.sha256(bound_body).hexdigest(),
        )
        requested = writer.emit(
            "workflow.invoke.requested", actor="test",
            payload=bound_payload,
        )
        writer.emit("workflow.invoke.accepted", actor="kernel", payload={"source_event_id": requested.id})

        rebound = observer.observe_once(state_dir=tmp_path, scheduler=scheduler, dispatch=False)
        assert rebound["errors"] == []
        fidelity_entries = [
            entry for entry in scheduler.entries.values() if entry.judge_type == "prd_fidelity"
        ]
        current_fidelity = next(
            entry for entry in fidelity_entries
            if entry.source_snapshot_digest != old_fidelity.source_snapshot_digest
        )
        current = project_channel(tmp_path, "ch-1")["evaluation"]
        assert any(
            row["judge_type"] == "prd_fidelity"
            and row["source_snapshot_digest"] == current_fidelity.source_snapshot_digest
            for row in current["stages"]
        )
        assert all(
            row["judge_type"] != "conclusion" or row["status"] != "complete"
            for row in current["stages"]
        )

        # A stale completion is still in the ledger for audit, but cannot
        # advance the public generation selected by the new queue row.
        writer.emit(
            "benchmark.evaluation.judge.completed", actor="evaluation-runner",
            payload={
                "evaluation_contract": "channel-prd-eval.v1",
                "prompt_version": EVALUATION_PROMPT_VERSION,
                "channel_id": "ch-1", "job_id": old_fidelity.job_id,
                "request_id": old_fidelity.job_id, "workflow_run_id": old_fidelity.workflow_run_id,
                "trial_id": old_fidelity.trial_id, "judge_type": "prd_fidelity",
                "source_snapshot_digest": old_fidelity.source_snapshot_digest,
                "freeze_event_id": old_fidelity.freeze_event_id, "status": "scored",
            },
        )
        current = project_channel(tmp_path, "ch-1")["evaluation"]
        fidelity = next(row for row in current["stages"] if row["judge_type"] == "prd_fidelity")
        assert fidelity["source_snapshot_digest"] == current_fidelity.source_snapshot_digest

        calls: list[str] = []

        def rebound_backend(**kwargs):
            calls.append(kwargs["judge_type"])
            return _scoring_backend(**kwargs)

        assert observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=rebound_backend,
        )["errors"] == []
        assert calls == ["prd_fidelity", "conclusion"]
        current = project_channel(tmp_path, "ch-1")["evaluation"]
        conclusion = next(row for row in current["stages"] if row["judge_type"] == "conclusion")
        assert conclusion["status"] == "complete"
        assert conclusion["source_snapshot_digest"] == current_fidelity.source_snapshot_digest
    finally:
        scheduler.close()


def test_exhausted_invalid_conclusion_sidecar_never_restarts_provider(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=_scoring_backend,
        )["errors"] == []
        path = next(
            path for path in (tmp_path / "artifacts/benchmark-evaluations/conclusions").rglob("*.json")
            if path.parent.name != "attempts"
        )
        original = json.loads(path.read_text(encoding="utf-8"))
        exhausted = {
            "request_id": original["request_id"],
            "workflow_run_id": original["workflow_run_id"],
            "judge_type": "conclusion",
            "trial_id": original["trial_id"],
            "source_snapshot_digest": original["source_snapshot_digest"],
            "attempt": 2,
            "max_attempts": 2,
            "status": "complete",
            "result": {"tampered": True},
        }
        attempt_path = path.parent / "attempts" / f"{original['request_id']}-attempt-2.json"
        attempt_path.write_text(json.dumps(exhausted), encoding="utf-8")
        path.unlink()
        calls: list[str] = []
        recovered = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler,
            backend=lambda **kwargs: calls.append(kwargs["judge_type"]) or _scoring_backend(**kwargs),
        )
        assert any("provider retry budget exhausted" in error for error in recovered["errors"])
        assert calls == []
        repaired = json.loads(path.read_text(encoding="utf-8"))
        assert repaired["status"] == "inconclusive"
        assert repaired["attempt"] == 2
        assert observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, dispatch=False,
        )["queued"] == 0
    finally:
        scheduler.close()
