"""Durable CAS/fence state for WebChat turns and interactions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from zf.web.agent_runtime_db import (
    connect,
    digest_json,
    dumps,
    loads,
    transaction,
    utc_now,
)
from zf.web.agent_turn_contract import (
    AgentInteractionValidationError,
    AgentTurnConflict,
    AgentTurnNotFound,
    CONTROL_OPERATIONS,
    INTERACTION_KINDS,
    TURN_ACTIVE_STATES,
    TURN_CLAIMABLE_STATES,
    TURN_TERMINAL_STATES,
    TurnClaim,
    decode_runtime_row as _row,
    decode_turn_row as _turn_row,
    lease_expiry as _lease_expiry,
    new_runtime_id as _id,
    validate_interaction_request,
)
from zf.web.agent_turn_lifecycle import AgentTurnLifecycleMixin


class AgentTurnStore(AgentTurnLifecycleMixin):
    """CAS/fence protected interaction state; never mutates delivery truth."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        with connect(self.state_dir):
            pass

    def submit(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        backend: str,
        permission_profile: str,
        idempotency_key: str,
        turn_id: str = "",
        task_id: str = "",
        input_message_id: str = "",
        permission_digest: str = "",
        tool_catalog_digest: str = "",
        profile_digest: str = "",
        context_digest: str = "",
        request: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not all((project_id, conversation_id, thread_id, backend, idempotency_key)):
            raise ValueError("project_id, conversation_id, thread_id, backend and idempotency_key are required")
        now = utc_now()
        turn_id = turn_id.strip() or _id("turn")
        with transaction(self.state_dir) as conn:
            existing = conn.execute(
                "SELECT * FROM webchat_turn WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return _turn_row(existing) or {}, False
            existing_turn = conn.execute(
                "SELECT * FROM webchat_turn WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if existing_turn is not None:
                if any(
                    str(existing_turn[field]) != expected
                    for field, expected in (
                        ("project_id", project_id),
                        ("conversation_id", conversation_id),
                        ("thread_id", thread_id),
                        ("backend", backend),
                    )
                ):
                    raise AgentTurnConflict("turn identity reused across runtime scope")
                return _turn_row(existing_turn) or {}, False
            try:
                conn.execute(
                    """
                    INSERT INTO webchat_turn(
                      turn_id, idempotency_key, project_id, conversation_id,
                      thread_id, task_id, backend, permission_profile,
                      permission_digest, tool_catalog_digest, profile_digest,
                      context_digest, input_message_id, request_json, state,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        turn_id, idempotency_key, project_id, conversation_id,
                        thread_id, task_id, backend, permission_profile,
                        permission_digest, tool_catalog_digest, profile_digest,
                        context_digest, input_message_id, dumps(request or {}), now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AgentTurnConflict(str(exc)) from exc
            created = conn.execute(
                "SELECT * FROM webchat_turn WHERE turn_id = ?", (turn_id,),
            ).fetchone()
            assert created is not None
            return _turn_row(created) or {}, True

    def get(self, turn_id: str) -> dict[str, Any]:
        with connect(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM webchat_turn WHERE turn_id = ?", (turn_id,),
            ).fetchone()
        if row is None:
            raise AgentTurnNotFound(turn_id)
        return _turn_row(row) or {}

    def list_turns(
        self,
        *,
        project_id: str = "",
        conversation_id: str = "",
        thread_id: str = "",
        states: Iterable[str] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("conversation_id", conversation_id),
            ("thread_id", thread_id),
        ):
            if value:
                where.append(f"{column} = ?")
                args.append(value)
        state_values = [value for value in states if value]
        if state_values:
            where.append("state IN (" + ",".join("?" for _ in state_values) + ")")
            args.extend(state_values)
        clause = " WHERE " + " AND ".join(where) if where else ""
        args.append(max(1, min(int(limit), 500)))
        with connect(self.state_dir) as conn:
            rows = conn.execute(
                f"SELECT * FROM webchat_turn{clause} ORDER BY created_at DESC LIMIT ?",
                tuple(args),
            ).fetchall()
        return [_turn_row(row) or {} for row in rows]

    def claim(
        self,
        turn_id: str,
        *,
        owner: str,
        lease_seconds: float = 60.0,
        expected_digests: dict[str, str] | None = None,
    ) -> TurnClaim:
        expected_digests = expected_digests or {}
        now = utc_now()
        with transaction(self.state_dir) as conn:
            turn = conn.execute(
                "SELECT * FROM webchat_turn WHERE turn_id = ?", (turn_id,),
            ).fetchone()
            if turn is None:
                raise AgentTurnNotFound(turn_id)
            if str(turn["state"]) not in TURN_CLAIMABLE_STATES:
                raise AgentTurnConflict(f"turn {turn_id} is {turn['state']}, not claimable")
            for key in (
                "permission_digest", "tool_catalog_digest", "profile_digest", "context_digest",
            ):
                expected = str(expected_digests.get(key) or "")
                pinned = str(turn[key] or "")
                if expected and pinned and expected != pinned:
                    raise AgentTurnConflict(f"{key} drift for turn {turn_id}")
            previous_attempt = str(turn["current_attempt_id"] or "")
            attempt_no = int(conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) FROM webchat_attempt WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()[0]) + 1
            attempt_id = _id("attempt")
            fence = _id("fence")
            expires = _lease_expiry(lease_seconds)
            conn.execute(
                """
                INSERT INTO webchat_attempt(
                  attempt_id, turn_id, attempt_no, state, owner, fence_token,
                  lease_expires_at, provider_session_id, resumed_from_attempt_id,
                  permission_digest, tool_catalog_digest, profile_digest,
                  context_digest, created_at, updated_at
                ) VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, turn_id, attempt_no, owner, fence, expires,
                    str(turn["provider_session_id"] or ""), previous_attempt,
                    str(turn["permission_digest"] or ""),
                    str(turn["tool_catalog_digest"] or ""),
                    str(turn["profile_digest"] or ""),
                    str(turn["context_digest"] or ""), now, now,
                ),
            )
            conn.execute(
                """
                UPDATE webchat_turn
                SET state = 'claimed', current_attempt_id = ?, revision = revision + 1,
                    terminal_reason = '', updated_at = ?
                WHERE turn_id = ?
                """,
                (attempt_id, now, turn_id),
            )
        return TurnClaim(turn_id, attempt_id, attempt_no, fence, expires, previous_attempt)

    def mark_running(
        self,
        claim: TurnClaim,
        *,
        provider_session_id: str = "",
        lease_seconds: float = 60.0,
    ) -> dict[str, Any]:
        return self._attempt_transition(
            claim,
            state="running",
            provider_session_id=provider_session_id,
            lease_expires_at=_lease_expiry(lease_seconds),
        )

    def renew(self, claim: TurnClaim, *, lease_seconds: float = 60.0) -> dict[str, Any]:
        return self._attempt_transition(
            claim,
            state="running",
            lease_expires_at=_lease_expiry(lease_seconds),
        )

    def settle(
        self,
        claim: TurnClaim,
        *,
        state: str,
        reason: str = "",
        provider_session_id: str = "",
        output_ref: str = "",
        pending_interaction_id: str = "",
    ) -> dict[str, Any]:
        allowed = TURN_TERMINAL_STATES | {"waiting_input", "retry_wait"}
        if state not in allowed:
            raise ValueError(f"unsupported turn settlement state {state!r}")
        now = utc_now()
        with transaction(self.state_dir) as conn:
            self._require_current_claim(conn, claim)
            conn.execute(
                """
                UPDATE webchat_attempt
                SET state = ?, terminal_reason = ?, provider_session_id = CASE
                    WHEN ? = '' THEN provider_session_id ELSE ? END,
                    updated_at = ?
                WHERE attempt_id = ? AND fence_token = ?
                """,
                (
                    state, reason, provider_session_id, provider_session_id,
                    now, claim.attempt_id, claim.fence_token,
                ),
            )
            conn.execute(
                """
                UPDATE webchat_turn
                SET state = ?, terminal_reason = ?, output_ref = CASE
                    WHEN ? = '' THEN output_ref ELSE ? END,
                    pending_interaction_id = ?, provider_session_id = CASE
                    WHEN ? = '' THEN provider_session_id ELSE ? END,
                    revision = revision + 1, updated_at = ?
                WHERE turn_id = ? AND current_attempt_id = ?
                """,
                (
                    state, reason, output_ref, output_ref,
                    pending_interaction_id, provider_session_id,
                    provider_session_id, now, claim.turn_id, claim.attempt_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM webchat_turn WHERE turn_id = ?", (claim.turn_id,),
            ).fetchone()
            assert row is not None
            return _turn_row(row) or {}

    def _attempt_transition(
        self,
        claim: TurnClaim,
        *,
        state: str,
        provider_session_id: str = "",
        lease_expires_at: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with transaction(self.state_dir) as conn:
            self._require_current_claim(conn, claim)
            conn.execute(
                """
                UPDATE webchat_attempt SET state = ?, provider_session_id = CASE
                  WHEN ? = '' THEN provider_session_id ELSE ? END,
                  lease_expires_at = CASE WHEN ? = '' THEN lease_expires_at ELSE ? END,
                  updated_at = ? WHERE attempt_id = ? AND fence_token = ?
                """,
                (
                    state, provider_session_id, provider_session_id,
                    lease_expires_at, lease_expires_at, now,
                    claim.attempt_id, claim.fence_token,
                ),
            )
            conn.execute(
                """
                UPDATE webchat_turn SET state = ?, provider_session_id = CASE
                  WHEN ? = '' THEN provider_session_id ELSE ? END,
                  revision = revision + 1, updated_at = ?
                WHERE turn_id = ? AND current_attempt_id = ?
                """,
                (state, provider_session_id, provider_session_id, now, claim.turn_id, claim.attempt_id),
            )
            row = conn.execute(
                "SELECT * FROM webchat_attempt WHERE attempt_id = ?", (claim.attempt_id,),
            ).fetchone()
            assert row is not None
            return dict(row)

    @staticmethod
    def _require_current_claim(conn: sqlite3.Connection, claim: TurnClaim) -> None:
        row = conn.execute(
            """
            SELECT t.current_attempt_id, t.state AS turn_state,
                   a.fence_token, a.state AS attempt_state
            FROM webchat_turn t JOIN webchat_attempt a ON a.attempt_id = ?
            WHERE t.turn_id = ?
            """,
            (claim.attempt_id, claim.turn_id),
        ).fetchone()
        if row is None:
            raise AgentTurnNotFound(claim.turn_id)
        if str(row["current_attempt_id"]) != claim.attempt_id or str(row["fence_token"]) != claim.fence_token:
            raise AgentTurnConflict("stale attempt owner/fence")
        if str(row["turn_state"]) in TURN_TERMINAL_STATES:
            raise AgentTurnConflict(f"turn is already {row['turn_state']}")
        if str(row["attempt_state"]) in TURN_TERMINAL_STATES:
            raise AgentTurnConflict(f"attempt is already {row['attempt_state']}")

    def append_part(
        self,
        claim: TurnClaim,
        *,
        kind: str,
        state: str,
        content_ref: str = "",
        content_digest: str = "",
        preview: str = "",
        refs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with transaction(self.state_dir) as conn:
            self._require_current_claim(conn, claim)
            seq = int(conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM webchat_part WHERE turn_id = ?",
                (claim.turn_id,),
            ).fetchone()[0]) + 1
            part_id = f"part-{claim.turn_id}-{seq:06d}"
            conn.execute(
                """
                INSERT INTO webchat_part(
                  part_id, turn_id, attempt_id, seq, kind, state,
                  content_ref, content_digest, preview, refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    part_id, claim.turn_id, claim.attempt_id, seq, kind, state,
                    content_ref, content_digest, preview[:12000], dumps(refs or {}), now,
                ),
            )
            conn.execute(
                "UPDATE webchat_turn SET stream_cursor = ?, updated_at = ? WHERE turn_id = ?",
                (seq, now, claim.turn_id),
            )
            row = conn.execute(
                "SELECT * FROM webchat_part WHERE part_id = ?", (part_id,),
            ).fetchone()
            assert row is not None
            return _row(row) or {}

    def replay_parts(
        self,
        turn_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        with connect(self.state_dir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM webchat_part WHERE turn_id = ? AND seq > ?
                ORDER BY seq ASC LIMIT ?
                """,
                (turn_id, max(0, int(after_seq)), max(1, min(int(limit), 1000))),
            ).fetchall()
            turn = conn.execute(
                "SELECT state, stream_cursor FROM webchat_turn WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if turn is None:
            raise AgentTurnNotFound(turn_id)
        items = [_row(row) or {} for row in rows]
        cursor = int(turn["stream_cursor"] or 0)
        has_more = bool(items and int(items[-1]["seq"]) < cursor)
        return {
            "schema_version": "agent-turn-parts.v1",
            "turn_id": turn_id,
            "items": items,
            "after_seq": max(0, int(after_seq)),
            "cursor": cursor,
            "has_more": has_more,
            "truncated": has_more,
            "next_after_seq": int(items[-1]["seq"]) if items else max(0, int(after_seq)),
            "turn_state": str(turn["state"]),
        }

    def request_interaction(
        self,
        *,
        turn_id: str,
        kind: str,
        request: dict[str, Any],
        attempt_id: str = "",
        interaction_id: str = "",
        revision: int = 1,
        expires_at: str = "",
        currentness: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if kind not in INTERACTION_KINDS:
            raise ValueError(f"unsupported interaction kind {kind!r}")
        validate_interaction_request(kind, request)
        interaction_id = interaction_id or _id("interaction")
        request_digest = digest_json(request)
        now = utc_now()
        with transaction(self.state_dir) as conn:
            turn = conn.execute(
                "SELECT current_attempt_id, state FROM webchat_turn WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if turn is None:
                raise AgentTurnNotFound(turn_id)
            if str(turn["state"]) in TURN_TERMINAL_STATES:
                raise AgentTurnConflict(f"turn {turn_id} is already {turn['state']}")
            bound_attempt = attempt_id or str(turn["current_attempt_id"] or "")
            if attempt_id and attempt_id != str(turn["current_attempt_id"] or ""):
                raise AgentTurnConflict("interaction attempt is not the current turn attempt")
            existing = conn.execute(
                """
                SELECT * FROM webchat_interaction WHERE interaction_id = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (interaction_id,),
            ).fetchone()
            if existing is not None:
                if int(existing["revision"]) == int(revision):
                    if str(existing["request_digest"]) != request_digest:
                        raise AgentTurnConflict("interaction identity reused with different request")
                    return _row(existing) or {}, False
                if int(revision) != int(existing["revision"]) + 1:
                    raise AgentTurnConflict("interaction revision must advance by one")
                if str(existing["status"]) == "pending":
                    conn.execute(
                        """
                        UPDATE webchat_interaction SET status = 'superseded', updated_at = ?
                        WHERE interaction_id = ? AND revision = ?
                        """,
                        (now, interaction_id, int(existing["revision"])),
                    )
            conn.execute(
                """
                INSERT INTO webchat_interaction(
                  interaction_id, turn_id, attempt_id, kind, revision,
                  request_digest, request_json, status, expires_at,
                  currentness_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    interaction_id, turn_id, bound_attempt, kind, int(revision),
                    request_digest, dumps(request), expires_at,
                    dumps(currentness or {}), now, now,
                ),
            )
            conn.execute(
                """
                UPDATE webchat_turn SET state = 'waiting_input',
                  pending_interaction_id = ?, revision = revision + 1, updated_at = ?
                WHERE turn_id = ?
                """,
                (interaction_id, now, turn_id),
            )
            created = conn.execute(
                "SELECT * FROM webchat_interaction WHERE interaction_id = ? AND revision = ?",
                (interaction_id, int(revision)),
            ).fetchone()
            assert created is not None
            return _row(created) or {}, True

    def answer_interaction(
        self,
        interaction_id: str,
        *,
        revision: int,
        response: dict[str, Any],
        expected_currentness: dict[str, Any] | None = None,
        resume: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        response_digest = digest_json(response)
        now = utc_now()
        with transaction(self.state_dir) as conn:
            item = conn.execute(
                """
                SELECT * FROM webchat_interaction
                WHERE interaction_id = ? AND revision = ?
                """,
                (interaction_id, int(revision)),
            ).fetchone()
            if item is None:
                latest = conn.execute(
                    "SELECT revision FROM webchat_interaction WHERE interaction_id = ? "
                    "ORDER BY revision DESC LIMIT 1",
                    (interaction_id,),
                ).fetchone()
                if latest is not None:
                    raise AgentTurnConflict(
                        f"stale interaction revision {revision}; current is {int(latest['revision'])}"
                    )
                raise AgentTurnNotFound(interaction_id)
            if int(item["revision"]) != int(revision):
                raise AgentTurnConflict("stale interaction revision")
            if str(item["status"]) == "answered":
                if str(item["response_digest"]) != response_digest:
                    raise AgentTurnConflict("interaction already answered differently")
                return _row(item) or {}, False
            if str(item["status"]) != "pending":
                raise AgentTurnConflict(f"interaction is {item['status']}")
            currentness = loads(item["currentness_json"], {})
            if expected_currentness is not None and digest_json(expected_currentness) != digest_json(currentness):
                raise AgentTurnConflict("interaction currentness mismatch")
            expires_at = str(item["expires_at"] or "")
            if expires_at and expires_at < now:
                conn.execute(
                    """
                    UPDATE webchat_interaction SET status = 'expired', updated_at = ?
                    WHERE interaction_id = ? AND revision = ?
                    """,
                    (now, interaction_id, int(revision)),
                )
                raise AgentTurnConflict("interaction expired")
            conn.execute(
                """
                UPDATE webchat_interaction SET status = 'answered',
                  response_digest = ?, response_json = ?, updated_at = ?
                WHERE interaction_id = ? AND revision = ?
                """,
                (response_digest, dumps(response), now, interaction_id, int(revision)),
            )
            response_approved = bool(
                response.get("approved")
                or str(response.get("decision") or "") in {"approve", "approved"}
            )
            if not resume:
                next_state = "completed"
                pending_id = ""
            elif str(item["kind"]) == "ask_user":
                next_state = "resume_queued"
                pending_id = ""
            elif response_approved:
                next_state = "waiting_input"
                pending_id = interaction_id
            else:
                next_state = "completed"
                pending_id = ""
            conn.execute(
                """
                UPDATE webchat_turn SET state = ?, pending_interaction_id = ?,
                  revision = revision + 1, updated_at = ?
                WHERE turn_id = ? AND pending_interaction_id = ?
                """,
                (next_state, pending_id, now, str(item["turn_id"]), interaction_id),
            )
            updated = conn.execute(
                "SELECT * FROM webchat_interaction WHERE interaction_id = ? AND revision = ?",
                (interaction_id, int(revision)),
            ).fetchone()
            assert updated is not None
            return _row(updated) or {}, True

    def get_interaction(self, interaction_id: str, *, revision: int | None = None) -> dict[str, Any]:
        revision_clause = " AND revision = ?" if revision is not None else ""
        order_clause = "" if revision is not None else " ORDER BY revision DESC LIMIT 1"
        args: tuple[Any, ...] = (
            (interaction_id, int(revision)) if revision is not None else (interaction_id,)
        )
        with connect(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM webchat_interaction WHERE interaction_id = ?"
                + revision_clause + order_clause,
                args,
            ).fetchone()
        if row is None:
            raise AgentTurnNotFound(interaction_id)
        return _row(row) or {}

    def list_interactions(
        self,
        *,
        turn_id: str = "",
        project_id: str = "",
        conversation_id: str = "",
        thread_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        if turn_id:
            where.append("i.turn_id = ?")
            args.append(turn_id)
        if project_id:
            where.append("t.project_id = ?")
            args.append(project_id)
        if conversation_id:
            where.append("t.conversation_id = ?")
            args.append(conversation_id)
        if thread_id:
            where.append("t.thread_id = ?")
            args.append(thread_id)
        if status:
            where.append("i.status = ?")
            args.append(status)
        clause = " WHERE " + " AND ".join(where) if where else ""
        args.append(max(1, min(int(limit), 500)))
        with connect(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT i.* FROM webchat_interaction i "
                "JOIN webchat_turn t ON t.turn_id = i.turn_id"
                f"{clause} ORDER BY i.created_at DESC LIMIT ?",
                tuple(args),
            ).fetchall()
        return [_row(row) or {} for row in rows]

    def consume_interaction(
        self,
        interaction_id: str,
        *,
        revision: int,
        expected_currentness: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Consume an answered interaction once at the exact approved scope."""

        now = utc_now()
        with transaction(self.state_dir) as conn:
            item = conn.execute(
                """
                SELECT * FROM webchat_interaction
                WHERE interaction_id = ? AND revision = ?
                """,
                (interaction_id, int(revision)),
            ).fetchone()
            if item is None:
                raise AgentTurnNotFound(interaction_id)
            currentness = loads(item["currentness_json"], {})
            if digest_json(currentness) != digest_json(expected_currentness):
                raise AgentTurnConflict("interaction currentness mismatch")
            if str(item["status"]) == "consumed":
                return _row(item) or {}, False
            if str(item["status"]) != "answered":
                raise AgentTurnConflict(f"interaction is {item['status']}")
            response = loads(item["response_json"], {})
            if not bool(response.get("approved", response.get("decision") == "approve")):
                raise AgentTurnConflict("interaction was not approved")
            conn.execute(
                """
                UPDATE webchat_interaction SET status = 'consumed', updated_at = ?
                WHERE interaction_id = ? AND revision = ? AND status = 'answered'
                """,
                (now, interaction_id, int(revision)),
            )
            conn.execute(
                """
                UPDATE webchat_turn SET state = 'resume_queued',
                  pending_interaction_id = '', revision = revision + 1, updated_at = ?
                WHERE turn_id = ? AND pending_interaction_id = ?
                """,
                (now, str(item["turn_id"]), interaction_id),
            )
            updated = conn.execute(
                """
                SELECT * FROM webchat_interaction
                WHERE interaction_id = ? AND revision = ?
                """,
                (interaction_id, int(revision)),
            ).fetchone()
            assert updated is not None
            return _row(updated) or {}, True

    def apply_control(
        self,
        *,
        operation: str,
        turn_id: str,
        idempotency_key: str,
        native_steer: bool = False,
        message: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Persist control intent and apply only mechanical turn transitions."""

        if operation not in CONTROL_OPERATIONS - {"queue"}:
            raise ValueError("operation must be steer, interrupt, or cancel")
        if not idempotency_key.strip():
            raise ValueError("control idempotency_key is required")
        with connect(self.state_dir) as conn:
            existing = conn.execute(
                "SELECT * FROM webchat_control_receipt WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if existing is not None:
            item = _row(existing) or {}
            if str(item.get("operation") or "") != operation or str(item.get("turn_id") or "") != turn_id:
                raise AgentTurnConflict("control idempotency key reused across operation or turn")
            return item, False
        turn = self.get(turn_id)
        attempt_id = str(turn.get("current_attempt_id") or "")
        turn_state = str(turn.get("state") or "")
        if turn_state == "waiting_input" and operation == "interrupt":
            raise AgentTurnConflict("cannot interrupt while waiting_input; answer, queue, or cancel")
        if turn_state == "waiting_input" and operation == "steer" and native_steer:
            raise AgentTurnConflict("cannot apply native steer while waiting_input")
        fallback = ""
        status = "accepted"
        if operation == "steer" and not native_steer:
            queued, _ = self.enqueue(
                project_id=str(turn["project_id"]),
                conversation_id=str(turn["conversation_id"]),
                thread_id=str(turn["thread_id"]),
                message=message,
                idempotency_key=f"{idempotency_key}:queue-fallback",
                request={"fallback_from": "steer", "source_turn_id": turn_id},
                source_turn_id=turn_id,
            )
            fallback = "queue"
            status = "queued_fallback"
            payload = {"queue_id": queued["queue_id"]}
        else:
            payload = {}
            now = utc_now()
            with transaction(self.state_dir) as conn:
                current = conn.execute(
                    "SELECT state, current_attempt_id FROM webchat_turn WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if current is None:
                    raise AgentTurnNotFound(turn_id)
                if str(current["state"]) in TURN_TERMINAL_STATES:
                    raise AgentTurnConflict(f"turn {turn_id} is already terminal")
                if operation == "cancel":
                    next_state = "cancelled"
                    attempt_state = "cancelled"
                elif operation == "interrupt":
                    next_state = "resume_queued"
                    attempt_state = "abandoned"
                else:
                    next_state = str(current["state"])
                    attempt_state = "running"
                    status = "applied"
                if operation in {"cancel", "interrupt"}:
                    conn.execute(
                        """
                        UPDATE webchat_turn SET state = ?, terminal_reason = ?,
                          revision = revision + 1, updated_at = ?
                        WHERE turn_id = ? AND current_attempt_id = ?
                        """,
                        (next_state, f"operator_{operation}", now, turn_id, attempt_id),
                    )
                    if attempt_id:
                        conn.execute(
                            """
                            UPDATE webchat_attempt SET state = ?, terminal_reason = ?, updated_at = ?
                            WHERE attempt_id = ?
                            """,
                            (attempt_state, f"operator_{operation}", now, attempt_id),
                        )
                    if operation == "cancel":
                        conn.execute(
                            """
                            UPDATE webchat_interaction SET status = 'cancelled', updated_at = ?
                            WHERE turn_id = ? AND status = 'pending'
                            """,
                            (now, turn_id),
                        )
                        conn.execute(
                            "UPDATE webchat_turn SET pending_interaction_id = '' WHERE turn_id = ?",
                            (turn_id,),
                        )
        return self.record_control(
            operation=operation,
            turn_id=turn_id,
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            status=status,
            fallback=fallback,
            payload=payload,
        )

    def enqueue(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        message: str,
        idempotency_key: str,
        request: dict[str, Any] | None = None,
        source_turn_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        if not message.strip():
            raise ValueError("queued message is required")
        if not thread_id.strip():
            raise ValueError("queued message thread_id is required")
        now = utc_now()
        with transaction(self.state_dir) as conn:
            existing = conn.execute(
                "SELECT * FROM webchat_queue WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if any(
                    str(existing[field]) != expected
                    for field, expected in (
                        ("project_id", project_id),
                        ("conversation_id", conversation_id),
                        ("thread_id", thread_id),
                        ("message", message),
                    )
                ):
                    raise AgentTurnConflict("queue idempotency key reused across runtime scope")
                return _row(existing) or {}, False
            queue_id = _id("queue")
            conn.execute(
                """
                INSERT INTO webchat_queue(
                  queue_id, idempotency_key, project_id, conversation_id,
                  thread_id, message, request_json, state, source_turn_id,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    queue_id, idempotency_key, project_id, conversation_id,
                    thread_id, message, dumps(request or {}), source_turn_id,
                    now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM webchat_queue WHERE queue_id = ?", (queue_id,),
            ).fetchone()
            assert row is not None
            return _row(row) or {}, True

    def list_queue(
        self,
        *,
        project_id: str,
        conversation_id: str = "",
        thread_id: str = "",
        include_terminal: bool = False,
        terminal_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        where = ["project_id = ?"]
        args: list[Any] = [project_id]
        if conversation_id:
            where.append("conversation_id = ?")
            args.append(conversation_id)
        if thread_id:
            where.append("thread_id = ?")
            args.append(thread_id)
        if not include_terminal:
            where.append("state IN ('queued', 'claimed')")
        with connect(self.state_dir) as conn:
            if include_terminal and terminal_limit is not None:
                active = conn.execute(
                    "SELECT * FROM webchat_queue WHERE " + " AND ".join(where)
                    + " AND state IN ('queued', 'claimed') ORDER BY created_at ASC",
                    tuple(args),
                ).fetchall()
                terminal = conn.execute(
                    "SELECT * FROM webchat_queue WHERE " + " AND ".join(where)
                    + " AND state NOT IN ('queued', 'claimed', 'completed') "
                    + "ORDER BY updated_at DESC LIMIT ?",
                    (*args, max(0, min(int(terminal_limit), 100))),
                ).fetchall()
                rows = sorted(
                    [*active, *terminal],
                    key=lambda row: (str(row["created_at"]), str(row["queue_id"])),
                )
                return [_row(row) or {} for row in rows]
            rows = conn.execute(
                "SELECT * FROM webchat_queue WHERE " + " AND ".join(where) + " ORDER BY created_at ASC",
                tuple(args),
            ).fetchall()
        return [_row(row) or {} for row in rows]

    def cancel_queue(self, queue_id: str, *, project_id: str = "") -> dict[str, Any]:
        now = utc_now()
        with transaction(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM webchat_queue WHERE queue_id = ?", (queue_id,),
            ).fetchone()
            if row is None:
                raise AgentTurnNotFound(queue_id)
            if project_id and str(row["project_id"]) != project_id:
                raise AgentTurnNotFound(queue_id)
            if str(row["state"]) == "claimed":
                raise AgentTurnConflict("claimed queue item cannot be cancelled")
            conn.execute(
                "UPDATE webchat_queue SET state = 'cancelled', updated_at = ? WHERE queue_id = ?",
                (now, queue_id),
            )
            updated = conn.execute(
                "SELECT * FROM webchat_queue WHERE queue_id = ?", (queue_id,),
            ).fetchone()
            assert updated is not None
            return _row(updated) or {}

    def claim_next_queue(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        claimed_turn_id: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with transaction(self.state_dir) as conn:
            row = conn.execute(
                """
                SELECT * FROM webchat_queue
                WHERE project_id = ? AND conversation_id = ? AND thread_id = ?
                  AND state = 'queued'
                ORDER BY created_at ASC LIMIT 1
                """,
                (project_id, conversation_id, thread_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE webchat_queue SET state = 'claimed', claimed_turn_id = ?, updated_at = ?
                WHERE queue_id = ? AND state = 'queued'
                """,
                (claimed_turn_id, now, str(row["queue_id"])),
            )
            updated = conn.execute(
                "SELECT * FROM webchat_queue WHERE queue_id = ?", (str(row["queue_id"]),),
            ).fetchone()
            return _row(updated)

    def finish_queue(self, queue_id: str, *, state: str = "completed") -> None:
        if state not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid queue terminal state")
        with transaction(self.state_dir) as conn:
            conn.execute(
                "UPDATE webchat_queue SET state = ?, updated_at = ? WHERE queue_id = ?",
                (state, utc_now(), queue_id),
            )

__all__ = [
    "AgentInteractionValidationError",
    "AgentTurnConflict",
    "AgentTurnNotFound",
    "AgentTurnStore",
    "CONTROL_OPERATIONS",
    "INTERACTION_KINDS",
    "TURN_ACTIVE_STATES",
    "TURN_CLAIMABLE_STATES",
    "TURN_TERMINAL_STATES",
    "TurnClaim",
    "validate_interaction_request",
]
