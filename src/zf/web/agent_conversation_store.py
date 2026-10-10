"""Immutable Kanban Agent conversation and branch storage."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from zf.core.security.redaction import redact_text
from zf.core.state.atomic_io import atomic_write_text
from zf.web.agent_runtime_db import connect, dumps, loads, transaction, utc_now


class ConversationConflict(RuntimeError):
    pass


class ConversationNotFound(KeyError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class AgentConversationStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.body_dir = self.state_dir / "operator" / "webchat-messages"
        self.summary_dir = self.state_dir / "operator" / "webchat-summaries"
        with connect(self.state_dir):
            pass

    def ensure_thread(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        title: str = "New chat",
        parent_thread_id: str = "",
        fork_message_id: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with transaction(self.state_dir) as conn:
            existing = conn.execute(
                "SELECT * FROM webchat_thread WHERE thread_id = ?", (thread_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["project_id"]) != project_id
                    or str(existing["conversation_id"]) != conversation_id
                ):
                    raise ConversationConflict("thread identity belongs to another project/conversation")
                return dict(existing)
            conn.execute(
                """
                INSERT INTO webchat_thread(
                  thread_id, project_id, conversation_id, title,
                  parent_thread_id, fork_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id, project_id, conversation_id, title[:200],
                    parent_thread_id, fork_message_id, now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM webchat_thread WHERE thread_id = ?", (thread_id,),
            ).fetchone()
            assert row is not None
            return dict(row)

    def add_message(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        role: str,
        origin: str,
        content: str,
        message_id: str = "",
        parent_message_id: str = "",
        supersedes_message_id: str = "",
        attachment_refs: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("role must be user, assistant, or system")
        if not thread_id or not conversation_id or not project_id:
            raise ValueError("project_id, conversation_id and thread_id are required")
        self.ensure_thread(
            project_id=project_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
        )
        message_id = message_id or _id("msg")
        digest = _digest(content)
        now = utc_now()
        content_ref = f"operator/webchat-messages/{message_id}.json"
        path = self.state_dir / content_ref
        with transaction(self.state_dir) as conn:
            existing = conn.execute(
                "SELECT * FROM webchat_message WHERE message_id = ?", (message_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["content_digest"]) != digest:
                    raise ConversationConflict("message identity reused with different content")
                return self._public(existing, include_content=True), False
            if supersedes_message_id:
                self._require_message(conn, supersedes_message_id, project_id, conversation_id)
            if parent_message_id:
                self._require_message(conn, parent_message_id, project_id, conversation_id)
            atomic_write_text(path, json.dumps({
                "schema_version": "webchat-message-body.v1",
                "message_id": message_id,
                "content": content,
                "digest": digest,
                "created_at": now,
            }, ensure_ascii=False, indent=2) + "\n")
            conn.execute(
                """
                INSERT INTO webchat_message(
                  message_id, project_id, conversation_id, thread_id,
                  parent_message_id, supersedes_message_id, role, origin,
                  content_ref, content_digest, attachment_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, project_id, conversation_id, thread_id,
                    parent_message_id, supersedes_message_id, role, origin,
                    content_ref, digest, dumps(attachment_refs or []), now,
                ),
            )
            conn.execute(
                "UPDATE webchat_thread SET updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
            row = conn.execute(
                "SELECT * FROM webchat_message WHERE message_id = ?", (message_id,),
            ).fetchone()
            assert row is not None
            return self._public(row, include_content=True), True

    @staticmethod
    def _require_message(
        conn: Any,
        message_id: str,
        project_id: str,
        conversation_id: str,
    ) -> None:
        row = conn.execute(
            "SELECT project_id, conversation_id FROM webchat_message WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise ConversationNotFound(message_id)
        if str(row["project_id"]) != project_id or str(row["conversation_id"]) != conversation_id:
            raise ConversationConflict("message belongs to another project/conversation")

    def get_message(
        self,
        message_id: str,
        *,
        include_content: bool = True,
        project_id: str = "",
    ) -> dict[str, Any]:
        with connect(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM webchat_message WHERE message_id = ?", (message_id,),
            ).fetchone()
        if row is None:
            raise ConversationNotFound(message_id)
        if project_id and str(row["project_id"]) != project_id:
            raise ConversationNotFound(message_id)
        return self._public(row, include_content=include_content)

    def list_messages(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        before: str = "",
        limit: int = 100,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        page_limit = max(1, min(int(limit), 500))
        with connect(self.state_dir) as conn:
            try:
                rows = self._thread_rows(
                    conn,
                    project_id=project_id,
                    conversation_id=conversation_id,
                    thread_id=thread_id,
                    seen=set(),
                )
            except ConversationNotFound:
                rows = []
        if not include_hidden:
            rows = [row for row in rows if not int(row["hidden"])]
        if before:
            rows = [row for row in rows if str(row["created_at"]) < before]
        has_more = len(rows) > page_limit
        page_rows = rows[-page_limit:]
        items = []
        for row in page_rows:
            item = self._public(row, include_content=True)
            source_thread_id = str(row["thread_id"])
            if source_thread_id != thread_id:
                item["inherited_from_thread_id"] = source_thread_id
                item["inherited"] = True
            items.append(item)
        return {
            "schema_version": "webchat-conversation-page.v1",
            "project_id": project_id,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "items": items,
            "next_before": items[0]["created_at"] if items else before,
            "has_more": has_more,
        }

    def message_visible_in_thread(
        self,
        message_id: str,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> bool:
        with connect(self.state_dir) as conn:
            try:
                rows = self._thread_rows(
                    conn,
                    project_id=project_id,
                    conversation_id=conversation_id,
                    thread_id=thread_id,
                    seen=set(),
                )
            except ConversationNotFound:
                return False
            return any(str(row["message_id"]) == message_id for row in rows)

    def _thread_rows(
        self,
        conn: Any,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        seen: set[str],
    ) -> list[Any]:
        if thread_id in seen:
            raise ConversationConflict("conversation thread ancestry contains a cycle")
        seen.add(thread_id)
        thread = conn.execute(
            """
            SELECT * FROM webchat_thread
            WHERE thread_id = ? AND project_id = ? AND conversation_id = ?
            """,
            (thread_id, project_id, conversation_id),
        ).fetchone()
        if thread is None:
            raise ConversationNotFound(thread_id)
        prefix: list[Any] = []
        parent_thread_id = str(thread["parent_thread_id"] or "")
        fork_message_id = str(thread["fork_message_id"] or "")
        if parent_thread_id and fork_message_id:
            parent_rows = self._thread_rows(
                conn,
                project_id=project_id,
                conversation_id=conversation_id,
                thread_id=parent_thread_id,
                seen=seen,
            )
            fork_index = next(
                (index for index, row in enumerate(parent_rows) if str(row["message_id"]) == fork_message_id),
                None,
            )
            if fork_index is None:
                raise ConversationConflict("fork message is not visible in the parent thread")
            prefix = parent_rows[: fork_index + 1]
        local = conn.execute(
            """
            SELECT * FROM webchat_message
            WHERE project_id = ? AND conversation_id = ? AND thread_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (project_id, conversation_id, thread_id),
        ).fetchall()
        seen.remove(thread_id)
        return [*prefix, *local]

    def list_threads(self, *, project_id: str, conversation_id: str) -> list[dict[str, Any]]:
        with connect(self.state_dir) as conn:
            rows = conn.execute(
                """
                SELECT t.*, COUNT(m.message_id) AS message_count
                FROM webchat_thread t
                LEFT JOIN webchat_message m ON m.thread_id = t.thread_id AND m.hidden = 0
                WHERE t.project_id = ? AND t.conversation_id = ?
                GROUP BY t.thread_id ORDER BY t.updated_at DESC
                """,
                (project_id, conversation_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def correct(
        self,
        message_id: str,
        *,
        content: str,
        origin: str = "web",
        project_id: str = "",
    ) -> dict[str, Any]:
        source = self.get_message(message_id, project_id=project_id)
        created, _ = self.add_message(
            project_id=str(source["project_id"]),
            conversation_id=str(source["conversation_id"]),
            thread_id=str(source["thread_id"]),
            role=str(source["role"]),
            origin=origin,
            content=content,
            parent_message_id=str(source.get("parent_message_id") or ""),
            supersedes_message_id=message_id,
            attachment_refs=list(source.get("attachment_refs") or []),
        )
        return created

    def fork(
        self,
        message_id: str,
        *,
        new_thread_id: str = "",
        title: str = "Fork",
        project_id: str = "",
    ) -> dict[str, Any]:
        source = self.get_message(
            message_id, include_content=False, project_id=project_id,
        )
        new_thread_id = new_thread_id or _id("thread")
        return self.ensure_thread(
            project_id=str(source["project_id"]),
            conversation_id=str(source["conversation_id"]),
            thread_id=new_thread_id,
            title=title,
            parent_thread_id=str(source["thread_id"]),
            fork_message_id=message_id,
        )

    def search(
        self,
        *,
        project_id: str,
        query: str,
        conversation_id: str = "",
        thread_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        needle = query.casefold().strip()
        if not needle:
            return []
        where = ["project_id = ?", "hidden = 0"]
        args: list[Any] = [project_id]
        if conversation_id:
            where.append("conversation_id = ?")
            args.append(conversation_id)
        if thread_id:
            where.append("thread_id = ?")
            args.append(thread_id)
        with connect(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM webchat_message WHERE " + " AND ".join(where)
                + " ORDER BY created_at DESC LIMIT 2000",
                tuple(args),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = self._public(row, include_content=True)
            content = str(item.get("content") or "")
            if needle not in content.casefold():
                continue
            item["preview"] = content[:300]
            item.pop("content", None)
            out.append(item)
            if len(out) >= max(1, min(int(limit), 200)):
                break
        return out

    def compact(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        summary: str,
        covered_message_ids: list[str],
    ) -> dict[str, Any]:
        if not covered_message_ids:
            raise ValueError("covered_message_ids is required")
        rows = [self.get_message(message_id, include_content=False) for message_id in covered_message_ids]
        if any(
            row["project_id"] != project_id
            or row["conversation_id"] != conversation_id
            or not self.message_visible_in_thread(
                str(row["message_id"]),
                project_id=project_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
            )
            for row in rows
        ):
            raise ConversationConflict("compact refs cross conversation/thread boundary")
        summary_id = _id("summary")
        payload = {
            "schema_version": "webchat-compact-summary.v1",
            "summary_id": summary_id,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "summary": summary,
            "covered_message_ids": covered_message_ids,
            "covered_digest": _digest("\n".join(row["content_digest"] for row in rows)),
            "created_at": utc_now(),
        }
        path = self.summary_dir / f"{summary_id}.json"
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        payload["summary_ref"] = str(path.relative_to(self.state_dir))
        payload["summary_digest"] = _digest(summary)
        with transaction(self.state_dir) as conn:
            conn.execute(
                """
                INSERT INTO webchat_compact_summary(
                  summary_id, project_id, conversation_id, thread_id,
                  summary_ref, summary_digest, covered_message_ids_json,
                  covered_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id, project_id, conversation_id, thread_id,
                    payload["summary_ref"], payload["summary_digest"],
                    dumps(covered_message_ids), payload["covered_digest"],
                    payload["created_at"],
                ),
            )
        return payload

    def latest_summary(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> dict[str, Any] | None:
        with connect(self.state_dir) as conn:
            row = conn.execute(
                """
                SELECT * FROM webchat_compact_summary
                WHERE project_id = ? AND conversation_id = ? AND thread_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id, conversation_id, thread_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["covered_message_ids"] = loads(
            item.pop("covered_message_ids_json"), [],
        )
        path = (self.state_dir / str(item["summary_ref"])).resolve()
        if not path.is_relative_to(self.summary_dir.resolve()) or not path.is_file():
            raise ConversationNotFound(str(item["summary_ref"]))
        body = json.loads(path.read_text(encoding="utf-8"))
        summary = str(body.get("summary") or "")
        if _digest(summary) != str(item["summary_digest"]):
            raise ConversationConflict("compact summary digest mismatch")
        item["summary"] = summary
        item["schema_version"] = "webchat-compact-summary.v1"
        return item

    def export(
        self,
        *,
        project_id: str,
        conversation_id: str,
        thread_id: str,
        format: str = "markdown",
    ) -> tuple[str, str]:
        items = self.list_messages(
            project_id=project_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            limit=500,
        )["items"]
        safe = [{**item, "content": redact_text(str(item.get("content") or ""))} for item in items]
        if format == "json":
            return "application/json", json.dumps(safe, ensure_ascii=False, indent=2)
        if format == "txt":
            return "text/plain", "\n\n".join(
                f"{item['role']}: {item['content']}" for item in safe
            )
        if format != "markdown":
            raise ValueError("format must be markdown, json, or txt")
        return "text/markdown", "\n\n".join(
            f"### {str(item['role']).title()}\n\n{item['content']}" for item in safe
        )

    def hide(self, message_id: str) -> None:
        with transaction(self.state_dir) as conn:
            if conn.execute(
                "SELECT 1 FROM webchat_message WHERE message_id = ?", (message_id,),
            ).fetchone() is None:
                raise ConversationNotFound(message_id)
            conn.execute(
                "UPDATE webchat_message SET hidden = 1 WHERE message_id = ?", (message_id,),
            )

    def _public(self, row: Any, *, include_content: bool) -> dict[str, Any]:
        item = dict(row)
        item["attachment_refs"] = loads(item.pop("attachment_refs_json", "[]"), [])
        item["hidden"] = bool(item.get("hidden"))
        if include_content:
            item["content"] = self._read_body(
                str(item["content_ref"]), str(item["content_digest"]),
            )
        return item

    def _read_body(self, content_ref: str, expected_digest: str) -> str:
        path = (self.state_dir / content_ref).resolve()
        root = self.body_dir.resolve()
        if not path.is_relative_to(root) or not path.exists():
            raise ConversationNotFound(content_ref)
        payload = json.loads(path.read_text(encoding="utf-8"))
        content = str(payload.get("content") or "")
        if _digest(content) != expected_digest:
            raise ConversationConflict("message content digest mismatch")
        return content


__all__ = [
    "AgentConversationStore",
    "ConversationConflict",
    "ConversationNotFound",
]
