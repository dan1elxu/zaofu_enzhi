"""Cancellation must stop a provider turn, discard its queue, and allow reuse."""

from pathlib import Path
import asyncio
import threading

from zf.core.config.schema import RoleConfig
from zf.core.state.role_sessions import RoleSessionRegistry
from zf.runtime.transport_stream_json import StreamJsonTransport


def test_terminate_stops_active_turn_and_queue_without_stopping_other_role(tmp_path: Path):
    started = {name: threading.Event() for name in ("dev", "qa")}
    stopped = {name: threading.Event() for name in ("dev", "qa")}
    release = threading.Event()
    calls: list[str] = []

    async def query(*, prompt, options):
        calls.append(prompt)
        if prompt == "reused":
            release.set()
            return
        try:
            started[prompt].set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        finally:
            stopped[prompt].set()
        if False:
            yield None

    transport = StreamJsonTransport(
        tmp_path,
        RoleSessionRegistry(tmp_path / "sessions.yaml", project_root=str(tmp_path)),
        query_fn=query,
        background_dispatch=True,
    )
    for name in started:
        transport.spawn(RoleConfig(name=name), argv=[])
        transport.send_task(name, tmp_path / "briefing.md", name)
    try:
        assert all(event.wait(2) for event in started.values())
        transport.send_task("dev", tmp_path / "briefing.md", "queued")
        transport.terminate("dev")
        assert stopped["dev"].wait(0.5), "terminate returned with provider still running"
        assert not stopped["qa"].is_set(), "another run's provider was stopped"
        assert "queued" not in calls
        assert not transport.is_alive("dev")
        assert not any(event.type == "agent.api_blocked" for event in transport.poll_events())
        transport.spawn(RoleConfig(name="dev"), argv=[])
        transport.send_task("dev", tmp_path / "briefing.md", "reused")
        assert release.wait(2), "terminated role cannot accept a new dispatch"
    finally:
        release.set()
        transport.shutdown()
