from __future__ import annotations

from pathlib import Path

from zf.web.agent_conversation_store import AgentConversationStore


def _message(store: AgentConversationStore, content: str = "original") -> dict:
    return store.add_message(
        project_id="p",
        conversation_id="c",
        thread_id="main",
        role="user",
        origin="web",
        content=content,
    )[0]


def test_correct_and_fork_preserve_immutable_source(tmp_path: Path) -> None:
    store = AgentConversationStore(tmp_path)
    source = _message(store)
    body_before = (tmp_path / source["content_ref"]).read_bytes()
    corrected = store.correct(source["message_id"], content="corrected")
    fork = store.fork(source["message_id"], title="Alternative")
    assert corrected["supersedes_message_id"] == source["message_id"]
    assert corrected["content_digest"] != source["content_digest"]
    assert (tmp_path / source["content_ref"]).read_bytes() == body_before
    assert fork["parent_thread_id"] == "main"
    assert fork["fork_message_id"] == source["message_id"]
    inherited = store.list_messages(
        project_id="p", conversation_id="c", thread_id=fork["thread_id"],
    )["items"]
    assert [item["message_id"] for item in inherited] == [source["message_id"]]
    assert inherited[0]["inherited_from_thread_id"] == "main"
    child = store.add_message(
        project_id="p", conversation_id="c", thread_id=fork["thread_id"],
        role="user", origin="web", content="child-only",
    )[0]
    parent_ids = {
        item["message_id"] for item in store.list_messages(
            project_id="p", conversation_id="c", thread_id="main",
        )["items"]
    }
    assert child["message_id"] not in parent_ids


def test_search_compact_export_and_scope(tmp_path: Path) -> None:
    store = AgentConversationStore(tmp_path)
    first = _message(store, "Implement the billing API token=secret")
    second = store.add_message(
        project_id="p", conversation_id="c", thread_id="main",
        role="assistant", origin="runtime", content="Use a transaction",
        parent_message_id=first["message_id"],
    )[0]
    store.add_message(
        project_id="other", conversation_id="c2", thread_id="other",
        role="user", origin="web", content="billing hidden elsewhere",
    )
    matches = store.search(project_id="p", query="billing")
    assert [item["message_id"] for item in matches] == [first["message_id"]]
    summary = store.compact(
        project_id="p", conversation_id="c", thread_id="main",
        summary="Billing API plan", covered_message_ids=[first["message_id"], second["message_id"]],
    )
    assert (tmp_path / summary["summary_ref"]).is_file()
    loaded_summary = store.latest_summary(
        project_id="p", conversation_id="c", thread_id="main",
    )
    assert loaded_summary is not None
    assert loaded_summary["summary"] == "Billing API plan"
    assert loaded_summary["covered_message_ids"] == [first["message_id"], second["message_id"]]
    media_type, exported = store.export(
        project_id="p", conversation_id="c", thread_id="main", format="markdown",
    )
    assert media_type == "text/markdown"
    assert "token=secret" not in exported


def test_page_cursor_and_hide_do_not_delete_body(tmp_path: Path) -> None:
    store = AgentConversationStore(tmp_path)
    message = _message(store)
    store.hide(message["message_id"])
    assert store.list_messages(project_id="p", conversation_id="c", thread_id="main")["items"] == []
    assert (tmp_path / message["content_ref"]).is_file()
