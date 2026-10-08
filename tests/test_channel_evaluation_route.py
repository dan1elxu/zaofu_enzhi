"""Read-only API coverage for the current Channel evaluation generation."""
from __future__ import annotations

from pathlib import Path
import re
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from zf.runtime.channel_projection import project_channel
from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer
from zf.runtime.evaluation_scheduler import EvaluationScheduler
from zf.runtime.evaluation_runner import conclusion_result_path, judge_result_path
from zf.web.server import create_app
from tests.evaluation_fixtures import _judge_reply, _seed_channel_state, _workflow_binding_payload


def _backend(**kwargs):
    packet = Path(kwargs["packet_path"]).read_text(encoding="utf-8")
    match = re.search(r"channel/message/[A-Za-z0-9._:-]+", packet)
    ref = match.group(0) if match else "channel/message/evt-1"
    return _judge_reply(
        kwargs["judge_type"],
        ref,
        with_bound="artifact/prd/workflow-bound.json" in packet,
    ), ""


def _client_with_evaluation(
    tmp_path: Path, monkeypatch, backend=_backend, *, expected_errors: list[str] | None = None,
    with_workflow_bind: bool = False,
) -> tuple[TestClient, Path, str]:
    monkeypatch.setenv("ZF_WORKSPACE_HOME", str(tmp_path / "workspace-home"))
    state_dir = tmp_path / ".zf"
    state_dir.mkdir()
    (state_dir / "kanban.json").write_text("[]", encoding="utf-8")
    (state_dir / "feature_list.json").write_text("[]", encoding="utf-8")
    _seed_channel_state(state_dir, with_workflow_bind=with_workflow_bind)
    scheduler = EvaluationScheduler(state_dir, inline=True)
    try:
        result = observer.observe_once(state_dir=state_dir, scheduler=scheduler, backend=backend)
        assert result["errors"] == (expected_errors or [])
    finally:
        scheduler.close()
    client = TestClient(create_app(state_dir))
    project_id = client.get("/api/workspace/projects").json()["active_project_id"]
    return client, state_dir, project_id


def test_channel_evaluation_endpoint_returns_only_current_sanitized_results(tmp_path: Path, monkeypatch) -> None:
    client, _state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch)

    response = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "channel.evaluation.v1"
    assert payload["channel_id"] == "ch-1"
    assert payload["workflow_run_id"]
    assert payload["status"] == "complete"
    assert [item["judge_type"] for item in payload["stages"]] == [
        "task_type", "contribution", "discussion", "prd_fidelity", "conclusion",
    ]
    assert all(item["result_available"] for item in payload["stages"])
    assert payload["scores"]["discussion"]["dimensions"]
    assert payload["scores"]["prd_fidelity"]["dimensions"]
    assert payload["scores"]["contribution"]["agents"]
    assert payload["conclusion"]["conclusion_zh"]
    assert payload["report"]["available"] is True
    encoded = str(payload)
    for forbidden in ("evidence_refs", "artifact_ref", "prompt_version", "audit", "transcript"):
        assert forbidden not in encoded


def test_channel_evaluation_rejects_symlinked_sidecar(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch)
    first = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    projected = project_channel(state_dir, "ch-1")["evaluation"]
    discussion = next(item for item in projected["stages"] if item["judge_type"] == "discussion")
    sidecar = judge_result_path(
        state_dir,
        workflow_run_id=first["workflow_run_id"],
        judge_type="discussion",
        request_id=discussion["request_id"],
    )
    outside = tmp_path / "outside.json"
    outside.write_text(sidecar.read_text(encoding="utf-8"), encoding="utf-8")
    sidecar.unlink()
    sidecar.symlink_to(outside)
    payload = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    discussion = next(item for item in payload["stages"] if item["judge_type"] == "discussion")
    assert discussion["result_available"] is False
    assert payload["report"]["available"] is False


