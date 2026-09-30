"""签署评审必须收到经过哈希校验的完整 PRD，而不只是路径或摘要。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_context import build_channel_context_pack
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_provider_prompt import build_channel_provider_prompt
from zf.runtime.channel_router import route_channel_message
from zf.runtime.channel_sidecar import channel_message_event_payload


def review_channel(state_dir: Path):
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    writer.emit("channel.created", actor="test", payload={
        "channel_id": "ch-review", "name": "Review", "thread_id": "main",
    })
    writer.emit("channel.member.invited", actor="test", payload={
        "channel_id": "ch-review", "member_id": "reviewer",
        "member_type": "persona", "backend": "fake",
        "channel_role": "critic", "visibility_profile": "reviewer",
        "permission_profile": "read_only", "permissions": ["read", "message"],
    })
    artifact_ref = "channels/ch-review/prd/r2.json"
    artifact_path = state_dir / artifact_ref
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    body = {"channel_id": "ch-review", "thread_id": "main", "revision": 2,
            "body": {"summary": "Short summary", "acceptance": "完整边界 " * 600 + "FINAL-ACCEPTANCE"}}
    artifact_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    payload = channel_message_event_payload(state_dir, {
        "channel_id": "ch-review", "thread_id": "main", "message_id": "msg-review",
        "member_id": "operator", "role": "user", "source": "runtime",
        "text": "@reviewer Review the exact synthesis artifact.",
        "mentions": ["reviewer"], "refs": {
            "consensus_review_id": "review-r2", "artifact_ref": artifact_ref,
            "artifact_digest": digest,
        },
    }, created_by="test")
    message = writer.emit("channel.message.posted", actor="test", payload=payload)
    return writer, message, artifact_path, digest


def test_router_binds_full_consensus_artifact_through_provider_prompt(tmp_path):
    writer, message, path, digest = review_channel(tmp_path)
    result = route_channel_message(
        state_dir=tmp_path, writer=writer, message_event=message,
        message_payload=message.payload, actor="test", source="runtime",
        dispatch_inline=False,
    )
    assert result.skipped == []
    channel = project_channel(tmp_path, "ch-review")
    request = channel["reply_requests"][0]
    pack = channel["context_packs"][0]
    assert pack["semantic_source_required"] is True
    assert pack["semantic_source_stage"] == "consensus_review"
    assert "semantic_source_documents" not in pack
    prompt = build_channel_provider_prompt(
        channel=channel, member=channel["members"][0], message=message.payload,
        request=request, state_dir=tmp_path,
    )
    assert "FINAL-ACCEPTANCE" in prompt
    assert digest in prompt
    assert "consumed_message_digests" not in prompt
    assert path.read_text(encoding="utf-8").endswith('"}}')


@pytest.mark.parametrize("failure", ["missing", "hash_mismatch", "unsafe_path", "missing_digest"])
def test_bad_review_artifact_is_rejected_before_dispatch(tmp_path, failure):
    writer, message, path, digest = review_channel(tmp_path)
    if failure == "missing":
        path.unlink()
    elif failure == "hash_mismatch":
        path.write_text("{}", encoding="utf-8")
    else:
        refs = dict(message.payload["refs"])
        refs["artifact_ref" if failure == "unsafe_path" else "artifact_digest"] = (
            "../outside.json" if failure == "unsafe_path" else ""
        )
        payload = {**message.payload, "message_id": "msg-invalid", "refs": refs}
        message = writer.emit("channel.message.posted", actor="test", payload=payload)
    result = route_channel_message(
        state_dir=tmp_path, writer=writer, message_event=message,
        message_payload=message.payload, actor="test", source="runtime", dispatch_inline=False,
    )
    assert result.skipped
    events = writer.event_log.read_all()
    assert any(e.type == "channel.context_pack.rejected" for e in events)
    assert not any(e.type == "channel.agent.reply.requested" for e in events)


def test_review_target_does_not_accumulate_unrelated_transcript(tmp_path):
    writer, message, path, digest = review_channel(tmp_path)
    channel = project_channel(tmp_path, "ch-review")
    channel["messages"].insert(0, {
        "message_id": "old", "thread_id": "main", "role": "assistant",
        "text": "previous discussion " * 10000,
    })
    pack = build_channel_context_pack(
        channel, channel_id="ch-review", thread_id="main", target_member_id="reviewer",
        trigger_message_id=message.payload["message_id"], state_dir=tmp_path,
    )
    assert pack["semantic_source_complete"] is True
    assert len(pack["semantic_source_documents"]) == 1
    assert "FINAL-ACCEPTANCE" in pack["semantic_source_documents"][0]["content"]
    assert pack["semantic_source_required_digests"] == [digest]


def test_review_artifact_over_budget_is_not_silently_truncated(tmp_path):
    from zf.runtime.channel_semantic_sources import build_semantic_source_bundle

    writer, message, path, digest = review_channel(tmp_path)
    bundle = build_semantic_source_bundle(
        project_channel(tmp_path, "ch-review"), thread_id="main",
        trigger_message_id=message.payload["message_id"], state_dir=tmp_path,
        max_source_chars=32,
    )
    assert bundle["required"] is True
    assert bundle["complete"] is False
    assert "semantic_source_budget_exceeded" in bundle["reason"]
    assert bundle["documents"] == []


@pytest.mark.parametrize("corrupt", [False, True])
def test_revision_uses_verified_prd_and_preserves_concurrent_delta(tmp_path, corrupt):
    from zf.runtime.channel_semantic_sources import build_semantic_source_bundle

    writer, _, path, digest = review_channel(tmp_path)
    channel = project_channel(tmp_path, "ch-review")
    channel["discussions"] = {"main": {"requirement_message_id": "requirement", "started_at": "01"}}
    channel["messages"] = [
        {"message_id": "requirement", "thread_id": "main", "role": "user", "text": "Original scope"},
        {"message_id": "old", "thread_id": "main", "role": "assistant", "text": "old " * 40000},
        {"message_id": "synth-1", "thread_id": "main", "source": "runtime", "refs": {"synthesis_request_id": "s1"}},
        {"message_id": "concurrent", "thread_id": "main", "role": "user", "text": "NEW-BOUNDARY during synthesis"},
        {"message_id": "reply-1", "thread_id": "main", "role": "assistant", "text": "duplicate draft " * 10000, "refs": {"request_id": "reply1"}},
        {"message_id": "synth-2", "thread_id": "main", "ts": "03", "refs": {"synthesis_request_id": "s2"}},
    ]
    channel["reply_requests"] = [{"request_id": "reply1", "message_id": "synth-1"}]
    channel["syntheses"] = [{"thread_id": "main", "request_id": "s1", "prd_revision": 2,
                             "artifact_ref": str(path.relative_to(tmp_path)), "artifact_digest": digest, "ts": "02"}]
    if corrupt:
        path.write_text("{}")
    bundle = build_semantic_source_bundle(channel, thread_id="main", trigger_message_id="synth-2", state_dir=tmp_path)
    if corrupt:
        assert bundle["complete"] is False
        assert bundle["documents"] == []
        assert "prd_checkpoint:hash_mismatch" in bundle["reason"]
    else:
        assert bundle["complete"] is True
        assert [d["kind"] for d in bundle["documents"]] == ["prd_checkpoint", "requirement", "owner_input"]
        assert "FINAL-ACCEPTANCE" in bundle["documents"][0]["content"]
        assert "NEW-BOUNDARY" in bundle["documents"][2]["content"]
