"""Durable queue and the real observer -> runner -> report -> Web wiring."""
import json
import threading
import time
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer, evaluation_runner as runner
from zf.runtime.channel_conversation_projection import project_channel_conversation
from zf.runtime.channel_projection import project_channel
from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, EvaluationScheduler
from zf.runtime.evaluation_turns import EvaluationCancelled, JudgeTurnObservation
from tests.test_evaluation_prd import _seed_channel_state, _judge_reply, _milestone_reply, _packet


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "evaluation did not settle"
        time.sleep(0.01)


def enqueue_chain(scheduler, channel):
    deps = []
    for stage in ("milestone", "communication", "prd_result", "conclusion"):
        job = f"{channel}-{stage}"
        scheduler.enqueue(EvaluationQueueEntry(
            job_id=job, channel_id=channel, workflow_run_id=f"wf-{channel}",
            judge_type=stage, depends_on=list(deps),
        ))
        deps.append(job)


def test_bounded_slots_allow_other_channel_to_finish_and_order_stages(tmp_path):
    scheduler = EvaluationScheduler(tmp_path, max_slots=2)
    for channel in ("a", "b", "c"):
        enqueue_chain(scheduler, channel)
    release = threading.Event()
    started = {key: threading.Event() for key in "abc"}
    calls = []
    def run(entry, slot):
        calls.append((entry.channel_id, entry.judge_type, slot))
        started[entry.channel_id].set()
        if entry.channel_id in {"a", "b"} and entry.judge_type == "milestone":
            assert release.wait(5)
        return {"status": "completed"}
    try:
        scheduler.tick(run)
        assert started["a"].wait(2) and started["b"].wait(2)
        assert not started["c"].is_set()
        assert {slot for _, _, slot in calls} == {0, 1}
        release.set()
        wait_until(lambda: all(entry.status == "completed" for entry in scheduler.entries.values()))
        for channel in "abc":
            assert [stage for ch, stage, _ in calls if ch == channel] == [
                "milestone", "communication", "prd_result", "conclusion",
            ]
    finally:
        release.set()
        scheduler.close()


def test_queue_recovery_and_single_owner(tmp_path):
    first = EvaluationScheduler(tmp_path, inline=True)
    enqueue_chain(first, "a")
    with pytest.raises(RuntimeError, match="already active"):
        EvaluationScheduler(tmp_path, inline=True)
    path = first.queue_path
    first.close()
    data = json.loads(path.read_text())
    data["entries"][0]["status"] = "running"  # process died during a provider turn
    path.write_text(json.dumps(data))
    second = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert second.entries["a-milestone"].status == "queued"
        calls = []
        second.tick(lambda entry, slot: calls.append(entry.judge_type) or {"status": "completed"})
        assert calls == ["milestone", "communication", "prd_result", "conclusion"]
        assert not second.enqueue(EvaluationQueueEntry("a-milestone", "a", "wf-a"))
    finally:
        second.close()


def test_queue_recovery_ignores_legacy_bookkeeping_fields(tmp_path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    enqueue_chain(scheduler, "a")
    path = scheduler.queue_path
    scheduler.close()
    data = json.loads(path.read_text())
    data["entries"][0]["attempts"] = {"provider": 1}
    data["entries"][0]["legacy_status"] = "running"
    path.write_text(json.dumps(data))

    recovered = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert recovered.entries["a-milestone"].status == "queued"
    finally:
        recovered.close()


def test_corrupt_queue_fails_closed_and_releases_owner(tmp_path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    path = scheduler.queue_path
    scheduler.close()
    path.write_text("broken")
    for _ in range(2):
        with pytest.raises(json.JSONDecodeError):
            EvaluationScheduler(tmp_path, inline=True)


def seed_second_channel(state):
    writer = EventWriter(EventLog(state / "events.jsonl"))
    for event in list(writer.event_log.read_all()):
        if event.payload.get("channel_id") == "ch-1":
            writer.emit(event.type, actor="test", payload={**event.payload, "channel_id": "ch-2"})


@pytest.mark.mock_e2e
def test_observer_mock_e2e_nonblocking_failure_report_and_web(tmp_path):
    _seed_channel_state(tmp_path, with_workflow_bind=True)
    seed_second_channel(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, max_slots=2)
    release, blocked = threading.Event(), threading.Event()
    calls = []
    def backend(**kwargs):
        calls.append((kwargs["workflow_run_id"], kwargs["judge_type"], kwargs["attempt"]))
        if "ch-1" in kwargs["workflow_run_id"] and kwargs["judge_type"] == "milestone":
            blocked.set()
            assert release.wait(5)
            return JudgeTurnObservation(status="aborted", error="interrupted")
        return _judge_reply(kwargs["judge_type"], with_bound=True), ""
    try:
        stats = observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=backend)
        assert stats["queued"] == 8 and blocked.wait(2)
        # Channel 2 reaches conclusion without releasing channel 1's turn.
        wait_until(lambda: all(e.status == "completed" for e in scheduler.entries.values() if e.channel_id == "ch-2"))
        assert any("ch-2" in run and stage == "conclusion" for run, stage, _ in calls)
        release.set()
        wait_until(lambda: all(e.status in {"completed", "inconclusive"} for e in scheduler.entries.values()))
        failures = [(stage, attempt) for run, stage, attempt in calls if "ch-1" in run and stage == "milestone"]
        assert failures == [("milestone", 1), ("milestone", 2)]
        assert not any("ch-1" in run and stage == "conclusion" for run, stage, _ in calls)
        report = runner.report_path(tmp_path, "channel-ch-1-main-r1").read_text()
        assert "inconclusive" in report and "overall_score: **None**" in report
        detail = project_channel_conversation(tmp_path, "ch-1")
        assert detail["evaluation"]["stages"][-1]["status"] == "inconclusive"
        before = list(calls)
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=backend)
        assert before == calls  # permanent failure is deduped on every later scan
    finally:
        release.set()
        scheduler.close()


