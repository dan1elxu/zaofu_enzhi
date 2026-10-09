"""Fail-closed handling for channel dispatch crashes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zf.core.events import EventWriter
from zf.runtime.channel_dispatch_capacity import channel_dispatch_lock
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_reply_remediation import emit_channel_reply_failed


def fail_pending_channel_replies_after_dispatch_failure(
    *,
    state_dir: Path,
    writer: EventWriter,
    channel_id: str,
    actor: str,
    source: str,
    reason: str,
    candidates: list[dict[str, Any]],
) -> list[str]:
    """Close requests that never reached ``reply.started`` after a crash.

    Only current ``pending``/``queued`` rows are closed here.  Rows that
    already started or completed remain owned by their normal lifecycle
    events.  ``retryable=True`` lets bounded channel remediation re-arm the
    request when a runtime is available.
    """
    pending_ids: list[str] = []
    expected = {str(row.get("request_id") or ""): row.get("event_id") for row in candidates}
    # Share the admission lock with reply.started. A new request, a newer
    # generation or a request claimed by another dispatcher is not ours to fail.
    with channel_dispatch_lock(Path(state_dir), channel_id):
        channel = project_channel(Path(state_dir), channel_id) or {}
        for request in channel.get("reply_requests") or []:
            request_id = str(request.get("request_id") or "")
            if not request_id or request_id not in expected:
                continue
            if request.get("event_id") != expected[request_id]:
                continue
            if request.get("status") not in {"pending", "queued"}:
                continue
            emit_channel_reply_failed(
                writer=writer,
                request=request,
                request_id=request_id,
                started_event_id=str(request.get("event_id") or ""),
                actor=actor,
                source=source,
                channel_id=channel_id,
                reason=reason,
                provider_session_id=str(request.get("provider_session_id") or ""),
                failure_status="dispatch_exception",
            )
            pending_ids.append(request_id)
    writer.emit(
        "channel.agent.reply.dispatch_failed",
        actor=actor,
        correlation_id=channel_id,
        payload={
            "channel_id": channel_id,
            "reason": reason,
            "request_ids": pending_ids,
            "source": source,
        },
    )
    return pending_ids


def emit_superseded_queued_reply(
    *, writer: EventWriter, channel_id: str, request: dict[str, Any], actor: str, source: str,
) -> None:
    request_id = str(request.get("request_id") or "")
    if request_id:
        emit_channel_reply_failed(
            writer=writer, channel_id=channel_id, request=request,
            request_id=request_id, started_event_id=str(request.get("event_id") or ""),
            actor=actor, source=source, reason="superseded by latest queued mention",
            provider_session_id=str(request.get("provider_session_id") or ""),
        )
