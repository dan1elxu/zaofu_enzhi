"""冻结评测输入的身份和摘要门禁。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.core.events.model import ZfEvent
from zf.runtime.channel_prd_freeze import emit_prd_frozen
from zf.runtime.evaluation_observer import (
    _bound_prd_payload,
    _bound_workflows,
    _freeze_points,
    _frozen_discussion_events,
    _packet_messages,
    _read_json_artifact,
    observe_once,
)
from zf.runtime.evaluation_packets import evaluation_request_id, evaluation_run_id
from zf.runtime.evaluation_scheduler import EvaluationScheduler
from tests.evaluation_fixtures import _seed_channel_state


def _freeze(channel_id: str, thread_id: str, revision: int, event_id: str) -> ZfEvent:
    return ZfEvent(
        type="channel.prd.frozen",
        id=event_id,
        payload={
            "channel_id": channel_id,
            "thread_id": thread_id,
            "prd_ref": f"channels/{channel_id}/{thread_id}/r{revision}.md",
            "prd_digest": "a" * 64,
            "prd_revision": revision,
            "source_event_id": f"consensus-{event_id}",
            "freeze_reason": "test",
        },
    )


def test_freeze_points_keep_each_thread_revision_and_occurrence() -> None:
    events = [
        _freeze("ch", "main", 1, "freeze-1"),
        _freeze("ch", "main", 2, "freeze-2"),
        _freeze("ch", "review", 1, "freeze-3"),
        _freeze("other", "main", 1, "freeze-4"),
    ]
    points = _freeze_points(events)
    assert len(points) == 4
    assert {
        (item["channel_id"], item["thread_id"], item["prd_revision"], item["event_id"])
        for item in points.values()
    } == {
        ("ch", "main", 1, "freeze-1"),
        ("ch", "main", 2, "freeze-2"),
        ("ch", "review", 1, "freeze-3"),
        ("other", "main", 1, "freeze-4"),
    }


@pytest.mark.parametrize("revision", [True, False, 1.0, 1.5, "1.5", " 1", "01", ""])
def test_freeze_points_reject_noncanonical_revision(revision: object) -> None:
    event = _freeze("ch", "main", 1, "freeze-invalid")
    event.payload["prd_revision"] = revision
    assert _freeze_points([event]) == {}


def test_freeze_points_do_not_fallback_from_explicit_invalid_revision() -> None:
    event = _freeze("ch", "main", 1, "freeze-invalid-fallback")
    event.payload.update({"prd_revision": 0, "revision": 1})
    assert _freeze_points([event]) == {}


def test_discussion_events_and_messages_are_identity_filtered() -> None:
    events = [
        ZfEvent(
            type="channel.question.opened",
            id="good",
            payload={"channel_id": "ch", "thread_id": "main", "question_id": "q"},
        ),
        ZfEvent(
            type="channel.question.opened",
            id="wrong-thread",
            payload={"channel_id": "ch", "thread_id": "other", "question_id": "q"},
        ),
        ZfEvent(
            type="channel.question.opened",
            id="missing",
            payload={"channel_id": "ch", "question_id": "q"},
        ),
    ]
    discussion = _frozen_discussion_events(
        events, channel_id="ch", thread_id="main",
    )
    assert [item["event_id"] for item in discussion] == ["good"]

    messages = _packet_messages(
        {
            "channel_id": "ch",
            "messages": [
                {"event_id": "m-good", "channel_id": "ch", "thread_id": "main", "text": "good"},
                {"event_id": "m-other", "channel_id": "ch", "thread_id": "other", "text": "other"},
                {"event_id": "m-cross", "channel_id": "other", "thread_id": "main", "text": "cross"},
                {"event_id": "m-missing", "channel_id": "ch", "text": "missing"},
            ],
        },
        channel_id="ch",
        thread_id="main",
        freeze_event_id="freeze",
        hydrate=lambda message: message["text"],
    )
    assert [item["event_id"] for item in messages] == ["m-good"]


def test_artifact_digest_and_bound_prd_are_fail_closed(tmp_path: Path) -> None:
    ref = "channels/ch/prd/r1.json"
    path = tmp_path / ref
    path.parent.mkdir(parents=True)
    body = {"channel_id": "ch", "thread_id": "main", "revision": 1}
    path.write_text(json.dumps(body), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert _read_json_artifact(tmp_path, ref, expected_digest=digest) == body
    with pytest.raises(ValueError, match="digest"):
        _read_json_artifact(tmp_path, ref, expected_digest="0" * 64)

    binding = {
        "channel_thread_id": "main",
        "channel_prd_ref": ref,
        "channel_prd_revision": "1",
        "channel_prd_digest": digest,
        "prd_ref": ref,
        "prd_digest": digest,
        "workflow_run_id": "wf",
        "event_id": "bind",
    }
    frozen = {
        "prd_ref": ref,
        "prd_digest": digest,
    }
    assert _bound_prd_payload(
        tmp_path, [binding], frozen, thread_id="main", prd_revision=1,
    )["body"] == body
    path.write_text(json.dumps({**body, "revision": 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        _bound_prd_payload(
            tmp_path, [binding], frozen, thread_id="main", prd_revision=1,
        )


@pytest.mark.parametrize(
    ("mismatched_key", "mismatched_value"),
    [
        ("channel_prd_readiness_ref", "channels/ch/readiness/r0.json"),
        ("channel_prd_readiness_digest", "c" * 64),
    ],
)
def test_bound_prd_requires_the_frozen_readiness_lineage(
    tmp_path: Path, mismatched_key: str, mismatched_value: str,
) -> None:
    ref = "channels/ch/prd/r1.json"
    path = tmp_path / ref
    path.parent.mkdir(parents=True)
    body = {"channel_id": "ch", "thread_id": "main", "revision": 1}
    path.write_text(json.dumps(body), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    readiness_ref = "channels/ch/readiness/r1.json"
    readiness_digest = "b" * 64
    binding = {
        "channel_thread_id": "main",
        "channel_prd_ref": ref,
        "channel_prd_revision": "1",
        "channel_prd_digest": digest,
        "channel_prd_readiness_ref": readiness_ref,
        "channel_prd_readiness_digest": readiness_digest,
        "prd_ref": ref,
        "prd_digest": digest,
        "workflow_run_id": "wf",
        "event_id": "bind",
    }
    frozen = {
        "prd_ref": ref,
        "prd_digest": digest,
        "readiness_ref": readiness_ref,
        "readiness_digest": readiness_digest,
    }
    binding[mismatched_key] = mismatched_value
    assert _bound_prd_payload(
        tmp_path, [binding], frozen, thread_id="main", prd_revision=1,
    ) is None

    binding[mismatched_key] = frozen[
        "readiness_ref" if mismatched_key.endswith("_ref") else "readiness_digest"
    ]
    assert _bound_prd_payload(
        tmp_path, [binding], frozen, thread_id="main", prd_revision=1,
    )["workflow_run_id"] == "wf"


def test_accepted_top_level_prd_override_cannot_rebind_requested_lineage() -> None:
    """Accepted metadata cannot swap the artifact named by the request."""

    requested = ZfEvent(
        type="workflow.invoke.requested",
        id="request-1",
        payload={
            "channel_id": "ch",
            "workflow_run_id": "wf",
            "source_refs": {
                "channel_id": "ch",
                "channel_thread_id": "main",
                "channel_prd_ref": "channels/ch/prd/r1.md",
                "channel_prd_revision": "1",
                "channel_prd_digest": "a" * 64,
            },
        },
    )
    accepted = ZfEvent(
        type="workflow.invoke.accepted",
        id="accepted-1",
        payload={
            "source_event_id": requested.id,
            # These top-level fields used to bypass the source_refs check.
            "prd_ref": "channels/ch/prd/other.md",
            "prd_digest": "b" * 64,
        },
    )

    assert _bound_workflows([requested, accepted]) == {}


def test_bound_prd_copy_ref_is_distinct_from_channel_prd_lineage() -> None:
    requested = ZfEvent(
        type="workflow.invoke.requested",
        id="request-copy",
        payload={
            "channel_id": "ch",
            "workflow_run_id": "wf-copy",
            "prd_ref": "workflow-bound/ch/prd.json",
            "prd_digest": "b" * 64,
            "source_refs": {
                "channel_id": "ch",
                "channel_thread_id": "main",
                "channel_prd_ref": "channels/ch/prd/r1.md",
                "channel_prd_revision": "1",
                "channel_prd_digest": "a" * 64,
            },
        },
    )
    accepted = ZfEvent(
        type="workflow.invoke.accepted",
        id="accepted-copy",
        payload={
            "source_event_id": requested.id,
            "channel_id": "ch",
            "prd_ref": "workflow-bound/ch/prd.json",
            "prd_digest": "sha256:" + "b" * 64,
            "source_refs": dict(requested.payload["source_refs"]),
        },
    )

    bindings = _bound_workflows([requested, accepted])
    assert bindings["ch"][0]["prd_ref"] == "workflow-bound/ch/prd.json"


def test_nested_bound_prd_injection_cannot_rebind_requested_lineage() -> None:
    requested = ZfEvent(
        type="workflow.invoke.requested",
        id="request-nested-injection",
        payload={
            "channel_id": "ch",
            "workflow_run_id": "wf-nested-injection",
            "source_refs": {
                "channel_id": "ch",
                "channel_thread_id": "main",
                "channel_prd_ref": "channels/ch/prd/r1.md",
                "channel_prd_revision": "1",
                "channel_prd_digest": "a" * 64,
            },
        },
    )
    accepted = ZfEvent(
        type="workflow.invoke.accepted",
        id="accepted-nested-injection",
        payload={
            "source_event_id": requested.id,
            "source_refs": {
                **requested.payload["source_refs"],
                "prd_ref": "workflow-bound/evil.json",
                "prd_digest": "b" * 64,
            },
        },
    )

    assert _bound_workflows([requested, accepted]) == {}


def test_nested_bound_copy_may_move_to_top_level_without_rebinding() -> None:
    requested = ZfEvent(
        type="workflow.invoke.requested",
        id="request-nested-copy",
        payload={
            "channel_id": "ch",
            "workflow_run_id": "wf-nested-copy",
            "source_refs": {
                "channel_id": "ch",
                "channel_thread_id": "main",
                "channel_prd_ref": "channels/ch/prd/r1.md",
                "channel_prd_revision": "1",
                "channel_prd_digest": "a" * 64,
                "prd_ref": "workflow-bound/ch/prd.json",
                "prd_digest": "b" * 64,
            },
        },
    )
    accepted = ZfEvent(
        type="workflow.invoke.accepted",
        id="accepted-nested-copy",
        payload={
            "source_event_id": requested.id,
            "prd_ref": "workflow-bound/ch/prd.json",
            "prd_digest": "sha256:" + "b" * 64,
            "source_refs": {
                key: value
                for key, value in requested.payload["source_refs"].items()
                if key not in {"prd_ref", "prd_digest"}
            },
        },
    )

    bindings = _bound_workflows([requested, accepted])
    assert bindings["ch"][0]["prd_ref"] == "workflow-bound/ch/prd.json"
    assert bindings["ch"][0]["prd_digest"] == "sha256:" + "b" * 64


def test_nested_bound_copy_change_is_rejected() -> None:
    requested = ZfEvent(
        type="workflow.invoke.requested",
        id="request-nested-copy-change",
        payload={
            "channel_id": "ch",
            "workflow_run_id": "wf-nested-copy-change",
            "source_refs": {
                "channel_id": "ch",
                "channel_thread_id": "main",
                "channel_prd_ref": "channels/ch/prd/r1.md",
                "channel_prd_revision": "1",
                "channel_prd_digest": "a" * 64,
                "prd_ref": "workflow-bound/ch/prd.json",
                "prd_digest": "b" * 64,
            },
        },
    )
    accepted = ZfEvent(
        type="workflow.invoke.accepted",
        id="accepted-nested-copy-change",
        payload={
            "source_event_id": requested.id,
            "source_refs": {
                **requested.payload["source_refs"],
                "prd_ref": "workflow-bound/other.json",
            },
        },
    )

    assert _bound_workflows([requested, accepted]) == {}


def test_nested_bound_copy_cannot_be_cleared_by_acceptance() -> None:
    requested = ZfEvent(
        type="workflow.invoke.requested",
        id="request-nested-clear",
        payload={
            "channel_id": "ch",
            "workflow_run_id": "wf-nested-clear",
            "source_refs": {
                "channel_id": "ch",
                "channel_thread_id": "main",
                "channel_prd_ref": "channels/ch/prd/r1.md",
                "channel_prd_revision": "1",
                "channel_prd_digest": "a" * 64,
                "prd_ref": "workflow-bound/ch/prd.json",
                "prd_digest": "b" * 64,
            },
        },
    )
    accepted = ZfEvent(
        type="workflow.invoke.accepted",
        id="accepted-nested-clear",
        payload={
            "source_event_id": requested.id,
            "source_refs": {"prd_digest": ""},
        },
    )

    assert _bound_workflows([requested, accepted]) == {}


def test_channel_prd_lineage_can_be_the_workflow_bound_pair() -> None:
    requested = ZfEvent(
        type="workflow.invoke.requested",
        id="request-channel-pair",
        payload={
            "channel_id": "ch",
            "workflow_run_id": "wf-channel-pair",
            "source_refs": {
                "channel_id": "ch",
                "channel_thread_id": "main",
                "channel_prd_ref": "channels/ch/prd/r1.md",
                "channel_prd_revision": "1",
                "channel_prd_digest": "a" * 64,
            },
        },
    )
    accepted = ZfEvent(
        type="workflow.invoke.accepted",
        id="accepted-channel-pair",
        payload={"source_event_id": requested.id},
    )

    bindings = _bound_workflows([requested, accepted])
    assert bindings["ch"][0]["prd_ref"] == "channels/ch/prd/r1.md"
    assert bindings["ch"][0]["prd_digest"] == "a" * 64


def test_accepted_workflow_run_id_cannot_rebind_requested_identity() -> None:
    requested = ZfEvent(
        type="workflow.invoke.requested",
        id="request-run-identity",
        payload={
            "channel_id": "ch",
            "workflow_run_id": "wf-requested",
            "source_refs": {
                "channel_id": "ch",
                "channel_thread_id": "main",
                "channel_prd_ref": "channels/ch/prd/r1.md",
                "channel_prd_revision": "1",
                "channel_prd_digest": "a" * 64,
            },
        },
    )
    accepted = ZfEvent(
        type="workflow.invoke.accepted",
        id="accepted-run-identity",
        payload={
            "source_event_id": requested.id,
            "workflow_run_id": "wf-forged",
        },
    )

    assert _bound_workflows([requested, accepted]) == {}


def test_run_id_segments_are_not_lossy() -> None:
    assert evaluation_run_id(
        channel_id="a/b", thread_id="main", prd_revision=1,
    ) != evaluation_run_id(
        channel_id="a-b", thread_id="main", prd_revision=1,
    )


def test_run_id_keeps_freeze_occurrences_independent() -> None:
    first = evaluation_run_id(
        channel_id="ch", thread_id="main", prd_revision=1,
        occurrence_id="freeze-1",
    )
    second = evaluation_run_id(
        channel_id="ch", thread_id="main", prd_revision=1,
        occurrence_id="freeze-2",
    )
    assert first != second
    assert first.endswith("-ffreeze-1")
    assert second.endswith("-ffreeze-2")
    assert evaluation_request_id(
        workflow_run_id=first,
        trial_id="same-trial",
        judge_type="contribution",
        source_snapshot_digest="d" * 64,
    ) != evaluation_request_id(
        workflow_run_id=second,
        trial_id="same-trial",
        judge_type="contribution",
        source_snapshot_digest="d" * 64,
    )

    # The occurrence suffix uses the same injective segment encoding as
    # channel/thread identities; event ids that only differ by a separator
    # must still address different result/report trees.
    assert evaluation_run_id(
        channel_id="ch", thread_id="main", prd_revision=1,
        occurrence_id="freeze/a",
    ) != evaluation_run_id(
        channel_id="ch", thread_id="main", prd_revision=1,
        occurrence_id="freeze-a",
    )


@pytest.mark.parametrize(
    ("synthesis_revision", "consensus_revision", "freeze_revision", "error_part"),
    [(1, 2, 2, "synthesis"), (2, 1, 2, "consensus")],
)
def test_freeze_rejects_consensus_or_synthesis_revision_drift(
    tmp_path: Path,
    synthesis_revision: int,
    consensus_revision: int,
    freeze_revision: int,
    error_part: str,
) -> None:
    """A matching ref/digest cannot smuggle a different PRD revision."""

    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit(
        "channel.created", actor="test",
        payload={"channel_id": "ch", "name": "ch", "task_id": "task"},
    )
    for member_id in ("m1", "m2"):
        writer.emit(
            "channel.member.added", actor="test",
            payload={
                "channel_id": "ch", "member_id": member_id,
                "member_type": "provider_agent", "backend": "fake",
                "name": member_id,
            },
        )
    for index, member_id in enumerate(("m1", "m2"), start=1):
        writer.emit(
            "channel.message.posted", actor="test",
            payload={
                "channel_id": "ch", "thread_id": "main",
                "message_id": f"message-{index}", "member_id": member_id,
                "role": "dev", "text": "proposal",
            },
        )

    prd_ref = "channels/ch/prd.md"
    (tmp_path / prd_ref).parent.mkdir(parents=True)
    (tmp_path / prd_ref).write_text("# Frozen PRD\n", encoding="utf-8")
    prd_digest = hashlib.sha256((tmp_path / prd_ref).read_bytes()).hexdigest()
    readiness_ref = "channels/ch/readiness.json"
    readiness_body = {
        "channel_id": "ch", "thread_id": "main", "revision": freeze_revision,
        "verdict": "ready", "gaps": [], "risks": [],
    }
    (tmp_path / readiness_ref).write_text(
        json.dumps(readiness_body, sort_keys=True), encoding="utf-8",
    )
    readiness_digest = hashlib.sha256(
        (tmp_path / readiness_ref).read_bytes()
    ).hexdigest()
    artifact = {
        "channel_id": "ch", "thread_id": "main",
        "artifact_ref": prd_ref, "artifact_digest": prd_digest,
        "prd_ref": prd_ref, "prd_digest": prd_digest,
        "prd_revision": synthesis_revision,
        "readiness_ref": readiness_ref, "readiness_digest": readiness_digest,
        "readiness_verdict": "ready", "required_signers": ["m1", "m2"],
        "product_mode": "multi_lens", "task_id": "task",
    }
    writer.emit("channel.synthesis.proposed", actor="test", payload=artifact)
    writer.emit("channel.consensus.proposed", actor="test", payload=artifact)
    for member_id in ("m1", "m2"):
        writer.emit(
            "channel.consensus.signed", actor=member_id,
            payload={**artifact, "member_id": member_id},
        )
    reached = writer.emit(
        "channel.consensus.reached", actor="kernel",
        payload={**artifact, "prd_revision": consensus_revision},
    )
    emit_prd_frozen(
        writer,
        consensus_payload={**artifact, "prd_revision": freeze_revision},
        source_event=reached,
        freeze_reason="test",
    )

    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        stats = observe_once(
            state_dir=tmp_path, scheduler=scheduler, dispatch=False,
        )
    finally:
        scheduler.close()
    assert stats["queued"] == 0
    assert stats["errors"]
    assert error_part in stats["errors"][0]


def test_freeze_without_source_event_does_not_borrow_projection_reached_event(
    tmp_path: Path,
) -> None:
    _seed_channel_state(tmp_path, emit_prd_freeze=False)
    events = EventLog(tmp_path / "events.jsonl").read_all()
    consensus = next(
        event.payload for event in reversed(events)
        if event.type == "channel.consensus.reached"
    )
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    payload = dict(consensus)
    payload.pop("source_event_id", None)
    writer.emit("channel.prd.frozen", actor="test", payload=payload)

    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        stats = observe_once(
            state_dir=tmp_path, scheduler=scheduler, dispatch=False,
        )
    finally:
        scheduler.close()
    assert stats["queued"] == 0
    assert any("source event is missing" in error for error in stats["errors"])
