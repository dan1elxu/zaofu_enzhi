"""Mechanical settlement of explicit, evidence-bound cross-review outcomes."""

from pathlib import Path
from typing import Any

from zf.core.events import EventWriter, ZfEvent
from zf.runtime.channel_reply_queue import current_review_reply_bindings


def cross_review_outcome_error(review: dict[str, Any], *, required: bool) -> str:
    outcome = review.get("outcome")
    if outcome is None and not required:
        return ""
    if outcome not in {"resolved", "needs_owner", "unresolved"}:
        return "cross_review_outcome_required:resolved|needs_owner|unresolved"
    if outcome == "needs_owner" and any(
        not isinstance(review.get(key), str) or not review[key].strip()
        for key in ("owner_question", "recommended_answer")
    ):
        return "cross_review_needs_owner_requires_question_and_recommendation"
    return ""


def settle_cross_review_question(
    *, state_dir: Path, writer: EventWriter, channel_id: str, thread_id: str,
    request_id: str, question_id: str, completed_event_id: str, source: str,
) -> None:
    # Concurrent reviewers must read occurrence truth after publishing their
    # result; the dispatch-time snapshot cannot authorize closing a question.
    from zf.runtime.channel_projection import project_channel

    channel = project_channel(state_dir, channel_id) or {}
    question = next((q for q in channel.get("open_questions", []) if q.get("question_id") == question_id), {})
    if question.get("status") != "open" or question.get("kind") != "fact":
        return
    reviews = channel.get("cross_reviews", [])
    current = next((r for r in reviews if r.get("request_id") == request_id), {})
    if current.get("outcome") == "needs_owner":
        writer.emit("channel.question.updated", actor="channel-cross-review:runtime",
                    causation_id=completed_event_id, correlation_id=channel_id, payload={
                        "channel_id": channel_id, "thread_id": thread_id, "question_id": question_id,
                        "question": current["owner_question"], "kind": "owner_decision", "target_member_id": "owner",
                        "recommended_answer": current["recommended_answer"], "why_it_matters": current.get("summary", ""),
                        "request_id": request_id, "source": source,
                    })
        return
    cohort = [r for r in reviews if r.get("question_id") == question_id
              and r.get("thread_id", "main") == thread_id
              and r.get("dedup_request_id", "") == current.get("dedup_request_id", "")]
    if not cohort or any(r.get("status") != "completed" or r.get("outcome", "resolved") != "resolved"
                         or not r.get("answer") or not r.get("evidence_refs") for r in cohort):
        return
    primary = next((r for r in cohort if r.get("target_member_id") == question.get("target_member_id")), current)
    writer.emit("channel.question.resolved", actor="channel-cross-review:runtime",
                causation_id=completed_event_id, correlation_id=channel_id, payload={
                    "channel_id": channel_id, "thread_id": thread_id, "question_id": question_id,
                    "resolution": "evidence", "resolved_by": primary.get("target_member_id"),
                    "answer": primary["answer"], "evidence_refs": list(dict.fromkeys(
                        ref for row in cohort for ref in row["evidence_refs"])),
                    "review_request_ids": [row["request_id"] for row in cohort], "source": source,
                })


REPAIRABLE_REVIEW_REASONS = frozenset({
    "invalid_missing_channel_cross_review",
    "cross_review_outcome_required:resolved|needs_owner|unresolved",
    "cross_review_needs_owner_requires_question_and_recommendation",
})


def rejected_review_reply(events: list[ZfEvent], rejection: ZfEvent) -> dict[str, Any]:
    """Bind a repairable format rejection to its actual completed provider turn."""
    if rejection.payload.get("reason") not in REPAIRABLE_REVIEW_REASONS:
        return {}
    reply = next((e for e in reversed(events) if e.id == rejection.causation_id), None)
    if reply is None or reply.type != "channel.agent.reply.completed":
        return {}
    if any(reply.payload.get(key) != rejection.payload.get(key)
           for key in ("channel_id", "target_member_id")):
        return {}
    if (reply.payload.get("thread_id") or "main") != (rejection.payload.get("thread_id") or "main"):
        return {}
    return reply.payload


def can_retry_rejected_review(
    events: list[ZfEvent], review: dict[str, Any], request: dict[str, Any], channel_id: str,
) -> bool:
    key = (channel_id, str(request.get("request_id") or ""))
    rejection = current_review_reply_bindings(events, status="rejected").get(key)
    if rejection is None or rejection.id != review.get("result_event_id"):
        return False
    rejected = rejected_review_reply(events, rejection)
    if not rejected or rejected.get("request_id") != key[1]:
        return False
    generation = int(request.get("run_generation") or 1)
    current = next((e.payload for e in reversed(events)
                    if e.type == "channel.agent.reply.requested"
                    and (e.payload.get("channel_id"), e.payload.get("request_id")) == key), {})
    return (generation == int(current.get("run_generation") or 1)
            and generation > int(rejected.get("run_generation") or 1))
