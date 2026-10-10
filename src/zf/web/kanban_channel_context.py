"""Read-only Channel Group context exposed to the Kanban Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zf.runtime.channel_setup_readiness import channel_control_anchor_status


def kanban_channel_group_context(
    state_dir: Path,
    *,
    channel_id: object,
    thread_id: object,
) -> dict[str, Any]:
    """Return bounded roster facts for requirement-driven Channel decisions.

    Requirement interpretation remains Agent-owned.  This projection only
    supplies the mechanical facts needed to see missing roles and the current
    workflow authority; it never chooses or invites a role.
    """

    normalized_id = str(channel_id or "").strip()
    normalized_thread = str(thread_id or "main").strip() or "main"
    if not normalized_id:
        return {}
    from zf.runtime.channel_projection import project_channel

    channel = project_channel(state_dir, normalized_id) or {}
    if not channel:
        return {
            "channel_id": normalized_id,
            "thread_id": normalized_thread,
            "status": "missing",
        }
    discussion = (
        channel.get("discussion")
        if isinstance(channel.get("discussion"), dict)
        else {}
    )
    anchor_status = channel_control_anchor_status(channel)
    members: list[dict[str, Any]] = []
    for raw_member in channel.get("members") or []:
        if not isinstance(raw_member, dict):
            continue
        members.append({
            "member_id": str(raw_member.get("member_id") or ""),
            "channel_role": str(raw_member.get("channel_role") or ""),
            "profile_id": str(raw_member.get("profile_id") or ""),
            "status": str(raw_member.get("status") or "active"),
            "permissions": [
                str(item) for item in raw_member.get("permissions") or []
                if str(item)
            ],
        })
    return {
        "channel_id": normalized_id,
        "thread_id": normalized_thread,
        "status": str(channel.get("status") or "active"),
        "leader_member_id": str(channel.get("leader_member_id") or ""),
        "leader_revision": int(channel.get("leader_revision") or 0),
        "control_anchor_policy": anchor_status["policy"],
        "setup_pending": anchor_status["setup_pending"],
        "setup_blockers": list(anchor_status["setup_blockers"]),
        "default_responder_id": str(
            discussion.get("default_responder_id") or ""
        ),
        "members": members,
        "workflow_trigger": "moderator_propose_workflow",
    }


__all__ = ["kanban_channel_group_context"]
