"""评测只消费合法冻结和精确对应的 accepted Workflow。"""
import json

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer
from zf.runtime import evaluation_runner as runner
from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, EvaluationScheduler
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
