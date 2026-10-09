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
