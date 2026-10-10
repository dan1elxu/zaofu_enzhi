"""Mechanical Phase-1 contribution completion checks."""

from __future__ import annotations

from typing import Any

from zf.runtime.channel_member_proposals import pending_member_proposals


def valid_phase1_members(
    channel: dict[str, Any],
    session: dict[str, Any],
    thread_id: str,
) -> set[str]:
    """Members whose current blind turn ended in a valid frozen contract."""

    roster = {
        str(member)
        for member in session.get("roster") or []
        if str(member)
    }
    # The original requirement remains the durable PRD provenance anchor. An
    # adaptive pass, however, has its own current blind-turn message; use it
    # for completion matching so preserving provenance does not strand the
    # focused roster in phase 1.
    trigger = str(
        session.get("adaptive_next_round_message_id")
        or session.get("requirement_message_id")
        or ""
    )
    if not roster or not trigger:
        return set()
    requests = {
        str(item.get("target_member_id") or ""): item
        for item in channel.get("reply_requests") or []
        if isinstance(item, dict)
        and str(item.get("message_id") or "") == trigger
        and str(item.get("target_member_id") or "") in roster
        and str(item.get("status") or "") == "completed"
    }
    raw = channel.get("contributions") or []
    contributions = list(raw.values()) if isinstance(raw, dict) else list(raw)
    return {
        member_id
        for member_id, request in requests.items()
        if any(
            _matches_current_reply(
                item,
                request=request,
                member_id=member_id,
                thread_id=thread_id,
                trigger=trigger,
            )
            for item in contributions
        )
    }


def _matches_current_reply(
    item: object,
    *,
    request: dict[str, Any],
    member_id: str,
    thread_id: str,
    trigger: str,
) -> bool:
    if not isinstance(item, dict):
        return False
    return (
        str(item.get("contract_status") or "") == "structured"
        and str(item.get("thread_id") or "main") == thread_id
        and str(item.get("member_id") or "") == member_id
        and str(item.get("message_id") or "") == trigger
        and str(item.get("request_id") or "")
        == str(request.get("request_id") or "")
        and int(item.get("run_generation") or 1)
        == int(request.get("run_generation") or 1)
        and str(item.get("source_reply_event_id") or "")
        == str(request.get("event_id") or "")
        # questions_frozen is intentionally NOT required here: a member who
        # reports freeze:false (unresolved questions of their own) would
        # otherwise strand phase1 forever — nothing in the runtime ever
        # flips a recorded contribution back to frozen, and the question
        # dedup flow that could settle those questions only starts AFTER
        # phase1 completes (2026-09-18 test_dev5 deadlock: 4/8 stuck for
        # hours). Open questions already live in the ledger and are settled
        # by the phase2 dedup/resolution flow.
    )


__all__ = ["valid_phase1_members"]


def ledger_converged(channel: dict[str, Any], session: dict[str, Any], thread_id: str) -> bool:
    """All roster members froze their questions and every question is resolved."""
    if str(session.get("phase_reason") or "") == "consensus_blocked":
        consensus = (channel.get("consensus") or {}).get(thread_id) or {}
        required = {str(member) for member in consensus.get("required_signers") or []}
        reviewed = set((consensus.get("signed") or {}).keys()) | {
            str(item.get("member_id") or "")
            for item in consensus.get("blocked") or []
            if isinstance(item, dict)
        }
        if required and not required <= reviewed:
            return False
    if pending_member_proposals(channel, thread_id):
        return False
    roster = {str(m) for m in session.get("roster") or []}
    if not roster:
        return False
    frozen = channel.get("questions_frozen") or {}
    frozen_members = set((frozen.get(thread_id) or {}).keys()) if isinstance(frozen, dict) else set()
    if not roster <= frozen_members:
        return False
    for question in channel.get("open_questions") or []:
        if not isinstance(question, dict):
            continue
        if str(question.get("thread_id") or "main") != thread_id:
            continue
        if str(question.get("status") or "") == "open":
            return False
    cross_reviews = channel.get("cross_reviews") or []
    candidates = (
        list(cross_reviews.values())
        if isinstance(cross_reviews, dict)
        else list(cross_reviews)
    )
    if any(
        isinstance(item, dict)
        and str(item.get("thread_id") or "main") == thread_id
        and str(item.get("status") or "") != "completed"
        for item in candidates
    ):
        return False
    # Zero questions with everyone frozen is legitimate convergence: nothing
    # was unclear. The freeze set is the gate, not the question count.
    return True
