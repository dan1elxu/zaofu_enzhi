"""Mechanical readiness checks for Channel Group control anchors."""

from __future__ import annotations

from typing import Any


_INACTIVE_MEMBER_STATUSES = {"failed", "rejected", "removed", "suspended"}


def channel_control_anchor_status(channel: dict[str, Any]) -> dict[str, Any]:
    """Return bounded Moderator/default-responder setup status.

    Legacy low-level ``channel-create`` events remain replayable.  Only
    channels marked ``strict`` (template/Kanban-created groups) are blocked
    until both control anchors resolve to roster members.
    """

    policy = str(channel.get("control_anchor_policy") or "legacy").strip()
    if policy != "strict":
        return {"policy": policy or "legacy", "setup_pending": False, "setup_blockers": []}

    raw_members = channel.get("members") or []
    members = (
        [item for item in raw_members.values() if isinstance(item, dict)]
        if isinstance(raw_members, dict)
        else [item for item in raw_members if isinstance(item, dict)]
    )
    by_id = {
        str(member.get("member_id") or ""): member
        for member in members
        if str(member.get("member_id") or "")
    }
    blockers: list[str] = []
    leader_id = str(channel.get("leader_member_id") or "").strip()
    leader = by_id.get(leader_id)
    if not leader_id:
        blockers.append("missing_moderator")
    elif leader is None:
        blockers.append("moderator_not_in_roster")
    else:
        if str(leader.get("status") or "active").lower() in _INACTIVE_MEMBER_STATUSES:
            blockers.append("moderator_inactive")
        if "propose_workflow" not in set(leader.get("permissions") or []):
            blockers.append("moderator_lacks_propose_workflow")

    discussion = channel.get("discussion")
    discussion = discussion if isinstance(discussion, dict) else {}
    responder_id = str(discussion.get("default_responder_id") or "").strip()
    responder = by_id.get(responder_id)
    if not responder_id:
        blockers.append("missing_default_responder")
    elif responder is None:
        blockers.append("default_responder_not_in_roster")
    elif str(responder.get("status") or "active").lower() in _INACTIVE_MEMBER_STATUSES:
        blockers.append("default_responder_inactive")

    return {
        "policy": "strict",
        "setup_pending": bool(blockers),
        "setup_blockers": blockers,
    }


__all__ = ["channel_control_anchor_status"]
