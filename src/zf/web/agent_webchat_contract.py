"""Versioned WebChat contracts and legacy compatibility projection.

This module is intentionally read-only. It normalizes interaction metadata but
never mutates delivery stores or artifact bodies.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


CONTRACT_VERSIONS = {
    "message": "webchat-message.v1",
    "turn": "webchat-turn.v1",
    "interaction": "webchat-interaction.v1",
    "artifact_version": "webchat-artifact-version.v1",
}

TURN_STATES = frozenset({
    "submitted",
    "queued",
    "claimed",
    "running",
    "waiting_input",
    "resume_queued",
    "retry_wait",
    "completed",
    "failed",
    "cancelled",
    "abandoned",
})
TURN_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "abandoned"})


@dataclass(frozen=True)
class SurfaceCapabilities:
    surface: str
    actions: tuple[str, ...]
    controls: tuple[str, ...]
    tool_classes: tuple[str, ...]
    completion_authority: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "webchat-surface-capabilities.v1",
            "surface": self.surface,
            "actions": list(self.actions),
            "controls": list(self.controls),
            "tool_classes": list(self.tool_classes),
            "completion_authority": self.completion_authority,
        }


_SURFACE_CAPABILITIES = {
    "kanban_agent": SurfaceCapabilities(
        surface="kanban_agent",
        actions=(
            "ask_user",
            "tool_approval",
            "action_approval",
            "artifact_publish",
            "artifact_adoption",
            "conversation_manage",
        ),
        controls=("queue", "steer", "interrupt", "cancel"),
        tool_classes=("read", "workspace", "controlled", "external"),
        completion_authority=False,
    ),
    "channel": SurfaceCapabilities(
        surface="channel",
        actions=("ask_user", "artifact_publish", "channel_handoff"),
        controls=("queue", "cancel"),
        tool_classes=("read", "external"),
        completion_authority=False,
    ),
    "worker": SurfaceCapabilities(
        surface="worker",
        actions=("report_evidence",),
        controls=("interrupt", "cancel"),
        tool_classes=("read", "workspace", "controlled"),
        completion_authority=False,
    ),
}


def capabilities_for_surface(surface: str, *, native_steer: bool = False) -> dict[str, Any]:
    key = surface.strip().lower().replace("-", "_")
    capability = _SURFACE_CAPABILITIES.get(key)
    if capability is None:
        raise ValueError(f"unknown WebChat surface {surface!r}")
    result = capability.as_dict()
    result["native_steer"] = bool(native_steer and "steer" in capability.controls)
    result["steer_fallback"] = "queue" if "steer" in capability.controls and not native_steer else ""
    return result


def validate_contract(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    expected = CONTRACT_VERSIONS.get(kind)
    if expected is None:
        raise ValueError(f"unknown WebChat contract kind {kind!r}")
    version = str(payload.get("schema_version") or "")
    if version != expected:
        raise ValueError(f"unsupported {kind} schema {version!r}; expected {expected!r}")
    required = {
        "message": ("message_id", "conversation_id", "thread_id", "content_ref", "content_digest"),
        "turn": ("turn_id", "conversation_id", "thread_id", "state"),
        "interaction": ("interaction_id", "turn_id", "kind", "revision", "request_digest"),
        "artifact_version": ("artifact_id", "version_id", "digest", "storage_ref"),
    }[kind]
    missing = [name for name in required if payload.get(name) in (None, "")]
    if missing:
        raise ValueError(f"{kind} contract missing: {', '.join(missing)}")
    if kind == "turn" and str(payload["state"]) not in TURN_STATES:
        raise ValueError(f"invalid turn state {payload['state']!r}")
    if kind == "interaction" and int(payload["revision"]) < 1:
        raise ValueError("interaction revision must be positive")
    for field in ("content_digest", "request_digest", "digest"):
        value = str(payload.get(field) or "")
        if value and not value.startswith("sha256:"):
            raise ValueError(f"{field} must use sha256")
    return dict(payload)


def _event_value(event: Any, name: str, default: Any = "") -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _payload(event: Any) -> dict[str, Any]:
    value = _event_value(event, "payload", {})
    return dict(value) if isinstance(value, Mapping) else {}


def build_legacy_shadow_projection(events: Iterable[Any]) -> dict[str, Any]:
    """Normalize legacy/new session events without writing any state.

    Stable semantic keys deduplicate a legacy reply containing an embedded
    plan/proposal and the corresponding first-class event emitted beside it.
    """

    items: dict[tuple[str, str], dict[str, Any]] = {}
    terminal_turns: dict[str, str] = {}
    waiting_input_turns: dict[str, str] = {}
    ignored_late = 0
    for event in events:
        event_type = str(_event_value(event, "type"))
        event_id = str(_event_value(event, "id"))
        payload = _payload(event)
        turn_id = str(payload.get("turn_id") or payload.get("run_id") or "")
        if event_type in {"kanban.agent.turn.completed", "kanban.agent.turn.failed"}:
            logical_state = str(payload.get("logical_state") or "")
            if logical_state == "waiting_input":
                waiting_input_turns[turn_id] = logical_state
                continue
            state = "completed" if event_type.endswith("completed") else "failed"
            terminal_turns[turn_id] = state
            items[("terminal", turn_id)] = {
                "kind": "terminal",
                "identity": turn_id,
                "state": state,
                "source_event_id": event_id,
                "adapter_provenance": "legacy_event",
            }
            continue
        if event_type == "agent.session.run.delta" and turn_id in terminal_turns:
            ignored_late += 1
            continue
        if event_type == "user.message":
            identity = str(payload.get("message_id") or event_id)
            items[("user", identity)] = {
                "kind": "user",
                "identity": identity,
                "source_event_id": event_id,
                "adapter_provenance": "legacy_event",
            }
            continue
        if event_type == "kanban.agent.reply":
            identity = str(payload.get("message_id") or event_id)
            items[("assistant", identity)] = {
                "kind": "assistant",
                "identity": identity,
                "source_event_id": event_id,
                "adapter_provenance": "legacy_event",
            }
            plan = payload.get("plan_request")
            if isinstance(plan, Mapping):
                plan_id = str(plan.get("request_id") or plan.get("plan_id") or turn_id or event_id)
                items[("plan", plan_id)] = {
                    "kind": "plan",
                    "identity": plan_id,
                    "source_event_id": event_id,
                    "adapter_provenance": "embedded_legacy_reply",
                }
            proposal = payload.get("action_proposal")
            if isinstance(proposal, Mapping):
                proposal_id = str(proposal.get("proposal_id") or turn_id or event_id)
                items[("proposal", proposal_id)] = {
                    "kind": "proposal",
                    "identity": proposal_id,
                    "source_event_id": event_id,
                    "adapter_provenance": "embedded_legacy_reply",
                }
            continue
        if event_type in {"kanban.agent.plan.requested", "kanban.agent.plan.proposed"}:
            identity = str(payload.get("request_id") or payload.get("plan_id") or turn_id or event_id)
            items[("plan", identity)] = {
                "kind": "plan",
                "identity": identity,
                "source_event_id": event_id,
                "adapter_provenance": "first_class_event",
            }
            continue
        if event_type in {"kanban.agent.action.proposed", "action.proposed"}:
            proposal = payload.get("proposal") if isinstance(payload.get("proposal"), Mapping) else payload
            identity = str(proposal.get("proposal_id") or turn_id or event_id)
            items[("proposal", identity)] = {
                "kind": "proposal",
                "identity": identity,
                "source_event_id": event_id,
                "adapter_provenance": "first_class_event",
            }
            continue
        if event_type.startswith("agent.session.tool."):
            identity = str(payload.get("tool_call_id") or payload.get("part_id") or event_id)
            items[("tool", identity)] = {
                "kind": "tool",
                "identity": identity,
                "source_event_id": event_id,
                "adapter_provenance": "agent_session_event",
            }

    ordered = list(items.values())
    counts: dict[str, int] = {}
    for item in ordered:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {
        "schema_version": "webchat-shadow-projection.v1",
        "is_derived_projection": True,
        "items": ordered,
        "counts": counts,
        "terminal_turns": terminal_turns,
        "waiting_input_turns": waiting_input_turns,
        "ignored_late_delta_count": ignored_late,
    }


__all__ = [
    "CONTRACT_VERSIONS",
    "TURN_STATES",
    "TURN_TERMINAL_STATES",
    "build_legacy_shadow_projection",
    "capabilities_for_surface",
    "validate_contract",
]
