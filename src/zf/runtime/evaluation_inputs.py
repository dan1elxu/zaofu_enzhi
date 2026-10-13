"""Frozen Channel/PRD evaluation input assembly and role snapshots.

This module owns immutable freeze discovery, packet input construction, and
frozen role/profile projections. It does not run providers or render reports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zf.runtime.channel_contracts import normalize_product_discussion_mode
from zf.runtime.evaluation_packets import (
    build_packet,
    evaluation_run_id,
    message_evidence_ref,
    stable_digest,
)
from zf.runtime.evaluation_workflow import (
    _bare_digest,
    _bound_prd_payload,
    _read_json_artifact,
    _require_artifact_identity,
    _revision_value,
    _strict_revision,
)

FREEZE_TRIGGER_EVENTS = frozenset({"channel.prd.frozen"})
TASK_TYPES = frozenset({"bugfix", "refactor", "greenfield", "ambiguous"})


def missing_prd_fidelity_inputs(packet: Any) -> list[str]:
    """Deterministic pre-flight check for the channel PRD fidelity stage."""

    def has_body(value: Any) -> bool:
        # Markdown and scalar JSON artifacts are wrapped by the observer.
        while isinstance(value, dict) and set(value) == {"body"}:
            value = value["body"]
        if isinstance(value, str):
            return bool(value.strip())
        return isinstance(value, (dict, list)) and bool(value)

    missing = []
    statement = ((packet.get("task_details") or {}).get("statement")
                 or (packet.get("task") or {}).get("statement"))
    if not isinstance(statement, str) or not statement.strip():
        missing.append("task.statement")
    if not has_body((packet.get("channel_frozen_prd") or {}).get("body")):
        missing.append("channel_frozen_prd.body")
    bound = packet.get("workflow_bound_prd")
    if bound and not has_body(bound.get("body")):
        missing.append("workflow_bound_prd.body")
    return missing


def _prepare_channel(
    *, state_dir: Path, events: list[Any], channel_id: str,
    freeze: dict[str, Any], bound_workflows: list[dict[str, Any]],
    project_root: Path | None = None,
    role_catalog: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Capture immutable inputs before any asynchronous provider work."""

    from zf.runtime.channel_projection import project_channel
    thread_id = str(freeze.get("thread_id") or "main")
    prd_revision = _strict_revision(freeze.get("prd_revision"))
    if (
        str(freeze.get("channel_id") or "") != channel_id
        or not thread_id
        or prd_revision < 1
    ):
        raise ValueError("freeze identity is incomplete or revision is invalid")
    canonical_event_id = str(
        freeze.get("consensus_event_id") or freeze.get("source_event_id") or ""
    ).strip()
    if not canonical_event_id:
        raise ValueError("canonical PRD freeze source event is missing")
    freeze_event_id = str(freeze.get("event_id") or "")
    if not freeze_event_id:
        raise ValueError("canonical PRD freeze event id is missing")
    source_index = next((
        index for index, event in enumerate(events)
        if str(getattr(event, "id", "") or "") == canonical_event_id
    ), -1)
    freeze_index = next((
        index for index, event in enumerate(events)
        if str(getattr(event, "id", "") or "") == freeze_event_id
    ), -1)
    if source_index < 0:
        raise ValueError("canonical PRD freeze source event is missing before freeze")
    # A source event must precede its actual freeze occurrence. Building the
    # packet from the freeze prefix would otherwise admit messages/roster edits
    # made after consensus but before an operator emitted the handoff.
    if freeze_index < 0 or source_index >= freeze_index:
        raise ValueError("canonical PRD freeze source event does not precede freeze")
    source_events = events[:source_index + 1]
    channel = project_channel(state_dir, channel_id, events=source_events) or {}
    consensus = (channel.get("consensus") or {}).get(thread_id) or {}
    required = set(consensus.get("required_signers") or [])
    review_complete = (
        bool(required) and required <= set(consensus.get("signed") or {})
        if normalize_product_discussion_mode(consensus.get("product_mode")) == "multi_lens"
        else consensus.get("human_confirmed") is True
    )
    freeze_type = str(freeze.get("event_type") or "channel.prd.frozen")
    matching_synthesis = next((
        synthesis for synthesis in channel.get("syntheses") or []
        if isinstance(synthesis, dict)
        and str(synthesis.get("thread_id") or "main") == thread_id
        and synthesis.get("artifact_ref") == freeze.get("prd_ref")
        and str(synthesis.get("artifact_digest") or "").removeprefix("sha256:")
            == str(freeze.get("prd_digest") or "").removeprefix("sha256:")
    ), None)
    if (
        not canonical_event_id or consensus.get("reached_event_id") != canonical_event_id
        or not review_complete
        or consensus.get("blocked") or consensus.get("revision_pending")
        or any(q.get("status") == "open" and str(q.get("thread_id") or "main") == thread_id
               for q in channel.get("open_questions") or [])
        or matching_synthesis is None
    ):
        raise ValueError("canonical PRD freeze lacks matching synthesis or complete review")
    if _revision_value(consensus.get("prd_revision")) != prd_revision:
        raise ValueError("canonical PRD freeze consensus revision mismatch")
    # The freeze occurrence must carry the same readiness lineage that was
    # reviewed for this exact synthesis.  Otherwise a forged freeze could
    # swap in another same-identity readiness artifact before evaluation.
    if _revision_value(
        matching_synthesis.get("prd_revision"),
        fallback=matching_synthesis.get("revision"),
    ) != prd_revision:
        raise ValueError("canonical PRD freeze synthesis revision mismatch")
    if (
        str(matching_synthesis.get("readiness_ref") or "")
        != str(freeze.get("readiness_ref") or "")
        or _bare_digest(matching_synthesis.get("readiness_digest"))
        != _bare_digest(freeze.get("readiness_digest"))
    ):
        raise ValueError("canonical PRD freeze readiness lineage mismatched")
    workflow_run_id = evaluation_run_id(
        channel_id=channel_id, thread_id=thread_id, prd_revision=prd_revision,
        occurrence_id=str(freeze.get("event_id") or ""),
    )

    messages: list[dict[str, Any]] = []

    prd_body = _read_json_artifact(
        state_dir, str(freeze.get("prd_ref") or ""),
        expected_digest=str(freeze.get("prd_digest") or ""),
    )
    readiness_ref = str(freeze.get("readiness_ref") or "").strip()
    readiness_digest = str(freeze.get("readiness_digest") or "").strip()
    if prd_revision > 0 and not readiness_ref and not readiness_digest:
        raise ValueError("canonical PRD readiness is missing")
    if bool(readiness_ref) != bool(readiness_digest):
        raise ValueError("canonical PRD readiness ref and digest must be supplied together")
    readiness = _read_json_artifact(
        state_dir,
        readiness_ref,
        expected_digest=readiness_digest if readiness_ref else None,
    )
    if readiness_ref and readiness is None:
        raise ValueError("canonical PRD readiness artifact is unreadable")
    if readiness is not None:
        _require_artifact_identity(
            readiness,
            channel_id=channel_id,
            thread_id=thread_id,
            prd_revision=prd_revision,
            label="readiness",
        )
    source_ref = str(freeze.get("source_snapshot_ref") or "").strip()
    source_digest = str(freeze.get("source_snapshot_digest") or "").strip()
    inline_snapshot = freeze.get("source_snapshot")
    if not source_ref and inline_snapshot is None:
        raise ValueError("canonical PRD source snapshot is missing")
    # A freeze handoff may carry the immutable snapshot inline, in which case
    # the digest is still mandatory but there is no sidecar ref.  A ref, when
    # present, is authoritative and also requires its digest.
    if not source_digest:
        raise ValueError("canonical PRD source snapshot digest is missing")
    source_snapshot = _read_json_artifact(
        state_dir,
        source_ref,
        expected_digest=source_digest if source_ref else None,
    )
    if source_ref and source_snapshot is None:
        # A ref is authoritative. Never replace an unreadable/mismatched
        # sidecar with a convenient inline copy from the event payload.
        raise ValueError("canonical PRD freeze source snapshot is unreadable")
    if not source_ref and inline_snapshot is not None:
        if not isinstance(inline_snapshot, dict) or not source_digest:
            raise ValueError("inline source snapshot must have a bound digest")
        if _bare_digest(source_digest) != stable_digest(inline_snapshot):
            raise ValueError("inline source snapshot digest mismatched")
        source_snapshot = dict(inline_snapshot)
    if source_snapshot is not None and not isinstance(source_snapshot, dict):
        raise ValueError("canonical PRD source snapshot must be an object")
    if source_snapshot is not None:
        _require_artifact_identity(
            source_snapshot,
            channel_id=channel_id,
            thread_id=thread_id,
            prd_revision=prd_revision,
            source_event_id=canonical_event_id,
            prd_ref=str(freeze.get("prd_ref") or ""),
            prd_digest=str(freeze.get("prd_digest") or ""),
            readiness_ref=readiness_ref,
            readiness_digest=readiness_digest,
            label="source snapshot",
        )
        _validate_source_snapshot_manifest(
            source_snapshot, source_events,
            channel_id=channel_id, thread_id=thread_id,
            source_event_id=canonical_event_id,
        )
        try:
            messages = _packet_messages(
                _snapshot_channel(source_snapshot, channel_id=channel_id),
                channel_id=channel_id, thread_id=thread_id, freeze_event_id="",
                hydrate=lambda payload: _strict_snapshot_message_text(state_dir, payload),
            )
        except Exception as exc:
            raise ValueError(f"evidence_missing: frozen message body unavailable: {exc}") from exc
    if not messages:
        return None
    if not prd_body:
        return None  # PRD §8.1: frozen PRD must be readable or we do not trigger
    if prd_body.get("schema_version") == "channel-prd.v1" and (
        prd_body.get("channel_id"), prd_body.get("thread_id"), prd_body.get("revision")
    ) != (channel_id, thread_id, prd_revision):
        raise ValueError("canonical PRD identity does not match freeze")

    frozen_prd = {
        "prd_ref": str(freeze.get("prd_ref") or ""),
        "prd_digest": str(freeze.get("prd_digest") or ""),
        "body": prd_body,
        "readiness_ref": readiness_ref,
        "readiness_digest": readiness_digest,
        "readiness_verdict": str(readiness.get("verdict") or "") if readiness else "",
        "gaps": readiness.get("gaps") if isinstance(readiness, dict) else [],
        "risks": readiness.get("risks") if isinstance(readiness, dict) else [],
        "source_snapshot_ref": str(freeze.get("source_snapshot_ref") or ""),
        "source_snapshot_digest": str(freeze.get("source_snapshot_digest") or ""),
        "source_snapshot": source_snapshot or {},
    }
    bound_prd = _bound_prd_payload(
        state_dir, bound_workflows, frozen_prd,
        channel_id=channel_id, thread_id=thread_id, prd_revision=prd_revision,
    )

    requirement_message_id = str(
        ((channel.get("discussions") or {}).get(thread_id) or {}).get(
            "requirement_message_id"
        ) or ""
    )
    # Derive the requirement from the already-hydrated frozen message slice.
    # Re-reading the projection through channel_requirement_text would permit
    # a relaxed sidecar fallback and make the packet depend on mutable state.
    requirement_messages = [
        message for message in messages
        if (
            str(message.get("role") or "").lower() in {"human", "user", "owner"}
            or (
                requirement_message_id
                and str(message.get("message_id") or "") == requirement_message_id
            )
        )
    ]
    task_statement = ""
    for message in messages:
        if requirement_message_id and str(message.get("message_id") or "") != requirement_message_id:
            continue
        if str(message.get("thread_id") or "") != thread_id:
            continue
        text = str(message.get("text") or "").strip()
        if text:
            task_statement = text
            break
    task = {
        "task_id": str(channel.get("origin_task_id") or channel_id),
        "statement": task_statement,
    }
    discussion_events = _snapshot_discussion_events(
        source_snapshot, channel_id=channel_id, thread_id=thread_id,
    )
    task_details = {
        "statement": task_statement,
        "source": "channel-requirement",
        "evidence_keys": [],
        "original_requirement": {
            "statement": task_statement,
            "evidence_refs": [message["evidence_ref"] for message in requirement_messages],
        },
        "original_requirement_text": task_statement,
        "clarifications": requirement_messages,
        "discussion_events": discussion_events,
        "freeze_event_id": str(freeze.get("event_id") or ""),
        "freeze_event_type": freeze_type,
        "freeze_reason": str(freeze.get("freeze_reason") or ""),
    }
    catalog = _snapshot_evaluation_roles() if role_catalog is None else role_catalog
    task_details["evaluation_role_catalog"] = catalog
    profiles = _frozen_profiles(channel, thread_id, messages, state_dir=state_dir)
    iteration_summary = {
        "member_count": len(profiles),
        "prd_revision": prd_revision,
        "message_count": len(messages),
        "complexity": str(
            (channel.get("complexity_decisions") or {})
            .get(thread_id, {})
            .get("complexity")
            or ""
        ),
    }

    # Identity covers the FROZEN inputs only. ``bound_prd`` is excluded so a
    # later workflow bind does not re-run contribution/discussion;
    # prd_fidelity folds a bound marker into its own digest instead (it
    # re-runs on bind).
    snapshot_source = {
        "task": task,
        "task_details": task_details,
        "agent_profiles": profiles,
        "messages": messages,
        "frozen_prd": frozen_prd,
    }
    source_snapshot_digest = stable_digest(snapshot_source)
    bound_digest = stable_digest(bound_prd or {})
    trial_id = source_snapshot_digest[:16]

    packets = {}
    for judge_type in ("contribution", "discussion", "prd_fidelity"):
        digest = source_snapshot_digest if judge_type != "prd_fidelity" else stable_digest({
            "source": source_snapshot_digest, "bound": bound_digest,
        })
        packet_task_details = _subagent_task_details(task_details)
        packets[judge_type] = build_packet(
            stage=judge_type, run_id=workflow_run_id, workflow_run_id=workflow_run_id,
            trial_id=digest[:16], source_snapshot_digest=digest, task=task,
            task_details=packet_task_details, agent_profiles=profiles, agent_relationships=[],
            iteration_summary=iteration_summary, channel_messages=messages,
            channel_frozen_prd=frozen_prd, workflow_bound_prd=bound_prd or {},
            evaluator_profile=judge_type,
            subagent_scope=judge_type,
        ).to_dict()
    # Classification is its own persisted provider turn.  It is not a fourth
    # quality score and cannot observe any subagent judgment.
    packets["task_type"] = build_packet(
        stage="task_type", run_id=workflow_run_id, workflow_run_id=workflow_run_id,
        trial_id=trial_id, source_snapshot_digest=source_snapshot_digest,
        task=task, task_details=task_details, agent_profiles=profiles,
        iteration_summary=iteration_summary, channel_messages=messages,
        channel_frozen_prd=frozen_prd, evaluator_profile="task_type",
    ).to_dict()
    return {
        "channel_id": channel_id, "workflow_run_id": workflow_run_id,
        "trial_id": trial_id, "freeze": freeze, "packets": packets,
        "source_snapshot_digest": packets["prd_fidelity"]["source_snapshot_digest"],
        "evaluation_contract": "channel-prd-eval.v1",
    }


