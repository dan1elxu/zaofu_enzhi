"""B1 eval driver + CLI: fake-provider end-to-end for the three-lane PRD eval."""

from __future__ import annotations

import json
import sys
import textwrap
from collections import Counter
from pathlib import Path

from zf.core.events import EventLog, EventWriter
from zf.runtime import channel_moderator, eval_lane_driver, eval_run
from zf.runtime import eval_driver
from zf.runtime.eval_driver import run_eval, run_summary
from zf.runtime.evaluation_artifacts import report_path
from zf.runtime.evaluation_prompts import (
    CROSS_COMPARE_PROMPT_VERSION,
    LANE_EVALUATION_CONTRACT,
    LANE_PRD_FIDELITY_PROMPT_VERSION,
)
from tests.evaluation_fixtures import (
    _cross_compare_reply,
    _judge_reply,
    _lane_prd_fidelity_reply,
)

PROMPT = "做一个命令行待办事项工具，支持增删改查与到期提醒，数据本地存储。MARKER-5521。"
PROMPT_VERSION = "eval-prompt.v1"
LANES = ("terminal", "kanban", "channel")

_FAKE_CLI_TEMPLATE = '''
import json
import os
import re
import sys

line = sys.stdin.readline()
prompt = ""
try:
    data = json.loads(line or "{}")
    content = data.get("message", {}).get("content", [])
    if isinstance(content, list) and content:
        prompt = str(content[0].get("text") or "")
except Exception:
    pass

session = "fake-eval-session"

def emit(text):
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}))
    print(json.dumps({"type": "assistant", "session_id": session,
                      "message": {"content": [{"type": "text", "text": text}]}}))
    print(json.dumps({"type": "result", "session_id": session,
                      "result": text, "is_error": False}))
    sys.stdout.flush()

DIMS = ["coverage", "fidelity", "actionability", "structure"]

if "bounded task-type classifier" in prompt:
    emit(json.dumps({
        "schema_version": "task_type_result.v1", "judge_type": "task_type",
        "status": "scored", "task_type": "greenfield", "confidence": 0.9,
        "evidence_refs": ["eval/prompt"], "rationale": "greenfield build",
        "missing_evidence": [],
    }, ensure_ascii=False))
elif "blind PRD quality evaluator" in prompt:
    emit(json.dumps({
        "schema_version": "lane_prd_fidelity_result.v2",
        "judge_type": "lane_prd_fidelity", "score_scale": "0-4",
        "checklist": [{
            "item_id": "req-1", "requirement": "prompt requirement",
            "verdict": "pass", "evidence_refs": ["eval/prd"],
        }],
        "dimensions": [{
            "dimension_id": d, "status": "scored", "score": 3, "confidence": 0.9,
            "evidence_refs": ["eval/prompt", "eval/prd"],
            "rationale": d + " ok", "verdict": "aligned",
        } for d in DIMS],
        "differences": [], "missing_evidence": [],
    }, ensure_ascii=False))
elif "cross-comparison judge" in prompt:
    labels = []
    for match in re.finditer(r'"label"\\s*:\\s*"([A-C])"', prompt):
        if match.group(1) not in labels:
            labels.append(match.group(1))
    if len(labels) < 2:
        labels = ["A", "B"]
    pairs = [(a, b) for i, a in enumerate(labels) for b in labels[i + 1:]]
    pairwise = [{
        "pair": a + "-" + b, "dimension_id": d, "winner": a, "margin": "medium",
        "confidence": 3, "evidence_refs": ["eval/prd/" + a, "eval/prd/" + b],
        "rationale": d + " " + a + " better",
    } for a, b in pairs for d in DIMS]
    attributions = [{
        "dimension_id": d, "pair": a + "-" + b, "category": "coverage_gap",
        "summary": a + " vs " + b + " on " + d,
        "evidence_refs": ["eval/prd/" + a, "eval/prd/" + b],
    } for a, b in pairs for d in DIMS]
    rankings = [{
        "dimension_id": d, "ranking": labels, "confidence": 3,
        "evidence_refs": ["eval/prd/" + label for label in labels],
        "rationale": d + " order",
    } for d in DIMS]
    rec = {
        "ranking": labels, "top": labels[0], "confidence": 3,
        "rationale": "dimension votes lead", "divergence_explanation": "",
        "key_gaps": [{
            "candidate": label, "gap": label + " gap vs " + labels[0],
            "evidence_refs": ["eval/prd/" + label, "eval/prd/" + labels[0]],
        } for label in labels[1:]],
    }
    emit(json.dumps({
        "schema_version": "cross_compare_result.v1", "judge_type": "cross_compare",
        "status": "scored", "score_scale": "0-4", "candidates": labels,
        "dimension_rankings": rankings, "pairwise": pairwise,
        "attributions": attributions, "conflicts": [], "recommendation": rec,
        "missing_evidence": [],
    }, ensure_ascii=False))
else:
    if "__SKIP_LANE__" not in os.getcwd():
        os.makedirs("prd", exist_ok=True)
        with open("prd/prd.md", "w", encoding="utf-8") as fh:
            fh.write("# Eval PRD\\n\\n" + prompt[-400:] + "\\n")
    emit("prd written")
'''


