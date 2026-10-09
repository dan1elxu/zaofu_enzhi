"""Channel creation handlers must never overwrite an existing channel."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_projection import project_channel
from zf.runtime.control_actions import ControlledActionService


def test_direct_create_conflicts_and_replays_without_overwriting(tmp_path: Path):
    state_dir = tmp_path / "state"
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    service = ControlledActionService(state_dir, writer, project_root=tmp_path)

    def create(name: str, key: str):
        payload = {"name": name, "channel_id": "ch-collision", "idempotency_key": key}
        requested = writer.emit("web.action.requested", actor="web", payload={"action": "channel-create", "request": payload})
        return service.execute(action="channel-create", requested_action="channel-create", payload=payload, requested=requested)

    first = create("Original", "first")
    assert first["ok"]
    replay = create("Original", "first")
    assert replay["ok"]
    assert replay["event_id"] == first["event_id"]
    for name, key in [("Overwrite", "second"), ("Overwrite", "first"), ("Original", "third")]:
        collision = create(name, key)
        assert collision["_status_code"] == 409
    assert project_channel(state_dir, "ch-collision")["name"] == "Original"
    assert len([event for event in writer.event_log.read_all() if event.type == "channel.created"]) == 1


def test_concurrent_channel_creators_cannot_both_win(tmp_path: Path):
    state_dir = tmp_path / "state"

    def create(index: int):
        writer = EventWriter(EventLog(state_dir / "events.jsonl"))
        service = ControlledActionService(state_dir, writer, project_root=tmp_path)
        payload = {"name": f"Creator {index}", "channel_id": "ch-race"}
        requested = writer.emit("web.action.requested", actor="web", payload={"action": "channel-create", "request": payload})
        return service.execute(action="channel-create", requested_action="channel-create", payload=payload, requested=requested)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sorted(result["_status_code"] for result in results) == [202, 409]
    events = EventLog(state_dir / "events.jsonl").read_all()
    assert len([event for event in events if event.type == "channel.created"]) == 1