def _freeze_points(events: list[Any]) -> dict[str, dict[str, Any]]:
    """Return every canonical freeze occurrence keyed by its full identity.

    A channel can have several independent threads and revisions. Keeping
    only channel_id here silently replaced an older revision with a later
    one before packet construction. The event id is part of the key so two
    valid freeze occurrences for the same revision also remain auditable.
    """

    points: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type not in FREEZE_TRIGGER_EVENTS or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        source_refs = payload.get("source_refs") if isinstance(payload.get("source_refs"), dict) else {}
        channel_id = str(payload.get("channel_id") or "").strip()
        thread_id = str(payload.get("thread_id") or "")
        if not channel_id or not thread_id or not str(event.id or ""):
            continue
        if "prd_revision" in payload:
            revision_raw = payload.get("prd_revision")
        elif "revision" in payload:
            revision_raw = payload.get("revision")
        elif "channel_prd_revision" in source_refs:
            revision_raw = source_refs.get("channel_prd_revision")
        else:
            revision_raw = None
        if revision_raw is None:
            continue
        prd_revision = _strict_revision(revision_raw)
        if prd_revision < 1:
            continue
        identity = [channel_id, thread_id, prd_revision, str(event.id)]
        key = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
        points[key] = {
            "event_id": str(event.id),
            "event_type": event.type,
            "channel_id": channel_id,
            "thread_id": thread_id,
            "consensus_event_id": str(
                payload.get("source_event_id")
                or payload.get("consensus_event_id")
                or ""
            ),
            "ts": event.ts or "",
            "prd_ref": str(
                payload.get("prd_ref")
                or payload.get("artifact_ref")
                or source_refs.get("channel_prd_ref")
                or ""
            ),
            "prd_digest": str(
                payload.get("prd_digest")
                or payload.get("artifact_digest")
                or source_refs.get("channel_prd_digest")
                or ""
            ),
            "prd_revision": prd_revision,
            "readiness_ref": str(
                payload.get("readiness_ref")
                or source_refs.get("readiness_ref")
                or source_refs.get("channel_prd_readiness_ref")
                or ""
            ),
            "readiness_digest": str(
                payload.get("readiness_digest")
                or source_refs.get("readiness_digest")
                or source_refs.get("channel_prd_readiness_digest")
                or ""
            ),
            "task_id": str(payload.get("task_id") or ""),
            "freeze_reason": str(payload.get("freeze_reason") or ""),
            "source_snapshot_ref": str(
                payload.get("source_snapshot_ref")
                or source_refs.get("source_snapshot_ref")
                or ""
            ),
            "source_snapshot_digest": str(
                payload.get("source_snapshot_digest")
                or source_refs.get("source_snapshot_digest")
                or ""
            ),
            "source_snapshot": payload.get("source_snapshot")
            if isinstance(payload.get("source_snapshot"), dict)
            else None,
        }
    return points



