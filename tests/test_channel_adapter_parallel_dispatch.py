"""Fan-out dispatch runs pending replies concurrently, not one turn at a time."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from zf.core.events import EventWriter
from zf.core.events.log import EventLog
from zf.runtime import channel_adapter
from zf.runtime.channel_adapter import dispatch_pending_replies
from zf.runtime.channel_message_ingress import dispatch_channel_replies_background
from zf.runtime.channel_dispatch_claim import (
    ChannelDispatchResult,
    with_channel_reply_dispatch_claim,
)

CH = "ch-par"


def _seed(
    tmp_path: Path,
    members: int = 3,
    *,
    backend: str = "persona",
    max_parallel_replies: int = 6,
) -> tuple[Path, EventWriter]:
    state_dir = tmp_path / ".zf"
    state_dir.mkdir()
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    for i in range(members):
        writer.emit(
            "channel.member.invited",
            actor="web",
            correlation_id=CH,
            payload={
                "channel_id": CH, "member_id": f"m-{i}",
                "member_type": (
                    "persona_agent" if backend == "persona" else "provider_agent"
                ),
                "provider": backend,
                "backend": backend,
                "permissions": ["read", "message", "summarize"],
            },
        )
    writer.emit(
        "channel.discussion.mode.set",
        actor="web",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "mode": "conversation",
            "max_parallel_replies": max_parallel_replies,
        },
    )
    writer.emit(
        "channel.message.posted", actor="web", correlation_id=CH,
        payload={"channel_id": CH, "thread_id": "main", "message_id": "m-1",
                 "member_id": "operator", "role": "user", "text": "@all go"},
    )
    for i in range(members):
        writer.emit(
            "channel.agent.reply.requested", actor="web", correlation_id=CH,
            payload={"channel_id": CH, "thread_id": "main",
                     "request_id": f"req-{i}", "message_id": "m-1",
                     "target_member_id": f"m-{i}", "status": "pending",
                     "source": "web"},
        )
    return state_dir, writer


def test_pending_replies_dispatch_concurrently(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _seed(tmp_path)
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow_dispatch(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.2)
        with lock:
            active -= 1
        return channel_adapter.ChannelDispatchResult(
            dispatched=[str(kwargs.get("request_id"))])

    monkeypatch.setattr(channel_adapter, "dispatch_reply_request", slow_dispatch)
    start = time.monotonic()
    result = dispatch_pending_replies(
        state_dir=state_dir, writer=writer, channel_id=CH,
        actor="web", source="web")
    elapsed = time.monotonic() - start
    assert sorted(result.dispatched) == ["req-0", "req-1", "req-2"]
    assert peak >= 2, "dispatches must overlap, not run one turn at a time"
    assert elapsed < 0.5, f"3 x 0.2s dispatches took {elapsed:.2f}s — still serial"


def test_dispatch_crash_closes_pending_requests_and_surfaces_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir, writer = _seed(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    monkeypatch.setattr(channel_adapter, "dispatch_candidate_waves", crash)
    result = dispatch_pending_replies(
        state_dir=state_dir,
        writer=writer,
        channel_id=CH,
        actor="web",
        source="web",
    )

    assert sorted(result.failed) == ["req-0", "req-1", "req-2"]
    events = EventLog(state_dir / "events.jsonl").read_all()
    failed = [
        event for event in events
        if event.type == "channel.agent.reply.failed"
    ]
    assert sorted(event.payload["request_id"] for event in failed) == [
        "req-0", "req-1", "req-2",
    ]
    assert all(event.payload["failure_status"] == "dispatch_exception" for event in failed)
    dispatch_failed = [
        event for event in events
        if event.type == "channel.agent.reply.dispatch_failed"
    ]
    assert dispatch_failed[-1].payload["request_ids"] == [
        "req-0", "req-1", "req-2",
    ]
    projected = channel_adapter.project_channel(state_dir, CH)
    assert projected is not None
    assert {row["status"] for row in projected["reply_requests"]} == {"failed"}


def test_dispatch_crash_does_not_close_new_or_started_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir, writer = _seed(tmp_path, members=1)
    # This request is not in the dispatch snapshot. It must remain available
    # for the next drain even when the current wave crashes.
    def crash(*args, **kwargs):
        writer.emit(
            "channel.agent.reply.requested", actor="web", correlation_id=CH,
            payload={"channel_id": CH, "thread_id": "main", "request_id": "req-new",
                     "message_id": "m-1", "target_member_id": "m-0", "status": "pending"},
        )
        raise RuntimeError("executor stopped")

    monkeypatch.setattr(channel_adapter, "dispatch_candidate_waves", crash)
    result = dispatch_pending_replies(
        state_dir=state_dir, writer=writer, channel_id=CH,
        actor="web", source="web")
    assert "req-0" in result.failed
    projected = channel_adapter.project_channel(state_dir, CH)
    rows = {row["request_id"]: row for row in projected["reply_requests"]}
    assert rows["req-new"]["status"] == "pending"


def test_background_thread_start_failure_closes_snapshot_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir, _writer = _seed(tmp_path, members=1)

    class StartFailureThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    monkeypatch.setattr(
        "zf.runtime.channel_message_ingress.threading.Thread",
        StartFailureThread,
    )
    dispatch_channel_replies_background(
        state_dir=state_dir,
        channel_id=CH,
        actor="test",
        source="test",
        project_root=tmp_path,
        config=None,
        openclaw_client=None,
        max_dispatch=2,
    )
    events = EventLog(state_dir / "events.jsonl").read_all()
    failed = [event for event in events if event.type == "channel.agent.reply.failed"]
    assert [event.payload["request_id"] for event in failed] == ["req-0"]
    assert failed[0].payload["failure_status"] == "dispatch_exception"


def test_single_candidate_stays_inline(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _seed(tmp_path, members=1)
    threads: list[str] = []

    def record_dispatch(**kwargs):
        threads.append(threading.current_thread().name)
        return channel_adapter.ChannelDispatchResult(
            dispatched=[str(kwargs.get("request_id"))])

    monkeypatch.setattr(channel_adapter, "dispatch_reply_request", record_dispatch)
    result = dispatch_pending_replies(
        state_dir=state_dir, writer=writer, channel_id=CH,
        actor="web", source="web")
    assert result.dispatched == ["req-0"]
    assert not threads[0].startswith("zf-channel-dispatch-"), \
        "single reply must not pay thread-pool overhead"


def test_same_reply_request_has_one_cross_process_dispatch_claim(
    tmp_path: Path,
) -> None:
    state_dir, writer = _seed(tmp_path, members=1)
    calls = 0
    calls_lock = threading.Lock()

    @with_channel_reply_dispatch_claim
    def slow_dispatch(**kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.2)
        return ChannelDispatchResult(
            dispatched=[str(kwargs.get("request_id"))],
        )

    kwargs = {
        "state_dir": state_dir,
        "writer": writer,
        "channel_id": CH,
        "request_id": "req-0",
        "actor": "test",
        "source": "test",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: slow_dispatch(**kwargs),
            range(2),
        ))

    assert calls == 1
    assert sum(bool(result.dispatched) for result in results) == 1
    assert any(
        item.get("reason") == "dispatch_claim_busy"
        for result in results
        for item in result.skipped
    )


def test_parallel_limit_caps_running_but_drains_every_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir, writer = _seed(
        tmp_path,
        members=5,
        backend="codex",
        max_parallel_replies=2,
    )
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow_headless(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        request = kwargs["request"]
        kwargs["writer"].emit(
            "channel.agent.reply.completed",
            actor="test-provider",
            causation_id=kwargs["started_event_id"],
            correlation_id=CH,
            payload={
                "channel_id": CH,
                "thread_id": "main",
                "request_id": request["request_id"],
                "message_id": request["message_id"],
                "target_member_id": request["target_member_id"],
                "reason": "test provider completed",
                "source": "test",
            },
        )
        with lock:
            active -= 1
        return channel_adapter.ChannelDispatchResult(
            dispatched=[request["request_id"]],
            completed=[request["request_id"]],
        )

    monkeypatch.setattr(channel_adapter, "_dispatch_headless_reply", slow_headless)
    result = dispatch_pending_replies(
        state_dir=state_dir,
        writer=writer,
        channel_id=CH,
        actor="web",
        source="web",
        max_dispatch=5,
        allow_queued=True,
    )

    assert sorted(result.completed) == [f"req-{index}" for index in range(5)]
    assert peak == 2
    detail = channel_adapter.project_channel(state_dir, CH)
    assert detail is not None
    assert {item["status"] for item in detail["reply_requests"]} == {"completed"}


def test_headless_channel_turn_never_resumes_provider_thread(tmp_path: Path) -> None:
    """2026-09-21 test_dev5: codex folds resumed thread history into literal
    "[Pasted Content N chars]" placeholders that pollute the member's
    context and leak into its reply. Channel turns carry their full context
    in the prompt (context pack + recent messages + contract), so the
    provider thread must ALWAYS be fresh — never resumed."""
    from zf.web.headless_agent import HeadlessTurnResult

    state_dir = tmp_path / ".zf"
    state_dir.mkdir()
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    writer.emit("channel.member.invited", actor="web", correlation_id=CH, payload={
        "channel_id": CH, "member_id": "m-0", "member_type": "provider_agent",
        "provider": "codex", "backend": "codex",
        "permissions": ["read", "message", "summarize"],
    })
    writer.emit("channel.discussion.mode.set", actor="web", correlation_id=CH, payload={
        "channel_id": CH, "mode": "conversation",
    })
    writer.emit("channel.message.posted", actor="web", correlation_id=CH, payload={
        "channel_id": CH, "thread_id": "main", "message_id": "m-1",
        "member_id": "operator", "role": "user", "text": "@m-0 go",
    })
    writer.emit("channel.agent.reply.requested", actor="web", correlation_id=CH, payload={
        "channel_id": CH, "thread_id": "main", "request_id": "req-0",
        "message_id": "m-1", "target_member_id": "m-0", "status": "pending",
        "source": "web",
    })

    seen: dict[str, object] = {}

    class _FakeAdapter:
        backend_id = "codex-headless"

        def available(self) -> bool:
            return True

        def run_turn(self, **kwargs: object) -> HeadlessTurnResult:
            seen["provider_session_id"] = kwargs.get("provider_session_id")
            return HeadlessTurnResult(
                ok=True, status="completed", backend=self.backend_id,
                thread_id=str(kwargs.get("thread_id") or ""),
                provider_session_id="", reply="done", messages=[], usage={},
            )

    result = channel_adapter.dispatch_reply_request(
        state_dir=state_dir,
        writer=writer,
        channel_id=CH,
        request_id="req-0",
        actor="test",
        source="test",
        project_root=tmp_path,
        headless_backends={"codex-headless": _FakeAdapter()},
    )
    assert result.completed == ["req-0"]
    assert seen["provider_session_id"] == "", (
        f"channel turns must start a fresh provider thread, got "
        f"provider_session_id={seen['provider_session_id']!r}"
    )
