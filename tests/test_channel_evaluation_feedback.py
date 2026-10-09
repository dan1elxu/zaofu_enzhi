"""Native evaluation findings reach typed member review with immutable binding."""

import json
from types import SimpleNamespace

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_evaluation_feedback import publish_evaluation_feedback
from zf.runtime.channel_prd_evaluation_reactor import react_channel_prd_evaluation_requested
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_reply_contract import emit_structured_reply_events
from zf.runtime.channel_semantic_sources import build_semantic_source_bundle
from zf.runtime.channel_sidecar import channel_message_event_payload
from zf.runtime.sidecar_refs import SidecarRefError, hydrate_sidecar_ref, write_sidecar_json


def fixture(tmp_path, monkeypatch, verdict="FAIL", findings=None, terminal=True):
    log = EventLog(tmp_path / "events.jsonl")
    writer = EventWriter(log)
    host = SimpleNamespace(state_dir=tmp_path, event_log=log, event_writer=writer)
    writer.emit("channel.created", actor="web", payload={"channel_id": "ch", "owner_actor_ref": "web"})
    for member, role in (("author", "pm"), ("reviewer", "critic")):
        writer.emit("channel.member.invited", actor="web", correlation_id="ch", payload={
            "channel_id": "ch", "member_id": member, "member_type": "persona_agent",
            "channel_role": role, "permissions": ["read", "message", "summarize"]})
    writer.emit("channel.message.posted", actor="web", correlation_id="ch", payload=channel_message_event_payload(
        tmp_path, {"channel_id": "ch", "thread_id": "main", "message_id": "source",
                   "role": "user", "member_id": "operator", "text": "完整原文：邀请 QA 必须批准。"},
        created_by="test"))
    writer.emit("channel.discussion.started", actor="web", correlation_id="ch", payload={
        "channel_id": "ch", "thread_id": "main", "mode": "multi_lens",
        "requirement_message_id": "source", "synthesizer": "author"})
    writer.emit("channel.discussion.phase.changed", actor="channel-discussion", correlation_id="ch",
                payload={"channel_id": "ch", "thread_id": "main", "phase": "phase3_synthesis"})
    prd = write_sidecar_json(tmp_path, "artifacts/prd/r1.json", {"markdown": "# PRD\n邀请 QA 必须批准。"},
                            kind="test_prd", schema_version="test", created_by="test", required=True)
    binding = {"channel_id": "ch", "thread_id": "main", "artifact_ref": prd["ref"],
               "artifact_digest": prd["sha256"], "prd_revision": 1}
    writer.emit("channel.consensus.proposed", actor="author", correlation_id="ch", payload={
        **binding, "proposed_by": "author", "required_signers": ["author", "reviewer"]})
    for member in ("author", "reviewer"):
        writer.emit("channel.consensus.signed", actor=member, correlation_id="ch",
                    payload={**binding, "member_id": member})
    request = writer.emit("channel.prd_evaluation.requested", actor="web", correlation_id="ch",
                          payload={**binding, "request_id": "evaluation-1", "role": "full"})
    manifest = write_sidecar_json(tmp_path, "artifacts/evaluation/input.json", {
        "context": {"channel_id": "ch", "thread_id": "main"},
        "target": {"ref": binding["artifact_ref"], "revision": 1}},
        kind="evaluation_input", schema_version="test", created_by="test", required=True)
    report = write_sidecar_json(tmp_path, "artifacts/evaluation/report.json", {
        "request_id": "evaluation-1", "verdict": verdict,
        "findings": [{"issue": "missing acceptance"}] if findings is None else findings,
        "manifest_ref": manifest["ref"], "manifest_digest": manifest["sha256"]},
        kind="evaluation_report", schema_version="test", created_by="test", required=True)
    completed = {"request_id": "evaluation-1", "verdict": verdict,
                 "report_ref": report["ref"], "report_digest": report["sha256"]}
    if terminal:
        writer.emit("output.evaluation.completed", actor="output-evaluator", correlation_id="ch",
                    payload=completed)
    routed = []
    def route(*args):
        message = args[-1]
        if any(row.type == "channel.agent.reply.requested" and row.payload.get("message_id") == message.payload["message_id"]
               for row in log.read_all()):
            return
        routed.append(message)
        writer.emit("channel.agent.reply.requested", actor="orchestrator-reactor", correlation_id="ch",
                    payload={"channel_id": "ch", "thread_id": "main", "request_id": "reply-request",
                             "target_member_id": "reviewer", "message_id": message.payload["message_id"]})

    monkeypatch.setattr("zf.runtime.channel_synthesis_reactor._route_runtime_message", route)
    return host, request, report, completed, routed


def reply(host, message, kind="revision_request", dispositions=None):
    refs = message.payload["refs"]
    review = {"verdict": "blocked", "artifact_digest": refs["artifact_digest"],
              "summary": "Source already requires this acceptance case.",
              "blocker_kind": kind, "blocker_question": "Add missing acceptance case.",
              "evidence_refs": ["source", "prd/r1.json"],
              "finding_dispositions": dispositions if dispositions is not None else [{
                  "finding_index": 0, "disposition": kind, "reason": "source section 1; PRD omits it"}]}
    emit_structured_reply_events(
        state_dir=host.state_dir, writer=host.event_writer,
        channel=project_channel(host.state_dir, "ch"),
        request={"thread_id": "main", "target_member_id": "reviewer"},
        message=message.payload, reply=json.dumps({"channel_consensus_review": review}),
        reply_event_id="reply-eval", actor="reviewer", source="runtime")


