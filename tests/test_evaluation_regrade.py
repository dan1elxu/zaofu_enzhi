"""Manual regrade stays isolated from the canonical evaluation run."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer
from zf.runtime import evaluation_regrade
from zf.runtime.evaluation_regrade import regrade_once
from zf.runtime.evaluation_scheduler import EvaluationScheduler
from tests.evaluation_fixtures import _judge_reply, _seed_channel_state, _workflow_binding_payload


def _backend(**kwargs):
    packet = Path(kwargs["packet_path"]).read_text(encoding="utf-8")
    match = re.search(r"channel/message/[A-Za-z0-9._:-]+", packet)
    ref = match.group(0) if match else "channel/message/evt-1"
    return _judge_reply(kwargs["judge_type"], ref, with_bound="artifact/prd/workflow-bound.json" in packet), ""


def _canonical_input(state_dir: Path) -> tuple[Path, bytes]:
    paths = list((state_dir / "artifacts/benchmark-evaluations/queue-inputs").glob("*.json"))
    assert len(paths) == 1
    return paths[0], paths[0].read_bytes()


def test_manual_regrade_isolated_and_late_messages_are_excluded(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        assert observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=_backend,
        )["errors"] == []
    finally:
        scheduler.close()

    source, source_bytes = _canonical_input(tmp_path)
    parent_report = next((tmp_path / "artifacts/benchmark-evaluations/reports").rglob("report.md"))
    parent_report_bytes = parent_report.read_bytes()
    parent_judges = {
        path: path.read_bytes()
        for path in (tmp_path / "artifacts/benchmark-evaluations/judges").rglob("*.json")
        if path.parent.name in {"task_type", "contribution", "discussion", "prd_fidelity"}
    }
    parent_conclusions = {
        path: path.read_bytes()
        for path in (tmp_path / "artifacts/benchmark-evaluations/conclusions").rglob("*.json")
        if path.parent.name != "attempts"
    }
    from zf.runtime.channel_projection import project_channel
    parent_projection = project_channel(tmp_path, "ch-1").get("evaluation")
    writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
    writer.emit("channel.message.posted", actor="late", payload={
        "channel_id": "ch-1", "thread_id": "main", "message_id": "late",
        "member_id": "m1", "role": "dev", "text": "late evidence must be excluded",
    })

    result = regrade_once(
        state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)),
        backend=_backend, regrade_id="manual-1",
    )
    assert result["queued"] == 5
    assert result["workflow_run_id"].startswith("regrade-manual-1")
    assert source.read_bytes() == source_bytes
    assert parent_report.read_bytes() == parent_report_bytes
    assert {path: path.read_bytes() for path in parent_judges} == parent_judges
    assert {path: path.read_bytes() for path in parent_conclusions} == parent_conclusions
    assert project_channel(tmp_path, "ch-1").get("evaluation") == parent_projection
    regrade_report = tmp_path / "artifacts/benchmark-evaluations/reports" / result["workflow_run_id"] / "report.md"
    assert regrade_report.is_file()
    audit = json.loads((tmp_path / result["audit_ref"]).read_text(encoding="utf-8"))
    assert audit["original_context_digest"] == source.stem
    assert set(audit["stage_digests"]) == {"task_type", "contribution", "discussion", "prd_fidelity"}
    prompts = list((tmp_path / "artifacts/benchmark-evaluations/judges" / result["workflow_run_id"]).rglob("*.packet.txt"))
    assert prompts and all("late evidence must be excluded" not in path.read_text(encoding="utf-8") for path in prompts)
    assert (tmp_path / "artifacts/benchmark-evaluations/regrades/manual-1/scheduler.json").is_file()


def test_default_observer_does_not_dispatch_manual_queue(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path)
    canonical = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=canonical, backend=_backend)
    finally:
        canonical.close()
    source, _ = _canonical_input(tmp_path)
    regrade_once(state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)), backend=_backend, regrade_id="manual-2")
    calls: list[str] = []
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        stats = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler,
            backend=lambda **kwargs: calls.append(kwargs["judge_type"]) or _backend(**kwargs),
        )
        assert stats["errors"] == []
        assert calls == []
    finally:
        scheduler.close()


def test_same_regrade_id_is_idempotent_and_source_tamper_is_rejected(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
    finally:
        scheduler.close()
    source, original = _canonical_input(tmp_path)
    first = regrade_once(
        state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)),
        backend=_backend, regrade_id="stable",
    )
    second = regrade_once(
        state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)),
        backend=lambda **kwargs: pytest.fail("idempotent regrade launched provider"),
        regrade_id="stable",
    )
    assert second["idempotent"] is True
    assert second["workflow_run_id"] == first["workflow_run_id"]
    assert source.read_bytes() == original
    source.write_text(
        source.read_text(encoding="utf-8").replace('"channel_id": "ch-1"', '"channel_id": "tampered"', 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest"):
        regrade_once(state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)), regrade_id="stable")


@pytest.mark.parametrize("field,value", [
    ("workflow_run_id", "regrade-other"),
    ("freeze_event_id", "other-freeze"),
    ("prompt_version", "other-prompt"),
    ("catalog_digest", "0" * 64),
    ("stage_digests", {}),
])
def test_completed_regrade_rejects_tampered_audit_identity(
    tmp_path: Path, field: str, value: object,
) -> None:
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
    finally:
        scheduler.close()
    source, _ = _canonical_input(tmp_path)
    regrade_once(
        state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)),
        backend=_backend, regrade_id="tamper",
    )
    audit_path = tmp_path / "artifacts/benchmark-evaluations/reports/regrade-tamper/regrade.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit[field] = value
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match="regrade (audit|clone) identity mismatch"):
        regrade_once(state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)), regrade_id="tamper")


def test_completed_regrade_rejects_audit_clone_swap(tmp_path: Path) -> None:
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
    finally:
        scheduler.close()
    source, _ = _canonical_input(tmp_path)
    input_ref = str(source.relative_to(tmp_path))
    regrade_once(state_dir=tmp_path, input_ref=input_ref, backend=_backend, regrade_id="first")
    regrade_once(state_dir=tmp_path, input_ref=input_ref, backend=_backend, regrade_id="second")
    first_path = tmp_path / "artifacts/benchmark-evaluations/reports/regrade-first/regrade.json"
    second_path = tmp_path / "artifacts/benchmark-evaluations/reports/regrade-second/regrade.json"
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    first.update({
        "workflow_run_id": second["workflow_run_id"],
        "trial_id": second["trial_id"],
        "regrade_context_ref": second["regrade_context_ref"],
        "regrade_context_digest": second["regrade_context_digest"],
        "catalog_digest": second["catalog_digest"],
        "prompt_version": second["prompt_version"],
    })
    first_path.write_text(json.dumps(first), encoding="utf-8")
    with pytest.raises(ValueError, match="regrade audit identity mismatch"):
        regrade_once(state_dir=tmp_path, input_ref=input_ref, regrade_id="first")


def test_interrupted_regrade_reuses_first_frozen_clone(tmp_path: Path, monkeypatch) -> None:
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
    finally:
        scheduler.close()
    source, _ = _canonical_input(tmp_path)
    original_enqueue = evaluation_regrade._enqueue_channel
    monkeypatch.setattr(
        evaluation_regrade, "_enqueue_channel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        regrade_once(state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)), regrade_id="resume")
    audit_path = tmp_path / "artifacts/benchmark-evaluations/reports/regrade-resume/regrade.json"
    first = json.loads(audit_path.read_text(encoding="utf-8"))
    assert first["status"] == "running"
    monkeypatch.setattr(evaluation_regrade, "snapshot_catalog", lambda: pytest.fail("must reuse first clone"))
    monkeypatch.setattr(evaluation_regrade, "_enqueue_channel", original_enqueue)
    resumed = regrade_once(
        state_dir=tmp_path, input_ref=str(source.relative_to(tmp_path)),
        backend=_backend, regrade_id="resume",
    )
    assert resumed["workflow_run_id"] == first["workflow_run_id"]
    assert json.loads(audit_path.read_text(encoding="utf-8"))["catalog_digest"] == first["catalog_digest"]


def test_cli_passes_project_context_to_manual_regrade(monkeypatch, capsys, tmp_path: Path) -> None:
    from zf.cli.main import build_parser
    from zf.cli import evaluation_regrade as cli

    context = type("Context", (), {"config_path": tmp_path / "zf.yaml", "state_dir": tmp_path / ".state", "project_root": tmp_path})()
    received = {}
    monkeypatch.delenv("ZF_STATE_DIR", raising=False)
    monkeypatch.setattr("zf.core.config.project_context.resolve_project_context", lambda **_kwargs: context)
    monkeypatch.setattr("zf.core.config.loader.load_config", lambda _path: "config")
    monkeypatch.setattr("zf.runtime.evaluation_regrade.regrade_once", lambda **kwargs: received.update(kwargs) or {"report_ref": "report.md"})
    args = build_parser().parse_args([
        "evaluation-regrade", "--input-ref", "artifacts/input.json", "--regrade-id", "cli",
    ])
    assert args.func is cli.run_evaluation_regrade
    assert args.func(args) == 0
    assert received == {"state_dir": context.state_dir, "input_ref": "artifacts/input.json", "config": "config", "project_root": context.project_root, "regrade_id": "cli"}
    assert json.loads(capsys.readouterr().out)["report_ref"] == "report.md"


def test_bound_context_reuses_admitted_stages_and_regrade_id_cannot_switch_source(tmp_path):
    _seed_channel_state(tmp_path)
    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=_backend)
        original = next(iter(scheduler.entries.values())).input_ref
        writer = EventWriter(EventLog(tmp_path / "events.jsonl"))
        request = writer.emit("workflow.invoke.requested", actor="test",
                              payload=_workflow_binding_payload(tmp_path))
        writer.emit("workflow.invoke.accepted", actor="kernel",
                    payload={"source_event_id": request.id})
        assert observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler, backend=_backend,
        )["queued"] == 2
        bound_ref = next(entry.input_ref for entry in scheduler.entries.values()
                         if entry.input_ref != original)
    finally:
        scheduler.close()
    result = regrade_once(state_dir=tmp_path, input_ref=bound_ref, backend=_backend,
                         regrade_id="bound")
    assert result["queued"] == 5
    assert (tmp_path / result["report_ref"]).is_file()
    with pytest.raises(ValueError, match="already bound"):
        regrade_once(state_dir=tmp_path, input_ref=original, backend=_backend, regrade_id="bound")


@pytest.mark.parametrize("input_ref", ["../escape.json", "/tmp/escape.json", "missing.json"])
def test_regrade_rejects_untrusted_input_ref(tmp_path: Path, input_ref: str) -> None:
    with pytest.raises(ValueError):
        regrade_once(state_dir=tmp_path, input_ref=input_ref, backend=_backend)
