"""Canonical freeze identity and artifact validation regressions."""
from __future__ import annotations

from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_consensus_identity import consensus_artifact_error
from zf.runtime.channel_discussion import advance_discussion
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_prd_freeze import emit_prd_frozen

CH = "ch-freeze"


def test_prd_freeze_carries_identity_bound_source_snapshot(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    writer = EventWriter(log)
    message = writer.emit("channel.message.posted", actor="agent", payload={
        "channel_id": "ch", "thread_id": "main", "message_id": "m1", "text": "requirement",
    })
    reached = writer.emit("channel.consensus.reached", actor="kernel", payload={
        "channel_id": "ch", "thread_id": "main", "prd_ref": "channels/ch/prd/r1.json",
        "prd_digest": "a" * 64, "prd_revision": 1,
        "readiness_ref": "channels/ch/prd/r1-readiness.json", "readiness_digest": "b" * 64,
        "source_refs": ["event:requirement"], "source": "test",
    })
    frozen = emit_prd_frozen(
        writer, consensus_payload=reached.payload, source_event=reached,
    )
    snapshot = frozen.payload["source_snapshot"]
    assert frozen.payload["source_snapshot_digest"]
    assert snapshot["channel_id"] == "ch"
    assert snapshot["thread_id"] == "main"
    assert snapshot["revision"] == 1
    assert [item["event_id"] for item in snapshot["events"]] == [message.id, reached.id]


def test_malformed_digest_never_bypasses_freeze(tmp_path: Path):
    assert consensus_artifact_error(tmp_path, {
        "prd_revision": 1, "artifact_ref": "channels/ch/prd/r1.json",
        "artifact_digest": "invalid",
    }) == "prd:digest_invalid"


@pytest.mark.parametrize("revision", [True, 0, 1.0, 1.5, "01", " 1", "bad"])
def test_noncanonical_consensus_revision_fails_closed(
    tmp_path: Path, revision: object,
) -> None:
    assert consensus_artifact_error(tmp_path, {
        "prd_revision": revision,
        "artifact_ref": "channels/ch/prd/r1.json",
        "artifact_digest": "a" * 64,
    }) == "prd:revision_invalid"


def test_malformed_consensus_revision_rejects_without_projection_crash(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    writer.emit(
        "channel.discussion.started",
        actor="test",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "roster": ["agent-1"],
            "synthesizer": "agent-1",
        },
    )
    writer.emit(
        "channel.discussion.phase.changed",
        actor="test",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "phase": "phase3_synthesis",
            "source": "test",
        },
    )
    writer.emit(
        "channel.member.invited",
        actor="test",
        payload={
            "channel_id": CH,
            "member_id": "agent-1",
            "member_type": "persona_agent",
            "channel_role": "dev",
        },
    )
    writer.emit(
        "channel.consensus.proposed",
        actor="agent-1",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "artifact_ref": "channels/ch/prd/r1.json",
            "artifact_digest": "a" * 64,
            "prd_revision": "bad",
            "required_signers": ["agent-1"],
            "product_mode": "multi_lens",
        },
    )
    writer.emit(
        "channel.consensus.signed",
        actor="agent-1",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "member_id": "agent-1",
        },
    )
    writer.emit(
        "channel.consensus.signed",
        actor="operator",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "member_id": "operator",
        },
    )

    emitted = advance_discussion(
        state_dir, writer, channel_id=CH, thread_id="main",
    )
    assert emitted == ["channel.consensus.review.rejected"]
    events = EventLog(state_dir / "events.jsonl").read_all()
    assert not any(event.type == "channel.consensus.reached" for event in events)
    rejection = next(
        event for event in events if event.type == "channel.consensus.review.rejected"
    )
    assert rejection.payload["reason"] == "consensus_artifact_invalid:prd:revision_invalid"
