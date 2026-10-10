from __future__ import annotations

from pathlib import Path

from zf.core.events import EventWriter, ZfEvent
from zf.core.events.log import EventLog
from zf.runtime.channel_router import route_channel_message
from zf.runtime.channel_setup_readiness import channel_control_anchor_status
from zf.web.headless_turn_outcome import classify_headless_turn


def test_strict_channel_waits_for_both_control_anchors() -> None:
    channel = {
        "control_anchor_policy": "strict",
        "leader_member_id": "moderator",
        "discussion": {"default_responder_id": "responder"},
        "members": [
            {"member_id": "moderator", "status": "invited", "permissions": ["read"]},
        ],
    }

    pending = channel_control_anchor_status(channel)

    assert pending["setup_pending"] is True
    assert pending["setup_blockers"] == [
        "moderator_lacks_propose_workflow",
        "default_responder_not_in_roster",
    ]

    channel["members"].append({
        "member_id": "responder",
        "status": "connected",
        "permissions": ["read", "message", "summarize"],
    })
    channel["members"][0]["permissions"].append("propose_workflow")

    assert channel_control_anchor_status(channel) == {
        "policy": "strict",
        "setup_pending": False,
        "setup_blockers": [],
    }


def test_legacy_channel_remains_readable_without_setup_anchors() -> None:
    assert channel_control_anchor_status({"members": []}) == {
        "policy": "legacy",
        "setup_pending": False,
        "setup_blockers": [],
    }


def test_strict_pending_channel_blocks_message_routing(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zf"
    state_dir.mkdir()
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    writer.emit(
        "channel.created",
        actor="web",
        correlation_id="ch-pending",
        payload={
            "channel_id": "ch-pending",
            "name": "Pending",
            "control_anchor_policy": "strict",
        },
    )
    message = writer.emit(
        "channel.message.posted",
        actor="web",
        correlation_id="ch-pending",
        payload={
            "channel_id": "ch-pending",
            "thread_id": "main",
            "message_id": "msg-1",
            "member_id": "operator",
            "role": "user",
            "source": "web",
            "text": "请开始讨论",
        },
    )

    result = route_channel_message(
        state_dir=state_dir,
        writer=writer,
        message_event=message,
        message_payload=message.payload,
        actor="web",
        source="web",
        dispatch_inline=False,
    )

    assert result.skipped == [{"reason": "channel_setup_pending"}]
    blocked = [event for event in writer.event_log.read_all() if event.type == "channel.route.blocked"]
    assert blocked[-1].payload["setup_blockers"] == [
        "missing_moderator",
        "missing_default_responder",
    ]


def test_headless_outcome_distinguishes_provider_timeout_and_incomplete() -> None:
    assert classify_headless_turn(status="timeout")["failure_class"] == "timeout"
    assert classify_headless_turn(
        status="incomplete", completion_reason="max_output_tokens"
    ) == {
        "outcome": "incomplete",
        "failure_class": "provider_incomplete",
        "retryable": True,
        "next_action": "continue_or_retry",
        "reason": "max_output_tokens",
    }
    assert classify_headless_turn(
        status="failed", error="codex authentication required"
    )["failure_class"] == "provider_error"
    assert classify_headless_turn(status="cancelled")["retryable"] is False
