"""Shared SQLite substrate for the Kanban Agent WebChat runtime.

The database owns interaction current state only. Task, Workflow, Run and
Channel business state remain in their existing canonical stores.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "agent-webchat-runtime.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(value: object, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def database_path(state_dir: Path) -> Path:
    return Path(state_dir) / "operator" / "webchat.sqlite3"


def connect(state_dir: Path) -> sqlite3.Connection:
    path = database_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_schema(conn)
    return conn


@contextmanager
def transaction(state_dir: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(state_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS webchat_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS webchat_turn (
          turn_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          project_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          thread_id TEXT NOT NULL,
          task_id TEXT NOT NULL DEFAULT '',
          backend TEXT NOT NULL,
          provider_session_id TEXT NOT NULL DEFAULT '',
          permission_profile TEXT NOT NULL,
          permission_digest TEXT NOT NULL DEFAULT '',
          tool_catalog_digest TEXT NOT NULL DEFAULT '',
          profile_digest TEXT NOT NULL DEFAULT '',
          context_digest TEXT NOT NULL DEFAULT '',
          input_message_id TEXT NOT NULL DEFAULT '',
          request_json TEXT NOT NULL DEFAULT '{}',
          state TEXT NOT NULL,
          current_attempt_id TEXT NOT NULL DEFAULT '',
          stream_cursor INTEGER NOT NULL DEFAULT 0,
          pending_interaction_id TEXT NOT NULL DEFAULT '',
          output_ref TEXT NOT NULL DEFAULT '',
          terminal_reason TEXT NOT NULL DEFAULT '',
          revision INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_webchat_turn_thread
          ON webchat_turn(project_id, conversation_id, thread_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_webchat_turn_state
          ON webchat_turn(state, updated_at);

        CREATE TABLE IF NOT EXISTS webchat_attempt (
          attempt_id TEXT PRIMARY KEY,
          turn_id TEXT NOT NULL REFERENCES webchat_turn(turn_id),
          attempt_no INTEGER NOT NULL,
          state TEXT NOT NULL,
          owner TEXT NOT NULL,
          fence_token TEXT NOT NULL UNIQUE,
          lease_expires_at TEXT NOT NULL,
          provider_session_id TEXT NOT NULL DEFAULT '',
          resumed_from_attempt_id TEXT NOT NULL DEFAULT '',
          permission_digest TEXT NOT NULL DEFAULT '',
          tool_catalog_digest TEXT NOT NULL DEFAULT '',
          profile_digest TEXT NOT NULL DEFAULT '',
          context_digest TEXT NOT NULL DEFAULT '',
          terminal_reason TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(turn_id, attempt_no)
        );
        CREATE INDEX IF NOT EXISTS idx_webchat_attempt_turn
          ON webchat_attempt(turn_id, attempt_no);

        CREATE TABLE IF NOT EXISTS webchat_part (
          part_id TEXT PRIMARY KEY,
          turn_id TEXT NOT NULL REFERENCES webchat_turn(turn_id),
          attempt_id TEXT NOT NULL REFERENCES webchat_attempt(attempt_id),
          seq INTEGER NOT NULL,
          kind TEXT NOT NULL,
          state TEXT NOT NULL,
          content_ref TEXT NOT NULL DEFAULT '',
          content_digest TEXT NOT NULL DEFAULT '',
          preview TEXT NOT NULL DEFAULT '',
          refs_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          UNIQUE(turn_id, seq)
        );

        CREATE TABLE IF NOT EXISTS webchat_interaction (
          interaction_id TEXT NOT NULL,
          turn_id TEXT NOT NULL REFERENCES webchat_turn(turn_id),
          attempt_id TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL,
          revision INTEGER NOT NULL,
          request_digest TEXT NOT NULL,
          request_json TEXT NOT NULL,
          response_digest TEXT NOT NULL DEFAULT '',
          response_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL,
          expires_at TEXT NOT NULL DEFAULT '',
          currentness_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(interaction_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_webchat_interaction_turn
          ON webchat_interaction(turn_id, status, created_at);

        CREATE TABLE IF NOT EXISTS webchat_queue (
          queue_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          project_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          thread_id TEXT NOT NULL,
          message TEXT NOT NULL,
          request_json TEXT NOT NULL DEFAULT '{}',
          state TEXT NOT NULL,
          source_turn_id TEXT NOT NULL DEFAULT '',
          claimed_turn_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_webchat_queue_thread
          ON webchat_queue(project_id, conversation_id, thread_id, state, created_at);

        CREATE TABLE IF NOT EXISTS webchat_control_receipt (
          receipt_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          operation TEXT NOT NULL,
          turn_id TEXT NOT NULL,
          attempt_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          fallback TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS webchat_thread (
          thread_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          title TEXT NOT NULL,
          parent_thread_id TEXT NOT NULL DEFAULT '',
          fork_message_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_webchat_thread_conversation
          ON webchat_thread(project_id, conversation_id, updated_at);

        CREATE TABLE IF NOT EXISTS webchat_message (
          message_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          thread_id TEXT NOT NULL REFERENCES webchat_thread(thread_id),
          parent_message_id TEXT NOT NULL DEFAULT '',
          supersedes_message_id TEXT NOT NULL DEFAULT '',
          role TEXT NOT NULL,
          origin TEXT NOT NULL,
          content_ref TEXT NOT NULL,
          content_digest TEXT NOT NULL,
          attachment_refs_json TEXT NOT NULL DEFAULT '[]',
          hidden INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_webchat_message_thread
          ON webchat_message(project_id, conversation_id, thread_id, created_at);

        CREATE TABLE IF NOT EXISTS webchat_compact_summary (
          summary_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          thread_id TEXT NOT NULL REFERENCES webchat_thread(thread_id),
          summary_ref TEXT NOT NULL,
          summary_digest TEXT NOT NULL,
          covered_message_ids_json TEXT NOT NULL,
          covered_digest TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_webchat_summary_thread
          ON webchat_compact_summary(project_id, conversation_id, thread_id, created_at);

        CREATE TABLE IF NOT EXISTS webchat_artifact (
          artifact_id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          logical_name TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(project_id, logical_name)
        );

        CREATE TABLE IF NOT EXISTS webchat_artifact_version (
          version_id TEXT PRIMARY KEY,
          artifact_id TEXT NOT NULL REFERENCES webchat_artifact(artifact_id),
          digest TEXT NOT NULL,
          media_type TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          storage_ref TEXT NOT NULL,
          visibility TEXT NOT NULL,
          producer_json TEXT NOT NULL DEFAULT '{}',
          lineage_json TEXT NOT NULL DEFAULT '{}',
          expires_at TEXT NOT NULL DEFAULT '',
          adopted_target_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(artifact_id, digest)
        );
        CREATE INDEX IF NOT EXISTS idx_webchat_artifact_version_visibility
          ON webchat_artifact_version(visibility, created_at);

        CREATE TABLE IF NOT EXISTS webchat_artifact_grant (
          version_id TEXT NOT NULL REFERENCES webchat_artifact_version(version_id),
          principal TEXT NOT NULL,
          capability TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(version_id, principal, capability)
        );

        CREATE TABLE IF NOT EXISTS webchat_profile_proposal (
          proposal_id TEXT PRIMARY KEY,
          profile_id TEXT NOT NULL,
          base_digest TEXT NOT NULL,
          proposal_digest TEXT NOT NULL UNIQUE,
          proposal_ref TEXT NOT NULL,
          status TEXT NOT NULL,
          validation_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS webchat_profile_revision (
          profile_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          profile_digest TEXT NOT NULL,
          profile_json TEXT NOT NULL,
          proposal_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          PRIMARY KEY(profile_id, revision),
          UNIQUE(profile_id, profile_digest)
        );
        """
    )
    row = conn.execute(
        "SELECT value FROM webchat_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is not None and str(row[0]) != SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported WebChat runtime schema {row[0]!r}; expected {SCHEMA_VERSION!r}"
        )
    conn.execute(
        "INSERT OR REPLACE INTO webchat_meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


__all__ = [
    "SCHEMA_VERSION",
    "connect",
    "database_path",
    "digest_json",
    "dumps",
    "loads",
    "transaction",
    "utc_now",
]
