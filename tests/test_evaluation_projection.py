"""Focused regression tests for the canonical Channel evaluation projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_projection import project_channel
from zf.runtime.evaluation_observer import _evaluation_status, _refresh_report, render_report
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_packets import evaluation_request_id
from zf.runtime.evaluation_artifacts import report_path


def _freeze(writer: EventWriter, *, channel_id: str = "ch-1", revision: int = 1):
    return writer.emit(
        "channel.prd.frozen",
        actor="kernel",
        payload={
            "channel_id": channel_id,
            "thread_id": "main",
            "prd_revision": revision,
            "source_event_id": "consensus-1",
            "freeze_reason": "test",
            "prd_ref": "channel-artifacts/prd.md",
            "prd_digest": "a" * 64,
        },
    )


def _evaluation_payload(freeze_id: str, *, run_id: str = "run-new", **extra: object):
    return {
        "channel_id": "ch-1",
        "evaluation_contract": "channel-prd-eval.v1",
        "prompt_version": EVALUATION_PROMPT_VERSION,
        "freeze_event_id": freeze_id,
        "trial_id": "trial-1",
        "source_snapshot_digest": "b" * 64,
        "workflow_run_id": run_id,
        "judge_type": "contribution",
        "job_id": f"{run_id}-contribution",
        "status": "queued",
        **extra,
    }


def test_projection_rejects_legacy_identity_and_late_old_run(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    writer.emit(
        "benchmark.evaluation.progress",
        actor="evaluation-scheduler",
        payload={**_evaluation_payload(freeze.id), "evaluation_contract": "legacy"},
    )
    writer.emit(
        "benchmark.evaluation.progress",
        actor="evaluation-scheduler",
        payload=_evaluation_payload(freeze.id),
    )
    writer.emit(
        "benchmark.evaluation.judge.completed",
        actor="evaluation-runner",
        payload=_evaluation_payload(
            freeze.id, run_id="run-old", job_id="run-old-contribution", status="scored",
        ),
    )
    evaluation = project_channel(tmp_path, "ch-1")["evaluation"]
    assert evaluation["workflow_run_id"] == "run-new"
    assert [row["workflow_run_id"] for row in evaluation["stages"]] == ["run-new"]


def test_projection_rejects_malformed_source_snapshot_digest(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    writer.emit(
        "benchmark.evaluation.progress",
        actor="evaluation-scheduler",
        payload=_evaluation_payload(
            freeze.id,
            source_snapshot_digest="not-a-sha256-digest",
        ),
    )
    assert project_channel(tmp_path, "ch-1")["evaluation"] == {}


def test_projection_rejects_explicit_zero_revision_instead_of_fallback(
    tmp_path: Path,
) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = writer.emit(
        "channel.prd.frozen",
        actor="kernel",
        payload={
            "channel_id": "ch-1",
            "thread_id": "main",
            "prd_revision": 0,
            "revision": 1,
            "source_event_id": "consensus-1",
        },
    )
    writer.emit(
        "benchmark.evaluation.progress",
        actor="evaluation-scheduler",
        payload=_evaluation_payload(freeze.id, prd_revision=1),
    )
    assert project_channel(tmp_path, "ch-1")["evaluation"] == {}


def test_projection_rejects_malformed_explicit_revision_instead_of_fallback(
    tmp_path: Path,
) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = writer.emit(
        "channel.prd.frozen",
        actor="kernel",
        payload={
            "channel_id": "ch-1",
            "thread_id": "main",
            "prd_revision": "bad",
            "revision": 1,
            "source_event_id": "consensus-1",
        },
    )
    writer.emit(
        "benchmark.evaluation.progress",
        actor="evaluation-scheduler",
        payload=_evaluation_payload(freeze.id, prd_revision=1),
    )
    assert project_channel(tmp_path, "ch-1")["evaluation"] == {}


def test_projection_rejects_noncanonical_progress_revision(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    for revision in (True, 1.0, 1.5, "01", " 1"):
        writer.emit(
            "benchmark.evaluation.progress",
            actor="evaluation-scheduler",
            payload=_evaluation_payload(freeze.id, prd_revision=revision),
        )
    assert project_channel(tmp_path, "ch-1")["evaluation"] == {}


def test_projection_requires_explicit_freeze_thread(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = writer.emit(
        "channel.prd.frozen",
        actor="kernel",
        payload={
            "channel_id": "ch-1",
            "prd_revision": 1,
            "source_event_id": "consensus-1",
            "freeze_reason": "test",
            "prd_ref": "channel-artifacts/prd.md",
            "prd_digest": "a" * 64,
        },
    )
    writer.emit(
        "benchmark.evaluation.progress",
        actor="evaluation-scheduler",
        payload=_evaluation_payload(freeze.id),
    )
    assert project_channel(tmp_path, "ch-1")["evaluation"] == {}


def test_projection_accepts_valid_explicit_revision_without_fallback(
    tmp_path: Path,
) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = writer.emit(
        "channel.prd.frozen",
        actor="kernel",
        payload={
            "channel_id": "ch-1",
            "thread_id": "main",
            "prd_revision": 1,
            "revision": 99,
            "source_event_id": "consensus-1",
        },
    )
    writer.emit(
        "benchmark.evaluation.progress",
        actor="evaluation-scheduler",
        payload=_evaluation_payload(freeze.id, prd_revision=1),
    )
    assert project_channel(tmp_path, "ch-1")["evaluation"]["stages"][0]["status"] == "queued"


def test_projection_preserves_terminal_incomplete_status(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    writer.emit(
        "benchmark.evaluation.judge.completed",
        actor="evaluation-runner",
        payload=_evaluation_payload(freeze.id, status="incomplete"),
    )
    evaluation = project_channel(tmp_path, "ch-1")["evaluation"]
    assert evaluation["stages"][0]["status"] == "incomplete"
    assert evaluation["stages"][0]["verdict"] == "incomplete"


def test_newer_freeze_hides_previous_run_until_new_progress(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    first = _freeze(writer, revision=1)
    writer.emit(
        "benchmark.evaluation.judge.completed",
        actor="evaluation-runner",
        payload=_evaluation_payload(first.id, run_id="run-old", status="scored"),
    )
    second = _freeze(writer, revision=2)
    evaluation = project_channel(tmp_path, "ch-1")["evaluation"]
    assert evaluation == {}
    writer.emit(
        "benchmark.evaluation.progress",
        actor="evaluation-scheduler",
        payload=_evaluation_payload(second.id, run_id="run-new", status="queued"),
    )
    evaluation = project_channel(tmp_path, "ch-1")["evaluation"]
    assert evaluation["workflow_run_id"] == "run-new"


def test_new_fidelity_generation_hides_old_conclusion_and_late_completion(
    tmp_path: Path,
) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    old_digest, new_digest, result_digest = "b" * 64, "c" * 64, "d" * 64

    def queue(stage: str, digest: str, job_id: str, trial: str) -> dict:
        return {
            **_evaluation_payload(freeze.id, run_id="run-new", judge_type=stage),
            "source_snapshot_digest": digest,
            "trial_id": trial,
            "job_id": job_id,
            "status": "queued",
        }

    old_fidelity = queue("prd_fidelity", old_digest, "fidelity-old", "trial-old")
    old_conclusion = queue("conclusion", old_digest, "conclusion-old", "trial-root")
    writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler", payload=old_fidelity)
    writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler", payload=old_conclusion)
    writer.emit(
        "benchmark.evaluation.conclusion.completed",
        actor="evaluation-runner",
        payload={**old_conclusion, "source_snapshot_digest": result_digest,
                 "request_id": "conclusion-result-old", "job_id": "conclusion-old",
                 "status": "complete"},
    )

    new_fidelity = queue("prd_fidelity", new_digest, "fidelity-new", "trial-new")
    writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler", payload=new_fidelity)
    evaluation = project_channel(tmp_path, "ch-1")["evaluation"]
    assert all(row["judge_type"] != "conclusion" for row in evaluation["stages"])

    # A late old completion cannot replace the current fidelity generation.
    writer.emit(
        "benchmark.evaluation.conclusion.completed",
        actor="evaluation-runner",
        payload={**old_conclusion, "source_snapshot_digest": result_digest,
                 "request_id": "conclusion-result-old", "job_id": "conclusion-old",
                 "status": "complete"},
    )
    evaluation = project_channel(tmp_path, "ch-1")["evaluation"]
    assert all(row["judge_type"] != "conclusion" for row in evaluation["stages"])

    new_conclusion = queue("conclusion", new_digest, "conclusion-new", "trial-root")
    writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler", payload=new_conclusion)
    writer.emit(
        "benchmark.evaluation.conclusion.completed",
        actor="evaluation-runner",
        payload={**new_conclusion, "source_snapshot_digest": "e" * 64,
                 "request_id": "conclusion-result-new", "job_id": "conclusion-new",
                 "status": "complete"},
    )
    evaluation = project_channel(tmp_path, "ch-1")["evaluation"]
    assert any(
        row["judge_type"] == "conclusion" and row["status"] == "complete"
        for row in evaluation["stages"]
    )


def test_report_does_not_call_incomplete_conclusion_complete() -> None:
    judges = {
        stage: {"status": "scored", "result": {}}
        for stage in ("contribution", "discussion", "prd_fidelity")
    }
    conclusion = {
        "status": "incomplete",
        "result": {
            "conclusion_zh": "证据不足",
            "team_findings": {
                "prd_ready_for_next_stage": False,
                "discussion_meaningful": False,
                "discussion_progressed": False,
                "problems": ["缺少证据"],
                "underperformed_roles": [],
                "unnecessary_roles": [],
            },
        },
    }
    assert _evaluation_status(judges, conclusion) == "incomplete"
    report = render_report(
        workflow_run_id="run",
        trial_id="trial",
        channel_id="ch-1",
        freeze={},
        judges=judges,
        conclusion=conclusion,
    )
    assert "Evaluation status: **incomplete**" in report
    assert "PRD 可进入下一阶段：`False`" in report


def test_audit_sidecars_are_versioned_by_trial_and_source(tmp_path: Path) -> None:
    judges = {
        stage: {"status": "scored", "result": {} }
        for stage in ("task_type", "contribution", "discussion", "prd_fidelity")
    }
    for trial, source in (("trial-1", "a" * 64), ("trial-2", "b" * 64)):
        _refresh_report(
            tmp_path,
            workflow_run_id="run/unsafe",
            trial_id=trial,
            channel_id="ch-1",
            freeze={"event_id": "freeze-1"},
            judges=judges,
            conclusion=None,
            source_snapshot_digest=source,
        )
    audit_dirs = list((report_path(tmp_path, "run/unsafe").parent / "audit").iterdir())
    assert len(audit_dirs) == 2
    source_digests = set()
    for path in audit_dirs:
        assert (path / "evaluation.json").exists()
        assert (path / "packet.manifest.json").exists()
        assert (path / "judge.json").exists()
        metadata = json.loads((path / "evaluation.json").read_text(encoding="utf-8"))
        assert metadata["prompt_version"] == EVALUATION_PROMPT_VERSION
        source_digests.add(metadata["source_snapshot_digest"])
    assert source_digests == {"a" * 64, "b" * 64}


def test_report_uses_safe_run_path_for_raw_judge_links() -> None:
    report = render_report(
        workflow_run_id="run/unsafe",
        trial_id="trial",
        channel_id="ch-1",
        freeze={},
        judges={},
    )
    assert "judges/run%2Funsafe/" in report
    assert "conclusions/run%2Funsafe/" in report
    assert "judges/run/unsafe/" not in report
    assert "conclusions/run/unsafe/" not in report


def _legacy_queue_payload(freeze_id: str, *, stage: str = "contribution") -> dict:
    payload = _evaluation_payload(freeze_id, judge_type=stage, status="running")
    payload.pop("evaluation_contract")
    payload.pop("prompt_version")
    payload["job_id"] = evaluation_request_id(**{
        key: payload[key] for key in (
            "workflow_run_id", "trial_id", "judge_type", "source_snapshot_digest",
        )
    })
    return payload


@pytest.mark.parametrize("stage", [
    "task_type", "contribution", "discussion", "prd_fidelity", "conclusion",
])
def test_legacy_completion_replays_from_matching_queue(tmp_path: Path, stage: str) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    queued = _legacy_queue_payload(freeze.id, stage=stage)
    writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler", payload=queued)
    request_id = queued["job_id"] if stage != "conclusion" else "conclusion-result-request"
    progress = {
        key: queued[key] for key in ("channel_id", "workflow_run_id", "trial_id", "judge_type")
    }
    progress.update(request_id=request_id, status="running", attempt=1)
    writer.emit("benchmark.evaluation.progress", actor="evaluation-runner", payload=progress)
    verdict = "complete" if stage == "conclusion" else "scored"
    completed = {**progress, "status": verdict}
    if stage != "conclusion":
        completed.update(
            prompt_version=EVALUATION_PROMPT_VERSION,
            source_snapshot_digest=queued["source_snapshot_digest"],
        )
    event_type = f"benchmark.evaluation.{'conclusion' if stage == 'conclusion' else 'judge'}.completed"
    writer.emit(event_type, actor="evaluation-runner", payload=completed)
    writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler",
                payload={**queued, "status": "completed"})
    original_ledger = (tmp_path / "events.jsonl").read_bytes()
    evaluation = project_channel(tmp_path, "ch-1")["evaluation"]
    assert len(evaluation["stages"]) == 1
    row = evaluation["stages"][0]
    assert (row["status"], row["verdict"], row["queue_status"]) == (verdict, verdict, "completed")
    assert (row["freeze_event_id"], row["request_id"], row["attempt"]) == (freeze.id, request_id, 1)
    assert row["terminal"] is True
    assert (tmp_path / "events.jsonl").read_bytes() == original_ledger


@pytest.mark.parametrize("conflict", [
    {"evaluation_contract": "legacy"},
    {"prompt_version": "prd-eval-prompts.v6"},
    {"freeze_event_id": "missing-freeze"},
    {"trial_id": "other-trial"},
    {"workflow_run_id": "other-run"},
    {"source_snapshot_digest": "c" * 64},
    {"source_snapshot_digest": "bad"},
    {"request_id": "other-request"},
    {"thread_id": "other-thread"},
    {"prd_revision": 2},
])
def test_legacy_completion_rejects_identity_conflicts(tmp_path: Path, conflict: dict) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    queued = _legacy_queue_payload(freeze.id)
    writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler", payload=queued)
    completed = {key: value for key, value in queued.items() if key not in {"job_id", "freeze_event_id"}}
    completed.update({"request_id": queued["job_id"], "status": "scored", **conflict})
    writer.emit("benchmark.evaluation.judge.completed", actor="evaluation-runner", payload=completed)
    row = project_channel(tmp_path, "ch-1")["evaluation"]["stages"][0]
    assert row["status"] == "running"
    assert not row.get("terminal")


@pytest.mark.parametrize("invalid_queue", [
    {"job_id": "unbound-job"},
    {"freeze_event_id": "missing-freeze"},
    {"prompt_version": "prd-eval-prompts.v6"},
])
def test_legacy_events_without_valid_queue_identity_stay_hidden(
    tmp_path: Path, invalid_queue: dict,
) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    queued = {**_legacy_queue_payload(freeze.id), **invalid_queue}
    writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler", payload=queued)
    writer.emit("benchmark.evaluation.judge.completed", actor="evaluation-runner", payload={
        "channel_id": "ch-1", "workflow_run_id": "run-new", "trial_id": "trial-1",
        "judge_type": "contribution", "request_id": queued["job_id"], "status": "scored",
    })
    assert project_channel(tmp_path, "ch-1")["evaluation"] == {}


@pytest.mark.parametrize("old_status", ["running", "completed"])
@pytest.mark.parametrize("explicit_identity", [False, True])
def test_unbound_conclusion_requires_unique_queue(
    tmp_path: Path, old_status: str, explicit_identity: bool,
) -> None:
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    freeze = _freeze(writer)
    for job_id, status in (("conclusion-1", old_status), ("conclusion-2", "running")):
        writer.emit("benchmark.evaluation.progress", actor="evaluation-scheduler", payload=
                    _evaluation_payload(freeze.id, judge_type="conclusion", job_id=job_id, status=status))
    payload = {
        "channel_id": "ch-1", "workflow_run_id": "run-new", "trial_id": "trial-1",
        "judge_type": "conclusion", "request_id": "conclusion-result-request", "status": "complete",
    }
    if explicit_identity:
        payload.update(
            evaluation_contract="channel-prd-eval.v1", prompt_version=EVALUATION_PROMPT_VERSION,
            freeze_event_id=freeze.id, source_snapshot_digest="c" * 64,
        )
    writer.emit("benchmark.evaluation.conclusion.completed", actor="evaluation-runner", payload=payload)
    assert project_channel(tmp_path, "ch-1")["evaluation"]["stages"][0]["status"] == "running"
