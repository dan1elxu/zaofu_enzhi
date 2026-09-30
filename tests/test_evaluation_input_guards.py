"""评测只消费合法冻结和精确对应的 accepted Workflow。"""
import hashlib
import json

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer
from zf.runtime import evaluation_runner as runner
from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, EvaluationScheduler
from zf.runtime.channel_sidecar import channel_message_event_payload
from zf.runtime.evaluation_inputs import _strict_snapshot_message_text
from tests.evaluation_fixtures import _seed_channel_state, _workflow_binding_payload


def discover(state_dir):
    scheduler = EvaluationScheduler(state_dir, inline=True)
    try:
        stats = observer.observe_once(state_dir=state_dir, scheduler=scheduler, dispatch=False)
        packets = [json.loads((state_dir / entry.input_ref).read_text())["packets"]["prd_fidelity"]
                   for entry in scheduler.entries.values() if entry.judge_type == "prd_fidelity"]
        return stats, packets
    finally:
        scheduler.close()


@pytest.mark.parametrize("mode", ["conversation", "clarification"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_owner_freeze_requires_owner_confirmation(tmp_path, mode, confirmed):
    _seed_channel_state(tmp_path, product_mode=mode, signers=(), owner_confirmed=confirmed)
    stats, packets = discover(tmp_path)
    assert stats["queued"] == (5 if confirmed else 0)
    assert bool(stats["errors"]) is not confirmed
    assert bool(packets) is confirmed


def test_owner_confirmation_does_not_replace_multi_lens_review(tmp_path):
    _seed_channel_state(tmp_path, signers=("m1",), owner_confirmed=True)
    stats, packets = discover(tmp_path)
    assert stats["errors"] and stats["queued"] == 0 and not packets


@pytest.mark.parametrize("outcome", ["pending", "rejected", "accepted"])
def test_only_accepted_request_binds_workflow(tmp_path, outcome):
    _seed_channel_state(tmp_path)
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    requested = writer.emit("workflow.invoke.requested", actor="test",
                            payload=_workflow_binding_payload(tmp_path))
    if outcome != "pending":
        writer.emit(f"workflow.invoke.{outcome}", actor="kernel",
                    payload={"source_event_id": requested.id})
    stats, packets = discover(tmp_path)
    assert stats["errors"] == [] and len(packets) == 1
    assert bool(packets[0]["workflow_bound_prd"]) is (outcome == "accepted")


@pytest.mark.parametrize("field,value", [
    ("channel_thread_id", "another-thread"),
    ("channel_prd_revision", "2"),
    ("channel_prd_ref", "older-prd.md"),
    ("channel_prd_digest", "0" * 64),
    ("channel_prd_digest", ""),
])
def test_accepted_other_prd_does_not_bind_current_freeze(tmp_path, field, value):
    _seed_channel_state(tmp_path)
    payload = _workflow_binding_payload(tmp_path)
    payload["source_refs"][field] = value
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    requested = writer.emit("workflow.invoke.requested", actor="test", payload=payload)
    writer.emit("workflow.invoke.accepted", actor="kernel",
                payload={"source_event_id": requested.id})
    stats, packets = discover(tmp_path)
    assert stats["errors"] == []
    assert packets[0]["workflow_bound_prd"] == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel_prd_ref", "channel-artifacts/ch-1/prd-r0.md"),
        ("channel_prd_revision", "0"),
        ("channel_prd_readiness_ref", "channel-artifacts/ch-1/readiness-r0.json"),
        ("channel_prd_readiness_digest", "0" * 64),
    ],
)
def test_accepted_source_refs_cannot_rewrite_requested_prd_lineage(
    tmp_path, field, value,
):
    _seed_channel_state(tmp_path)
    payload = _workflow_binding_payload(tmp_path)
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    requested = writer.emit(
        "workflow.invoke.requested", actor="test", payload=payload,
    )
    accepted_refs = dict(payload["source_refs"])
    accepted_refs[field] = value
    writer.emit(
        "workflow.invoke.accepted", actor="kernel",
        payload={"source_event_id": requested.id, "source_refs": accepted_refs},
    )
    stats, packets = discover(tmp_path)
    assert stats["errors"] == []
    assert packets[0]["workflow_bound_prd"] == {}


