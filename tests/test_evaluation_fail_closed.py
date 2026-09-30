from __future__ import annotations

import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_artifacts as artifacts
from zf.runtime import evaluation_observer as observer
from zf.runtime import evaluation_runner as runner
from zf.runtime.evaluation_packets import PACKET_SCHEMA_VERSION, evaluation_request_id, stable_digest
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_roles import snapshot_catalog
from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, EvaluationScheduler
from tests.evaluation_fixtures import _seed_channel_state


def _packet(stage: str, *, digest: str = "a" * 64, trial: str = "trial") -> dict:
    return {
        "schema_version": PACKET_SCHEMA_VERSION,
        "run_id": "run-1",
        "workflow_run_id": "run-1",
        "trial_id": trial,
        "stage": stage,
        "source_snapshot_digest": digest,
        "prompt_version": EVALUATION_PROMPT_VERSION,
        "task": {"statement": "fix the issue"},
        "task_details": {
            "statement": "fix the issue",
            "evaluation_role_catalog": snapshot_catalog(),
            "freeze_event_id": "freeze-1",
        },
        "agent_profiles": [],
        "agent_relationships": [],
        "iteration_summary": {},
        "channel_messages": [],
        "channel_frozen_prd": {},
        "workflow_bound_prd": {},
        "prior_judges": [],
        "allowed_evidence_refs": [],
        "forbidden_refs": [],
    }


def _context() -> tuple[dict, Path]:
    packets = {
        stage: _packet(stage)
        for stage in ("task_type", "contribution", "discussion", "prd_fidelity")
    }
    context = {
        "channel_id": "ch-1",
        "workflow_run_id": "run-1",
        "trial_id": "trial",
        "freeze": {"event_id": "freeze-1"},
        "packets": packets,
        "source_snapshot_digest": "a" * 64,
        "evaluation_contract": "channel-prd-eval.v1",
    }
    return context, Path("artifacts/benchmark-evaluations/queue-inputs") / f"{stable_digest(context)}.json"


@pytest.mark.parametrize(
    "classifier_status",
    ["abstain", "evidence_missing", "invalid", "ambiguous", "", "unknown"],
)
def test_classifier_failure_never_calls_quality_provider(
    tmp_path, monkeypatch, classifier_status,
):
    context, relative = _context()
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(context), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(observer, "read_judge_result", lambda *args, **kwargs: (
        {"status": classifier_status, "result": {
            "schema_version": "task_type_result.v1", "judge_type": "task_type",
            "status": classifier_status, "task_type": "bugfix", "confidence": 0.9,
        }} if kwargs.get("judge_type") == "task_type" else None
    ))
    monkeypatch.setattr(observer, "run_judge", lambda **kwargs: calls.append(kwargs["judge_type"]))
    monkeypatch.setattr(observer, "_refresh_report", lambda *args, **kwargs: None)
    entry = EvaluationQueueEntry(
        job_id=evaluation_request_id(
            workflow_run_id="run-1", trial_id="trial", judge_type="contribution",
            source_snapshot_digest="a" * 64,
        ),
        channel_id="ch-1", workflow_run_id="run-1", trial_id="trial",
        judge_type="contribution", source_snapshot_digest="a" * 64,
        input_ref=str(relative), input_digest=stable_digest(context),
    )
    result = observer._evaluate_stage(
        state_dir=tmp_path, project_root=None, config=None, entry=entry,
        slot_id=0, backend=None, log=lambda _: None,
    )
    assert result["status"] == "inconclusive"
    assert calls == []


