"""Route guard for strict Channel Group setup."""

from __future__ import annotations

from typing import Any

from zf.core.events import EventWriter
from zf.core.events.model import ZfEvent
from zf.runtime.channel_setup_readiness import channel_control_anchor_status


def emit_setup_pending_route_block(
    *,
    channel: dict[str, Any],
    writer: EventWriter,
    actor: str,
    channel_id: str,
    thread_id: str,
    message_id: str,
    source: str,
    message_event: ZfEvent,
) -> str:
    status = channel_control_anchor_status(channel)
    if not status["setup_pending"]:
        return ""
    reason = "channel_setup_pending"
    writer.emit(
        "channel.route.blocked",
        actor=actor,
        task_id=message_event.task_id,
        causation_id=message_event.id,
        correlation_id=channel_id,
        payload={
            "channel_id": channel_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "member_id": str((message_event.payload or {}).get("member_id") or ""),
            "reason": reason,
            "setup_blockers": list(status["setup_blockers"]),
            "source": source,
        },
    )
    return reason


__all__ = ["emit_setup_pending_route_block"]
