"""A3 eval lane launchers: terminal/kanban via fake headless CLI, channel via fake provider."""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

from zf.core.events import EventLog, EventWriter
from zf.runtime import channel_moderator, eval_lanes
from zf.runtime.channel_dispatch_claim import ChannelDispatchResult
from zf.runtime.eval_lanes import (
    canonical_lane_id,
    lane_prd_path,
    run_channel_lane,
    run_kanban_lane,
    run_lane,
    run_terminal_lane,
)


def _write_fake_claude(tmp_path: Path, *, write_prd: bool = True, sleep_s: float = 0.0) -> str:
    script = tmp_path / "fake_claude_headless.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json
            import sys
            import time

            time.sleep({sleep_s!r})
            line = sys.stdin.readline()
            prompt = ""
            try:
                data = json.loads(line or "{{}}")
                content = data.get("message", {{}}).get("content", [])
                if isinstance(content, list) and content:
                    prompt = str(content[0].get("text") or "")
            except Exception:
                pass
            if {write_prd!r}:
                import os
                os.makedirs("prd", exist_ok=True)
                with open("prd/prd.md", "w", encoding="utf-8") as fh:
                    fh.write("# Eval PRD\\n\\n" + prompt[-400:] + "\\n")
            session = "fake-eval-session"
            print(json.dumps({{"type": "system", "subtype": "init", "session_id": session}}))
            print(json.dumps({{"type": "assistant", "session_id": session,
                              "message": {{"content": [{{"type": "text", "text": "prd written"}}]}}}}))
            print(json.dumps({{"type": "result", "session_id": session,
                              "result": "done", "is_error": False}}))
            sys.stdout.flush()
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


def test_terminal_lane_writes_prd_with_fake_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "ZF_KANBAN_AGENT_CLAUDE_HEADLESS_CMD", _write_fake_claude(tmp_path)
    )
    lane_dir = tmp_path / "lane-terminal"
    result = run_terminal_lane(
        lane_dir=lane_dir,
        prompt="Build a widget catalog. MARKER-7749.",
        timeout_s=30,
    )
    assert result.ok, result.reason
    assert result.status == "completed"
    assert result.prd_path == lane_dir / "prd" / "prd.md"
    text = result.prd_path.read_text(encoding="utf-8")
    assert "MARKER-7749" in text
    assert result.detail["backend"] == "claude-headless"


def test_terminal_lane_fails_when_prd_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "ZF_KANBAN_AGENT_CLAUDE_HEADLESS_CMD",
        _write_fake_claude(tmp_path, write_prd=False),
    )
    result = run_terminal_lane(
        lane_dir=tmp_path / "lane-terminal", prompt="x", timeout_s=30
    )
    assert not result.ok
    assert result.status == "failed"
    assert result.reason == "prd_missing"
    assert result.prd_path is None


def test_terminal_lane_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "ZF_KANBAN_AGENT_CLAUDE_HEADLESS_CMD",
        _write_fake_claude(tmp_path, sleep_s=30.0),
    )
    started = time.monotonic()
    result = run_terminal_lane(
        lane_dir=tmp_path / "lane-terminal", prompt="x", timeout_s=1.0
    )
    assert time.monotonic() - started < 20
    assert not result.ok
    assert result.status == "timeout"
    assert result.reason == "timeout"


def test_kanban_lane_writes_prd_with_fake_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "ZF_KANBAN_AGENT_CLAUDE_HEADLESS_CMD", _write_fake_claude(tmp_path)
    )
    lane_dir = tmp_path / "lane-kanban"
    result = run_kanban_lane(
        lane_dir=lane_dir,
        prompt="Build a widget catalog. MARKER-8811.",
        timeout_s=30,
    )
    assert result.ok, result.reason
    assert result.status == "completed"
    assert "MARKER-8811" in lane_prd_path(lane_dir).read_text(encoding="utf-8")
    threads = list((lane_dir / ".zf" / "operator" / "threads").glob("*.json"))
    assert threads, "kanban lane must keep its thread store inside the lane dir"


def _fake_moderator_json_turn(**kwargs):
    thread_key = str(kwargs.get("thread_key") or "")
    if thread_key.startswith("channel-moderator-classify:"):
        return {"complexity": "simple"}, ""
    return (
        {
            "action": "conclude",
            "speakers": [],
            "focus": "",
            "rationale": "deterministic test verdict",
        },
        "",
    )