@pytest.mark.parametrize(
    "classifier_status",
    ["abstain", "evidence_missing", "invalid", "ambiguous"],
)
def test_observer_dispatches_only_classifier_for_explicit_non_scored_verdict(
    tmp_path, classifier_status,
):
    """A high-confidence named type cannot make the scheduler release roles."""

    message_ids = _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    calls: list[str] = []

    def backend(**kwargs):
        calls.append(kwargs["judge_type"])
        assert kwargs["judge_type"] == "task_type"
        return json.dumps({
            "schema_version": "task_type_result.v1",
            "judge_type": "task_type",
            "status": classifier_status,
            "task_type": "bugfix",
            "confidence": 0.99,
            "evidence_refs": [f"channel/message/{message_ids[0]}"],
            "rationale": "分类器明确拒绝授权后续角色评测。",
            "missing_evidence": [],
        }), ""

    try:
        observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=backend,
        )
        assert calls and set(calls) == {"task_type"}
        entries = scheduler.entries
        assert next(
            item for item in entries.values() if item.judge_type == "task_type"
        ).status == "inconclusive"
        for stage in ("contribution", "discussion", "prd_fidelity"):
            entry = next(item for item in entries.values() if item.judge_type == stage)
            assert entry.status == "inconclusive"
            assert "dependency inconclusive" in entry.error
    finally:
        scheduler.close()


def test_scheduler_settles_quality_dependents_of_failed_classifier(tmp_path):
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    calls: list[str] = []
    try:
        scheduler.enqueue(EvaluationQueueEntry(
            job_id="classifier", channel_id="ch-1", workflow_run_id="run-1",
            judge_type="task_type",
        ))
        scheduler.enqueue(EvaluationQueueEntry(
            job_id="quality", channel_id="ch-1", workflow_run_id="run-1",
            judge_type="contribution", depends_on=["classifier"],
        ))
        scheduler.tick(lambda entry, slot: calls.append(entry.judge_type) or {
            "status": "inconclusive" if entry.judge_type == "task_type" else "completed",
        })
        assert calls == ["task_type"]
        assert scheduler.entries["quality"].status == "inconclusive"
        assert "dependency inconclusive" in scheduler.entries["quality"].error
    finally:
        scheduler.close()


def test_queue_input_digest_and_path_are_fail_closed(tmp_path):
    context, relative = _context()
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**context, "channel_id": "tampered"}), encoding="utf-8")
    entry = EvaluationQueueEntry(
        job_id=evaluation_request_id(
            workflow_run_id="run-1", trial_id="trial", judge_type="task_type",
            source_snapshot_digest="a" * 64,
        ),
        channel_id="ch-1", workflow_run_id="run-1", trial_id="trial",
        judge_type="task_type", source_snapshot_digest="a" * 64,
        input_ref=str(relative), input_digest=stable_digest(context),
    )
    result = observer._evaluate_stage(
        state_dir=tmp_path, project_root=None, config=None, entry=entry,
        slot_id=0, backend=None, log=lambda _: None,
    )
    assert result["status"] == "inconclusive"
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert not scheduler.enqueue(EvaluationQueueEntry(
            job_id="escape", channel_id="ch", workflow_run_id="run",
            judge_type="task_type", input_ref="../outside.json",
        ))
    finally:
        scheduler.close()


def test_scheduler_rejects_duplicate_job_ids_on_recovery(tmp_path):
    queue = tmp_path / "artifacts/benchmark-evaluations/scheduler.json"
    queue.parent.mkdir(parents=True)
    row = {"job_id": "same", "channel_id": "ch", "workflow_run_id": "run", "judge_type": "task_type"}
    queue.write_text(json.dumps({
        "schema_version": "benchmark.evaluation.scheduler.v1",
        "entries": [row, row],
    }), encoding="utf-8")
    try:
        EvaluationScheduler(tmp_path, inline=True)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate queue identity must fail closed")


def test_tampered_judge_sidecar_does_not_dedup(tmp_path, monkeypatch):
    packet = _packet("discussion")
    packet["allowed_evidence_refs"] = ["channel/message/x"]
    reply = {
        "schema_version": "discussion_result.v2",
        "judge_type": "discussion",
        "dimensions": [
             {"dimension_id": key, "score": 4, "confidence": 0.9,
             "evidence_refs": ["channel/message/x"], "rationale": "evidence"}
            for key in ("information_exchange", "clarity", "task_assistance",
                        "efficiency", "necessity")
        ],
        "discussion_meaningful": True,
        "discussion_progressed": True,
        "missing_evidence": [],
    }
    result = runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
        workflow_run_id="run-1", trial_id="trial", judge_type="discussion",
        packet=packet, backend=lambda **kwargs: (json.dumps(reply), ""),
    )
    path = artifacts.judge_result_path(
        tmp_path, workflow_run_id="run-1", judge_type="discussion",
        request_id=result["request_id"],
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"]["dimensions"][0]["score"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert artifacts.read_judge_result(
        tmp_path, workflow_run_id="run-1", judge_type="discussion",
        request_id=result["request_id"], expected_trial_id="trial",
        expected_source_snapshot_digest="a" * 64,
    ) is None


