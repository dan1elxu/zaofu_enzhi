"""Synthesis publication follows durable requests across adaptive passes."""
import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_reply_contract import emit_structured_reply_events


def emit_reply(state: Path, writer: EventWriter, request_id: str) -> None:
    emit_structured_reply_events(
        state_dir=state, writer=writer, channel=project_channel(state, "ch-round"),
        request={"thread_id": "main", "target_member_id": "synthesizer"},
        message={"refs": {"synthesis_request_id": request_id}},
        reply=json.dumps({"channel_synthesis": {"summary": request_id, "open_questions": []}}),
        reply_event_id=f"reply-{request_id}", actor="test", source="test",
    )


@pytest.mark.parametrize("fault", ["none", "old_late", "wrong_context", "wrong_revision"])
def test_new_pass_publishes_and_stale_requests_do_not(tmp_path: Path, fault: str):
    state = tmp_path / "state"
    writer = EventWriter(EventLog(state / "events.jsonl"))
    def emit(kind: str, **payload):
        return writer.emit(kind, actor="test", payload={"channel_id": "ch-round", "thread_id": "main", **payload})
    emit("channel.created", owner_actor_ref="web")
    emit("channel.discussion.started", revision=1, context_digest="context-1", roster=["pm", "synthesizer"])
    emit("channel.synthesis.requested", request_id="synth-1", discussion_revision=1, discussion_context_digest="context-1")
    emit_reply(state, writer, "synth-1")
    emit("channel.synthesis.requested", request_id="synth-old-late", discussion_revision=1, discussion_context_digest="context-1")
    emit("channel.discussion.continued", revision=2, context_digest="context-2", roster=["pm"])
    emit("channel.discussion.phase.changed", phase="phase3_synthesis")
    emit("channel.synthesis.requested", request_id="synth-2",
         discussion_revision=1 if fault == "wrong_revision" else 2,
         discussion_context_digest="wrong" if fault == "wrong_context" else "context-2")
    if fault == "old_late":
        emit_reply(state, writer, "synth-old-late")
    # A fresh writer models replay/restart without process-local counters.
    writer = EventWriter(EventLog(state / "events.jsonl"))
    emit_reply(state, writer, "synth-2")
    emit_reply(state, writer, "synth-2")
    events = writer.event_log.read_all()
    proposals = [e.payload for e in events if e.type == "channel.synthesis.proposed"]
    expected = ["synth-1", "synth-2"] if fault in {"none", "old_late"} else ["synth-1"]
    assert [p["request_id"] for p in proposals] == expected
    if len(expected) == 2:
        assert proposals[1]["prd_revision"] == proposals[0]["prd_revision"] + 1
    stale = [e.payload["request_id"] for e in events if e.type == "channel.synthesis.stale_ignored"]
    if fault == "old_late":
        assert stale == ["synth-old-late"]
    elif fault != "none":
        assert stale == ["synth-2"]
