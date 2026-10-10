"""Leader invitation intents backed by contribution evidence and a public catalog."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from zf.runtime.channel_sidecar import hydrate_channel_context_pack_payload


def catalog_digest(profile: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def prepare_member_proposals(channel, request, contribution, *, state_dir) -> tuple[list[dict], str]:
    rows = contribution.get("member_invitations") or []
    if not rows:
        return [], ""
    if not isinstance(rows, list) or len(rows) > 32:
        return [], "member_invitations must be a bounded list"
    member_id = str(request.get("target_member_id") or "")
    if member_id != channel.get("leader_member_id"):
        return [], "only the current Channel leader may propose invitations"
    packs = channel.get("context_packs") or []
    if isinstance(packs, dict):
        packs = list(packs.values())
    pack = next((p for p in packs if p.get("context_pack_id") == request.get("context_pack_id")), {})
    pack = hydrate_channel_context_pack_payload(state_dir, pack, strict=True) if pack else {}
    catalog = {p["profile_id"]: p for p in pack.get("channel_profile_catalog") or []}
    members = {str(m.get("member_id")): m for m in channel.get("members") or []}
    seen: set[str] = set()
    proposals = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"member_id", "profile_id", "reason", "source_refs", "required_reviewer"}:
            return [], "invitation only accepts member_id, profile_id, reason, source_refs, required_reviewer"
        target = str(row.get("member_id") or "").strip()
        profile = catalog.get(str(row.get("profile_id") or ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", target) or target in seen:
            return [], "invitation member_id must be valid and unique"
        if target in members:
            return [], "invitation target is already a Channel member"
        if not profile:
            return [], "invitation profile must exist in the supplied public catalog"
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            return [], "invitation requires a reason"
        if (not isinstance(row.get("source_refs"), list) or not row["source_refs"]
                or any(not isinstance(ref, str) or not ref.strip() for ref in row["source_refs"])):
            return [], "invitation requires source_refs"
        if "required_reviewer" in row and not isinstance(row["required_reviewer"], bool):
            return [], "required_reviewer must be boolean"
        seen.add(target)
        proposals.append({**row, "member_id": target, "profile_id": profile["profile_id"],
                          "profile_revision": profile["revision"], "profile_catalog_digest": catalog_digest(profile),
                          "channel_role": profile["channel_role"], "required_reviewer": row.get("required_reviewer", True)})
    return proposals, ""


def emit_member_proposals(writer, *, channel, request, proposals, descriptor, reply_event_id):
    channel_id = channel["channel_id"]
    thread_id = str(request.get("thread_id") or "main")
    prior = {e.payload.get("proposal_id") for e in writer.event_log.read_all()
             if e.type == "channel.member.invitation.proposed"}
    for proposal in proposals:
        proposal_id = "invite-" + hashlib.sha256(
            f"{channel_id}:{thread_id}:{reply_event_id}:{proposal['member_id']}".encode()
        ).hexdigest()[:20]
        if proposal_id in prior:
            continue
        writer.emit("channel.member.invitation.proposed", actor=request["target_member_id"],
                    causation_id=reply_event_id, correlation_id=channel_id, payload={
                        "channel_id": channel_id, "thread_id": thread_id, "proposal_id": proposal_id,
                        "leader_member_id": channel.get("leader_member_id"),
                        "leader_revision": int(channel.get("leader_revision") or 1),
                        "artifact_ref": descriptor["ref"], "artifact_digest": descriptor["sha256"],
                        **proposal,
                    })


def pending_member_proposals(channel, thread_id: str) -> list[dict]:
    rows = (channel or {}).get("member_invitation_proposals") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [row for row in rows if row.get("thread_id", "main") == thread_id and row.get("status") == "pending"]


def apply_member_proposal_event(channel, event, payload):
    proposals = channel.setdefault("member_invitation_proposals", {})
    proposal_id = str(payload.get("proposal_id") or "")
    if event.type == "channel.member.invitation.proposed":
        proposals.setdefault(proposal_id, {**payload, "status": "pending", "event_id": event.id, "ts": event.ts})
    elif proposal_id in proposals:
        proposals[proposal_id].update(status=str(payload.get("decision") or ""),
                                      decided_by=event.actor, decision_event_id=event.id)
