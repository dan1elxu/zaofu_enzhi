"""Consensus review context contains the exact, hash-bound PRD."""

from __future__ import annotations

import hashlib
import json
from tests.channel_dispatch_fixture import channel_dispatches
from pathlib import Path
from types import SimpleNamespace

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_context import build_channel_context_pack
from zf.runtime.channel_contract_artifacts import (
    persist_channel_prd,
    persist_channel_prd_readiness,
)
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_provider_prompt import build_channel_provider_prompt
from zf.runtime.channel_router import route_channel_message
from zf.runtime.channel_sidecar import (
    channel_message_event_payload,
    hydrate_channel_context_pack_payload,
)
from zf.runtime.channel_synthesis_reactor import react_channel_consensus_proposed


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
    readiness = persist_channel_prd_readiness(
        state_dir, channel_id="ch-review", thread_id="main", revision=2,
        body={"verdict": "ready", "implementation_start": False},
        created_by="test", source_event_id="evt-synthesis",
    )
    prd = persist_channel_prd(
        state_dir, channel_id="ch-review", thread_id="main", revision=2,
        previous_ref="", previous_digest="", readiness_descriptor=readiness,
        body={"summary": "Short summary", "markdown": "完整边界 " * 600 + "FINAL-ACCEPTANCE"},
        created_by="test", source_event_id="evt-synthesis",
    )
    artifact_ref = prd["ref"]
    artifact_path = state_dir / artifact_ref
    digest = prd["sha256"]
    payload = channel_message_event_payload(state_dir, {
        "channel_id": "ch-review", "thread_id": "main", "message_id": "msg-review",
        "member_id": "operator", "role": "user", "source": "runtime",
        "text": "@reviewer Review the exact synthesis artifact.",
        "mentions": ["reviewer"], "refs": {
            "consensus_review_id": "review-r2", "artifact_ref": artifact_ref,
            "artifact_digest": digest, "prd_revision": 2,
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
    assert pack["semantic_source_required_digests"] == [digest]
    assert "semantic_source_documents" not in pack
    prompt = build_channel_provider_prompt(
        channel=channel, member=channel["members"][0], message=message.payload,
        request=request, state_dir=tmp_path,
    )
    assert "FINAL-ACCEPTANCE" in prompt
    assert digest in prompt
    assert "consumed_message_digests" not in prompt
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


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
    documents = pack["semantic_source_documents"]
    assert {document["kind"] for document in documents} == {"channel_prd", "channel_prd_readiness"}
    target = next(document for document in documents if document["kind"] == "channel_prd")
    assert target["content"]["body"]["markdown"].endswith("FINAL-ACCEPTANCE")
    assert target["artifact_digest"] == digest
    assert "previous discussion" not in json.dumps(documents, ensure_ascii=False)


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


def review_fixture(tmp_path: Path):
    state_dir = tmp_path / ".runtime" / "channel state"
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    writer.emit("channel.created", actor="test", payload={"channel_id": "ch-review", "name": "Review"})
    writer.emit("channel.member.invited", actor="test", payload={
        "channel_id": "ch-review", "member_id": "arch", "member_type": "provider_agent",
        "provider": "fake", "backend": "fake", "channel_role": "arch",
        "permissions": ["read", "message", "summarize"],
    })
    readiness = persist_channel_prd_readiness(
        state_dir, channel_id="ch-review", thread_id="main", revision=1,
        body={"verdict": "ready", "implementation_start": True, "reason": "Owner inputs resolved"},
        created_by="pm", source_event_id="evt-synthesis",
    )
    prd = persist_channel_prd(
        state_dir, channel_id="ch-review", thread_id="main", revision=1,
        previous_ref="", previous_digest="", readiness_descriptor=readiness,
        body={"summary": "Exact PRD", "markdown": "full evidence " * 500 + "END-OF-EXACT-PRD"},
        created_by="pm", source_event_id="evt-synthesis",
    )
    host = SimpleNamespace(state_dir=state_dir, event_log=writer.event_log, event_writer=writer,
                           project_root=tmp_path, config=None, openclaw_client=None)
    payload = {"channel_id": "ch-review", "thread_id": "main", "proposed_by": "pm",
               "required_signers": ["pm", "arch"], "artifact_ref": prd["ref"],
               "artifact_digest": prd["sha256"], "prd_revision": 1}
    return host, payload, prd, readiness


def test_consensus_caller_supplies_complete_bound_prd_and_readiness(tmp_path: Path, channel_dispatches):
    host, payload, prd, readiness = review_fixture(tmp_path)
    proposed = host.event_writer.emit("channel.consensus.proposed", actor="pm", payload=payload)
    host.event_writer.emit("channel.consensus.signed", actor="pm", payload={
        **payload, "member_id": "pm",
    })
    react_channel_consensus_proposed(host, proposed)
    react_channel_consensus_proposed(host, proposed)
    channel_dispatches()
    events = host.event_log.read_all()
    packs = [event for event in events if event.type == "channel.context_pack.built"]
    assert len(packs) == 1
    pack = hydrate_channel_context_pack_payload(host.state_dir, packs[0].payload, strict=True)
    assert pack["semantic_source_required"] is True
    assert pack["semantic_source_complete"] is True
    documents = pack["semantic_source_documents"]
    target = next(doc for doc in documents if doc.get("artifact_ref") == prd["ref"])
    assert target["artifact_digest"] == prd["sha256"]
    assert target["content"]["body"]["markdown"].endswith("END-OF-EXACT-PRD")
    assert any(doc.get("artifact_digest") == readiness["sha256"] for doc in documents)
    channel = project_channel(host.state_dir, "ch-review")
    request = channel["reply_requests"][0]
    trigger = next(message for message in channel["messages"] if message["message_id"] == request["message_id"])
    prompt = build_channel_provider_prompt(channel=channel, member={"member_id": "arch"},
                                          message=trigger, request=request, state_dir=host.state_dir)
    assert "END-OF-EXACT-PRD" in prompt
    assert "Do not call tools" in prompt
    assert len([event for event in events if event.type == "channel.consensus.signed"
                and event.payload.get("member_id") == "arch"]) == 1


@pytest.mark.parametrize("fault", ["missing", "digest", "outside", "readiness", "foreign_thread", "no_digest"])
def test_consensus_dispatch_rejects_unreadable_or_unbound_source(tmp_path: Path, fault: str):
    host, payload, prd, readiness = review_fixture(tmp_path)
    if fault == "missing":
        payload["artifact_ref"] = "channels/ch-review/prd/missing.json"
    elif fault == "digest":
        payload["artifact_digest"] = "0" * 64
    elif fault == "outside":
        payload["artifact_ref"] = "../outside.json"
    elif fault == "readiness":
        (host.state_dir / readiness["ref"]).write_text("{}")
    elif fault == "foreign_thread":
        payload["thread_id"] = "other"
    elif fault == "no_digest":
        payload["artifact_digest"] = ""
    proposed = host.event_writer.emit("channel.consensus.proposed", actor="pm", payload=payload)
    react_channel_consensus_proposed(host, proposed)
    events = host.event_log.read_all()
    assert any(event.type == "channel.context_pack.rejected" for event in events)
    assert not any(event.type in {"channel.agent.reply.requested", "channel.consensus.signed"} for event in events)
