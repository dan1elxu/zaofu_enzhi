from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from zf.web.agent_runtime_db import connect
from zf.web.agent_turn_store import (
    AgentInteractionValidationError,
    AgentTurnConflict,
    AgentTurnStore,
)


def _submit(store: AgentTurnStore, **overrides: object) -> dict:
    values = {
        "project_id": "project-1",
        "conversation_id": "conversation-1",
        "thread_id": "thread-1",
        "backend": "codex-headless",
        "permission_profile": "workspace_writer",
        "idempotency_key": "submit-1",
        "request": {"message": "implement API"},
    }
    values.update(overrides)
    return store.submit(**values)[0]  # type: ignore[arg-type]


def test_submit_is_durable_and_idempotent(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path / "state-custom")
    first, created = store.submit(
        project_id="p",
        conversation_id="c",
        thread_id="t",
        backend="codex-headless",
        permission_profile="read_only",
        idempotency_key="same",
        request={"message": "hello"},
    )
    second, duplicated = store.submit(
        project_id="p",
        conversation_id="c",
        thread_id="t",
        backend="codex-headless",
        permission_profile="read_only",
        idempotency_key="same",
        request={"message": "hello"},
    )
    assert created is True
    assert duplicated is False
    assert first["turn_id"] == second["turn_id"]
    assert second["request"] == {"message": "hello"}
    assert (tmp_path / "state-custom" / "operator" / "webchat.sqlite3").is_file()


def test_unknown_store_schema_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    AgentTurnStore(state)
    with sqlite3.connect(state / "operator" / "webchat.sqlite3") as conn:
        conn.execute(
            "UPDATE webchat_meta SET value = 'agent-webchat-runtime.v999' "
            "WHERE key = 'schema_version'"
        )
    with pytest.raises(RuntimeError, match="unsupported WebChat runtime schema"):
        AgentTurnStore(state)


def test_only_one_concurrent_claim_wins_and_stale_fence_cannot_settle(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)

    def claim(owner: str):
        try:
            return store.claim(turn["turn_id"], owner=owner, lease_seconds=1)
        except AgentTurnConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    winner = next(item for item in claims if item is not None)
    assert len([item for item in claims if item is not None]) == 1

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    assert len(store.reconcile_expired(now=expired)) == 0
    future = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    assert len(store.reconcile_expired(now=future)) == 1
    recovery = store.claim(turn["turn_id"], owner="worker-recovery")
    with pytest.raises(AgentTurnConflict, match="stale"):
        store.settle(winner, state="completed")
    settled = store.settle(recovery, state="completed")
    assert settled["state"] == "completed"
    assert [item["attempt_no"] for item in store.attempts(turn["turn_id"])] == [1, 2]


