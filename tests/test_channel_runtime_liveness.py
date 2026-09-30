"""Slow synthesis must not occupy the watcher or lose its live generation."""

from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from tests.channel_dispatch_fixture import channel_dispatches
from zf.core.config.schema import ProjectConfig, RoleConfig, SessionConfig, ZfConfig
from zf.core.events import EventLog, EventWriter, ZfEvent
from zf.core.state.session import SessionStore
from zf.runtime import channel_adapter
from zf.runtime.channel_reply_remediation import remediate_channel_replies
from zf.runtime.orchestrator import Orchestrator
from zf.runtime.tmux import TmuxSession
from zf.runtime.transport import TmuxTransport


def test_slow_synthesis_keeps_other_channel_and_tick_live(tmp_path, monkeypatch, channel_dispatches):
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    SessionStore(state_dir / "session.yaml").create(project_root=str(tmp_path))
    (state_dir / "kanban.json").write_text("[]\n")
    log = EventLog(state_dir / "events.jsonl")
    writer = EventWriter(log)
    writer.append(ZfEvent(type="loop.started", actor="test"))
    config = ZfConfig(project=ProjectConfig(name="test"),
                      session=SessionConfig(tmux_session="test"),
                      roles=[RoleConfig(name="dev", backend="mock")])
    orch = Orchestrator(state_dir, config, TmuxTransport(TmuxSession(session_name="test", dry_run=True)))
    requests = []
    for member in ("slow", "fast"):
        writer.emit("channel.created", actor="test", payload={"channel_id": member, "name": member})
        writer.emit("channel.member.added", actor="test", payload={
            "channel_id": member, "member_id": member, "backend": "fake",
            "member_type": "provider_agent", "permissions": ["read", "message"],
        })
        requests.append(writer.emit("channel.synthesis.requested", actor="test", payload={
            "channel_id": member, "request_id": f"synth-{member}", "target_member_id": member,
        }))
    slow_started, fast_started, release = Event(), Event(), Event()

    def provider(member, message, pack):
        if member["member_id"] == "slow":
            slow_started.set()
            assert release.wait(8), "test failed to release provider"
        else:
            fast_started.set()
        return "review complete"

    monkeypatch.setattr(channel_adapter, "build_fake_channel_reply", provider)
    tick = Thread(target=orch.run_once, kwargs={"events": requests}, daemon=True)
    try:
        tick.start()
        assert slow_started.wait(3)
        assert fast_started.wait(3), "another channel was blocked by slow synthesis"
        tick.join(3)
        assert not tick.is_alive(), "watcher waited for a provider turn"
        # A live long turn exceeds the 15-minute stale heuristic; it must
        # retain its generation so its eventual result remains admissible.
        outcome = remediate_channel_replies(
            writer, events=log.read_all(), now=datetime.now(timezone.utc) + timedelta(minutes=20),
        )
        assert outcome == {"redispatched": [], "exhausted": []}
        orch.run_once(events=requests)
        slow_requests = [e for e in log.read_all() if e.type == "channel.agent.reply.requested"
                         and e.payload.get("channel_id") == "slow"]
        assert len(slow_requests) == 1
        assert slow_requests[0].payload["run_generation"] == 1
    finally:
        release.set()
        tick.join(5)
        channel_dispatches()
    completed = [e for e in log.read_all() if e.type == "channel.agent.reply.completed"]
    assert {e.payload["channel_id"] for e in completed} == {"slow", "fast"}
    assert all(e.payload["run_generation"] == 1 for e in completed)


def test_abandoned_reply_is_rearmed_after_claim_is_released(tmp_path):
    from zf.core.state.locks import locked_path
    from zf.runtime.channel_dispatch_claim import channel_reply_claim_path

    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit("channel.agent.reply.requested", actor="test", payload={
        "channel_id": "ch", "request_id": "reply", "target_member_id": "member", "status": "pending",
    })
    writer.emit("channel.agent.reply.started", actor="test", payload={
        "channel_id": "ch", "request_id": "reply", "run_generation": 1,
    })
    future = datetime.now(timezone.utc) + timedelta(minutes=20)
    with locked_path(channel_reply_claim_path(tmp_path, "ch", "reply")):
        assert remediate_channel_replies(writer, events=writer.event_log.read_all(), now=future)["redispatched"] == []
    assert remediate_channel_replies(writer, events=writer.event_log.read_all(), now=future)["redispatched"] == ["reply"]
