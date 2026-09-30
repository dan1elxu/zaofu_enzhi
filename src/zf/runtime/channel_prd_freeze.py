"""Canonical Channel PRD freeze event helpers.

``channel.consensus.reached`` remains the historical consensus event.  The
evaluation boundary consumes the explicit ``channel.prd.frozen`` occurrence so
manual, auto, and other Channel surfaces can share one immutable handoff
contract without making evaluation depend on a particular consensus driver.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from zf.core.events import EventWriter, ZfEvent


PRD_FROZEN_EVENT = "channel.prd.frozen"
_REVISION_RE = re.compile(r"^[1-9][0-9]*$")


def _safe_revision(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str) and _REVISION_RE.fullmatch(value):
        return int(value)
    return 0


def prd_frozen_payload(
    consensus_payload: dict[str, Any],
    *,
    source_event_id: str,
    freeze_reason: str,
) -> dict[str, Any]:
    """Return a complete, provenance-bound freeze payload.

    The payload is a copy of the canonical consensus identity plus the source
    occurrence that caused the freeze.  Consumers must use the refs and
    digests from this payload rather than looking up a later Channel state.
    """

    payload = {
        key: value
        for key, value in consensus_payload.items()
        if key not in {
            "source", "source_event_id", "freeze_reason",
            # The freeze helper creates a new immutable source snapshot below.
            # Do not carry a synthesis-era ref/digest into the handoff and then
            # overwrite only the digest with the inline snapshot digest.
            "source_snapshot_ref", "source_snapshot_digest", "source_snapshot",
        }
    }
    payload.update(
        source_event_id=str(source_event_id or ""),
        freeze_reason=str(freeze_reason or "consensus_reached"),
        source=str(consensus_payload.get("source") or ""),
    )
    return payload


def _stable_digest(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_snapshot(
    writer: EventWriter,
    *,
    consensus_payload: dict[str, Any],
    source_event: ZfEvent,
) -> dict[str, Any]:
    """Capture the bounded, identity-bearing source view at freeze time.

    The snapshot is inline in ``channel.prd.frozen`` so evaluation can replay
    the exact handoff even if later Channel messages change.  It contains
    decision-bearing occurrences only; full PRD/readiness bodies remain bound
    by their own ref/digest pairs.
    """

    channel_id = str(consensus_payload.get("channel_id") or "")
    thread_id = str(consensus_payload.get("thread_id") or "main")
    revision = _safe_revision(consensus_payload.get("prd_revision"))
    events: list[dict[str, Any]] = []
    ignored = {
        "channel.member.connected", "channel.member.disconnected",
        "channel.typing.started", "channel.typing.stopped", "channel.message.read",
    }
    try:
        prior_events = writer.event_log.read_all()
    except Exception:
        prior_events = []
    for event in prior_events:
        if event.type in ignored or not event.type.startswith("channel."):
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        if str(payload.get("channel_id") or "") != channel_id:
            continue
        if str(payload.get("thread_id") or "") != thread_id:
            continue
        if event.id == source_event.id or event.type in {
            "channel.message.posted", "channel.question.opened", "channel.question.updated",
            "channel.question.resolved", "channel.consensus.proposed", "channel.consensus.signed",
            "channel.synthesis.proposed", "channel.finding.recorded",
        }:
            events.append({
                "event_id": str(event.id),
                "event_type": str(event.type),
                "actor": str(event.actor or ""),
                "ts": str(event.ts or ""),
                "payload": payload,
            })
        if event.id == source_event.id:
            break
    return {
        "schema_version": "channel.prd.evaluation-source-snapshot.v1",
        "channel_id": channel_id,
        "thread_id": thread_id,
        "revision": revision,
        "source_event_id": str(source_event.id or ""),
        "prd_ref": str(consensus_payload.get("prd_ref") or ""),
        "prd_digest": str(consensus_payload.get("prd_digest") or ""),
        "readiness_ref": str(consensus_payload.get("readiness_ref") or ""),
        "readiness_digest": str(consensus_payload.get("readiness_digest") or ""),
        "source_refs": list(consensus_payload.get("source_refs") or []),
        "events": events,
    }


def emit_prd_frozen(
    writer: EventWriter,
    *,
    consensus_payload: dict[str, Any],
    source_event: ZfEvent,
    freeze_reason: str = "consensus_reached",
    actor: str = "channel-prd-freeze",
    task_id: str = "",
    correlation_id: str = "",
) -> ZfEvent:
    """Append the canonical freeze occurrence after a consensus event."""

    payload = prd_frozen_payload(
        consensus_payload,
        source_event_id=source_event.id,
        freeze_reason=freeze_reason,
    )
    snapshot = _source_snapshot(
        writer, consensus_payload=consensus_payload, source_event=source_event,
    )
    payload["source_snapshot"] = snapshot
    payload["source_snapshot_digest"] = _stable_digest(snapshot)
    return writer.emit(
        PRD_FROZEN_EVENT,
        actor=actor,
        task_id=task_id or source_event.task_id,
        causation_id=source_event.id,
        correlation_id=correlation_id or source_event.correlation_id,
        payload=payload,
    )


__all__ = ["PRD_FROZEN_EVENT", "emit_prd_frozen", "prd_frozen_payload"]