def _validate_source_snapshot_manifest(
    snapshot: dict[str, Any], events: list[Any], *,
    channel_id: str, thread_id: str, source_event_id: str,
) -> None:
    """Verify the freeze snapshot contains the canonical event manifest.

    The freeze helper records decision-bearing events through the consensus
    source occurrence.  Rebuilding that same manifest here prevents a forged
    or incomplete inline snapshot from silently changing the evidence set.
    """
    raw_events = snapshot.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("source snapshot event manifest is missing")
    from zf.runtime.channel_prd_freeze import source_snapshot_event_manifest

    expected = source_snapshot_event_manifest(
        events, channel_id=channel_id, thread_id=thread_id,
        source_event_id=source_event_id,
    )
    if raw_events != expected:
        raise ValueError("source snapshot event manifest does not match freeze ledger")


def _snapshot_channel(snapshot: dict[str, Any], *, channel_id: str) -> dict[str, Any]:
    """Expose the verified source manifest in the existing packet helper shape."""
    messages = []
    for item in snapshot.get("events") or []:
        if not isinstance(item, dict) or item.get("event_type") != "channel.message.posted":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        messages.append({**payload, "event_id": str(item.get("event_id") or "")})
    return {"channel_id": channel_id, "messages": messages}


def _strict_snapshot_message_text(state_dir: Path, payload: dict[str, Any]) -> str:
    """Accept a full sidecar body or an inline legacy message, never a preview."""
    from zf.runtime.channel_sidecar import (
        CHANNEL_MESSAGE_BODY_SCHEMA_VERSION, channel_payload_sidecar_descriptor,
        hydrate_channel_message_text,
    )
    from zf.runtime.sidecar_refs import hydrate_sidecar_ref

    descriptor = channel_payload_sidecar_descriptor(payload, "message_body")
    if not descriptor and str(payload.get("body_ref") or "").strip():
        descriptor = {
            "kind": "channel_message_body",
            "ref": str(payload["body_ref"]),
            "sha256": str(payload.get("body_sha256") or ""),
            "byte_count": int(payload.get("body_byte_count") or 0),
            "content_type": "application/json",
            "schema_version": CHANNEL_MESSAGE_BODY_SCHEMA_VERSION,
            "encoding": "utf-8",
            "required": True,
        }
    if descriptor:
        body = hydrate_sidecar_ref(state_dir, descriptor).payload
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError("declared channel message sidecar has no text body")
        return text
    return hydrate_channel_message_text(state_dir, payload, strict=True)


