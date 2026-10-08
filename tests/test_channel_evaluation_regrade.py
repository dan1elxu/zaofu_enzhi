"""The Eval page selects a verified replay without changing canonical state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_projection import project_channel
from zf.runtime.evaluation_regrade import regrade_once
from zf.web.channel_evaluation_route import build_channel_evaluation
from tests.test_channel_evaluation_route import _backend, _client_with_evaluation
from tests.evaluation_fixtures import _workflow_binding_payload


def _regrade(state: Path, rid: str, *, backend=_backend):
    entries = json.loads((state / "artifacts/benchmark-evaluations/scheduler.json").read_text())["entries"]
    source = next(row["input_ref"] for row in reversed(entries) if row["judge_type"] == "prd_fidelity")
    return regrade_once(state_dir=state, input_ref=source, regrade_id=rid, backend=backend)


def test_latest_completed_regrade_replaces_web_report_only(tmp_path, monkeypatch):
    client, state, project = _client_with_evaluation(tmp_path, monkeypatch)
    url = f"/api/projects/{project}/channels/ch-1/evaluation"
    before = client.get(url).json()
    canonical = project_channel(state, "ch-1")["evaluation"]
    root = state / "artifacts/benchmark-evaluations"
    original = root / "reports" / before["workflow_run_id"] / "report.md"
    original_bytes = original.read_bytes()
    queue_bytes = (root / "scheduler.json").read_bytes()

    def checking_backend(**kwargs):
        # Progress and partial sidecars cannot hide the prior report.
        assert client.get(url).json()["workflow_run_id"] == before["workflow_run_id"]
        return _backend(**kwargs)

    first = _regrade(state, "z-first", backend=checking_backend)
    payload = client.get(url).json()
    assert payload["workflow_run_id"] == first["workflow_run_id"]
    assert payload["evaluation_origin"] == "manual_regrade"
    assert payload["report"]["available"]
    assert first["workflow_run_id"] in payload["report"]["markdown"]
    assert all(row["result_available"] for row in payload["stages"])
    assert project_channel(state, "ch-1")["evaluation"] == canonical
    assert original.read_bytes() == original_bytes
    assert (root / "scheduler.json").read_bytes() == queue_bytes

    # Publication follows the verified terminal result and full report; final
    # audit bookkeeping can lag the event that refreshes an open browser.
    audit_path = state / first["audit_ref"]
    audit = json.loads(audit_path.read_text())
    audit["status"] = "running"
    audit_path.write_text(json.dumps(audit))
    assert client.get(url).json()["workflow_run_id"] == first["workflow_run_id"]

    second = _regrade(state, "a-second")
    assert client.get(url).json()["workflow_run_id"] == second["workflow_run_id"]
    # File order/mtime and delayed progress are not completion order.
    (state / first["report_ref"]).touch()
    for event_type in ("benchmark.evaluation.progress", "benchmark.evaluation.conclusion.completed"):
        event = next(e for e in EventLog(state / "events.jsonl").read_all()
                     if e.type == event_type and e.payload.get("workflow_run_id") == first["workflow_run_id"])
        EventWriter(EventLog(state / "events.jsonl")).emit(event.type, actor="test", payload=event.payload)
    assert client.get(url).json()["workflow_run_id"] == second["workflow_run_id"]
    (state / second["report_ref"]).write_text("corrupted report")
    assert client.get(url).json()["workflow_run_id"] == first["workflow_run_id"]


@pytest.mark.parametrize("damage", ["audit", "input", "report", "symlink", "result"])
def test_unverified_regrade_does_not_replace_original(tmp_path, monkeypatch, damage):
    client, state, project = _client_with_evaluation(tmp_path, monkeypatch)
    url = f"/api/projects/{project}/channels/ch-1/evaluation"
    before = client.get(url).json()
    replay = _regrade(state, "damaged")
    audit_path = state / replay["audit_ref"]
    audit = json.loads(audit_path.read_text())
    if damage == "audit":
        audit["freeze_event_id"] = "other-freeze"
        audit_path.write_text(json.dumps(audit))
    elif damage == "input":
        (state / audit["regrade_context_ref"]).write_text("{}")
    elif damage == "result":
        path = next((state / "artifacts/benchmark-evaluations/judges" / replay["workflow_run_id"] / "prd_fidelity").glob("evalreq-*.json"))
        path.write_text("{}")
    elif damage == "symlink":
        outside = tmp_path / "outside-audit.json"
        outside.write_bytes(audit_path.read_bytes())
        audit_path.unlink()
        audit_path.symlink_to(outside)
    else:
        (state / replay["report_ref"]).unlink()
    assert client.get(url).json()["workflow_run_id"] == before["workflow_run_id"]


def test_old_binding_regrade_cannot_replace_new_fidelity_generation(tmp_path, monkeypatch):
    from zf.runtime import evaluation_observer as observer
    from zf.runtime.evaluation_scheduler import EvaluationScheduler

    _client, state, _project = _client_with_evaluation(tmp_path, monkeypatch)
    replay = _regrade(state, "before-binding")
    assert build_channel_evaluation(state, "ch-1")["workflow_run_id"] == replay["workflow_run_id"]
    writer = EventWriter(EventLog(state / "events.jsonl"))
    request = writer.emit("workflow.invoke.requested", actor="test", payload=_workflow_binding_payload(state))
    writer.emit("workflow.invoke.accepted", actor="kernel", payload={"source_event_id": request.id})
    scheduler = EvaluationScheduler(state, inline=True)
    try:
        observer.observe_once(state_dir=state, scheduler=scheduler, dispatch=False)
    finally:
        scheduler.close()
    payload = build_channel_evaluation(state, "ch-1")
    assert payload["workflow_run_id"] != replay["workflow_run_id"]
    assert not payload["report"]["available"]


def test_valid_missing_evidence_regrade_is_displayed(tmp_path, monkeypatch):
    client, state, project = _client_with_evaluation(tmp_path, monkeypatch)

    def missing_backend(**kwargs):
        raw, error = _backend(**kwargs)
        if kwargs["judge_type"] == "prd_fidelity":
            body = json.loads(raw)
            body["missing_evidence"] = ["冻结需求比较材料不足"]
            body["dimensions"][0].update(status="evidence_missing", score=None, verdict="evidence_missing")
            raw = json.dumps(body)
        return raw, error

    replay = _regrade(state, "missing-evidence", backend=missing_backend)
    payload = client.get(f"/api/projects/{project}/channels/ch-1/evaluation").json()
    assert payload["workflow_run_id"] == replay["workflow_run_id"]
    assert payload["report"]["available"]
    assert payload["scores"]["prd_fidelity"]["missing_evidence"] == ["冻结需求比较材料不足"]


def test_failed_provider_regrade_keeps_previous_report(tmp_path, monkeypatch):
    from zf.runtime.evaluation_turns import JudgeTurnObservation

    client, state, project = _client_with_evaluation(tmp_path, monkeypatch)
    url = f"/api/projects/{project}/channels/ch-1/evaluation"
    before = client.get(url).json()
    _regrade(state, "provider-failed", backend=lambda **kwargs: JudgeTurnObservation(
        status="provider_error", error="provider unavailable",
    ))
    after = client.get(url).json()
    assert after["workflow_run_id"] == before["workflow_run_id"]
    assert after["report"] == before["report"]