def test_claim_revalidates_pinned_digests(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(
        store,
        permission_digest="sha256:permission",
        tool_catalog_digest="sha256:tools",
        profile_digest="sha256:profile",
        context_digest="sha256:context",
    )
    with pytest.raises(AgentTurnConflict, match="tool_catalog_digest drift"):
        store.claim(
            turn["turn_id"],
            owner="worker",
            expected_digests={"tool_catalog_digest": "sha256:changed"},
        )


def test_committed_part_replay_is_ordered_and_terminal_cannot_append(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    claim = store.claim(turn["turn_id"], owner="worker")
    store.mark_running(claim)
    one = store.append_part(claim, kind="text", state="committed", preview="one")
    two = store.append_part(claim, kind="tool", state="committed", preview="two")
    page = store.replay_parts(turn["turn_id"], after_seq=one["seq"])
    assert [item["part_id"] for item in page["items"]] == [two["part_id"]]
    store.settle(claim, state="completed")
    with pytest.raises(AgentTurnConflict):
        store.append_part(claim, kind="text", state="committed", preview="late")


def test_part_replay_reports_explicit_refetch_cursor_when_bounded(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    claim = store.claim(turn["turn_id"], owner="worker")
    for index in range(12):
        store.append_part(claim, kind="text", state="committed", preview=str(index))
    first = store.replay_parts(turn["turn_id"], limit=5)
    assert first["truncated"] is True
    assert first["has_more"] is True
    assert first["next_after_seq"] == 5
    second = store.replay_parts(turn["turn_id"], after_seq=first["next_after_seq"], limit=20)
    assert [item["seq"] for item in second["items"]] == list(range(6, 13))
    assert second["truncated"] is False


def test_one_thousand_committed_parts_replay_without_cursor_gap(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    claim = store.claim(turn["turn_id"], owner="worker")
    for index in range(1_000):
        store.append_part(
            claim,
            kind="text",
            state="committed",
            preview=f"delta-{index}",
        )

    first = store.replay_parts(turn["turn_id"], limit=500)
    second = store.replay_parts(
        turn["turn_id"], after_seq=first["next_after_seq"], limit=500,
    )
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert [item["seq"] for item in [*first["items"], *second["items"]]] == list(
        range(1, 1_001)
    )
    assert store.get(turn["turn_id"])["stream_cursor"] == 1_000


def test_interaction_survives_reload_and_answer_consumes_once(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    claim = store.claim(turn["turn_id"], owner="worker")
    interaction, created = store.request_interaction(
        turn_id=turn["turn_id"],
        attempt_id=claim.attempt_id,
        kind="tool_approval",
        interaction_id="approval-1",
        request={
            "tool": "workspace.edit", "args_digest": "sha256:args",
            "choices": ["approve", "deny"],
        },
        currentness={"args_digest": "sha256:args", "attempt_id": claim.attempt_id},
    )
    assert created is True
    reloaded = AgentTurnStore(tmp_path)
    assert reloaded.get(turn["turn_id"])["state"] == "waiting_input"
    answered, consumed = reloaded.answer_interaction(
        "approval-1",
        revision=1,
        response={"approved": True},
        expected_currentness={"args_digest": "sha256:args", "attempt_id": claim.attempt_id},
    )
    assert consumed is True
    again, duplicate = reloaded.answer_interaction(
        "approval-1",
        revision=1,
        response={"approved": True},
    )
    assert duplicate is False
    assert answered["response_digest"] == again["response_digest"]
    _, first_consume = reloaded.consume_interaction(
        "approval-1",
        revision=1,
        expected_currentness={"args_digest": "sha256:args", "attempt_id": claim.attempt_id},
    )
    _, second_consume = reloaded.consume_interaction(
        "approval-1",
        revision=1,
        expected_currentness={"args_digest": "sha256:args", "attempt_id": claim.attempt_id},
    )
    assert first_consume is True
    assert second_consume is False


def test_applied_action_approval_can_complete_without_provider_resume(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    claim = store.claim(turn["turn_id"], owner="worker")
    store.request_interaction(
        turn_id=turn["turn_id"],
        attempt_id=claim.attempt_id,
        kind="action_approval",
        interaction_id="proposal-1",
        request={"action": "create-task", "proposal_id": "proposal-1"},
        currentness={"attempt_id": claim.attempt_id},
    )

    _, consumed = store.answer_interaction(
        "proposal-1",
        revision=1,
        response={"decision": "approved", "applied_action": "create-task"},
        expected_currentness={"attempt_id": claim.attempt_id},
        resume=False,
    )

    assert consumed is True
    assert store.get(turn["turn_id"])["state"] == "completed"
    assert store.get(turn["turn_id"])["pending_interaction_id"] == ""


def test_interaction_revision_supersedes_and_rejects_stale_answer(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    first, _ = store.request_interaction(
        turn_id=turn["turn_id"],
        kind="ask_user",
        interaction_id="question-1",
        revision=1,
        request={"question": "A?"},
    )
    second, _ = store.request_interaction(
        turn_id=turn["turn_id"],
        kind="ask_user",
        interaction_id="question-1",
        revision=2,
        request={"question": "B?"},
    )
    assert first["revision"] == 1 and second["revision"] == 2
    with pytest.raises(AgentTurnConflict, match="superseded"):
        store.answer_interaction("question-1", revision=1, response={"answer": "old"})


@pytest.mark.parametrize(
    ("interaction_payload", "message"),
    [
        ({"question": "Token?", "token": "do-not-store"}, "secret fields"),
        ({"question": "Pick", "options": [{"id": "same"}, {"id": "same"}]}, "mutually distinct"),
        ({"question": "x" * 2_001}, "exceeds 2000"),
    ],
)
def test_invalid_interaction_requests_fail_before_becoming_pending(
    tmp_path: Path,
    interaction_payload: dict,
    message: str,
) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    with pytest.raises(AgentInteractionValidationError, match=message):
        store.request_interaction(
            turn_id=turn["turn_id"], kind="ask_user", request=interaction_payload,
        )
    assert store.get(turn["turn_id"])["state"] == "queued"
    assert store.list_interactions(turn_id=turn["turn_id"]) == []


def test_queue_and_controls_are_server_durable_with_explicit_steer_fallback(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    store.claim(turn["turn_id"], owner="worker")
    receipt, created = store.apply_control(
        operation="steer",
        turn_id=turn["turn_id"],
        idempotency_key="steer-1",
        native_steer=False,
        message="change the API name",
    )
    assert created is True
    assert receipt["status"] == "queued_fallback"
    assert receipt["fallback"] == "queue"
    reloaded = AgentTurnStore(tmp_path)
    queue = reloaded.list_queue(project_id="project-1")
    assert len(queue) == 1
    assert queue[0]["message"] == "change the API name"
    duplicate, duplicate_created = reloaded.apply_control(
        operation="steer",
        turn_id=turn["turn_id"],
        idempotency_key="steer-1",
        native_steer=False,
        message="change the API name",
    )
    assert duplicate_created is False
    assert duplicate["receipt_id"] == receipt["receipt_id"]
    reloaded.cancel_queue(queue[0]["queue_id"], project_id="project-1")
    assert reloaded.list_queue(project_id="project-1") == []
    recent = reloaded.list_queue(
        project_id="project-1", include_terminal=True, terminal_limit=4,
    )
    assert [(item["queue_id"], item["state"]) for item in recent] == [
        (queue[0]["queue_id"], "cancelled"),
    ]


def test_cancel_fences_late_provider_completion(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    claim = store.claim(turn["turn_id"], owner="worker")
    store.mark_running(claim)
    receipt, _ = store.apply_control(
        operation="cancel",
        turn_id=turn["turn_id"],
        idempotency_key="cancel-1",
    )
    assert receipt["status"] == "accepted"
    assert store.get(turn["turn_id"])["state"] == "cancelled"
    with pytest.raises(AgentTurnConflict):
        store.settle(claim, state="completed")


def test_waiting_input_interrupt_conflicts_and_cancel_clears_pending_request(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    turn = _submit(store)
    claim = store.claim(turn["turn_id"], owner="worker")
    interaction, _ = store.request_interaction(
        turn_id=turn["turn_id"], attempt_id=claim.attempt_id,
        kind="ask_user", request={"question": "Choose?"},
    )
    with pytest.raises(AgentTurnConflict, match="waiting_input"):
        store.apply_control(
            operation="interrupt", turn_id=turn["turn_id"],
            idempotency_key="interrupt-waiting",
        )
    receipt, created = store.apply_control(
        operation="cancel", turn_id=turn["turn_id"], idempotency_key="cancel-waiting",
    )
    assert created is True and receipt["status"] == "accepted"
    assert store.get(turn["turn_id"])["pending_interaction_id"] == ""
    assert store.get_interaction(interaction["interaction_id"])["status"] == "cancelled"
    duplicate, duplicate_created = store.apply_control(
        operation="cancel", turn_id=turn["turn_id"], idempotency_key="cancel-waiting",
    )
    assert duplicate_created is False
    assert duplicate["receipt_id"] == receipt["receipt_id"]


def test_turn_store_does_not_create_delivery_state_files(tmp_path: Path) -> None:
    store = AgentTurnStore(tmp_path)
    _submit(store)
    assert not (tmp_path / "kanban.json").exists()
    assert not (tmp_path / "session.yaml").exists()
    assert not (tmp_path / "feature_list.json").exists()
    with connect(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM webchat_turn").fetchone()[0] == 1