@pytest.mark.parametrize("stage", ["contribution", "discussion", "prd_fidelity"])
def test_missing_evidence_is_visible_redacted_and_preserves_partial_scores(
    tmp_path: Path, monkeypatch, stage: str,
) -> None:
    missing = "缺少判断该项所需的已确认需求澄清原文。"

    def backend(**kwargs):
        raw, model = _backend(**kwargs)
        reply = json.loads(raw)
        if kwargs["judge_type"] == stage:
            reply["missing_evidence"] = [
                missing, "补充 /home/operator/private/evidence.json API_KEY=missing-evidence-test-key",
            ]
            scores = reply["agent_findings" if stage == "contribution" else "dimensions"]
            scores[0].update(score=None, status="evidence_missing", rationale=missing)
        return json.dumps(reply, ensure_ascii=False), model

    client, state_dir, project_id = _client_with_evaluation(
        tmp_path, monkeypatch, backend, expected_errors=[f"{stage}: evidence_missing"],
        with_workflow_bind=True,
    )
    before = (state_dir / "events.jsonl").read_bytes()

    response = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation")

    assert response.status_code == 200
    payload = response.json()
    item = next(item for item in payload["stages"] if item["judge_type"] == stage)
    assert item["status"] == "evidence_missing"
    assert item["result_available"] is True
    assert item["result"]["missing_evidence"][0] == missing
    assert item["result"]["missing_evidence"] == payload["scores"][stage]["missing_evidence"]
    scores = item["result"]["agents" if stage == "contribution" else "dimensions"]
    assert scores[0]["score"] is None and scores[0]["status"] == "evidence_missing"
    assert all(score["score"] == 3 and score["status"] == "scored" for score in scores[1:])
    assert payload["status"] == "incomplete"
    assert "/home/operator/" not in response.text
    assert "missing-evidence-test-key" not in response.text
    assert "[REDACTED_SECRET]" in response.text
    assert (state_dir / "events.jsonl").read_bytes() == before


def test_channel_evaluation_rejects_same_header_report_tamper(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch)
    first = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    report_path = state_dir / "artifacts/benchmark-evaluations/reports" / first["workflow_run_id"] / "report.md"
    body = report_path.read_text(encoding="utf-8")
    report_path.write_text(body.replace("## Scores", "## Scores\n\nTAMPERED"), encoding="utf-8")
    payload = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    assert payload["report"]["available"] is False


def test_channel_evaluation_endpoint_marks_missing_or_unbound_sidecar_unavailable(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch)
    first = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    projected = project_channel(state_dir, "ch-1")["evaluation"]
    discussion = next(item for item in projected["stages"] if item["judge_type"] == "discussion")
    path = judge_result_path(
        state_dir,
        workflow_run_id=first["workflow_run_id"],
        judge_type="discussion",
        request_id=discussion["request_id"],
    )
    path.unlink()

    payload = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()

    discussion = next(item for item in payload["stages"] if item["judge_type"] == "discussion")
    assert discussion["status"] == "scored"
    assert discussion["result_available"] is False
    assert "result" not in discussion
    assert payload["status"] == "incomplete"


def test_channel_evaluation_rejects_tampered_judge_and_missing_conclusion(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch)
    first = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    projected = project_channel(state_dir, "ch-1")["evaluation"]
    contribution = next(item for item in projected["stages"] if item["judge_type"] == "contribution")
    judge_path = judge_result_path(
        state_dir, workflow_run_id=first["workflow_run_id"],
        judge_type="contribution", request_id=contribution["request_id"],
    )
    judge = json.loads(judge_path.read_text(encoding="utf-8"))
    judge["result"]["status"] = "invalid"
    judge_path.write_text(json.dumps(judge), encoding="utf-8")
    payload = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    assert next(item for item in payload["stages"] if item["judge_type"] == "contribution")["result_available"] is False

    conclusion = next(item for item in projected["stages"] if item["judge_type"] == "conclusion")
    conclusion_path = conclusion_result_path(state_dir, first["workflow_run_id"], conclusion["request_id"])
    conclusion_path.unlink()
    payload = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    assert payload["conclusion"] == {}