def test_quality_result_with_wrong_role_metadata_is_invalid(tmp_path):
    packet = _packet("discussion")
    packet.update(
        task_type="bugfix",
        role_ref="evaluation_roles/bugfix.md",
        role_digest="a" * 64,
    )
    packet["task_details"]["evaluation_metadata"] = {
        "task_type": "bugfix",
        "role_ref": "evaluation_roles/bugfix.md",
        "role_digest": "a" * 64,
        "role_version": "evaluation-role-bugfix.v1",
    }
    packet["allowed_evidence_refs"] = ["channel/message/x"]
    reply = {
        "schema_version": "discussion_result.v2",
        "judge_type": "discussion",
        "task_type": "bugfix",
        "role_id": "refactor",
        "role_version": "evaluation-role-bugfix.v1",
        "dimensions": [
            {
                "dimension_id": key,
                "score": 4,
                "confidence": 0.9,
                "evidence_refs": ["channel/message/x"],
                "rationale": "evidence",
            }
            for key in (
                "information_exchange", "clarity", "task_assistance",
                "efficiency", "necessity",
            )
        ],
        "discussion_meaningful": True,
        "discussion_progressed": True,
        "missing_evidence": [],
    }
    calls = []
    result = runner.run_judge(
        state_dir=tmp_path,
        project_root=None,
        config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
        workflow_run_id="run-1",
        trial_id="trial",
        judge_type="discussion",
        packet=packet,
        backend=lambda **kwargs: calls.append(kwargs["judge_type"]) or (json.dumps(reply), ""),
    )
    assert result["status"] == "invalid"
    assert "role_id mismatch" in result["error"]
    assert len(calls) == 2  # bounded schema repair, never an unbounded retry


@pytest.mark.parametrize("child_value", [True, False])
def test_conclusion_packet_keeps_frozen_role_ref_and_digest(tmp_path, child_value):
    role_ref = "evaluation_roles/bugfix.md"
    role_digest = "a" * 64
    judges = {
        "task_type": {
            "status": "scored",
            "result": {
                "schema_version": "task_type_result.v1",
                "judge_type": "task_type",
                "status": "scored",
                "task_type": "bugfix",
                "confidence": 0.9,
            },
        },
    }
    for stage in ("contribution", "discussion", "prd_fidelity"):
        judges[stage] = {
            "status": "scored",
            "request_id": f"request-{stage}",
            "role_ref": role_ref,
            "role_digest": role_digest,
            "result": {
                "schema_version": f"{stage}_result.v2",
                "judge_type": stage,
                "status": "scored",
                "task_type": "bugfix",
                "agent_findings": [],
                "dimensions": [],
                **({"discussion_meaningful": child_value, "discussion_progressed": child_value}
                   if stage == "discussion" else {}),
                **({"prd_ready_for_next_stage": child_value}
                   if stage == "prd_fidelity" else {}),
            },
        }
    response = {
        "schema_version": "conclusion_result.v2",
        "judge_type": "conclusion",
        "status": "complete",
        "score_scale": "0-4",
        "agent_scores": [],
        "team_findings": {
            "discussion_meaningful": not child_value,
            "discussion_progressed": not child_value,
            "prd_ready_for_next_stage": not child_value,
            "problems": [],
            "underperformed_roles": [],
            "unnecessary_roles": [],
        },
        "conclusion_zh": "完成。",
        "next_actions": [],
        "judge_refs": [],
        "evidence_refs": [],
    }
    result = runner.run_conclusion(
        state_dir=tmp_path,
        project_root=None,
        config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
        workflow_run_id="run-1",
        trial_id="trial",
        judges=judges,
        backend=lambda **kwargs: (json.dumps(response), ""),
    )
    packet = json.loads((tmp_path / result["packet_ref"]).read_text(encoding="utf-8"))
    assert packet["task_type"] == "bugfix"
    assert packet["role_ref"] == role_ref
    assert packet["role_digest"] == role_digest
    assert result["result"]["team_findings"] == {
        "discussion_meaningful": child_value,
        "discussion_progressed": child_value,
        "prd_ready_for_next_stage": child_value,
        "problems": [],
        "underperformed_roles": [],
        "unnecessary_roles": [],
    }


