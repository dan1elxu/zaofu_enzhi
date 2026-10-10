from __future__ import annotations

import json
from pathlib import Path

import pytest

from zf.web.agent_webchat_contract import (
    build_legacy_shadow_projection,
    capabilities_for_surface,
    validate_contract,
)


def test_contract_version_and_required_identity_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        validate_contract("turn", {"schema_version": "webchat-turn.v2"})
    with pytest.raises(ValueError, match="missing"):
        validate_contract("message", {"schema_version": "webchat-message.v1"})
    assert validate_contract("turn", {
        "schema_version": "webchat-turn.v1", "turn_id": "t", "conversation_id": "c",
        "thread_id": "th", "state": "waiting_input",
    })["state"] == "waiting_input"


def test_surface_capabilities_have_negative_authority_boundaries() -> None:
    kanban = capabilities_for_surface("kanban_agent", native_steer=False)
    channel = capabilities_for_surface("channel")
    worker = capabilities_for_surface("worker")
    assert kanban["steer_fallback"] == "queue"
    assert "action_approval" not in channel["actions"]
    assert "conversation_manage" not in worker["actions"]
    assert all(item["completion_authority"] is False for item in (kanban, channel, worker))


def test_shadow_projection_deduplicates_embedded_and_first_class_cards() -> None:
    events = [
        {"id": "u1", "type": "user.message", "payload": {}},
        {"id": "r1", "type": "kanban.agent.reply", "payload": {
            "turn_id": "turn-1",
            "plan_request": {"request_id": "plan-1"},
            "action_proposal": {"proposal_id": "proposal-1"},
        }},
        {"id": "p1", "type": "kanban.agent.plan.requested", "payload": {"request_id": "plan-1"}},
        {"id": "a1", "type": "kanban.agent.action.proposed", "payload": {"proposal": {"proposal_id": "proposal-1"}}},
        {"id": "done", "type": "kanban.agent.turn.completed", "payload": {"turn_id": "turn-1"}},
        {"id": "late", "type": "agent.session.run.delta", "payload": {"turn_id": "turn-1"}},
    ]
    projection = build_legacy_shadow_projection(events)
    assert projection["counts"] == {"user": 1, "assistant": 1, "plan": 1, "proposal": 1, "terminal": 1}
    assert projection["ignored_late_delta_count"] == 1


def test_shadow_projection_does_not_treat_waiting_input_as_terminal() -> None:
    projection = build_legacy_shadow_projection([{
        "id": "provider-exit",
        "type": "kanban.agent.turn.completed",
        "payload": {"turn_id": "turn-question", "logical_state": "waiting_input"},
    }])
    assert projection["terminal_turns"] == {}
    assert projection["waiting_input_turns"] == {"turn-question": "waiting_input"}
    assert projection["counts"] == {}


def test_frozen_legacy_fixture_has_shadow_parity_without_duplicate_cards() -> None:
    fixture = Path(__file__).parent / "fixtures" / "webchat" / "legacy-kanban-channel-workflow.json"
    events = json.loads(fixture.read_text(encoding="utf-8"))
    projection = build_legacy_shadow_projection(events)
    assert projection["counts"] == {
        "user": 1, "assistant": 2, "plan": 2, "proposal": 1, "terminal": 1,
    }
    assert projection["terminal_turns"] == {"turn-1": "completed"}
    assert projection["waiting_input_turns"] == {"turn-2": "waiting_input"}
    identities = [(item["kind"], item["identity"]) for item in projection["items"]]
    assert identities.count(("plan", "plan-1")) == 1
    assert identities.count(("proposal", "proposal-1")) == 1