def test_conclusion_sidecar_without_freeze_field_is_bound_by_completion_event(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch)
    first = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    projected = project_channel(state_dir, "ch-1")["evaluation"]
    conclusion = next(item for item in projected["stages"] if item["judge_type"] == "conclusion")
    path = conclusion_result_path(state_dir, first["workflow_run_id"], conclusion["request_id"])
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body.get("freeze_event_id") in (None, "")
    assert next(item for item in first["stages"] if item["judge_type"] == "conclusion")["result_available"] is True


def test_new_workflow_bind_does_not_reuse_old_report(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch)
    before = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    requested = writer.emit("workflow.invoke.requested", actor="test", payload=_workflow_binding_payload(state_dir))
    writer.emit("workflow.invoke.accepted", actor="kernel", payload={"source_event_id": requested.id})
    scheduler = EvaluationScheduler(state_dir, inline=True)
    try:
        result = observer.observe_once(state_dir=state_dir, scheduler=scheduler, dispatch=False)
        assert result["errors"] == []
    finally:
        scheduler.close()
    after = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    assert after["workflow_run_id"] == before["workflow_run_id"]
    fidelity = next(item for item in after["stages"] if item["judge_type"] == "prd_fidelity")
    assert fidelity["status"] == "queued"
    assert after["report"]["available"] is False
    assert after["conclusion"] == {}


def test_frozen_roster_name_is_stable_after_live_member_rename(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch)
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    writer.emit(
        "channel.member.added",
        actor="operator",
        payload={
            "channel_id": "ch-1", "member_id": "m1", "member_type": "provider_agent",
            "backend": "claude-code", "name": "Renamed live agent",
        },
    )
    payload = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation").json()
    assert payload["report"]["available"] is True
    assert "Renamed live agent" not in payload["report"]["markdown"]
    assert "Agent m1" in payload["report"]["markdown"]
    assert payload["scores"]["contribution"]["agents"][0]["agent_name"] == "Agent m1"


def test_channel_evaluation_redacts_echoed_runtime_paths_and_secrets(tmp_path: Path, monkeypatch) -> None:
    private_text = " /home/operator/private/packet.json API_KEY=placeholder-test-key"

    def backend(**kwargs):
        raw, model = _backend(**kwargs)
        reply = json.loads(raw)
        for item in reply.get("dimensions", []) + reply.get("agent_findings", []):
            item["rationale"] += private_text
        if kwargs["judge_type"] == "conclusion":
            reply["conclusion_zh"] += private_text
        return json.dumps(reply, ensure_ascii=False), model

    client, state_dir, project_id = _client_with_evaluation(tmp_path, monkeypatch, backend)
    before = (state_dir / "events.jsonl").read_bytes()
    response = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation")
    assert response.status_code == 200
    assert response.json()["report"]["available"] is True
    assert "/home/operator/" not in response.text
    assert "placeholder-test-key" not in response.text
    assert "[REDACTED_SECRET]" in response.text
    assert (state_dir / "events.jsonl").read_bytes() == before


def test_channel_evaluation_endpoint_for_channel_without_evaluation_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZF_WORKSPACE_HOME", str(tmp_path / "workspace-home"))
    state_dir = tmp_path / ".zf"
    state_dir.mkdir()
    (state_dir / "kanban.json").write_text("[]", encoding="utf-8")
    (state_dir / "feature_list.json").write_text("[]", encoding="utf-8")
    _seed_channel_state(state_dir, freeze=False)
    client = TestClient(create_app(state_dir))
    project_id = client.get("/api/workspace/projects").json()["active_project_id"]

    response = client.get(f"/api/projects/{project_id}/channels/ch-1/evaluation")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert payload["workflow_run_id"] == ""
    assert len(payload["stages"]) == 5
    assert payload["report"] == {"available": False}


def test_channel_evaluation_project_scope_rejects_unknown_project(tmp_path: Path, monkeypatch) -> None:
    client, _state_dir, _project_id = _client_with_evaluation(tmp_path, monkeypatch)
    response = client.get("/api/projects/project-does-not-exist/channels/ch-1/evaluation")
    assert response.status_code in {404, 400}
