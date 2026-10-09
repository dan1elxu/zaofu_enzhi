"""Malformed cross-review output gets bounded repair without changing its decision."""
from types import SimpleNamespace

import pytest

from zf.runtime.channel_deliberation_contract import apply_cross_review_reply
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_reply_remediation import remediate_channel_replies
from tests.channel_wait import wait_for_channel
from tests.test_channel_formal_reply_queue import CH, queued, review, runtime


REASON = "cross_review_needs_owner_requires_question_and_recommendation"


def rejected_runtime(tmp_path, *, reason=REASON, generation=1):
    writer = runtime(tmp_path)
    writer.emit("channel.question.opened", actor="test", payload={
        "channel_id": CH, "thread_id": "main", "question_id": "q-format",
        "question": "Does leaving a group revoke its manager relation?", "kind": "fact",
        "target_member_id": "arch", "asked_by": "arch",
    })
    review(writer, "format")
    queued(writer, "format")
    if generation > 1:
        writer.emit("channel.agent.reply.requested", actor="test", payload={
            "channel_id": CH, "request_id": "reply-format", "run_generation": generation,
        })
    completed = writer.emit("channel.agent.reply.completed", actor="test", payload={
        "channel_id": CH, "thread_id": "main", "request_id": "reply-format",
        "message_id": "msg-format", "target_member_id": "arch", "run_generation": generation,
    })
    writer.emit("channel.cross_review.rejected", actor="arch", causation_id=completed.id, payload={
        "channel_id": CH, "thread_id": "main", "request_id": "format",
        "target_member_id": "arch", "reason": reason,
    })
    return writer


def test_format_repair_preserves_review_identity_and_runs_in_background(tmp_path):
    writer = rejected_runtime(tmp_path)
    inline = []
    result = remediate_channel_replies(writer, events=writer.event_log.read_all(), dispatch=lambda *x: inline.append(x))
    assert result == {"redispatched": ["reply-format"], "exhausted": []}
    assert inline == []
    channel = project_channel(tmp_path, CH)
    assert channel["reply_requests"][0]["run_generation"] == 2
    assert channel["cross_reviews"][0]["status"] == "rejected", "repair is not semantic approval"
    assert channel["open_questions"][0]["status"] == "open"
    assert remediate_channel_replies(writer, events=writer.event_log.read_all())["redispatched"] == []


@pytest.mark.parametrize("reason", ["cross_review_target_mismatch", "cross_review_question_mismatch", "unknown_cross_review_request"])
def test_identity_and_authority_rejections_are_not_retried(tmp_path, reason):
    writer = rejected_runtime(tmp_path, reason=reason)
    assert remediate_channel_replies(writer, events=writer.event_log.read_all())["redispatched"] == []


@pytest.mark.parametrize("terminal", ["channel.closed", "channel.discussion.continued", "channel.discussion.closed",
                                     "channel.member.removed", "channel.member.suspended", "channel.cross_review.completed"])
def test_format_repair_does_not_revive_abandoned_review(tmp_path, terminal):
    writer = rejected_runtime(tmp_path)
    writer.emit(terminal, actor="test", payload={"channel_id": CH, "thread_id": "main", "member_id": "arch", "request_id": "format"})
    assert remediate_channel_replies(writer, events=writer.event_log.read_all())["redispatched"] == []


def test_format_repair_has_the_existing_generation_cap(tmp_path):
    writer = rejected_runtime(tmp_path, generation=3)
    assert remediate_channel_replies(writer, events=writer.event_log.read_all()) == {"redispatched": [], "exhausted": ["reply-format"]}
    assert remediate_channel_replies(writer, events=writer.event_log.read_all()) == {"redispatched": [], "exhausted": []}


def test_repaired_reply_becomes_owner_question_without_resolving_it(tmp_path):
    writer = rejected_runtime(tmp_path)
    remediate_channel_replies(writer, events=writer.event_log.read_all())
    channel = project_channel(tmp_path, CH)
    apply_cross_review_reply(state_dir=tmp_path, writer=writer, channel=channel,
        channel_id=CH, thread_id="main", member_id="arch", request=channel["reply_requests"][0],
        refs={"cross_review_request_id": "format", "question_id": "q-format", "cross_review_contract": "v2"},
        review={"summary": "Source leaves a choice", "answer": "Not explicit in source",
                "evidence_refs": ["PRD.md#groups"], "outcome": "needs_owner",
                "owner_question": "Should leaving revoke the group manager relation?",
                "recommended_answer": "Revoke it atomically to prevent self-rejoining."},
        reply_event_id="new-completed", actor="arch", source="test")
    channel = project_channel(tmp_path, CH)
    assert channel["cross_reviews"][0]["status"] == "completed"
    assert channel["open_questions"][0]["kind"] == "owner_decision"
    assert channel["open_questions"][0]["status"] == "open"


def test_kernel_sweep_repairs_rejected_format_via_real_dispatch_entry(tmp_path):
    from zf.runtime.orchestrator_reactor import EventReactorMixin
    writer = rejected_runtime(tmp_path)
    host = SimpleNamespace(state_dir=tmp_path, event_log=writer.event_log, event_writer=writer,
                           project_root=tmp_path, config=None)
    EventReactorMixin._check_channel_reply_remediation(host)
    wait_for_channel(lambda: any(e.type == "channel.cross_review.completed" for e in writer.event_log.read_all()))
    channel = project_channel(tmp_path, CH)
    assert channel["cross_reviews"][0]["status"] == "completed"
    assert channel["reply_requests"][0]["run_generation"] == 2


@pytest.mark.parametrize("generation", [1, 3])
def test_stale_or_unrequested_generation_cannot_replace_rejection(tmp_path, generation):
    writer = rejected_runtime(tmp_path)
    remediate_channel_replies(writer, events=writer.event_log.read_all())
    channel = project_channel(tmp_path, CH)
    before = len(writer.event_log.read_all())
    request = {**channel["reply_requests"][0], "run_generation": generation}
    apply_cross_review_reply(state_dir=tmp_path, writer=writer, channel=channel,
        channel_id=CH, thread_id="main", member_id="arch", request=request,
        refs={"cross_review_request_id": "format", "question_id": "q-format"},
        review={"summary": "Late result", "answer": "Resolved", "evidence_refs": ["PRD.md"]},
        reply_event_id="late", actor="arch", source="test")
    assert len(writer.event_log.read_all()) == before
    assert project_channel(tmp_path, CH)["cross_reviews"][0]["reason"] == REASON