def _write_fake_cli(tmp_path: Path, *, skip_lane: str = "") -> str:
    script = tmp_path / "fake_eval_cli.py"
    script.write_text(
        _FAKE_CLI_TEMPLATE.replace("__SKIP_LANE__", skip_lane or "__never_skip__"),
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


def _fake_moderator_json_turn(**kwargs):
    thread_key = str(kwargs.get("thread_key") or "")
    if thread_key.startswith("channel-moderator-classify:"):
        return {"complexity": "simple"}, ""
    return (
        {
            "action": "conclude",
            "speakers": [],
            "focus": "",
            "rationale": "deterministic test verdict",
        },
        "",
    )


def _setup_fake_providers(tmp_path: Path, monkeypatch, *, skip_lane: str = "") -> None:
    monkeypatch.setenv(
        "ZF_KANBAN_AGENT_CLAUDE_HEADLESS_CMD",
        _write_fake_cli(tmp_path, skip_lane=skip_lane),
    )
    monkeypatch.setattr(
        channel_moderator, "agent_json_turn", _fake_moderator_json_turn
    )


def _cross_labels_from_inputs(packet_path: Path) -> list[str]:
    inputs = Path(packet_path).parents[3] / "inputs"
    for path in sorted(inputs.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("stage") == "cross_compare":
            return [str(item["label"]) for item in data["task_details"]["candidates"]]
    raise AssertionError("cross_compare packet not persisted under inputs/")


def _judge_backend(calls: list[str]):
    def backend(**kwargs):
        judge_type = str(kwargs.get("judge_type") or "")
        calls.append(judge_type)
        if judge_type == "task_type":
            return _judge_reply("task_type", "eval/prompt"), ""
        if judge_type == "lane_prd_fidelity":
            return _lane_prd_fidelity_reply(), ""
        if judge_type == "cross_compare":
            labels = _cross_labels_from_inputs(Path(kwargs["packet_path"]))
            return _cross_compare_reply(candidates=tuple(labels)), ""
        raise AssertionError(f"unexpected judge type: {judge_type}")

    return backend


def _run_kwargs(state_dir: Path, tmp_path: Path, calls: list[str], **overrides):
    kwargs = dict(
        prompt_text=PROMPT,
        n_trials=1,
        backend="claude-headless",
        channel_backend="fake",
        timeout_s=120.0,
        state_dir=state_dir,
        project_root=tmp_path,
        writer=EventWriter(EventLog(state_dir / "events.jsonl")),
        judge_backend=_judge_backend(calls),
        lane_overrides={"channel": {"poll_interval_s": 0.05}},
    )
    kwargs.update(overrides)
    return kwargs


def _events(state_dir: Path):
    return EventLog(state_dir / "events.jsonl").read_all()


def _queue_entries(run_dir: Path) -> list[dict]:
    queue = json.loads(
        (run_dir / "artifacts" / "benchmark-evaluations" / "scheduler.json").read_text(
            encoding="utf-8"
        )
    )
    return queue["entries"]


def _judge_sidecars(run_dir: Path, judge_type: str) -> list[Path]:
    root = run_dir / "artifacts" / "benchmark-evaluations" / "judges"
    return [
        path
        for path in root.rglob("*.json")
        if path.parent.name == judge_type and "attempts" not in path.parent.parts
    ]


# --------------------------------------------------------------- happy path


def test_run_eval_end_to_end_fake_providers(tmp_path: Path, monkeypatch) -> None:
    _setup_fake_providers(tmp_path, monkeypatch)
    state_dir = tmp_path / "state"
    calls: list[str] = []
    results = run_eval(**_run_kwargs(state_dir, tmp_path, calls))
    assert len(results) == 1
    result = results[0]
    assert result.status == "completed", result.error
    assert not result.skipped
    run_id = result.run_id
    digest, trial = eval_run.parse_eval_run_id(run_id)
    assert trial == 1
    run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=digest, trial=trial)

    assert (run_dir / "run.json").is_file()
    assert eval_run.prompt_packet_path(state_dir, prompt_digest=digest).is_file()
    for lane in LANES:
        prd = eval_run.lane_prd_path(run_dir, lane)
        assert prd.is_file() and prd.read_text(encoding="utf-8").strip()
    assert "MARKER-5521" in eval_run.lane_prd_path(run_dir, "terminal").read_text(
        encoding="utf-8"
    )
    assert "MARKER-5521" in eval_run.lane_prd_path(run_dir, "kanban").read_text(
        encoding="utf-8"
    )
    assert "Deterministic test PRD" in eval_run.lane_prd_path(
        run_dir, "channel"
    ).read_text(encoding="utf-8")

    manifest = eval_run.load_manifest(state_dir, run_id)
    assert manifest.status == "completed"
    assert manifest.all_lanes_prd_frozen

    events = _events(state_dir)
    by_type = Counter(event.type for event in events)
    assert by_type == Counter({
        "benchmark.eval.run.started": 1,
        "benchmark.eval.lane.completed": 3,
        "benchmark.eval.run.completed": 1,
    })
    assert events[0].type == "benchmark.eval.run.started"
    assert events[-1].type == "benchmark.eval.run.completed"
    lane_events = [e for e in events if e.type == "benchmark.eval.lane.completed"]
    assert {e.payload["lane"] for e in lane_events} == set(LANES)
    assert all(
        e.payload["status"] == "prd_frozen" and e.payload["prd_digest"]
        for e in lane_events
    )
    assert events[-1].payload["all_lanes_prd_frozen"] is True

    assert Counter(calls) == Counter({
        "task_type": 1, "lane_prd_fidelity": 3, "cross_compare": 1,
    })
    entries = _queue_entries(run_dir)
    assert len(entries) == 5
    assert Counter(entry["judge_type"] for entry in entries) == Counter(
        {"task_type": 1, "lane_prd_fidelity": 3, "cross_compare": 1}
    )
    assert all(entry["status"] == "completed" for entry in entries)

    lane_sidecars = _judge_sidecars(run_dir, "lane_prd_fidelity")
    assert len(lane_sidecars) == 3
    for path in lane_sidecars:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == "scored"
        assert record["result"]["lane_prd_fidelity_score"] == 3.0
        assert record["prompt_version"] == LANE_PRD_FIDELITY_PROMPT_VERSION
        assert record["evaluation_contract"] == LANE_EVALUATION_CONTRACT
    cross_sidecars = _judge_sidecars(run_dir, "cross_compare")
    assert len(cross_sidecars) == 1
    cross = json.loads(cross_sidecars[0].read_text(encoding="utf-8"))
    assert cross["status"] == "scored"
    assert cross["prompt_version"] == CROSS_COMPARE_PROMPT_VERSION
    assert cross["result"]["recommendation"]["top"] in ("A", "B", "C")

    inputs = run_dir / "artifacts" / "benchmark-evaluations" / "inputs"
    packets = [json.loads(path.read_text(encoding="utf-8")) for path in inputs.glob("*.json")]
    lane_packets = [p for p in packets if p.get("stage") == "lane_prd_fidelity"]
    assert len(lane_packets) == 3
    for packet in lane_packets:
        details = packet["task_details"]
        assert details["source"] == "eval-run-lane-prd"
        assert "lane" not in details
        assert packet["allowed_evidence_refs"] == ["eval/prompt", "eval/prd"]
    cross_packets = [p for p in packets if p.get("stage") == "cross_compare"]
    assert len(cross_packets) == 1
    candidates = cross_packets[0]["task_details"]["candidates"]
    assert sorted(item["label"] for item in candidates) == ["A", "B", "C"]
    assert all(set(item) == {"label", "prd", "blind"} for item in candidates)

    report = report_path(run_dir, run_id).read_text(encoding="utf-8")
    assert "## Cross Compare 横向对比" in report
    assert "Eval Run Conclusion" in report
    assert "推荐排名" in report
    for lane in LANES:
        lane_wf = eval_run.lane_eval_run_id(prompt_digest=digest, trial=1, lane=lane)
        lane_report = report_path(run_dir, lane_wf)
        assert lane_report.is_file()
        assert "Lane PRD Fidelity" in lane_report.read_text(encoding="utf-8")

    conclusion = json.loads((run_dir / "conclusion.json").read_text(encoding="utf-8"))
    assert conclusion["status"] == "complete"
    assert conclusion["task_type"] == "bugfix"
    assert set(conclusion["lanes"]) == set(LANES)
    assert all(
        info["blind_mean"] == 3.0 for info in conclusion["lanes"].values()
    )
    assert conclusion["recommendation"]["top_lane"] in LANES
    labels = json.loads((run_dir / "labels.json").read_text(encoding="utf-8"))
    assert sorted(labels["label_to_lane"]) == ["A", "B", "C"]
    assert sorted(labels["label_to_lane"].values()) == sorted(LANES)

    summary = run_summary(state_dir, run_id)
    assert summary["conclusion"]["status"] == "complete"
    assert summary["report_path"] == str(report_path(run_dir, run_id))
    assert set(summary["lane_report_paths"]) == set(LANES)


def test_run_eval_rerun_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _setup_fake_providers(tmp_path, monkeypatch)
    state_dir = tmp_path / "state"
    calls: list[str] = []
    first = run_eval(**_run_kwargs(state_dir, tmp_path, calls))[0]
    assert first.status == "completed"
    event_count = len(_events(state_dir))

    second_calls: list[str] = []
    second = run_eval(**_run_kwargs(state_dir, tmp_path, second_calls))[0]
    assert second.status == "completed"
    assert second.skipped
    assert second.run_id == first.run_id
    assert second_calls == []
    assert len(_events(state_dir)) == event_count


def test_run_eval_multi_trial_independent_dirs(tmp_path: Path, monkeypatch) -> None:
    _setup_fake_providers(tmp_path, monkeypatch)
    state_dir = tmp_path / "state"
    calls: list[str] = []
    results = run_eval(**_run_kwargs(
        state_dir, tmp_path, calls, n_trials=3, lanes=("terminal", "kanban"),
        channel_backend="", lane_overrides={},
    ))
    assert [item.status for item in results] == ["completed"] * 3
    assert len({item.run_id for item in results}) == 3
    assert Counter(calls) == Counter({
        "task_type": 3, "lane_prd_fidelity": 6, "cross_compare": 3,
    })
    digest = results[0].prompt_digest
    group = eval_run.run_group_dir(state_dir, prompt_digest=digest)
    assert sorted(path.name for path in group.glob("trial-*")) == [
        "trial-1", "trial-2", "trial-3",
    ]
    assert eval_run.prompt_packet_path(state_dir, prompt_digest=digest).is_file()

    extra_calls: list[str] = []
    extended = run_eval(**_run_kwargs(
        state_dir, tmp_path, extra_calls, n_trials=4, lanes=("terminal", "kanban"),
        channel_backend="", lane_overrides={},
    ))
    assert [item.skipped for item in extended] == [True, True, True, False]
    assert Counter(extra_calls) == Counter({
        "task_type": 1, "lane_prd_fidelity": 2, "cross_compare": 1,
    })


# ------------------------------------------------- lane failure / recovery


def test_run_eval_lane_failure_does_not_block_others(tmp_path: Path, monkeypatch) -> None:
    _setup_fake_providers(tmp_path, monkeypatch, skip_lane="lanes/terminal")
    state_dir = tmp_path / "state"
    calls: list[str] = []
    result = run_eval(**_run_kwargs(state_dir, tmp_path, calls))[0]
    assert result.status == "completed"
    run_id = result.run_id
    digest, _ = eval_run.parse_eval_run_id(run_id)
    run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=digest, trial=1)

    manifest = eval_run.load_manifest(state_dir, run_id)
    assert manifest.lanes["terminal"].status == "failed"
    assert manifest.lanes["terminal"].error == "prd_missing"
    assert manifest.lanes["kanban"].status == "prd_frozen"
    assert manifest.lanes["channel"].status == "prd_frozen"

    events = _events(state_dir)
    lane_events = [e for e in events if e.type == "benchmark.eval.lane.completed"]
    terminal_event = next(e for e in lane_events if e.payload["lane"] == "terminal")
    assert terminal_event.payload["status"] == "failed"
    assert terminal_event.payload["error"] == "prd_missing"
    run_done = [e for e in events if e.type == "benchmark.eval.run.completed"]
    assert len(run_done) == 1
    assert run_done[0].payload["all_lanes_prd_frozen"] is False

    assert Counter(calls) == Counter({
        "task_type": 1, "lane_prd_fidelity": 2, "cross_compare": 1,
    })
    entries = _queue_entries(run_dir)
    assert len(entries) == 4
    assert all(entry["status"] == "completed" for entry in entries)
    cross = json.loads(
        _judge_sidecars(run_dir, "cross_compare")[0].read_text(encoding="utf-8")
    )
    assert cross["status"] == "scored"
    assert len(cross["result"]["candidates"]) == 2

    conclusion = json.loads((run_dir / "conclusion.json").read_text(encoding="utf-8"))
    assert conclusion["status"] == "incomplete"
    assert conclusion["missing_lanes"] == ["terminal"]
    report = report_path(run_dir, run_id).read_text(encoding="utf-8")
    assert "terminal" in report
    assert "Cross Compare 横向对比" in report


