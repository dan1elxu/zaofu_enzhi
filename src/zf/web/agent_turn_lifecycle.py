"""Crash reconciliation and recovery operations for durable WebChat turns."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zf.web.agent_runtime_db import connect, dumps, transaction, utc_now
from zf.web.agent_turn_contract import (
    AgentTurnConflict,
    AgentTurnNotFound,
    CONTROL_OPERATIONS,
    TURN_TERMINAL_STATES,
    decode_runtime_row,
    new_runtime_id,
)


class AgentTurnLifecycleMixin:
    """Lifecycle methods split from the core CAS/fence store."""

    state_dir: Path

    def reconcile_queue_claims(self) -> int:
        """Requeue claims that crashed before their destination turn existed."""

        updated = 0
        now = utc_now()
        with transaction(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT queue_id, claimed_turn_id FROM webchat_queue WHERE state = 'claimed'",
            ).fetchall()
            for row in rows:
                turn = conn.execute(
                    "SELECT state FROM webchat_turn WHERE turn_id = ?",
                    (str(row["claimed_turn_id"] or ""),),
                ).fetchone()
                if turn is not None:
                    continue
                conn.execute(
                    """
                    UPDATE webchat_queue SET state = 'queued', claimed_turn_id = '', updated_at = ?
                    WHERE queue_id = ? AND state = 'claimed'
                    """,
                    (now, str(row["queue_id"])),
                )
                updated += 1
        return updated

    def record_control(
        self,
        *,
        operation: str,
        turn_id: str,
        idempotency_key: str,
        status: str,
        attempt_id: str = "",
        fallback: str = "",
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if operation not in CONTROL_OPERATIONS:
            raise ValueError(f"unsupported control operation {operation!r}")
        with transaction(self.state_dir) as conn:
            existing = conn.execute(
                "SELECT * FROM webchat_control_receipt WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return decode_runtime_row(existing) or {}, False
            receipt_id = new_runtime_id("receipt")
            conn.execute(
                """
                INSERT INTO webchat_control_receipt(
                  receipt_id, idempotency_key, operation, turn_id, attempt_id,
                  status, fallback, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id, idempotency_key, operation, turn_id, attempt_id,
                    status, fallback, dumps(payload or {}), utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM webchat_control_receipt WHERE receipt_id = ?", (receipt_id,),
            ).fetchone()
            assert row is not None
            return decode_runtime_row(row) or {}, True

    def list_control_receipts(
        self,
        *,
        project_id: str,
        conversation_id: str = "",
        thread_id: str = "",
        limit: int = 160,
    ) -> list[dict[str, Any]]:
        """Return bounded receipts through their owning turn scope."""

        where = ["t.project_id = ?"]
        args: list[Any] = [project_id]
        if conversation_id:
            where.append("t.conversation_id = ?")
            args.append(conversation_id)
        if thread_id:
            where.append("t.thread_id = ?")
            args.append(thread_id)
        args.append(max(1, min(int(limit), 500)))
        with connect(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT r.* FROM webchat_control_receipt r "
                "JOIN webchat_turn t ON t.turn_id = r.turn_id WHERE "
                + " AND ".join(where)
                + " ORDER BY r.created_at DESC LIMIT ?",
                tuple(args),
            ).fetchall()
        return [decode_runtime_row(row) or {} for row in rows]

    def reconcile_expired(self, *, now: str | None = None) -> list[dict[str, Any]]:
        now = now or utc_now()
        reconciled: list[dict[str, Any]] = []
        with transaction(self.state_dir) as conn:
            rows = conn.execute(
                """
                SELECT a.*, t.state AS turn_state, t.current_attempt_id
                FROM webchat_attempt a JOIN webchat_turn t ON t.turn_id = a.turn_id
                WHERE a.state IN ('claimed', 'running') AND a.lease_expires_at <= ?
                  AND t.current_attempt_id = a.attempt_id
                """,
                (now,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE webchat_attempt SET state = 'abandoned', terminal_reason = ?, updated_at = ? WHERE attempt_id = ?",
                    ("lost_owner_lease_expired", now, str(row["attempt_id"])),
                )
                conn.execute(
                    """
                    UPDATE webchat_turn SET state = 'resume_queued',
                      terminal_reason = 'lost_owner_lease_expired',
                      revision = revision + 1, updated_at = ?
                    WHERE turn_id = ? AND current_attempt_id = ?
                    """,
                    (now, str(row["turn_id"]), str(row["attempt_id"])),
                )
                reconciled.append(dict(row))
        return reconciled

    def fail_recovery(self, turn_id: str, *, reason: str) -> dict[str, Any]:
        """Terminalize an unclaimed recovery candidate with an owner-visible reason."""

        now = utc_now()
        with transaction(self.state_dir) as conn:
            row = conn.execute(
                "SELECT state FROM webchat_turn WHERE turn_id = ?", (turn_id,),
            ).fetchone()
            if row is None:
                raise AgentTurnNotFound(turn_id)
            if str(row["state"]) in TURN_TERMINAL_STATES:
                return self.get(turn_id)  # type: ignore[attr-defined]
            if str(row["state"]) not in {"resume_queued", "retry_wait"}:
                raise AgentTurnConflict(f"turn {turn_id} is not awaiting recovery")
            conn.execute(
                """
                UPDATE webchat_turn SET state = 'failed', terminal_reason = ?,
                  revision = revision + 1, updated_at = ? WHERE turn_id = ?
                """,
                (reason[:500], now, turn_id),
            )
        return self.get(turn_id)  # type: ignore[attr-defined]

    def attempts(self, turn_id: str) -> list[dict[str, Any]]:
        with connect(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM webchat_attempt WHERE turn_id = ? ORDER BY attempt_no ASC",
                (turn_id,),
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = ["AgentTurnLifecycleMixin"]
