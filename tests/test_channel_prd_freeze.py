"""受控入口 → 讨论 → 修订 → 全文复审 → 冻结；provider 全部隔离为 fake。"""
from __future__ import annotations

from tests.channel_dispatch_fixture import channel_dispatches

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import channel_adapter
from zf.runtime.channel_consensus_identity import consensus_artifact_error
from zf.runtime.channel_discussion import advance_discussion
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_synthesis_reactor import react_channel_consensus_proposed, react_channel_synthesis_requested
from zf.runtime.channel_prd_freeze import emit_prd_frozen
from zf.runtime.control_actions import ControlledActionService

CH = "ch-freeze"


def test_prd_freeze_carries_identity_bound_source_snapshot(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    writer = EventWriter(log)
    message = writer.emit("channel.message.posted", actor="agent", payload={
        "channel_id": "ch", "thread_id": "main", "message_id": "m1", "text": "requirement",
    })
    reached = writer.emit("channel.consensus.reached", actor="kernel", payload={
        "channel_id": "ch", "thread_id": "main", "prd_ref": "channels/ch/prd/r1.json",
        "prd_digest": "a" * 64, "prd_revision": 1,
        "readiness_ref": "channels/ch/prd/r1-readiness.json", "readiness_digest": "b" * 64,
        "source_refs": ["event:requirement"], "source": "test",
    })
    frozen = emit_prd_frozen(
        writer, consensus_payload=reached.payload, source_event=reached,
    )
    snapshot = frozen.payload["source_snapshot"]
    assert frozen.payload["source_snapshot_digest"]
    assert snapshot["channel_id"] == "ch"
    assert snapshot["thread_id"] == "main"
    assert snapshot["revision"] == 1
    assert [item["event_id"] for item in snapshot["events"]] == [message.id, reached.id]


def execute(host, action: str, payload: dict) -> dict:
    requested = host.event_writer.emit("web.action.requested", actor="web", payload={"action": action})
    return host.service.execute(action=action, requested_action=action, requested=requested,
                                payload={"channel_id": CH, **payload})


def latest(host, event_type: str):
    return next(e for e in reversed(host.event_log.read_all()) if e.type == event_type)


def advance(host):
    return advance_discussion(host.state_dir, host.event_writer, channel_id=CH, thread_id="main")


def proposed(tmp_path: Path, channel_dispatches):
    state_dir = tmp_path / "runtime"
    log = EventLog(state_dir / "events.jsonl")
    writer = EventWriter(log)
    host = SimpleNamespace(state_dir=state_dir, event_log=log, event_writer=writer,
                           project_root=tmp_path, config=None, openclaw_client=None)
    host.service = ControlledActionService(state_dir, writer, project_root=tmp_path,
                                           actor="web", source="web", surface="web")
    assert execute(host, "channel-create-from-template", {
        "template_id": "quick-change", "overrides": {"backend": "fake"},
    })["ok"]
    writer.emit("channel.discussion.mode.set", actor="web", payload={
        "channel_id": CH, "mode": "multi_lens", "auto": True, "moderator": False,
    })
    assert execute(host, "channel-discussion-start", {
        "message": "Keep README.md and its documented acceptance boundary.", "mode": "multi_lens",
    })["ok"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        findings = [e for e in log.read_all() if e.type == "channel.finding.recorded"]
        if len(findings) == 3:
            break
        time.sleep(0.02)
    assert len(findings) == 3
    advance(host)
    advance(host)
    react_channel_synthesis_requested(host, latest(host, "channel.synthesis.requested"))
    channel_dispatches()
    return host, latest(host, "channel.consensus.proposed")


@pytest.mark.parametrize("corrupt", [False, True])
def test_full_prd_rework_review_and_freeze(tmp_path: Path, monkeypatch, corrupt: bool, channel_dispatches):
    host, first = proposed(tmp_path, channel_dispatches)
    original = channel_adapter.build_fake_channel_reply
    reviewed = []
    restore = []

    def provider(member, message, pack):
        refs = message.get("refs") or {}
        if refs.get("consensus_review_id"):
            document = pack["semantic_source_documents"][0]
            assert document["message_body_digest"] == refs["artifact_digest"]
            body = json.loads(document["content"])
            assert body["body"]["synthesis"]["acceptance_criteria"]
            reviewed.append((member["member_id"], body["revision"]))
            if member["member_id"] == "qa_analyst":
                if body["revision"] == 1:
                    return json.dumps({"channel_consensus_review": {
                        "verdict": "blocked", "summary": "Need the exact README check.",
                        "artifact_digest": refs["artifact_digest"],
                        "blocker_question": "Which command verifies README.md?",
                    }})
                if corrupt:
                    # Corrupt after reading, before the final signature can
                    # trigger the router's automatic freeze transition.
                    path = host.state_dir / refs["artifact_ref"]
                    restore.append((path, path.read_bytes()))
                    path.write_text("{}")
        return original(member, message, pack)

    monkeypatch.setattr(channel_adapter, "build_fake_channel_reply", provider)
    react_channel_consensus_proposed(host, first)
    channel_dispatches()
    advance(host)
    channel = project_channel(host.state_dir, CH)
    assert channel["discussions"]["main"]["state"] == "phase2_relay"
    assert not channel["consensus"]["main"].get("reached_event_id")
    question = next(q for q in channel["open_questions"] if q["status"] == "open")
    assert execute(host, "channel-question-resolve", {
        "question_id": question["question_id"], "resolution": "answered",
        "answer": "Use test -f README.md; preserve it in the next PRD.",
    })["ok"]
    advance(host)
    react_channel_synthesis_requested(host, latest(host, "channel.synthesis.requested"))
    channel_dispatches()
    second = latest(host, "channel.consensus.proposed")
    assert second.payload["prd_revision"] == 2
    assert second.payload["previous_prd_digest"] == first.payload["artifact_digest"]
    channel = project_channel(host.state_dir, CH)
    assert not channel["consensus"]["main"]["blocked"]
    assert set(channel["consensus"]["main"]["signed"]) == {"tech_leader"}
    advance(host)
    assert not project_channel(host.state_dir, CH)["consensus"]["main"].get("reached_event_id")
    react_channel_consensus_proposed(host, second)
    channel_dispatches()
    react_channel_consensus_proposed(host, first)
    channel_dispatches()
    assert reviewed.count(("qa_analyst", 2)) == 1
    if corrupt:
        advance(host)
        advance(host)
        rejected = [e for e in host.event_log.read_all() if e.type == "channel.consensus.review.rejected"
                    and "consensus_artifact_invalid" in e.payload.get("reason", "")]
        assert len(rejected) == 1
        assert not project_channel(host.state_dir, CH)["consensus"]["main"].get("reached_event_id")
        for path, data in restore:
            path.write_bytes(data)
    advance(host)
    advance(host)
    reached = [e for e in host.event_log.read_all() if e.type == "channel.consensus.reached"]
    assert len(reached) == 1
    assert reached[0].payload["prd_revision"] == 2
    assert reached[0].payload["artifact_digest"] == second.payload["artifact_digest"]
    assert set(reached[0].payload["signed_by"]) == set(second.payload["required_signers"])
    assert project_channel(host.state_dir, CH)["discussions"]["main"]["last_outcome"] == "consensus"


def test_review_retry_is_controlled_bound_and_idempotent(tmp_path: Path, channel_dispatches):
    host, first = proposed(tmp_path, channel_dispatches)
    path = host.state_dir / first.payload["artifact_ref"]
    before = path.read_bytes()
    payload = {"consensus_event_id": first.id, "reason": "Repaired missing review context."}
    assert execute(host, "channel-consensus-review-retry", payload)["ok"]
    second = latest(host, "channel.consensus.proposed")
    assert second.id != first.id
    assert second.payload["artifact_digest"] == first.payload["artifact_digest"]
    assert not execute(host, "channel-consensus-review-retry", payload)["ok"]
    host.event_writer.emit("channel.consensus.signed", actor="dev_reviewer", payload={
        "channel_id": CH, "member_id": "dev_reviewer", "consensus_event_id": first.id,
        "artifact_ref": first.payload["artifact_ref"], "artifact_digest": first.payload["artifact_digest"],
        "prd_revision": 1,
    })
    assert not project_channel(host.state_dir, CH)["consensus"]["main"]["signed"]
    react_channel_consensus_proposed(host, first)
    channel_dispatches()
    react_channel_consensus_proposed(host, second)
    channel_dispatches()
    advance(host)
    assert path.read_bytes() == before
    consensus = project_channel(host.state_dir, CH)["consensus"]["main"]
    assert consensus["status"] == "reached"
    assert set(consensus["signed"]) == set(second.payload["required_signers"])
    assert not execute(host, "channel-consensus-review-retry", {
        **payload, "consensus_event_id": second.id,
    })["ok"]


@pytest.mark.parametrize("blocker", ["busy", "open_question", "not_owner", "corrupt"])
def test_review_retry_cannot_bypass_gates(tmp_path: Path, blocker: str, channel_dispatches):
    host, first = proposed(tmp_path, channel_dispatches)
    if blocker == "busy":
        host.event_writer.emit("channel.agent.reply.requested", actor="test", payload={
            "channel_id": CH, "request_id": "busy", "target_member_id": "qa_analyst",
        })
    elif blocker == "open_question":
        host.event_writer.emit("channel.question.opened", actor="test", payload={
            "channel_id": CH, "question_id": "q-open", "question": "Unresolved requirement?",
        })
    elif blocker == "not_owner":
        host.service.actor = "intruder"
    else:
        (host.state_dir / first.payload["artifact_ref"]).write_text("{}")
    assert not execute(host, "channel-consensus-review-retry", {
        "consensus_event_id": first.id, "reason": "Retry",
    })["ok"]
    assert latest(host, "channel.consensus.proposed").id == first.id


def test_malformed_digest_never_bypasses_freeze(tmp_path: Path):
    assert consensus_artifact_error(tmp_path, {
        "prd_revision": 1, "artifact_ref": "channels/ch/prd/r1.json",
        "artifact_digest": "invalid",
    }) == "prd:digest_invalid"


@pytest.mark.parametrize("revision", [True, 0, 1.0, 1.5, "01", " 1", "bad"])
def test_noncanonical_consensus_revision_fails_closed(
    tmp_path: Path, revision: object,
) -> None:
    assert consensus_artifact_error(tmp_path, {
        "prd_revision": revision,
        "artifact_ref": "channels/ch/prd/r1.json",
        "artifact_digest": "a" * 64,
    }) == "prd:revision_invalid"


def test_malformed_consensus_revision_rejects_without_projection_crash(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir()
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    writer.emit(
        "channel.discussion.started",
        actor="test",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "roster": ["agent-1"],
            "synthesizer": "agent-1",
        },
    )
    writer.emit(
        "channel.discussion.phase.changed",
        actor="test",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "phase": "phase3_synthesis",
            "source": "test",
        },
    )
    writer.emit(
        "channel.member.invited",
        actor="test",
        payload={
            "channel_id": CH,
            "member_id": "agent-1",
            "member_type": "persona_agent",
            "channel_role": "dev",
        },
    )
    writer.emit(
        "channel.consensus.proposed",
        actor="agent-1",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "artifact_ref": "channels/ch/prd/r1.json",
            "artifact_digest": "a" * 64,
            "prd_revision": "bad",
            "required_signers": ["agent-1"],
            "product_mode": "multi_lens",
        },
    )
    writer.emit(
        "channel.consensus.signed",
        actor="agent-1",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "member_id": "agent-1",
        },
    )
    writer.emit(
        "channel.consensus.signed",
        actor="operator",
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "member_id": "operator",
        },
    )

    emitted = advance_discussion(
        state_dir, writer, channel_id=CH, thread_id="main",
    )
    assert emitted == ["channel.consensus.review.rejected"]
    events = EventLog(state_dir / "events.jsonl").read_all()
    assert not any(event.type == "channel.consensus.reached" for event in events)
    rejection = next(
        event for event in events if event.type == "channel.consensus.review.rejected"
    )
    assert rejection.payload["reason"] == "consensus_artifact_invalid:prd:revision_invalid"


def test_review_retry_web_entry_is_token_gated(tmp_path: Path, monkeypatch, channel_dispatches):
    from fastapi.testclient import TestClient
    from zf.web.server import create_app

    host, first = proposed(tmp_path, channel_dispatches)
    monkeypatch.setenv("ZF_WEB_ACTION_TOKEN", "test-review-retry")
    payload = {"channel_id": CH, "consensus_event_id": first.id, "reason": "Repaired review context"}
    with TestClient(create_app(host.state_dir, project_root=tmp_path)) as client:
        route = "/api/actions/channel-consensus-review-retry"
        assert client.post(route, json=payload).status_code == 403
        result = client.post(route, json=payload, headers={"x-zf-web-token": "test-review-retry"})
        assert result.status_code in (200, 202), result.text
        assert result.json()["ok"]