def test_native_reactor_dispatches_findings_and_signed_member_can_revise(tmp_path, monkeypatch):
    host, request, report, completed, routed = fixture(tmp_path, monkeypatch, terminal=False)

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    # The terminal was written by the mocked engine; dispatch is tested through the actual reactor.
    monkeypatch.setattr("zf.runtime.channel_prd_evaluation_reactor.threading.Thread", InlineThread)
    monkeypatch.setattr("zf.runtime.channel_prd_evaluation_reactor._request_item", lambda *args: object())
    monkeypatch.setattr("zf.runtime.channel_prd_evaluation_reactor.evaluate_output",
                        lambda **kwargs: host.event_writer.emit("output.evaluation.completed",
                                                                actor="output-evaluator", payload=completed))
    react_channel_prd_evaluation_requested(host, request)
    assert len(routed) == 1
    assert routed[0].payload["mentions"] == ["reviewer"]
    bundle = build_semantic_source_bundle(project_channel(tmp_path, "ch"), thread_id="main",
                                          trigger_message_id=routed[0].payload["message_id"], state_dir=tmp_path)
    assert bundle["complete"]
    assert any("完整原文" in str(row["content"]) for row in bundle["documents"])
    assert any(row["kind"] == "evaluation_report" and row["content"]["verdict"] == "FAIL"
               for row in bundle["documents"])
    publish_evaluation_feedback(host, request)
    assert len(routed) == 1
    reply(host, routed[0])
    reply(host, routed[0])
    blocked = [row for row in host.event_log.read_all() if row.type == "channel.consensus.blocked"]
    assert len(blocked) == 1
    assert blocked[0].payload["blocker_kind"] == "revision_request"
    reviewed = [row for row in host.event_log.read_all() if row.type == "channel.prd_evaluation.feedback.reviewed"]
    assert len(reviewed) == 1
    disposition = hydrate_sidecar_ref(tmp_path, reviewed[0].payload["review"]).payload
    assert disposition["review"]["finding_dispositions"][0]["reason"].startswith("source")
    # Original FAIL survives member triage unchanged.
    assert hydrate_sidecar_ref(tmp_path, report).payload["verdict"] == "FAIL"


@pytest.mark.parametrize("kind", ["revision_request", "owner_decision"])
def test_feedback_uses_existing_revision_or_ask_user_route(tmp_path, monkeypatch, kind):
    from zf.runtime.channel_review_rework import open_unseen_consensus_blockers

    host, request, _, _, routed = fixture(tmp_path, monkeypatch)
    publish_evaluation_feedback(host, request)
    reply(host, routed[0], kind)
    channel = project_channel(tmp_path, "ch")
    open_unseen_consensus_blockers(host.event_writer, channel, channel["consensus"]["main"],
                                  actor="channel-discussion", source="runtime", channel_id="ch",
                                  thread_id="main", synthesizer="author")
    question = project_channel(tmp_path, "ch")["open_questions"][0]
    assert question["kind"] == kind
    assert question["status"] == ("resolved" if kind == "revision_request" else "open")
    assert question["target_member_id"] == ("author" if kind == "revision_request" else "owner")


@pytest.mark.parametrize("defect", ["stale", "closed", "clean", "corrupt"])
def test_feedback_cannot_change_stale_closed_clean_or_corrupt_targets(tmp_path, monkeypatch, defect):
    host, request, _, _, routed = fixture(tmp_path, monkeypatch,
                                          verdict="PASS" if defect == "clean" else "FAIL",
                                          findings=[] if defect == "clean" else None)
    if defect == "stale":
        host.event_writer.emit("channel.consensus.proposed", actor="author", correlation_id="ch", payload={
            "channel_id": "ch", "thread_id": "main", "artifact_ref": "prd/r2.json", "artifact_digest": "2" * 64})
    elif defect == "closed":
        host.event_writer.emit("channel.discussion.closed", actor="channel-discussion", correlation_id="ch",
                               payload={"channel_id": "ch", "thread_id": "main"})
    elif defect == "corrupt":
        (tmp_path / "artifacts/evaluation/report.json").write_text("{}")
    if defect == "corrupt":
        with pytest.raises(SidecarRefError):
            publish_evaluation_feedback(host, request)
    else:
        publish_evaluation_feedback(host, request)
        assert any(row.type == "channel.prd_evaluation.feedback.skipped" for row in host.event_log.read_all())
    assert not routed


@pytest.mark.parametrize("defect", ["forged", "other_member", "missing_disposition", "report_digest", "review_id", "owner_confirmed"])
def test_feedback_review_rejects_forged_scope_or_incomplete_evidence(tmp_path, monkeypatch, defect):
    host, request, _, _, routed = fixture(tmp_path, monkeypatch)
    publish_evaluation_feedback(host, request)
    message = routed[0]
    if defect == "forged":
        message.payload["refs"]["evaluation_feedback_id"] = "forged"
    elif defect == "other_member":
        message.payload["refs"]["thread_id"] = "other-thread"
    elif defect == "report_digest":
        message.payload["refs"]["evaluation_report"]["sha256"] = "0" * 64
    elif defect == "review_id":
        message.payload["refs"]["consensus_review_id"] = "new-review-id"
    elif defect == "owner_confirmed":
        host.event_writer.emit("channel.consensus.signed", actor="web", correlation_id="ch",
                               payload={"channel_id": "ch", "thread_id": "main", "member_id": "web"})
    reply(host, message, dispositions=[] if defect == "missing_disposition" else None)
    assert not any(row.type == "channel.consensus.blocked" for row in host.event_log.read_all())
    assert any(row.type == "channel.consensus.review.rejected" for row in host.event_log.read_all())
