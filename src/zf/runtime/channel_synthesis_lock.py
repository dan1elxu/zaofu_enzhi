"""Stable lock and in-flight lookup for Channel synthesis requests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


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
    completed_request_ids = {
        str(item.get("request_id") or "")
        for item in channel.get("syntheses") or []
        if isinstance(item, dict) and str(item.get("request_id") or "")
    }
    for item in reversed(channel.get("synthesis_requests") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("thread_id") or "main") != thread_id:
            continue
        request_id = str(item.get("request_id") or "")
        # A requested synthesis is no longer in flight once its structured
        # proposal has landed.  This matters after an adaptive next round:
        # the old request remains in the append-only projection, but phase 2
        # must be able to issue the next generation.
        if request_id and request_id in completed_request_ids:
            continue
        if str(item.get("status") or "requested") in {"blocked", "stale_ignored"}:
            continue
        return item
    return None