def test_queued_frozen_inputs_survive_discovery_source_disappearing(tmp_path):
    _seed_channel_state(tmp_path)
    first = EvaluationScheduler(tmp_path, inline=True)
    observer.observe_once(state_dir=tmp_path, scheduler=first, dispatch=False)
    first.close()
    (tmp_path / "channel-artifacts/ch-1/prd.md").unlink()
    second = EvaluationScheduler(tmp_path, inline=True)
    calls = []
    try:
        def backend(**kwargs):
            calls.append(kwargs["judge_type"])
            return _judge_reply(kwargs["judge_type"]), ""
        stats = observer.observe_once(state_dir=tmp_path, scheduler=second, backend=backend)
        assert not stats["errors"]
        assert calls == ["milestone", "communication", "prd_result", "conclusion"]
        assert runner.report_path(tmp_path, "channel-ch-1-main-r1").exists()
    finally:
        second.close()


def judge_args(tmp_path, backend):
    return dict(state_dir=tmp_path, project_root=None, config=None,
                writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
                workflow_run_id="wf", trial_id="trial", judge_type="milestone",
                packet=_packet(), backend=backend)


@pytest.mark.parametrize("status", ["timeout", "provider_error", "aborted"])
def test_transport_retry_success_preserves_failed_attempt(tmp_path, status):
    calls = []
    def backend(**kwargs):
        calls.append((kwargs["attempt"], kwargs["schema_attempt"]))
        if len(calls) == 1:
            return JudgeTurnObservation(status=status, error="failure")
        return _milestone_reply(), ""
    result = runner.run_judge(**judge_args(tmp_path, backend))
    assert result["status"] == "scored" and calls == [(1, 1), (2, 1)]
    path = runner.judge_result_path(tmp_path, workflow_run_id="wf", judge_type="milestone", request_id=result["request_id"])
    attempt = json.loads((path.parent / "attempts" / f"{result['request_id']}-attempt-1.json").read_text())
    assert attempt["status"] == ("judge_timeout" if status == "timeout" else status)
    assert attempt["retryable"] is True


def test_schema_retry_is_separate_and_bounded(tmp_path):
    calls = []
    def backend(**kwargs):
        call = (kwargs["attempt"], kwargs["schema_attempt"])
        calls.append(call)
        return ("", "network failure") if call == (1, 2) else ("bad json", "")
    record = runner.run_judge(**judge_args(tmp_path, backend))
    assert record["status"] == "invalid"
    assert calls == [(1, 1), (1, 2), (2, 1), (2, 2)]


def test_crash_recovery_cannot_reset_provider_budget(tmp_path):
    calls = []
    def crash(**kwargs):
        calls.append(kwargs["attempt"])
        raise KeyboardInterrupt()  # process loss bypasses ordinary backend error handling
    args = judge_args(tmp_path, crash)
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            runner.run_judge(**args)
    record = runner.run_judge(**args)
    assert record["status"] == "inconclusive" and record["attempt"] == 2
    assert calls == [1, 2]
    assert runner.run_judge(**args) == record


