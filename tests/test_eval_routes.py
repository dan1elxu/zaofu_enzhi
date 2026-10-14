"""Read-only Web API coverage for three-lane PRD eval runs (eval_routes.py)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from zf.core.events import EventLog, EventWriter
from zf.runtime import eval_run
from zf.runtime.evaluation_artifacts import write_report
from zf.runtime.evaluation_lane_packets import (
    EVAL_PROMPT_REF,
    assign_candidate_labels,
    blind_summary_from_record,
    build_cross_compare_packet,
    build_lane_prd_fidelity_packet,
    build_lane_task_type_packet,
)
from zf.runtime.evaluation_packets import (
    evaluation_request_id,
    write_immutable_packet,
)
from zf.runtime.evaluation_prompts import prompt_version_for_stage
from zf.runtime.evaluation_runner import run_judge
from zf.runtime.evaluation_scheduler import (
    EvaluationQueueEntry,
    EvaluationScheduler,
)
from zf.web.server import create_app
from tests.evaluation_fixtures import (
    _cross_compare_reply,
    _judge_reply,
    _lane_prd_fidelity_reply,
)

PROMPT = "做一个命令行待办事项工具，支持增删改查与到期提醒，数据本地存储。"
PROMPT_VERSION = "eval-prompt.v1"
LANES = ("terminal", "kanban", "channel")
PRD_BODIES = {
    "terminal": "# 待办工具 PRD（terminal 产出）\n\n## 范围\n增删改查。\n\n## 验收\n- 新增 -> verify: 列表可见\n",
    "kanban": "# 待办工具 PRD（kanban 产出）\n\n## 范围\n增删改查、到期提醒。\n",
    "channel": "# 待办工具 PRD（channel 产出）\n\n## 背景\nCLI 待办。\n\n## 风险\n本地存储损坏。\n",
}
LANE_SCORES = {"terminal": 4, "kanban": 3, "channel": 2}
REPORT_BODY = "# Eval Report\n\n推荐排名与差异归因见正文。\n"

FORBIDDEN_FIELDS = (
    "evidence_refs",
    "artifact_ref",
    "artifact_digest",
    "input_ref",
    "input_digest",
    "source_snapshot_digest",
    "workflow_run_id",
    "started_event_id",
    "prompt_ref",
    "run_dir",
)


def _writer(state_dir: Path) -> EventWriter:
    return EventWriter(EventLog(state_dir / "events.jsonl"))


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path, str]:
    monkeypatch.setenv("ZF_WORKSPACE_HOME", str(tmp_path / "workspace-home"))
    state_dir = tmp_path / ".zf"
    state_dir.mkdir()
    (state_dir / "kanban.json").write_text("[]", encoding="utf-8")
    (state_dir / "feature_list.json").write_text("[]", encoding="utf-8")
    (state_dir / "events.jsonl").write_text("", encoding="utf-8")
    client = TestClient(create_app(state_dir))
    project_id = client.get("/api/workspace/projects").json()["active_project_id"]
    return client, state_dir, project_id


def _start_run(state_dir: Path, *, trial: int = 1, lanes=LANES) -> eval_run.EvalRunManifest:
    return eval_run.start_eval_run(
        state_dir,
        prompt_text=PROMPT,
        prompt_version=PROMPT_VERSION,
        trial=trial,
        lanes=lanes,
        writer=_writer(state_dir),
    )


def _freeze_lane(state_dir: Path, run_id: str, lane: str) -> None:
    digest, trial = eval_run.parse_eval_run_id(run_id)
    run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
    prd_path = eval_run.lane_prd_path(run_dir, lane)
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(PRD_BODIES[lane], encoding="utf-8")
    eval_run.mark_lane_running(state_dir, run_id, lane)
    eval_run.complete_lane(
        state_dir, run_id, lane,
        status=eval_run.LANE_STATUS_PRD_FROZEN,
        writer=_writer(state_dir),
    )


def _run_dir(state_dir: Path, manifest: eval_run.EvalRunManifest) -> Path:
    return eval_run.eval_run_dir(
        state_dir, prompt_digest=manifest.prompt_digest, trial=manifest.trial,
    )


def _drive_trial_evaluation(
    state_dir: Path, manifest: eval_run.EvalRunManifest,
) -> dict[str, str]:
    """Run the full judge chain inside the trial dir with fake backends.

    Returns the expected candidate label -> lane mapping.
    """
    run_dir = _run_dir(state_dir, manifest)
    writer = EventWriter(EventLog(run_dir / "events.jsonl"))
    trial_id = f"trial-{manifest.trial}"
    lanes = list(manifest.lanes)
    lane_wf = {
        lane: eval_run.lane_eval_run_id(
            prompt_digest=manifest.prompt_digest, trial=manifest.trial, lane=lane,
        )
        for lane in lanes
    }
    labels = assign_candidate_labels(lanes, seed=manifest.run_id)
    scheduler = EvaluationScheduler(run_dir, inline=True)
    packets: dict[str, object] = {}
    backends: dict[str, object] = {}
    records: dict[str, dict] = {}

    def add(judge_type, packet, workflow_run_id, channel_id, depends_on, backend) -> str:
        input_path, input_digest = write_immutable_packet(run_dir, packet)
        entry = EvaluationQueueEntry(
            job_id=evaluation_request_id(
                workflow_run_id=workflow_run_id,
                trial_id=trial_id,
                judge_type=judge_type,
                source_snapshot_digest=str(packet.get("source_snapshot_digest") or ""),
                prompt_version=prompt_version_for_stage(judge_type),
            ),
            channel_id=channel_id,
            workflow_run_id=workflow_run_id,
            trial_id=trial_id,
            judge_type=judge_type,
            source_snapshot_digest=str(packet.get("source_snapshot_digest") or ""),
            input_ref=input_path.relative_to(run_dir).as_posix(),
            input_digest=input_digest,
            depends_on=list(depends_on),
        )
        assert scheduler.enqueue(entry)
        packets[entry.job_id] = packet
        backends[entry.job_id] = backend
        return entry.job_id

    def runner(entry: EvaluationQueueEntry, slot: int) -> dict:
        record = run_judge(
            state_dir=run_dir, project_root=None, config=None, writer=writer,
            workflow_run_id=entry.workflow_run_id, trial_id=entry.trial_id,
            judge_type=entry.judge_type, packet=packets[entry.job_id],
            backend=backends[entry.job_id], slot_id=slot,
        )
        records[entry.job_id] = record
        return record

    try:
        task_type_job = add(
            "task_type",
            build_lane_task_type_packet(
                run_id=manifest.run_id, workflow_run_id=manifest.run_id,
                trial_id=trial_id, prompt_text=PROMPT,
            ),
            manifest.run_id, "task-type", [],
            lambda **kwargs: (_judge_reply("task_type", EVAL_PROMPT_REF), ""),
        )
        lane_jobs: dict[str, str] = {}
        for lane in lanes:
            score = LANE_SCORES[lane]
            lane_jobs[lane] = add(
                "lane_prd_fidelity",
                build_lane_prd_fidelity_packet(
                    run_id=manifest.run_id, workflow_run_id=lane_wf[lane],
                    trial_id=trial_id, prompt_text=PROMPT,
                    prd_body=PRD_BODIES[lane],
                    task_type="bugfix", task_type_confidence=0.9,
                ),
                lane_wf[lane], f"lane-{lane}", [task_type_job],
                lambda _score=score, **kwargs: (_lane_prd_fidelity_reply(score=_score), ""),
            )
        scheduler.tick(runner)

        lane_by_label = {label: lane for lane, label in labels.items()}
        candidates = []
        for label in ("A", "B", "C"):
            lane = lane_by_label[label]
            candidates.append({
                "label": label,
                "prd_body": PRD_BODIES[lane],
                "prd_digest": hashlib.sha256(PRD_BODIES[lane].encode("utf-8")).hexdigest(),
                "blind": blind_summary_from_record(records[lane_jobs[lane]]),
            })
        ranking = tuple(sorted(
            ("A", "B", "C"),
            key=lambda label: -next(
                item["blind"]["mean"] for item in candidates if item["label"] == label
            ),
        ))
        add(
            "cross_compare",
            build_cross_compare_packet(
                run_id=manifest.run_id, workflow_run_id=manifest.run_id,
                trial_id=trial_id, prompt_text=PROMPT, candidates=candidates,
                task_type="bugfix",
            ),
            manifest.run_id, "cross-compare", list(lane_jobs.values()),
            lambda **kwargs: (_cross_compare_reply(ranking=ranking), ""),
        )
        scheduler.tick(runner)
    finally:
        scheduler.close()
    write_report(run_dir, manifest.run_id, REPORT_BODY, final=False)
    return {label: lane for lane, label in labels.items()}


def _completed_run(state_dir: Path) -> tuple[eval_run.EvalRunManifest, dict[str, str]]:
    manifest = _start_run(state_dir)
    for lane in LANES:
        _freeze_lane(state_dir, manifest.run_id, lane)
    label_map = _drive_trial_evaluation(state_dir, manifest)
    eval_run.complete_run(state_dir, manifest.run_id, writer=_writer(state_dir))
    return manifest, label_map


def _expected_ranking(label_map: dict[str, str]) -> list[str]:
    return sorted(label_map, key=lambda label: -LANE_SCORES[label_map[label]])


# --------------------------------------------------------------------- index


def test_eval_runs_index_empty_when_no_runs(tmp_path: Path, monkeypatch) -> None:
    client, _state_dir, project_id = _client(tmp_path, monkeypatch)
    response = client.get(f"/api/projects/{project_id}/eval/runs")
    assert response.status_code == 200
    assert response.json() == {"schema_version": "eval.runs.v1", "groups": []}


def test_eval_runs_index_groups_trials_and_scores(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client(tmp_path, monkeypatch)
    manifest, label_map = _completed_run(state_dir)
    running = _start_run(state_dir, trial=2)
    before = (state_dir / "events.jsonl").read_bytes()

    response = client.get(f"/api/projects/{project_id}/eval/runs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "eval.runs.v1"
    assert [group["prompt_digest"] for group in payload["groups"]] == [manifest.prompt_digest]
    group = payload["groups"][0]
    assert group["prompt_version"] == PROMPT_VERSION
    assert [trial["run_id"] for trial in group["trials"]] == [manifest.run_id, running.run_id]

    done = group["trials"][0]
    assert done["trial"] == 1
    assert done["status"] == "completed"
    assert done["started_at"] and done["finished_at"]
    assert {lane: row["status"] for lane, row in done["lanes"].items()} == {
        lane: "prd_frozen" for lane in LANES
    }
    assert all(row["error"] == "" for row in done["lanes"].values())
    assert done["composite_scores"] == {
        lane: float(LANE_SCORES[lane]) for lane in LANES
    }
    assert done["recommendation"] == _expected_ranking(label_map)

    pending = group["trials"][1]
    assert pending["status"] == "running"
    assert pending["finished_at"] is None
    assert all(row["status"] == "pending" for row in pending["lanes"].values())
    assert pending["composite_scores"] is None
    assert pending["recommendation"] is None

    encoded = json.dumps(payload, ensure_ascii=False)
    for forbidden in FORBIDDEN_FIELDS:
        assert forbidden not in encoded
    assert str(tmp_path) not in response.text
    assert (state_dir / "events.jsonl").read_bytes() == before


# -------------------------------------------------------------------- detail


def test_eval_run_detail_completed_run(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client(tmp_path, monkeypatch)
    manifest, label_map = _completed_run(state_dir)
    before = (state_dir / "events.jsonl").read_bytes()

    response = client.get(f"/api/projects/{project_id}/eval/runs/{manifest.run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "eval.run.v1"
    assert payload["run_id"] == manifest.run_id
    assert payload["prompt_digest"] == manifest.prompt_digest
    assert payload["prompt_version"] == PROMPT_VERSION
    assert payload["prompt_text"] == PROMPT
    assert payload["trial"] == 1
    assert payload["status"] == "completed"

    for lane in LANES:
        row = payload["lanes"][lane]
        assert row["status"] == "prd_frozen"
        assert row["error"] == ""
        assert row["started_at"] and row["finished_at"]
        assert row["prd_markdown"] == PRD_BODIES[lane]

    evaluation = payload["evaluation"]
    assert evaluation["task_type"] == {
        "status": "scored", "task_type": "bugfix", "confidence": 0.9,
    }
    for lane in LANES:
        score = evaluation["lane_scores"][lane]
        assert score["status"] == "scored"
        assert score["dimensions"] == {
            dimension: LANE_SCORES[lane]
            for dimension in ("coverage", "fidelity", "actionability", "structure")
        }
        assert score["composite"] == float(LANE_SCORES[lane])
    compare = evaluation["cross_compare"]
    assert compare["status"] == "scored"
    assert compare["recommendation"] == _expected_ranking(label_map)
    assert len(compare["dimension_rankings"]) == 4
    assert all(
        item["ranking"] == compare["recommendation"]
        for item in compare["dimension_rankings"]
    )
    assert len(compare["key_gaps"]) == 2
    assert compare["conflicts"] == []

    assert payload["label_map"] == label_map
    assert payload["report_markdown"] == REPORT_BODY

    encoded = json.dumps(payload, ensure_ascii=False)
    for forbidden in FORBIDDEN_FIELDS:
        assert forbidden not in encoded
    assert str(tmp_path) not in response.text
    assert (state_dir / "events.jsonl").read_bytes() == before


def test_eval_run_detail_running_run_is_bounded(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client(tmp_path, monkeypatch)
    manifest = _start_run(state_dir)
    eval_run.mark_lane_running(state_dir, manifest.run_id, "terminal")

    payload = client.get(f"/api/projects/{project_id}/eval/runs/{manifest.run_id}").json()

    assert payload["status"] == "running"
    assert payload["prompt_text"] == PROMPT
    assert payload["lanes"]["terminal"]["status"] == "running"
    assert payload["lanes"]["terminal"]["prd_markdown"] is None
    assert payload["lanes"]["kanban"] == {
        "status": "pending", "error": "", "started_at": "",
        "finished_at": "", "prd_markdown": None,
    }
    assert payload["evaluation"] == {
        "task_type": None,
        "lane_scores": {lane: None for lane in LANES},
        "cross_compare": None,
    }
    assert payload["label_map"] == {}
    assert payload["report_markdown"] is None


def test_eval_run_detail_marks_lanes_outside_the_run_absent(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client(tmp_path, monkeypatch)
    manifest = _start_run(state_dir, lanes=("terminal", "kanban"))

    payload = client.get(f"/api/projects/{project_id}/eval/runs/{manifest.run_id}").json()

    assert payload["lanes"]["channel"]["status"] == "absent"
    assert payload["lanes"]["channel"]["prd_markdown"] is None
    assert payload["evaluation"]["lane_scores"]["channel"] is None


@pytest.mark.parametrize("run_id", ["not-a-run", f"{'0' * 64}-trial-9", f"{'0' * 64}-trial-0"])
def test_eval_run_detail_unknown_run_id_is_404(tmp_path: Path, monkeypatch, run_id: str) -> None:
    client, _state_dir, project_id = _client(tmp_path, monkeypatch)
    response = client.get(f"/api/projects/{project_id}/eval/runs/{run_id}")
    assert response.status_code == 404


def test_eval_routes_reject_unknown_project(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, _project_id = _client(tmp_path, monkeypatch)
    manifest, _label_map = _completed_run(state_dir)
    assert client.get("/api/projects/project-does-not-exist/eval/runs").status_code in {404, 400}
    response = client.get(f"/api/projects/project-does-not-exist/eval/runs/{manifest.run_id}")
    assert response.status_code in {404, 400}


# ------------------------------------------------------------------- guards


def test_eval_run_detail_hides_tampered_lane_prd(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client(tmp_path, monkeypatch)
    manifest, _label_map = _completed_run(state_dir)
    prd_path = eval_run.lane_prd_path(_run_dir(state_dir, manifest), "kanban")
    prd_path.write_text("# 篡改后的 PRD\n", encoding="utf-8")

    payload = client.get(f"/api/projects/{project_id}/eval/runs/{manifest.run_id}").json()

    assert payload["lanes"]["kanban"]["prd_markdown"] is None
    assert payload["lanes"]["terminal"]["prd_markdown"] == PRD_BODIES["terminal"]


def test_eval_run_detail_rejects_symlinked_lane_prd(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client(tmp_path, monkeypatch)
    manifest, _label_map = _completed_run(state_dir)
    prd_path = eval_run.lane_prd_path(_run_dir(state_dir, manifest), "kanban")
    outside = tmp_path / "outside.md"
    outside.write_text(PRD_BODIES["kanban"], encoding="utf-8")
    prd_path.unlink()
    prd_path.symlink_to(outside)

    payload = client.get(f"/api/projects/{project_id}/eval/runs/{manifest.run_id}").json()

    assert payload["lanes"]["kanban"]["prd_markdown"] is None


def test_eval_run_detail_redacts_paths_and_secrets(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client(tmp_path, monkeypatch)
    manifest = _start_run(state_dir)
    dirty = (
        "# PRD\n\n配置位于 /home/operator/private/config.toml，"
        "凭据 API_KEY=eval-routes-test-key 见部署手册。\n"
    )
    digest, trial = eval_run.parse_eval_run_id(manifest.run_id)
    run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
    prd_path = eval_run.lane_prd_path(run_dir, "terminal")
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(dirty, encoding="utf-8")
    eval_run.mark_lane_running(state_dir, manifest.run_id, "terminal")
    eval_run.complete_lane(
        state_dir, manifest.run_id, "terminal",
        status=eval_run.LANE_STATUS_PRD_FROZEN, writer=_writer(state_dir),
    )

    response = client.get(f"/api/projects/{project_id}/eval/runs/{manifest.run_id}")

    assert response.status_code == 200
    assert response.json()["lanes"]["terminal"]["prd_markdown"]
    assert "/home/operator/" not in response.text
    assert "eval-routes-test-key" not in response.text
    assert "[运行路径]" in response.text
    assert "[REDACTED_SECRET]" in response.text


def test_eval_run_detail_hides_tampered_prompt_packet(tmp_path: Path, monkeypatch) -> None:
    client, state_dir, project_id = _client(tmp_path, monkeypatch)
    manifest, _label_map = _completed_run(state_dir)
    packet_path = eval_run.prompt_packet_path(state_dir, prompt_digest=manifest.prompt_digest)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["prompt_text"] = "tampered prompt"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")

    payload = client.get(f"/api/projects/{project_id}/eval/runs/{manifest.run_id}").json()

    assert payload["prompt_text"] == ""
