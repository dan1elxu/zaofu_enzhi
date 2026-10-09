from types import SimpleNamespace

import pytest

from zf.core.events import EventLog, EventWriter

from tests.test_channel_adapter_parallel_dispatch import CH, _seed
from zf.runtime import channel_adapter
from zf.runtime.channel_projection import project_channel


def test_late_provider_reply_does_not_overwrite_newer_message(tmp_path, monkeypatch):
    state, writer = _seed(tmp_path, members=1, backend="codex")
    def run(**kwargs):
        writer.emit("channel.agent.reply.requested", actor="runtime", payload={
            **kwargs["request"], "channel_id": CH, "request_id": "req-0", "run_generation": 2,
        })
        writer.emit("channel.agent.reply.started", actor="runtime", payload={
            **kwargs["request"], "channel_id": CH, "request_id": "req-0", "run_generation": 2,
        })
        writer.emit("channel.message.posted", actor="m-0", payload={
            "channel_id": CH, "thread_id": "main", "message_id": "msg-req-0-reply",
            "member_id": "m-0", "role": "assistant", "text": "current reply",
            "refs": {"request_id": "req-0", "run_generation": 2},
        })
        writer.emit("channel.agent.reply.completed", actor="runtime", payload={
            **kwargs["request"], "channel_id": CH, "request_id": "req-0", "run_generation": 2,
        })
        return SimpleNamespace(ok=True, reply="obsolete reply", backend="codex-headless")
    monkeypatch.setattr(channel_adapter, "_run_headless_reply", run)
    channel_adapter.dispatch_reply_request(
        state_dir=state, writer=writer, channel_id=CH, request_id="req-0",
        actor="test", source="test", project_root=tmp_path,
    )
    view = project_channel(state, CH)
    reply = next(m for m in view["messages"] if m["message_id"] == "msg-req-0-reply")
    assert reply["text"] == "current reply"
    assert view["reply_requests"][0]["run_generation"] == 2
    assert view["reply_requests"][0]["status"] == "completed"


@pytest.mark.parametrize("late_generation", [1, 3])
def test_projection_replay_keeps_current_body_when_old_or_unrequested_body_arrives(tmp_path, late_generation):
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit("channel.agent.reply.requested", actor="runtime", payload={
        "channel_id": "ch", "request_id": "req", "run_generation": 2,
    })
    body = {"channel_id": "ch", "thread_id": "main", "message_id": "reply", "role": "assistant",
            "text": "current", "refs": {"request_id": "req", "run_generation": 2}}
    writer.emit("channel.message.posted", actor="member", payload=body)
    writer.emit("channel.message.posted", actor="member", payload={
        **body, "text": "late", "refs": {"request_id": "req", "run_generation": late_generation},
    })
    view = project_channel(tmp_path, "ch")
    assert [m["text"] for m in view["messages"]] == ["current"]


def test_current_dispatch_binds_message_generation_without_filtering_user_quotes(tmp_path, monkeypatch):
    state, writer = _seed(tmp_path, members=1, backend="codex")
    monkeypatch.setattr(channel_adapter, "_run_headless_reply", lambda **kwargs:
                        SimpleNamespace(ok=True, reply="current", backend="codex-headless"))
    result = channel_adapter.dispatch_reply_request(
        state_dir=state, writer=writer, channel_id=CH, request_id="req-0",
        actor="test", source="test", project_root=tmp_path,
    )
    assert result.completed == ["req-0"]
    writer.emit("channel.message.posted", actor="web", payload={
        "channel_id": CH, "thread_id": "main", "message_id": "user-quote", "role": "user",
        "text": "Quoted previous attempt", "refs": {"request_id": "req-0", "run_generation": 0},
    })
    rows = {m["message_id"]: m for m in project_channel(state, CH)["messages"]}
    assert rows["msg-req-0-reply"]["refs"]["run_generation"] == 1
    assert rows["user-quote"]["text"] == "Quoted previous attempt"
