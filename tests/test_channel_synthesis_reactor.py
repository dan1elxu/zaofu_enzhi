"""channel.synthesis.blocked recovery reactor."""

from types import SimpleNamespace

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_synthesis_reactor import (
    react_channel_synthesis_blocked,
)
from zf.runtime.channel_synthesis_repair import (
    MAX_SYNTHESIS_REPAIR_ATTEMPTS,
    synthesis_repair_id,
)


def test_blocked_emits_one_final_repair_request(tmp_path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    host = SimpleNamespace(
        event_writer=writer,
        event_log=writer.event_log,
    )
    blocked = writer.emit(
        "channel.synthesis.blocked",
        actor="zf-kernel",
        correlation_id="ch-test",
        payload={
            "channel_id": "ch-test",
            "thread_id": "main",
            "request_id": "synth-exhaust",
            "target_member_id": "synthesizer",
            "repair_revision": MAX_SYNTHESIS_REPAIR_ATTEMPTS,
            "contract_status": "invalid_semantic_source_coverage",
            "contract_error": "semantic_source_coverage_missing:abcdef",
            "source_reply_event_id": "evt-r2",
        },
    )

    react_channel_synthesis_blocked(host, blocked)

    repairs = [
        event
        for event in writer.event_log.read_all()
        if event.type == "channel.synthesis.repair.requested"
    ]
    assert len(repairs) == 1
    payload = repairs[0].payload
    assert payload["repair_revision"] == MAX_SYNTHESIS_REPAIR_ATTEMPTS + 1
    assert payload["repair_id"] == synthesis_repair_id(
        "ch-test", "main", "synth-exhaust", MAX_SYNTHESIS_REPAIR_ATTEMPTS + 1
    )
    assert payload["recovery"] == "blocked_final_repair"
    assert payload["contract_error"] == (
        "semantic_source_coverage_missing:abcdef"
    )


def test_blocked_recovery_is_idempotent(tmp_path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    host = SimpleNamespace(
        event_writer=writer,
        event_log=writer.event_log,
    )
    blocked = writer.emit(
        "channel.synthesis.blocked",
        actor="zf-kernel",
        correlation_id="ch-test",
        payload={
            "channel_id": "ch-test",
            "thread_id": "main",
            "request_id": "synth-exhaust",
            "target_member_id": "synthesizer",
            "repair_revision": MAX_SYNTHESIS_REPAIR_ATTEMPTS,
        },
    )

    react_channel_synthesis_blocked(host, blocked)
    react_channel_synthesis_blocked(host, blocked)

    repairs = [
        event
        for event in writer.event_log.read_all()
        if event.type == "channel.synthesis.repair.requested"
    ]
    assert len(repairs) == 1


def test_blocked_without_request_identity_is_noop(tmp_path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    host = SimpleNamespace(
        event_writer=writer,
        event_log=writer.event_log,
    )
    blocked = writer.emit(
        "channel.synthesis.blocked",
        actor="zf-kernel",
        correlation_id="ch-test",
        payload={"channel_id": "ch-test", "thread_id": "main"},
    )

    react_channel_synthesis_blocked(host, blocked)

    assert not any(
        event.type == "channel.synthesis.repair.requested"
        for event in writer.event_log.read_all()
    )