def test_run_eval_resumes_completed_lanes_without_rerun(
    tmp_path: Path, monkeypatch
) -> None:
    _setup_fake_providers(tmp_path, monkeypatch)
    state_dir = tmp_path / "state"
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    manifest = eval_run.start_eval_run(
        state_dir, prompt_text=PROMPT, prompt_version=PROMPT_VERSION,
        trial=1, lanes=LANES, writer=writer,
    )
    run_dir = eval_run.eval_run_dir(
        state_dir, prompt_digest=manifest.prompt_digest, trial=1,
    )
    for lane in LANES:
        prd = eval_run.lane_prd_path(run_dir, lane)
        prd.parent.mkdir(parents=True, exist_ok=True)
        prd.write_text(
            "# PRD\n\n覆盖 MARKER-5521 的全部显式需求，含验收标准。\n",
            encoding="utf-8",
        )
        eval_run.mark_lane_running(state_dir, manifest.run_id, lane)
        eval_run.complete_lane(
            state_dir, manifest.run_id, lane,
            status=eval_run.LANE_STATUS_PRD_FROZEN, writer=writer,
        )

    def _boom(*args, **kwargs):
        raise AssertionError("run_lane must not be called for terminal lanes")

    monkeypatch.setattr(eval_lane_driver, "run_lane", _boom)
    calls: list[str] = []
    result = run_eval(**_run_kwargs(state_dir, tmp_path, calls))[0]
    assert result.status == "completed"
    assert not result.skipped
    assert Counter(calls) == Counter({
        "task_type": 1, "lane_prd_fidelity": 3, "cross_compare": 1,
    })
    events = _events(state_dir)
    assert Counter(event.type for event in events) == Counter({
        "benchmark.eval.run.started": 1,
        "benchmark.eval.lane.completed": 3,
        "benchmark.eval.run.completed": 1,
    })
    assert eval_run.load_manifest(state_dir, manifest.run_id).status == "completed"


