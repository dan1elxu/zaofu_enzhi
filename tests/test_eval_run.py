"""Tests for the three-lane PRD eval run state model (eval_run.py)."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import eval_run
from zf.runtime.eval_run import (
    EvalRunManifest,
    LaneState,
)

PROMPT = "为一个本地 Markdown 笔记应用写 PRD,覆盖标签与全文搜索。"
PROMPT_VERSION = "eval-prompt.v1"


def _writer(state_dir: Path) -> EventWriter:
    return EventWriter(EventLog(state_dir / "events.jsonl"))


def _events(state_dir: Path) -> list:
    return EventLog(state_dir / "events.jsonl").read_all()


def _start(state_dir: Path, *, trial: int = 1, prompt: str = PROMPT,
           lanes=("terminal", "kanban", "channel")) -> EvalRunManifest:
    return eval_run.start_eval_run(
        state_dir,
        prompt_text=prompt,
        prompt_version=PROMPT_VERSION,
        trial=trial,
        lanes=lanes,
        writer=_writer(state_dir),
    )


def _freeze_lane_prd(state_dir: Path, run_id: str, lane: str,
                     body: str = "# PRD\n\nDo X.\n") -> None:
    digest, trial = eval_run.parse_eval_run_id(run_id)
    run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
    prd_path = eval_run.lane_prd_path(run_dir, lane)
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(body, encoding="utf-8")
    eval_run.mark_lane_running(state_dir, run_id, lane)
    eval_run.complete_lane(
        state_dir, run_id, lane,
        status=eval_run.LANE_STATUS_PRD_FROZEN,
        writer=_writer(state_dir),
    )


# --------------------------------------------------------------------- ids


def test_prompt_digest_is_stable_and_version_bound() -> None:
    digest = eval_run.prompt_digest(PROMPT, prompt_version=PROMPT_VERSION)
    assert digest == eval_run.prompt_digest(PROMPT, prompt_version=PROMPT_VERSION)
    assert len(digest) == 64
    assert digest != eval_run.prompt_digest(PROMPT, prompt_version="eval-prompt.v2")
    assert digest != eval_run.prompt_digest("别的提示词", prompt_version=PROMPT_VERSION)


@pytest.mark.parametrize("text,version", [("", PROMPT_VERSION), ("  ", PROMPT_VERSION),
                                          (PROMPT, ""), (PROMPT, " ")])
def test_prompt_digest_rejects_empty_identity(text: str, version: str) -> None:
    with pytest.raises(ValueError):
        eval_run.prompt_digest(text, prompt_version=version)


def test_run_id_round_trips_directory_identity() -> None:
    digest = eval_run.prompt_digest(PROMPT, prompt_version=PROMPT_VERSION)
    run_id = eval_run.eval_run_id(prompt_digest=digest, trial=3)
    assert run_id == f"{digest}-trial-3"
    assert eval_run.parse_eval_run_id(run_id) == (digest, 3)


@pytest.mark.parametrize("run_id", ["", "no-trial", f"{'d' * 64}-trial-0",
                                    f"{'d' * 64}-trial-x", "-trial-1"])
def test_parse_run_id_rejects_malformed(run_id: str) -> None:
    with pytest.raises(ValueError):
        eval_run.parse_eval_run_id(run_id)


@pytest.mark.parametrize("trial", [0, -1, "x"])
def test_run_id_rejects_bad_trial(trial) -> None:
    with pytest.raises(ValueError):
        eval_run.eval_run_id(prompt_digest="d" * 64, trial=trial)


def test_lane_eval_run_id_is_deterministic_unique_and_path_safe() -> None:
    digest = eval_run.prompt_digest(PROMPT, prompt_version=PROMPT_VERSION)
    first = eval_run.lane_eval_run_id(prompt_digest=digest, trial=1, lane="terminal")
    assert first == eval_run.lane_eval_run_id(
        prompt_digest=digest, trial=1, lane="terminal")
    others = {
        eval_run.lane_eval_run_id(prompt_digest=digest, trial=1, lane="kanban"),
        eval_run.lane_eval_run_id(prompt_digest=digest, trial=2, lane="terminal"),
        eval_run.lane_eval_run_id(prompt_digest="e" * 64, trial=1, lane="terminal"),
    }
    assert first not in others and len(others) == 3
    assert "/" not in first
    with pytest.raises(ValueError):
        eval_run.lane_eval_run_id(prompt_digest=digest, trial=1, lane="mystery")


# --------------------------------------------------------------------- paths


def test_run_layout_matches_design_contract(tmp_path: Path) -> None:
    digest = "a" * 64
    run_dir = eval_run.eval_run_dir(tmp_path, prompt_digest=digest, trial=2)
    assert run_dir == (
        tmp_path / "artifacts" / "benchmark-evaluations" / "runs"
        / digest / "trial-2"
    )
    assert eval_run.run_manifest_path(run_dir) == run_dir / "run.json"
    assert eval_run.lane_prd_path(run_dir, "terminal") == (
        run_dir / "lanes" / "terminal" / "prd" / "prd.md"
    )
    assert eval_run.lane_state_dir(run_dir, "kanban") == (
        run_dir / "lanes" / "kanban" / ".zf"
    )
    assert eval_run.prompt_packet_path(tmp_path, prompt_digest=digest) == (
        tmp_path / "artifacts" / "benchmark-evaluations" / "runs"
        / digest / "prompt.json"
    )


# ----------------------------------------------------------- prompt packet


def test_prompt_packet_is_immutable_and_shared_across_trials(tmp_path: Path) -> None:
    packet = eval_run.freeze_prompt_packet(
        tmp_path, prompt_text=PROMPT, prompt_version=PROMPT_VERSION)
    assert packet["schema_version"] == "benchmark.eval.prompt.v1"
    path = eval_run.prompt_packet_path(tmp_path, prompt_digest=packet["prompt_digest"])
    first_bytes = path.read_bytes()
    again = eval_run.freeze_prompt_packet(
        tmp_path, prompt_text=PROMPT, prompt_version=PROMPT_VERSION)
    assert again == packet
    assert path.read_bytes() == first_bytes


def test_prompt_packet_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    packet = eval_run.freeze_prompt_packet(
        tmp_path, prompt_text=PROMPT, prompt_version=PROMPT_VERSION)
    path = eval_run.prompt_packet_path(tmp_path, prompt_digest=packet["prompt_digest"])
    path.write_text(json.dumps({**packet, "prompt_text": "tampered"}), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        eval_run.freeze_prompt_packet(
            tmp_path, prompt_text=PROMPT, prompt_version=PROMPT_VERSION)


# ---------------------------------------------------------------- lifecycle


def test_start_eval_run_writes_manifest_and_started_event(tmp_path: Path) -> None:
    manifest = _start(tmp_path, trial=2)
    digest = eval_run.prompt_digest(PROMPT, prompt_version=PROMPT_VERSION)
    assert manifest.run_id == f"{digest}-trial-2"
    assert manifest.status == eval_run.RUN_STATUS_RUNNING
    assert manifest.prompt_version == PROMPT_VERSION
    assert manifest.prompt_ref.endswith("prompt.json")
    assert manifest.created_at
    assert set(manifest.lanes) == {"terminal", "kanban", "channel"}
    assert all(
        lane.status == eval_run.LANE_STATUS_PENDING
        for lane in manifest.lanes.values()
    )

    on_disk = eval_run.load_manifest(tmp_path, manifest.run_id)
    assert on_disk == manifest
    raw = json.loads(
        eval_run.run_manifest_path(
            eval_run.eval_run_dir(tmp_path, prompt_digest=digest, trial=2),
        ).read_text(encoding="utf-8")
    )
    assert raw["schema_version"] == "benchmark.eval.run.v1"

    events = _events(tmp_path)
    assert [event.type for event in events] == ["benchmark.eval.run.started"]
    payload = events[0].payload
    assert payload["run_id"] == manifest.run_id
    assert payload["prompt_digest"] == digest
    assert payload["trial"] == 2
    assert payload["lanes"] == ["terminal", "kanban", "channel"]
    assert events[0].correlation_id == manifest.run_id
    assert manifest.started_event_id == events[0].id


def test_start_eval_run_is_fail_closed_on_duplicate_trial(tmp_path: Path) -> None:
    _start(tmp_path, trial=1)
    with pytest.raises(ValueError, match="already exists"):
        _start(tmp_path, trial=1)
    second = _start(tmp_path, trial=2)
    assert second.run_id.endswith("-trial-2")
    assert len(_events(tmp_path)) == 2


def test_start_eval_run_validates_lane_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown eval lane"):
        _start(tmp_path, lanes=("terminal", "bogus"))
    with pytest.raises(ValueError, match="at least one lane"):
        _start(tmp_path, lanes=())
    manifest = _start(tmp_path, lanes=("kanban", "kanban"))
    assert list(manifest.lanes) == ["kanban"]


def test_lane_lifecycle_freezes_prd_and_emits_event(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    run_dir = eval_run.eval_run_dir(
        tmp_path, prompt_digest=manifest.prompt_digest, trial=1)
    prd_path = eval_run.lane_prd_path(run_dir, "terminal")
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text("# PRD\n\nDo X.\n", encoding="utf-8")
    expected_digest = hashlib.sha256(prd_path.read_bytes()).hexdigest()

    running = eval_run.mark_lane_running(tmp_path, manifest.run_id, "terminal")
    assert running.lanes["terminal"].status == eval_run.LANE_STATUS_RUNNING
    assert running.lanes["terminal"].started_at

    done = eval_run.complete_lane(
        tmp_path, manifest.run_id, "terminal",
        status=eval_run.LANE_STATUS_PRD_FROZEN, writer=_writer(tmp_path),
    )
    lane = done.lanes["terminal"]
    assert lane.status == eval_run.LANE_STATUS_PRD_FROZEN
    assert lane.prd_ref == "lanes/terminal/prd/prd.md"
    assert lane.prd_digest == expected_digest
    assert lane.completed_at

    event = _events(tmp_path)[-1]
    assert event.type == "benchmark.eval.lane.completed"
    assert event.payload["lane"] == "terminal"
    assert event.payload["status"] == "prd_frozen"
    assert event.payload["prd_digest"] == expected_digest
    assert event.correlation_id == manifest.run_id
    assert event.causation_id == manifest.started_event_id


def test_complete_lane_enforces_prd_contract_path(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    eval_run.mark_lane_running(tmp_path, manifest.run_id, "kanban")
    with pytest.raises(ValueError, match="contract violated"):
        eval_run.complete_lane(
            tmp_path, manifest.run_id, "kanban",
            status=eval_run.LANE_STATUS_PRD_FROZEN,
        )
    lane = eval_run.load_manifest(tmp_path, manifest.run_id).lanes["kanban"]
    assert lane.status == eval_run.LANE_STATUS_RUNNING


def test_failed_lane_requires_reason_and_timeout_is_a_reason(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    with pytest.raises(ValueError, match="reason"):
        eval_run.complete_lane(
            tmp_path, manifest.run_id, "terminal",
            status=eval_run.LANE_STATUS_FAILED,
        )
    done = eval_run.complete_lane(
        tmp_path, manifest.run_id, "terminal",
        status=eval_run.LANE_STATUS_FAILED,
        error=eval_run.LANE_ERROR_TIMEOUT,
        writer=_writer(tmp_path),
    )
    lane = done.lanes["terminal"]
    assert lane.status == eval_run.LANE_STATUS_FAILED
    assert lane.error == "timeout"
    event = _events(tmp_path)[-1]
    assert event.payload["status"] == "failed"
    assert event.payload["error"] == "timeout"


def test_lane_transitions_are_fail_closed(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    _freeze_lane_prd(tmp_path, manifest.run_id, "terminal")
    with pytest.raises(ValueError, match="already completed"):
        eval_run.complete_lane(
            tmp_path, manifest.run_id, "terminal",
            status=eval_run.LANE_STATUS_FAILED, error="late failure",
        )
    with pytest.raises(ValueError, match="not pending"):
        eval_run.mark_lane_running(tmp_path, manifest.run_id, "terminal")
    with pytest.raises(ValueError, match="unknown eval lane"):
        eval_run.complete_lane(
            tmp_path, manifest.run_id, "bogus",
            status=eval_run.LANE_STATUS_FAILED, error="x",
        )
    with pytest.raises(ValueError, match="terminal status"):
        eval_run.complete_lane(
            tmp_path, manifest.run_id, "kanban", status="running",
        )


def test_lane_must_be_part_of_the_run(tmp_path: Path) -> None:
    manifest = _start(tmp_path, lanes=("terminal",))
    with pytest.raises(ValueError, match="has no lane"):
        eval_run.mark_lane_running(tmp_path, manifest.run_id, "kanban")


def test_complete_run_requires_all_lanes_terminal(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    _freeze_lane_prd(tmp_path, manifest.run_id, "terminal")
    with pytest.raises(ValueError, match="open lanes"):
        eval_run.complete_run(tmp_path, manifest.run_id)


def test_complete_run_emits_summary_with_missing_lanes(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    _freeze_lane_prd(tmp_path, manifest.run_id, "terminal")
    _freeze_lane_prd(tmp_path, manifest.run_id, "kanban")
    eval_run.complete_lane(
        tmp_path, manifest.run_id, "channel",
        status=eval_run.LANE_STATUS_FAILED,
        error=eval_run.LANE_ERROR_TIMEOUT,
        writer=_writer(tmp_path),
    )
    done = eval_run.complete_run(
        tmp_path, manifest.run_id, writer=_writer(tmp_path))
    assert done.status == eval_run.RUN_STATUS_COMPLETED
    assert done.completed_at
    assert not done.all_lanes_prd_frozen

    event = _events(tmp_path)[-1]
    assert event.type == "benchmark.eval.run.completed"
    assert event.payload["lanes"] == {
        "terminal": "prd_frozen", "kanban": "prd_frozen", "channel": "failed",
    }
    assert event.payload["all_lanes_prd_frozen"] is False
    assert event.causation_id == manifest.started_event_id

    with pytest.raises(ValueError, match="not running"):
        eval_run.complete_run(tmp_path, manifest.run_id)


def test_happy_path_event_sequence_and_manifest(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    for lane in ("terminal", "kanban", "channel"):
        _freeze_lane_prd(tmp_path, manifest.run_id, lane)
    done = eval_run.complete_run(
        tmp_path, manifest.run_id, writer=_writer(tmp_path))
    assert done.all_lanes_prd_frozen
    assert [event.type for event in _events(tmp_path)] == (
        ["benchmark.eval.run.started"]
        + ["benchmark.eval.lane.completed"] * 3
        + ["benchmark.eval.run.completed"]
    )
    assert _events(tmp_path)[-1].payload["all_lanes_prd_frozen"] is True


# -------------------------------------------------------------------- reads


def test_list_eval_runs_groups_and_sorts(tmp_path: Path) -> None:
    first = _start(tmp_path, trial=1)
    second = _start(tmp_path, trial=2)
    other = _start(tmp_path, trial=1, prompt="另一个项目的提示词。")
    runs = eval_run.list_eval_runs(tmp_path)
    expected = sorted(
        [first, second, other],
        key=lambda run: (run.prompt_digest, run.trial),
    )
    assert [run.run_id for run in runs] == [run.run_id for run in expected]
    grouped = eval_run.list_eval_runs(tmp_path, prompt_digest=first.prompt_digest)
    assert [run.run_id for run in grouped] == [first.run_id, second.run_id]
    assert eval_run.list_eval_runs(tmp_path / "empty-state") == []


def test_load_manifest_fail_closed_on_corrupt_state(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    with pytest.raises(FileNotFoundError):
        eval_run.load_manifest(tmp_path, f"{'f' * 64}-trial-9")
    run_dir = eval_run.eval_run_dir(
        tmp_path, prompt_digest=manifest.prompt_digest, trial=1)
    eval_run.run_manifest_path(run_dir).write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        eval_run.load_manifest(tmp_path, manifest.run_id)
    eval_run.run_manifest_path(run_dir).write_text(
        json.dumps({"schema_version": "benchmark.eval.run.v0"}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        eval_run.load_manifest(tmp_path, manifest.run_id)


# --------------------------------------------------------------- concurrency


def test_concurrent_lane_completions_serialize_on_the_manifest(tmp_path: Path) -> None:
    manifest = _start(tmp_path)
    run_dir = eval_run.eval_run_dir(
        tmp_path, prompt_digest=manifest.prompt_digest, trial=1)
    for lane in eval_run.DEFAULT_LANES:
        prd_path = eval_run.lane_prd_path(run_dir, lane)
        prd_path.parent.mkdir(parents=True, exist_ok=True)
        prd_path.write_text(f"# PRD from {lane}\n", encoding="utf-8")
        eval_run.mark_lane_running(tmp_path, manifest.run_id, lane)

    def freeze(lane: str) -> str:
        eval_run.complete_lane(
            tmp_path, manifest.run_id, lane,
            status=eval_run.LANE_STATUS_PRD_FROZEN,
            writer=_writer(tmp_path),
        )
        return lane

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sorted(pool.map(freeze, eval_run.DEFAULT_LANES)) == sorted(
            eval_run.DEFAULT_LANES)

    done = eval_run.load_manifest(tmp_path, manifest.run_id)
    assert done.all_lanes_terminal
    assert all(
        lane.prd_digest for lane in done.lanes.values()
    ), "concurrent completions must not lose lane updates"
    lane_events = [
        event for event in _events(tmp_path)
        if event.type == "benchmark.eval.lane.completed"
    ]
    assert {event.payload["lane"] for event in lane_events} == set(
        eval_run.DEFAULT_LANES)


def test_lane_state_dataclass_round_trip() -> None:
    lane = LaneState(lane="terminal", status="prd_frozen", prd_ref="x", prd_digest="d")
    assert LaneState.from_dict(lane.to_dict()) == lane
    assert LaneState.from_dict({}).lane == ""
    manifest = eval_run.EvalRunManifest.from_dict({
        "schema_version": "benchmark.eval.run.v1",
        "run_id": "r", "lanes": {"terminal": lane.to_dict()},
    })
    assert manifest.lanes["terminal"] == lane
    assert manifest.to_dict()["lanes"]["terminal"]["status"] == "prd_frozen"


def test_manifest_artifacts_source_round_trip(tmp_path: Path) -> None:
    writer = EventWriter(EventLog(tmp_path / "state" / "events.jsonl"))
    manifest = eval_run.start_eval_run(
        tmp_path / "state", prompt_text=PROMPT, prompt_version="v", trial=1,
        lanes=["terminal"], artifacts_source="digest0-trial-9", writer=writer,
    )
    assert manifest.artifacts_source == "digest0-trial-9"
    loaded = eval_run.load_manifest(tmp_path / "state", manifest.run_id)
    assert loaded.artifacts_source == "digest0-trial-9"
    events = EventLog(tmp_path / "state" / "events.jsonl").read_all()
    started = [e for e in events if e.type == "benchmark.eval.run.started"]
    assert started[0].payload["artifacts_source"] == "digest0-trial-9"
    # Old manifests without the field load with an empty marker.
    run_dir = eval_run.eval_run_dir(
        tmp_path / "state", prompt_digest=manifest.prompt_digest, trial=1,
    )
    raw = json.loads(eval_run.run_manifest_path(run_dir).read_text(encoding="utf-8"))
    del raw["artifacts_source"]
    assert eval_run.EvalRunManifest.from_dict(raw).artifacts_source == ""