def _snapshot_discussion_events(
    snapshot: dict[str, Any], *, channel_id: str, thread_id: str,
) -> list[dict[str, Any]]:
    """Reuse event filtering over the already verified source manifest."""
    from zf.core.events.model import ZfEvent

    events = [
        ZfEvent(
            type=str(item.get("event_type") or ""), id=str(item.get("event_id") or ""),
            actor=str(item.get("actor") or ""), ts=str(item.get("ts") or ""),
            payload=dict(item.get("payload") or {}),
        )
        for item in snapshot.get("events") or [] if isinstance(item, dict)
    ]
    return _frozen_discussion_events(events, channel_id=channel_id, thread_id=thread_id)


def _frozen_profiles(
    channel: dict[str, Any],
    thread_id: str,
    messages: list[dict[str, Any]],
    *, state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Agent profiles from FROZEN facts only — never runtime state.

    The evaluation identity (§8.2) binds a source snapshot digest; if any
    runtime-volatile field (member connection status, typing, heartbeats)
    leaked in, every observer pass would mint a new digest and re-run the
    judges forever (2026-09-16 incident). Participation authority is the
    discussion roster recorded at discussion start; fallback is the sorted
    set of member ids present in the frozen message slice. Member metadata
    is limited to config-static keys.
    """

    session = (channel.get("discussions") or {}).get(thread_id) or {}
    roster = [
        str(member_id).strip()
        for member_id in (session.get("roster") or [])
        if str(member_id).strip()
    ]
    if not roster:
        roster = sorted({
            str(message.get("member_id") or "")
            for message in messages
            if str(message.get("member_id") or "").strip()
        })
    members = {
        str(member.get("member_id") or ""): member
        for member in (channel.get("members") or [])
        if isinstance(member, dict) and str(member.get("member_id") or "").strip()
    }
    profiles: list[dict[str, Any]] = []
    for member_id in roster:
        member = members.get(member_id) or {}
        profile = {
            "agent_id": member_id,
            # The name communicates the Agent's purpose and remains visible
            # to judges.  Mutable connection/typing/presence are excluded.
            "agent_name": str(member.get("name") or member.get("display_name") or member_id),
            "channel_role": str(
                member.get("channel_role") or member.get("role") or ""
            ),
            "role_context_ref": str(member.get("role_context_ref") or ""),
            "role_source": "declared" if member.get("channel_role") or member.get("role") else "unspecified",
        }
        # Member profile snapshots are persisted when the role is assigned;
        # reading that frozen body does not consult a live role.md file.
        if state_dir is not None and member.get("profile_snapshot_ref"):
            from zf.runtime.channel_profiles import load_channel_profile_role_definition

            definition = load_channel_profile_role_definition(state_dir, member)
            profile["role_definition"] = definition
        profiles.append(profile)
    return profiles


def _subagent_task_details(details: dict[str, Any]) -> dict[str, Any]:
    """Project the frozen task context before a role is selected.

    Keep the catalog digest and source material references, but avoid putting
    every task role body into each quality subagent's input packet.  The
    classifier packet remains the authoritative full catalog snapshot.
    """

    projected = dict(details)
    catalog = projected.pop("evaluation_role_catalog", None)
    if isinstance(catalog, dict):
        projected["evaluation_role_catalog_ref"] = {
            "schema_version": catalog.get("schema_version") or catalog.get("catalog_version") or "",
            "catalog_version": catalog.get("catalog_version") or "",
            "catalog_digest": catalog.get("catalog_digest") or "",
        }
    return projected


def _frozen_discussion_events(
    events: list[Any], *, channel_id: str = "", thread_id: str = "",
) -> list[dict[str, Any]]:
    """Capture decision-bearing discussion occurrences in the frozen packet.

    Message bodies remain in ``channel_messages``.  This
    compact event index preserves questions, reviews and decisions that may
    not have a message projection, while excluding runtime presence noise.
    """

    ignored = {
        "channel.member.connected", "channel.member.disconnected",
        "channel.typing.started", "channel.typing.stopped", "channel.message.read",
    }
    output: list[dict[str, Any]] = []
    target_channel = str(channel_id or "").strip()
    target_thread = str(thread_id or "").strip()
    if not target_channel or not target_thread:
        return []
    for event in events:
        event_id = str(getattr(event, "id", "") or "")
        event_type = str(getattr(event, "type", "") or "")
        payload = getattr(event, "payload", {})
        if not event_id or event_type in ignored or not event_type.startswith("channel."):
            continue
        if not isinstance(payload, dict):
            continue
        event_channel = str(payload.get("channel_id") or "").strip()
        event_thread = str(payload.get("thread_id") or "").strip()
        # This helper receives the complete event prefix, which may contain
        # other channels and threads. Missing identity is never inferred from
        # the event correlation id or a default thread.
        if event_channel != target_channel or event_thread != target_thread:
            continue
        output.append({
            "evidence_ref": f"channel/event/{event_id}",
            "event_id": event_id,
            "event_type": event_type,
            "channel_id": event_channel,
            "thread_id": event_thread,
            "actor": str(getattr(event, "actor", "") or ""),
            "payload": payload,
        })
    return output


def _snapshot_evaluation_roles() -> dict[str, Any]:
    """Freeze the evaluator role catalog before any provider turn.

    ``evaluation_roles`` is deliberately a small seam owned by the role
    catalog implementation.  The observer does not fall back to a live file
    or invent a profile: a missing/invalid catalog blocks the evaluation.
    """

    from zf.runtime.evaluation_roles import snapshot_catalog

    return snapshot_catalog()


def _packet_messages(
    channel: dict[str, Any],
    *,
    freeze_event_id: str,
    hydrate: Any,
    channel_id: str = "",
    thread_id: str = "",
) -> list[dict[str, Any]]:
    """Messages up to the freeze event, filtered by frozen identity."""

    target_channel = str(channel_id or channel.get("channel_id") or "").strip()
    target_thread = str(thread_id or "").strip()
    if not target_channel or not target_thread:
        return []
    messages: list[dict[str, Any]] = []
    for message in channel.get("messages") or []:
        if not isinstance(message, dict):
            continue
        event_id = str(message.get("event_id") or "")
        if not event_id:
            continue
        # ``project_channel`` already scoped this list to target_channel; the
        # projection omits a redundant channel_id on ordinary messages.
        message_channel = str(message.get("channel_id") or target_channel).strip()
        message_thread = str(message.get("thread_id") or "").strip()
        if message_channel != target_channel or message_thread != target_thread:
            continue
        if freeze_event_id and event_id == freeze_event_id:
            break
        text = hydrate(message)
        messages.append({
            "evidence_ref": message_evidence_ref(event_id),
            "event_id": event_id,
            "message_id": str(message.get("message_id") or ""),
            "channel_id": message_channel,
            "thread_id": message_thread,
            "member_id": str(message.get("member_id") or ""),
            "role": str(message.get("role") or ""),
            "text": str(text or ""),
        })
    return messages


__all__ = [
    "FREEZE_TRIGGER_EVENTS",
    "TASK_TYPES",
    "_prepare_channel",
    "_freeze_points",
    "_frozen_profiles",
    "_subagent_task_details",
    "_frozen_discussion_events",
    "_snapshot_evaluation_roles",
    "_packet_messages",
    "missing_prd_fidelity_inputs",
]