def test_later_other_revision_does_not_displace_matching_binding(tmp_path):
    _seed_channel_state(tmp_path)
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    for revision in (1, 2):
        payload = _workflow_binding_payload(tmp_path)
        payload["workflow_run_id"] = f"wf-r{revision}"
        payload["source_refs"]["channel_prd_revision"] = str(revision)
        payload["source_refs"]["channel_prd_digest"] = "sha256:" + payload["source_refs"]["channel_prd_digest"]
        requested = writer.emit("workflow.invoke.requested", actor="test", payload=payload)
        writer.emit("workflow.invoke.accepted", actor="kernel",
                    payload={"source_event_id": requested.id})
    stats, packets = discover(tmp_path)
    assert stats["errors"] == []
    bound = packets[0]["workflow_bound_prd"]
    assert bound["workflow_run_id"] == "wf-r1"
    assert bound["bound_from_channel_prd_ref"] == "channel-artifacts/ch-1/prd.md"


def test_acceptance_without_exact_request_does_not_borrow_another_request(tmp_path):
    _seed_channel_state(tmp_path)
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    payload = _workflow_binding_payload(tmp_path)
    writer.emit("workflow.invoke.requested", actor="test", payload=payload)
    writer.emit("workflow.invoke.accepted", actor="kernel",
                payload={"source_event_id": "missing-request", "workflow_run_id": "wf-bound-1"})
    stats, packets = discover(tmp_path)
    assert stats["errors"] == [] and packets[0]["workflow_bound_prd"] == {}


def test_retired_queue_stage_is_never_dispatched(tmp_path):
    """A pre-existing v1 queue row is retained for audit but cannot run."""
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert not scheduler.enqueue(EvaluationQueueEntry(
            job_id="legacy", channel_id="ch", workflow_run_id="wf",
            judge_type="retired_stage",
        ))
        assert "legacy" not in scheduler.entries
    finally:
        scheduler.close()


def test_retired_queue_rows_are_terminal_on_restart(tmp_path):
    queue = tmp_path / "artifacts/benchmark-evaluations/scheduler.json"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        json.dumps({
            "schema_version": "benchmark.evaluation.scheduler.v1",
            "entries": [{
                "job_id": "legacy", "channel_id": "ch", "workflow_run_id": "wf",
                "judge_type": "retired_stage", "status": "queued",
            }],
        }),
        encoding="utf-8",
    )
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        entry = scheduler.entries["legacy"]
        assert entry.status == "inconclusive"
        assert "retired" in entry.error
        calls = []
        scheduler.tick(lambda item, slot: calls.append(item.judge_type) or {"status": "completed"})
        assert calls == []
    finally:
        scheduler.close()


def test_retired_judge_type_cannot_reuse_legacy_sidecar(tmp_path):
    with pytest.raises(ValueError, match="retired"):
        runner.run_judge(
            state_dir=tmp_path,
            project_root=None,
            config=None,
            writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
            workflow_run_id="wf",
            trial_id="trial",
            judge_type="retired_stage",
            packet={},
            backend=lambda **kwargs: pytest.fail("legacy stage launched provider"),
        )


def _single_freeze(events):
    points = observer._freeze_points(events)
    assert len(points) == 1
    return next(iter(points.values()))


def test_packet_uses_consensus_source_snapshot_not_late_pre_freeze_changes(tmp_path):
    """A delayed freeze must not admit material after its consensus source."""
    _seed_channel_state(tmp_path, emit_prd_freeze=False)
    log = EventLog(tmp_path / "events.jsonl")
    writer = EventWriter(log)
    reached = next(event for event in log.read_all()
                   if event.type == "channel.consensus.reached")
    writer.emit(
        "channel.message.posted", actor="owner",
        payload={
            "channel_id": "ch-1", "thread_id": "main", "message_id": "late-requirement",
            "member_id": "owner", "role": "user", "text": "late requirement must not enter r1",
        },
    )
    writer.emit(
        "channel.discussion.continued", actor="owner",
        payload={
            "channel_id": "ch-1", "thread_id": "main", "roster": ["late-agent"],
            "requirement_message_id": "late-requirement",
        },
    )
    from zf.runtime.channel_prd_freeze import emit_prd_frozen
    emit_prd_frozen(writer, consensus_payload=reached.payload, source_event=reached,
                    freeze_reason="delayed-test", actor="kernel")

    stats, packets = discover(tmp_path)
    assert stats["errors"] == []
    assert len(packets) == 1
    packet = packets[0]
    assert "late requirement" not in packet["task"]["statement"]
    assert {message["message_id"] for message in packet["channel_messages"]} == {"msg-1", "msg-2"}
    assert {profile["agent_id"] for profile in packet["agent_profiles"]} == {"m1", "m2"}