def test_report_archives_trials_snapshots_recovery_and_legacy(tmp_path):
    path = runner.report_path(tmp_path, "wf")
    path.parent.mkdir(parents=True)
    path.write_text("legacy")
    args = dict(prompt_version="v1", trial_id="trial", source_snapshot_digest="snapshot")
    runner.write_report(tmp_path, "wf", "first", **args)
    runner.write_report(tmp_path, "wf", "recovered", **args)
    runner.write_report(tmp_path, "wf", "new trial", **{**args, "trial_id": "trial2"})
    runner.write_report(tmp_path, "wf", "new binding", **{**args, "source_snapshot_digest": "bound"})
    bodies = {p.read_text() for p in path.parent.glob("report-*.md")}
    assert {"legacy", "first", "recovered", "new trial", "new binding"} <= bodies
    assert path.read_text() == "new binding"


def test_projection_new_queued_revision_not_masked_by_old_completion(tmp_path):
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    base = {"channel_id": "channel", "judge_type": "milestone"}
    for job, run, status in [("old", "r0", "queued"), ("new", "r1", "queued"), ("old", "r0", "running"), ("old", "r0", "completed")]:
        writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler",
                    payload={**base, "job_id": job, "workflow_run_id": run, "status": status})
    evaluation = project_channel(tmp_path, "channel")["evaluation"]
    assert evaluation["workflow_run_id"] == "r1"
    assert evaluation["stages"][0]["status"] == "queued"


def test_observer_shutdown_leaves_consumed_attempt_for_restart(tmp_path):
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, max_slots=1)
    started = threading.Event()
    def cancelled_backend(**kwargs):
        started.set()
        assert kwargs["stop_event"].wait(3)
        raise EvaluationCancelled()
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=cancelled_backend)
        assert started.wait(2)
    finally:
        scheduler.close()
    recovered = EvaluationScheduler(tmp_path, inline=True)
    calls = []
    def success(**kwargs):
        calls.append((kwargs["judge_type"], kwargs["attempt"]))
        return _judge_reply(kwargs["judge_type"]), ""
    try:
        stats = observer.observe_once(state_dir=tmp_path, scheduler=recovered, backend=success)
        assert not stats["errors"]
        assert calls[0] == ("milestone", 2)
        assert len(calls) == 4
    finally:
        recovered.close()


def test_legacy_timeout_conclusion_is_replaced_without_provider(tmp_path):
    from zf.runtime.evaluation_packets import evaluation_request_id, stable_digest
    judges = {stage: {"status": "judge_timeout", "error": "legacy timeout"}
              for stage in ("milestone", "communication", "prd_result")}
    request = evaluation_request_id(workflow_run_id="wf", trial_id="t", judge_type="conclusion",
                                    source_snapshot_digest=stable_digest({key: judges[key] for key in sorted(judges)}))
    runner.write_conclusion_result(tmp_path, "wf", request, {"status": "judge_timeout"})
    def forbidden(**kwargs):
        pytest.fail("failed judges must never launch conclusion provider")
    record, ran = observer._pending_or_run_conclusion(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")), backend=forbidden,
        workflow_run_id="wf", trial_id="t", judges=judges,
    )
    assert ran and record["status"] == "inconclusive"
    assert record["result"]["overall_score"] is None


def test_cli_once_only_queues(tmp_path, monkeypatch):
    import argparse
    from types import SimpleNamespace
    from zf.cli.evaluation_watch import run_evaluation_watch
    from zf.core.config import loader, project_context
    _seed_channel_state(tmp_path)
    monkeypatch.delenv("ZF_STATE_DIR", raising=False)
    monkeypatch.setattr(project_context, "resolve_project_context", lambda **kwargs: SimpleNamespace(
        state_dir=tmp_path, project_root=tmp_path, config_path=tmp_path / "zf.yaml"))
    monkeypatch.setattr(loader, "load_config", lambda path: None)
    monkeypatch.setattr(runner, "_visible_tmux_backend", lambda **kwargs: pytest.fail("--once launched provider"))
    try:
        assert run_evaluation_watch(argparse.Namespace(interval=None, once=True)) == 0
        queue = json.loads((tmp_path / "artifacts/benchmark-evaluations/scheduler.json").read_text())
        assert len(queue["entries"]) == 4
        assert {entry["status"] for entry in queue["entries"]} == {"queued"}
    finally:
        from zf.runtime.evaluation_scheduler import scheduler_for_state
        scheduler_for_state(tmp_path, inline=False).close()