def test_conclusion_fails_closed_when_role_bound_field_is_missing(tmp_path):
    judges = {
        "contribution": {"status": "scored", "result": {"agent_findings": []}},
        "discussion": {"status": "scored", "result": {}},
        "prd_fidelity": {"status": "scored", "result": {}},
    }
    result = runner.run_conclusion(
        state_dir=tmp_path,
        project_root=None,
        config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
        workflow_run_id="run-1",
        trial_id="trial",
        judges=judges,
        backend=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("missing role-bound conclusion field must not start main Judge")
        ),
    )
    assert result["status"] == "incomplete"
    assert "missing role-bound conclusion field" in result["error"]
    assert result["result"]["team_findings"] == {
        "problems": [result["error"]],
        "underperformed_roles": [],
        "unnecessary_roles": [],
    }


def test_malformed_inconclusive_sidecar_exhausts_without_provider(tmp_path):
    packet = _packet("discussion")
    request_id = evaluation_request_id(
        workflow_run_id="run-1", trial_id="trial", judge_type="discussion",
        source_snapshot_digest="a" * 64,
    )
    path = artifacts.judge_result_path(
        tmp_path, workflow_run_id="run-1", judge_type="discussion", request_id=request_id,
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "request_id": request_id, "workflow_run_id": "run-1",
        "judge_type": "discussion", "trial_id": "trial",
        "source_snapshot_digest": "a" * 64, "status": "inconclusive",
        "attempt": 2, "max_attempts": 2, "result": {"tampered": True},
    }), encoding="utf-8")
    record = runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")),
        workflow_run_id="run-1", trial_id="trial", judge_type="discussion",
        packet=packet, backend=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("bounded recovery must not call provider")
        ),
    )
    assert record["status"] == "inconclusive"
    assert record["attempt"] == 2


def test_empty_scored_judge_result_cannot_be_reused(tmp_path):
    request_id = evaluation_request_id(
        workflow_run_id="run-1", trial_id="trial", judge_type="discussion",
        source_snapshot_digest="a" * 64,
    )
    path = artifacts.judge_result_path(
        tmp_path, workflow_run_id="run-1", judge_type="discussion", request_id=request_id,
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": "benchmark.evaluation.record.v1",
        "judge_type": "discussion",
        "status": "scored",
        "request_id": request_id,
        "run_id": "run-1",
        "workflow_run_id": "run-1",
        "trial_id": "trial",
        "source_snapshot_digest": "a" * 64,
        "prompt_version": EVALUATION_PROMPT_VERSION,
        "result": {},
    }), encoding="utf-8")
    assert artifacts.read_judge_result(
        tmp_path, workflow_run_id="run-1", judge_type="discussion", request_id=request_id,
        expected_trial_id="trial", expected_source_snapshot_digest="a" * 64,
    ) is None


def test_empty_complete_conclusion_result_cannot_be_reused(tmp_path):
    source_digest = "b" * 64
    request_id = evaluation_request_id(
        workflow_run_id="run-1", trial_id="trial", judge_type="conclusion",
        source_snapshot_digest=source_digest,
    )
    path = artifacts.conclusion_result_path(tmp_path, "run-1", request_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": "benchmark.evaluation.record.v1",
        "judge_type": "conclusion",
        "status": "complete",
        "request_id": request_id,
        "run_id": "run-1",
        "workflow_run_id": "run-1",
        "trial_id": "trial",
        "source_snapshot_digest": source_digest,
        "prompt_version": EVALUATION_PROMPT_VERSION,
        "result": {},
    }), encoding="utf-8")
    assert artifacts.read_conclusion_result(tmp_path, "run-1", request_id) is None
