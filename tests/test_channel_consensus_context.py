"""Consensus reviewers receive the exact complete PRD before provider dispatch."""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_contract_artifacts import persist_channel_prd, persist_channel_prd_readiness
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_provider_prompt import build_channel_provider_prompt
from zf.runtime.channel_sidecar import hydrate_channel_context_pack_payload
from zf.runtime.channel_synthesis_reactor import react_channel_consensus_proposed


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


def test_consensus_caller_supplies_complete_bound_prd_and_readiness(tmp_path: Path):
    host, payload, prd, readiness = review_fixture(tmp_path)
    proposed = host.event_writer.emit("channel.consensus.proposed", actor="pm", payload=payload)
    react_channel_consensus_proposed(host, proposed)
    react_channel_consensus_proposed(host, proposed)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if any(e.type == "channel.consensus.signed" for e in host.event_log.read_all()):
            break
        time.sleep(0.01)
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
    assert len([event for event in events if event.type == "channel.consensus.signed"]) == 1


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