def test_tampered_frozen_source_manifest_fails_closed(tmp_path):
    _seed_channel_state(tmp_path)
    events = list(EventLog(tmp_path / "events.jsonl").read_all())
    freeze = _single_freeze(events)
    tampered = dict(freeze)
    snapshot = dict(tampered["source_snapshot"])
    snapshot["events"] = list(snapshot["events"][:-1])
    tampered["source_snapshot"] = snapshot
    tampered["source_snapshot_digest"] = observer.stable_digest(snapshot)

    with pytest.raises(ValueError, match="manifest"):
        observer._prepare_channel(
            state_dir=tmp_path, events=events, channel_id="ch-1", freeze=tampered,
            bound_workflows=[],
        )


def test_corrupt_frozen_message_body_never_falls_back_to_preview(tmp_path):
    _seed_channel_state(tmp_path)
    path = tmp_path / "events.jsonl"
    events = list(EventLog(path).read_all())
    message = next(event for event in events if event.type == "channel.message.posted")
    message.payload.update({
        "text": "preview must not become evidence",
        "text_preview": "preview must not become evidence",
        "body_ref": "channels/ch-1/messages/missing.json",
        "body_sha256": "0" * 64,
        "body_byte_count": 1,
    })
    frozen = next(event for event in events if event.type == "channel.prd.frozen")
    snapshot = dict(frozen.payload["source_snapshot"])
    manifest = []
    for item in snapshot["events"]:
        updated = dict(item)
        if updated.get("event_id") == message.id:
            updated["payload"] = dict(message.payload)
        manifest.append(updated)
    snapshot["events"] = manifest
    frozen.payload["source_snapshot"] = snapshot
    frozen.payload["source_snapshot_digest"] = observer.stable_digest(snapshot)
    path.write_text("".join(event.to_json() + "\n" for event in events), encoding="utf-8")

    stats, packets = discover(tmp_path)
    assert stats["queued"] == 0
    assert packets == []
    assert any("evidence_missing: frozen message body unavailable" in error for error in stats["errors"])


def test_strict_snapshot_hydration_accepts_full_message_body(tmp_path):
    body = "x" * 5000
    payload = channel_message_event_payload(
        tmp_path,
        {"channel_id": "ch-1", "thread_id": "main", "message_id": "m-long", "text": body},
        created_by="test",
    )
    assert _strict_snapshot_message_text(tmp_path, payload) == body


@pytest.mark.parametrize("mutation", ["missing", "hash", "empty"])
def test_strict_snapshot_hydration_rejects_bad_or_empty_sidecar(tmp_path, mutation):
    payload = channel_message_event_payload(
        tmp_path,
        {"channel_id": "ch-1", "thread_id": "main", "message_id": "m-body", "text": "full body"},
        created_by="test",
    )
    descriptor = dict(payload["refs"]["message_body"])
    if mutation == "missing":
        descriptor["ref"] = "channels/ch-1/messages/missing.json"
    elif mutation == "hash":
        descriptor["sha256"] = "0" * 64
    else:
        from zf.runtime.sidecar_refs import sidecar_path
        empty = json.dumps({"schema_version": "channel.message.body.v1"}, ensure_ascii=False) + "\n"
        target = sidecar_path(tmp_path, descriptor["ref"])
        target.write_text(empty, encoding="utf-8")
        descriptor["sha256"] = hashlib.sha256(empty.encode()).hexdigest()
        descriptor["byte_count"] = len(empty.encode())
    bad = dict(payload)
    bad["refs"] = {"message_body": descriptor}
    with pytest.raises(Exception):
        _strict_snapshot_message_text(tmp_path, bad)


@pytest.mark.parametrize("source_id", ["missing-source", "late-source"])
def test_freeze_source_must_exist_before_its_occurrence(tmp_path, source_id):
    _seed_channel_state(tmp_path)
    events = list(EventLog(tmp_path / "events.jsonl").read_all())
    freeze = dict(_single_freeze(events))
    if source_id == "late-source":
        from zf.core.events.model import ZfEvent
        events.append(ZfEvent(
            type="channel.consensus.reached", id=source_id,
            payload={"channel_id": "ch-1", "thread_id": "main"},
        ))
    freeze["consensus_event_id"] = source_id
    freeze["source_event_id"] = source_id
    with pytest.raises(ValueError, match="source event"):
        observer._prepare_channel(
            state_dir=tmp_path, events=events, channel_id="ch-1", freeze=freeze,
            bound_workflows=[],
        )
