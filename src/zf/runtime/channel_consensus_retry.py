"""Controlled re-review of one unchanged PRD; never manufactures signatures."""

from __future__ import annotations

from typing import Any

from zf.core.state.locks import locked_path
from zf.runtime.channel_consensus_identity import consensus_artifact_error
from zf.runtime.channel_owner_authority import channel_owner_authority_error
from zf.runtime.channel_projection import project_channel
from zf.runtime.control_actions_helpers import _required_text, _stable_control_id


def retry_consensus_review(service: Any, *, requested, action: str,
                           requested_action: str, payload: dict) -> dict:
    channel_id = _required_text(payload, "channel_id")
    thread_id = str(payload.get("thread_id") or "main")
    expected = _required_text(payload, "consensus_event_id")
    reason = _required_text(payload, "reason")
    lock = service.state_dir / "locks" / _stable_control_id(
        "channel-consensus", channel_id, thread_id,
    )
    with locked_path(lock):
        channel = project_channel(service.state_dir, channel_id) or {}
        consensus = (channel.get("consensus") or {}).get(thread_id) or {}
        error = channel_owner_authority_error(
            channel, actor=service.actor, capability="channel.consensus.confirm",
        )
        if not error and (not expected or not reason):
            error = "consensus_event_id and reason are required"
        if not error and (
            consensus.get("proposed_event_id") != expected
            or consensus.get("status") == "reached"
            or consensus.get("revision_pending")
        ):
            error = "consensus_review_target_stale"
        if not error and any(
            row.get("status") in {"pending", "queued", "running", "started"}
            and str(row.get("thread_id") or "main") == thread_id
            for row in channel.get("reply_requests") or []
        ):
            error = "consensus_review_busy"
        if not error and any(
            row.get("status") == "open" and str(row.get("thread_id") or "main") == thread_id
            for row in channel.get("open_questions") or []
        ):
            error = "consensus_review_open_questions"
        if not error:
            error = consensus_artifact_error(service.state_dir, consensus)
        prior = next((e for e in service.writer.event_log.read_all() if e.id == expected), None)
        if not error and (prior is None or prior.type != "channel.consensus.proposed"):
            error = "consensus_proposal_missing"
        if error:
            return service._failed(
                requested=requested, action=action, requested_action=requested_action,
                task_id=None, reason=error, status_code=409,
            )
        # A new proposal occurrence invalidates all old review rounds, while
        # preserving the exact immutable PRD identity and the old event ledger.
        event = service.writer.emit(
            "channel.consensus.proposed", actor=service.actor,
            causation_id=requested.id, correlation_id=channel_id,
            payload={**prior.payload, "review_retry_of": expected,
                     "review_retry_reason": reason, "source": service.surface},
        )
        service.writer.emit(
            "channel.discussion.phase.changed", actor=service.actor,
            causation_id=event.id, correlation_id=channel_id,
            payload={"channel_id": channel_id, "thread_id": thread_id,
                     "phase": "phase3_synthesis", "reason": "consensus_review_retry",
                     "source": service.surface},
        )
        service._completed(
            requested=requested, event=event, action=action, requested_action=requested_action,
            task_id=None, status="review_requested",
            extra={"channel_id": channel_id, "thread_id": thread_id},
        )
        return {"ok": True, "status": "review_requested", "event_id": event.id,
                "artifact_ref": consensus["artifact_ref"],
                "artifact_digest": consensus["artifact_digest"]}