# --------------------------------------------------------------------- CLI


def test_cli_eval_run_list_show_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    _setup_fake_providers(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / "cli-state"
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(PROMPT, encoding="utf-8")

    from zf.cli.main import main

    rc = main([
        "eval", "run", "--prompt-file", str(prompt_file),
        "--state-dir", str(state_dir), "--timeout-s", "120",
        "--channel-backend", "fake", "--channel-poll-s", "0.05",
        "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    run_id = payload[0]["run_id"]
    assert payload[0]["status"] == "completed"
    assert payload[0]["conclusion_status"] == "complete"
    assert Path(payload[0]["report_path"]).is_file()

    rc = main(["eval", "list", "--state-dir", str(state_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert run_id in out
    assert "terminal=prd_frozen" in out

    rc = main(["eval", "show", run_id, "--state-dir", str(state_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "complete" in out
    assert "推荐排名" in out
    assert "lane terminal: prd_frozen" in out

    rc = main(["eval", "list", "--state-dir", str(state_dir),
               "--prompt-digest", payload[0]["prompt_digest"], "--format", "json"])
    assert rc == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["run_id"] for item in listed] == [run_id]

    rc = main(["eval", "show", "nonexistent-trial-9", "--state-dir", str(state_dir)])
    assert rc == 1
    assert "Error" in capsys.readouterr().err


def test_cli_eval_run_argument_validation(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    from zf.cli.main import main

    assert main(["eval", "run", "--prompt-file", str(tmp_path / "missing.md")]) == 2
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(PROMPT, encoding="utf-8")
    assert main([
        "eval", "run", "--prompt-file", str(prompt_file),
        "--state-dir", str(tmp_path / "s1"), "--lanes", "terminal,bogus",
    ]) == 2
    assert main([
        "eval", "run", "--prompt-file", str(prompt_file),
        "--state-dir", str(tmp_path / "s2"), "--n", "0",
    ]) == 2
    capsys.readouterr()
    assert not (tmp_path / "s1").exists()
    assert not (tmp_path / "s2").exists()


def test_cli_eval_group_keeps_static_subcommands() -> None:
    from zf.cli import eval_preset
    from zf.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "preset", "zf.yaml"])
    assert args.func is eval_preset.run_eval_preset
    args = parser.parse_args([
        "eval", "output", "--kind", "prd_output",
        "--objective", "x", "--target", "t.md",
    ])
    assert args.func is eval_preset.run_eval_output
    args = parser.parse_args(["eval"])
    assert args.func is not None


def test_cli_eval_bare_group_prints_help(capsys) -> None:
    from zf.cli.main import main

    assert main(["eval"]) == 0
    assert "run" in capsys.readouterr().out


def test_drive_lane_channel_shared_state_injects_workspace_ledger(tmp_path: Path, monkeypatch) -> None:
    from zf.runtime.eval_lanes import LaneResult

    captured: dict = {}

    def fake_run_lane(lane, **kwargs):
        captured.update(kwargs)
        return LaneResult(
            lane=lane, ok=False, status="failed", reason="stop",
            started_at="", finished_at="", detail={},
        )

    monkeypatch.setattr(eval_lane_driver, "run_lane", fake_run_lane)
    state_dir = tmp_path / "state"
    manifest = eval_run.start_eval_run(
        state_dir, prompt_text="p", prompt_version="v", trial=3,
        lanes=["channel"], writer=None,
    )
    run_dir = eval_run.eval_run_dir(
        state_dir, prompt_digest=manifest.prompt_digest, trial=3
    )
    eval_lane_driver._drive_lane(
        state_dir=state_dir, run_id=manifest.run_id, run_dir=run_dir,
        lane="channel", prompt_text="p", backend="codex-headless",
        channel_backend="", channel_shared_state=True, timeout_s=1.0,
        writer=None, lane_overrides=None,
    )
    assert captured["state_dir"] == state_dir
    assert captured["channel_id"] == "eval-prd-t3"


def test_drive_lane_channel_isolated_default_unchanged(tmp_path: Path, monkeypatch) -> None:
    from zf.runtime.eval_lanes import LaneResult

    captured: dict = {}

    def fake_run_lane(lane, **kwargs):
        captured.update(kwargs)
        return LaneResult(
            lane=lane, ok=False, status="failed", reason="stop",
            started_at="", finished_at="", detail={},
        )

    monkeypatch.setattr(eval_lane_driver, "run_lane", fake_run_lane)
    state_dir = tmp_path / "state"
    manifest = eval_run.start_eval_run(
        state_dir, prompt_text="p", prompt_version="v", trial=1,
        lanes=["channel"], writer=None,
    )
    run_dir = eval_run.eval_run_dir(
        state_dir, prompt_digest=manifest.prompt_digest, trial=1
    )
    eval_lane_driver._drive_lane(
        state_dir=state_dir, run_id=manifest.run_id, run_dir=run_dir,
        lane="channel", prompt_text="p", backend="codex-headless",
        channel_backend="", channel_shared_state=False, timeout_s=1.0,
        writer=None, lane_overrides=None,
    )
    assert "state_dir" not in captured
    assert "channel_id" not in captured
