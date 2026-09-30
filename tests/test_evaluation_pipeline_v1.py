"""Focused contract checks for the unified Channel/PRD evaluation route."""

from __future__ import annotations

import json
from pathlib import Path

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer
from zf.runtime.channel_prd_freeze import emit_prd_frozen
from zf.runtime.evaluation_scheduler import EvaluationScheduler
from tests.evaluation_fixtures import _seed_channel_state


def _emit_unified_freeze(state_dir: Path) -> None:
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    consensus = next(
        event for event in writer.event_log.read_all()
        if event.type == "channel.consensus.reached"
    )
    emit_prd_frozen(
        writer,
        consensus_payload=consensus.payload,
        source_event=consensus,
        freeze_reason="test",
    )


def _emit_production_inline_freeze(state_dir: Path) -> None:
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    consensus = next(
        event for event in writer.event_log.read_all()
        if event.type == "channel.consensus.reached"
    )
    emit_prd_frozen(
        writer,
        consensus_payload=consensus.payload,
        source_event=consensus,
        freeze_reason="test-production-path",
    )


def test_unified_freeze_queues_classifier_and_three_isolated_subagents(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path, emit_prd_freeze=False)
    _emit_unified_freeze(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        stats = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, dispatch=False,
        )
        assert stats["errors"] == []
        assert stats["queued"] == 5
        entries = scheduler.entries
        assert [entry.judge_type for entry in entries.values()] == [
            "task_type", "contribution", "discussion", "prd_fidelity", "conclusion",
        ]
        classifier = next(entry for entry in entries.values() if entry.judge_type == "task_type")
        for stage in ("contribution", "discussion", "prd_fidelity"):
            entry = next(item for item in entries.values() if item.judge_type == stage)
            assert entry.depends_on == [classifier.job_id]
        conclusion = next(entry for entry in entries.values() if entry.judge_type == "conclusion")
        assert set(conclusion.depends_on) == {
            item.job_id for item in entries.values()
            if item.judge_type in {"contribution", "discussion", "prd_fidelity"}
        }

        queue_input = next((tmp_path / "artifacts/benchmark-evaluations/queue-inputs").glob("*.json"))
        payload = json.loads(queue_input.read_text(encoding="utf-8"))
        assert payload["evaluation_contract"] == "channel-prd-eval.v1"
        packets = payload["packets"]
        assert set(packets) == {"task_type", "contribution", "discussion", "prd_fidelity"}
        assert packets["task_type"]["task_details"]["evaluation_role_catalog"]["catalog_digest"]
        assert packets["task_type"]["task_details"]["discussion_events"]
        for stage in ("contribution", "discussion", "prd_fidelity"):
            assert packets[stage]["prior_judges"] == []
            assert [item["agent_name"] for item in packets[stage]["agent_profiles"]] == [
                "Agent m1", "Agent m2",
            ]
    finally:
        scheduler.close()


def test_production_inline_freeze_queues_evaluation(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path, emit_prd_freeze=False)
    _emit_production_inline_freeze(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        stats = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, dispatch=False,
        )
        assert stats["errors"] == []
        assert stats["queued"] == 5
    finally:
        scheduler.close()


def test_consensus_reached_alone_does_not_start_role_evaluation(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path, emit_prd_freeze=False)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        stats = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, dispatch=False,
        )
        assert stats["errors"] == []
        assert stats["channels_seen"] == 0
        assert stats["queued"] == 0
        assert scheduler.entries == {}
    finally:
        scheduler.close()


def test_receipt_alone_does_not_start_role_evaluation(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path, freeze=False, receipt_kind="other")
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        stats = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, dispatch=False,
        )
        assert stats["errors"] == []
        assert stats["channels_seen"] == 0
        assert stats["queued"] == 0
        assert scheduler.entries == {}
    finally:
        scheduler.close()
