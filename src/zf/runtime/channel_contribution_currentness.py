"""Ignore a tracked contribution dispatched before the current discussion pass."""
from __future__ import annotations


def ignore_stale_contribution(writer, *, channel_id, thread_id, request, reply_event_id):
    request_id = str(request.get("request_id") or "")
    if not request_id:
        return False
    events = writer.event_log.read_all()
    request_position = None
    boundary_position = -1
    boundary_id = ""
    for index, event in enumerate(events):
        payload = event.payload
        if payload.get("channel_id") != channel_id or str(payload.get("thread_id") or "main") != thread_id:
            continue
        if event.type == "channel.agent.reply.requested" and payload.get("request_id") == request_id:
            request_position = index
        if event.type in {"channel.discussion.started", "channel.discussion.continued"}:
            boundary_position, boundary_id = index, event.id
    if request_position is None or request_position > boundary_position:
        return False
    if not any(e.type == "channel.contribution.stale_ignored" and e.payload.get("source_reply_event_id") == reply_event_id for e in events):
        writer.emit("channel.contribution.stale_ignored", actor="channel-contract", causation_id=reply_event_id,
                    correlation_id=channel_id, payload={"channel_id": channel_id, "thread_id": thread_id,
                    "request_id": request_id, "source_reply_event_id": reply_event_id,
                    "superseded_by_event_id": boundary_id, "reason": "discussion_pass_changed"})
    return True
