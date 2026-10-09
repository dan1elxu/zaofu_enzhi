"""Stable lock and in-flight lookup for Channel synthesis requests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from zf.core.events import ZfEvent


def channel_synthesis_lock_path(
    state_dir: Path,
    channel_id: str,
    thread_id: str,
) -> Path:
    digest = hashlib.sha1(
        f"channel-synthesis:{channel_id}:{thread_id}".encode("utf-8")
    ).hexdigest()[:16]
    return Path(state_dir) / "locks" / f"channel-synthesis-{digest}"


def synthesis_request_in_flight(
    channel: dict[str, Any],
    thread_id: str,
) -> dict[str, Any] | None:
    """Return the active request for this discussion generation, if present."""
    sessions = channel.get("discussions")
    session = sessions.get(thread_id) if isinstance(sessions, dict) else {}
    if not isinstance(session, dict):
        session = {}
    if (
        str(session.get("state") or "") == "phase2_relay"
        and str(session.get("phase_reason") or "")
        in {"synthesis_questions_opened", "consensus_blocked"}
    ):
        return None
    for item in reversed(channel.get("synthesis_requests") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("thread_id") or "main") != thread_id:
            continue
        if str(item.get("status") or "requested") in {
            "requested",
            "repair_requested",
        }:
            return item
        return None
    return None


def prior_synthesis_questions_are_resolved(
    *,
    channel: dict[str, Any],
    prior_synthesis_event: ZfEvent,
    events: list[ZfEvent],
) -> bool:
    """Whether a later synthesis follows Owner closure of the prior draft."""
    question_ids = {
        str(event.payload.get("question_id") or "")
        for event in events
        if event.type == "channel.question.opened"
        and event.causation_id == prior_synthesis_event.id
        and isinstance(event.payload, dict)
        and str(event.payload.get("question_id") or "")
    }
    if not question_ids:
        return False
    question_statuses = {
        str(item.get("question_id") or ""): str(item.get("status") or "")
        for item in channel.get("open_questions") or []
        if isinstance(item, dict)
    }
    return all(question_statuses.get(question_id) == "resolved" for question_id in question_ids)


def mark_exhausted_synthesis(channel: dict[str, Any], event: ZfEvent) -> None:
    """Fold terminal retry exhaustion without rewriting ledger history."""
    payload = event.payload
    reply = channel["reply_requests"].get(str(payload.get("request_id") or ""))
    if not reply or reply.get("status") != "failed":
        return
    if int(payload.get("run_generation") or 1) != int(reply.get("run_generation") or 1):
        return
    reply["remediation_exhausted_event_id"] = event.id
    message = channel["messages"].get(str(reply.get("message_id") or ""), {})
    synthesis_id = (message.get("refs") or {}).get("synthesis_request_id")
    if not synthesis_id:
        return
    for request in channel["synthesis_requests"]:
        if request.get("request_id") == synthesis_id and request.get("status") in {"requested", "repair_requested"}:
            request.update(status="failed", recovery_reply_id=reply["request_id"],
                           reason=str(payload.get("reason") or "reply retries exhausted"), updated_at=event.ts)


def retired_synthesis_reply_ids(channel: dict[str, Any], thread_id: str) -> set[str]:
    """Historic exhausted attempts stay visible but do not block a newer request."""
    requests = [r for r in channel.get("synthesis_requests", []) if str(r.get("thread_id") or "main") == thread_id]
    if not requests:
        return set()
    latest = requests[-1].get("request_id")
    return {str(r["recovery_reply_id"]) for r in requests
            if r.get("request_id") != latest and r.get("recovery_reply_id")}