def test_channel_lane_fake_provider_reaches_prd_frozen(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        channel_moderator, "agent_json_turn", _fake_moderator_json_turn
    )
    lane_dir = tmp_path / "lane-channel"
    result = run_channel_lane(
        lane_dir=lane_dir,
        prompt="为一个小工具产品写 PRD:命令行天气预报,支持城市参数与缓存。",
        timeout_s=120,
        backend="fake",
        poll_interval_s=0.05,
    )
    assert result.ok, f"{result.reason} {result.detail}"
    assert result.status == "completed"
    assert result.prd_path == lane_dir / "prd" / "prd.md"
    text = result.prd_path.read_text(encoding="utf-8")
    assert "Deterministic test PRD" in text
    events = EventLog(lane_dir / ".zf" / "events.jsonl").read_all()
    assert any(event.type == "channel.prd.frozen" for event in events)
    assert result.detail["prd_ref"]
    assert result.detail["prd_digest"]


def test_channel_lane_timeout_when_replies_never_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        channel_moderator, "agent_json_turn", lambda **_kw: (None, "disabled")
    )
    monkeypatch.setattr(
        eval_lanes,
        "dispatch_pending_replies",
        lambda **_kw: ChannelDispatchResult(),
    )
    started = time.monotonic()
    result = run_channel_lane(
        lane_dir=tmp_path / "lane-channel",
        prompt="x",
        timeout_s=1.0,
        backend="fake",
        poll_interval_s=0.05,
    )
    assert time.monotonic() - started < 20
    assert not result.ok
    assert result.status == "timeout"
    assert result.reason == "timeout"


def test_channel_lane_create_failure(tmp_path: Path) -> None:
    result = run_channel_lane(
        lane_dir=tmp_path / "lane-channel",
        prompt="x",
        template_id="does-not-exist",
        timeout_s=5,
        poll_interval_s=0.05,
    )
    assert not result.ok
    assert result.status == "failed"
    assert result.reason.startswith("channel_create_failed")


def test_run_lane_dispatch_and_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "ZF_KANBAN_AGENT_CLAUDE_HEADLESS_CMD", _write_fake_claude(tmp_path)
    )
    lane_dir = tmp_path / "lane"
    result = run_lane(
        "terminal", lane_dir=lane_dir, prompt="MARKER-3311 requirement", timeout_s=30
    )
    assert result.ok, result.reason
    unknown = run_lane("sideways", lane_dir=lane_dir, prompt="x")
    assert not unknown.ok
    assert unknown.status == "failed"
    assert "unknown lane" in unknown.reason
    assert canonical_lane_id("channel-auto") == "channel"
    assert canonical_lane_id("kanban") == "kanban"
    assert canonical_lane_id("bogus") == ""


def test_channel_lane_shared_state_dir_scopes_by_channel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        channel_moderator, "agent_json_turn", _fake_moderator_json_turn
    )
    shared = tmp_path / "workspace-state"
    shared.mkdir()
    shared_log = EventLog(shared / "events.jsonl")
    EventWriter(shared_log).emit(
        "channel.prd.frozen",
        actor="test",
        payload={"channel_id": "ch-other", "prd_ref": "nope.json", "prd_digest": "x"},
    )
    lane_dir = tmp_path / "lane-channel"
    result = run_channel_lane(
        lane_dir=lane_dir,
        prompt="为一个小工具产品写 PRD:命令行天气预报,支持城市参数与缓存。",
        timeout_s=120,
        backend="fake",
        poll_interval_s=0.05,
        state_dir=shared,
        channel_id="eval-prd-t1",
    )
    assert result.ok, f"{result.reason} {result.detail}"
    assert result.detail["channel_id"] != "ch-other"
    assert result.prd_path == lane_dir / "prd" / "prd.md"
    shared_events = shared_log.read_all()
    assert any(
        event.type == "channel.prd.frozen"
        and (event.payload or {}).get("channel_id") == result.detail["channel_id"]
        for event in shared_events
    )
    assert not (lane_dir / ".zf" / "events.jsonl").exists()
