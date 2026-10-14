"""Driver loop for the three-lane PRD eval (docs/ideas/three-lane-prd-eval.md).

One ``run_eval`` call owns one run-group: for each trial it starts the eval
run (manifest + ``benchmark.eval.run.started`` on the main ledger), drives the
lanes in parallel threads, freezes the lane material into immutable packets,
and runs the judge chain (task_type -> lane_prd_fidelity x N -> cross_compare)
on the trial directory's own scheduler.  The conclusion is a deterministic
aggregation — no provider turn — rendered through ``render_report`` and
persisted next to the lane artifacts inside the trial directory.

Identity mechanics make the driver re-entrant: the prompt packet, run id,
judge request ids, and queue entries are all deterministic digests, so a
completed (prompt_digest, trial) pair is skipped and an interrupted trial
resumes from its persisted scheduler/judge state without re-calling providers.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from zf.core.events import EventLog, EventWriter
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime import eval_run
from zf.runtime.eval_lanes import (
    DEFAULT_LANE_TIMEOUT_S,
    LANE_CHANNEL,
    LaneResult,
    canonical_lane_id,
    run_lane,
)
from zf.runtime.evaluation_artifacts import (
    read_judge_result,
    report_path,
    write_report,
)
from zf.runtime.evaluation_lane_packets import (
    assign_candidate_labels,
    blind_summary_from_record,
    build_cross_compare_packet,
    build_lane_prd_fidelity_packet,
    build_lane_task_type_packet,
)
from zf.runtime.evaluation_packets import (
    evaluation_request_id,
    read_packet,
    write_immutable_packet,
)
from zf.runtime.evaluation_prompts import (
    CROSS_COMPARE_JUDGE_TYPE,
    LANE_PRD_FIDELITY_JUDGE_TYPE,
    prompt_version_for_stage,
)
from zf.runtime.evaluation_report import render_report
from zf.runtime.evaluation_runner import run_judge
from zf.runtime.evaluation_scheduler import (
    EvaluationQueueEntry,
    EvaluationScheduler,
    scheduler_for_state,
)
from zf.runtime.evaluation_turns import JudgeTurnObservation
from zf.runtime.evaluation_types import EvaluationPacket

TASK_TYPE_JUDGE_TYPE = "task_type"
CONCLUSION_SCHEMA_VERSION = "benchmark.eval.run.conclusion.v1"
LABELS_SCHEMA_VERSION = "benchmark.eval.run.labels.v1"
CONCLUSION_FILENAME = "conclusion.json"
LABELS_FILENAME = "labels.json"

# Judge record statuses that count as a genuine verdict for the scheduler;
# anything else (judge_timeout, inconclusive, provider_error) is a transport
# failure and leaves the queue entry inconclusive.
_VERDICT_STATUSES = frozenset({
    "scored", "ambiguous", "evidence_missing", "abstain", "conflict", "invalid",
})


@dataclass(frozen=True)
class EvalRunResult:
    """Driver-level outcome of one trial (lane failures are data, not errors)."""

    run_id: str
    trial: int
    prompt_digest: str
    status: str  # "completed" | "failed"
    skipped: bool = False
    run_dir: str = ""
    report_path: str = ""
    conclusion_path: str = ""
    conclusion_status: str = ""
    lanes: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_eval(
    *,
    prompt_text: str = "",
    prompt_file: str | Path | None = None,
    n_trials: int = 1,
    lanes: Iterable[str] = eval_run.DEFAULT_LANES,
    backend: str = "",
    channel_backend: str = "",
    timeout_s: float = DEFAULT_LANE_TIMEOUT_S,
    prompt_version: str = eval_run.DEFAULT_PROMPT_VERSION,
    state_dir: Path,
    config: Any | None = None,
    project_root: Path | None = None,
    writer: EventWriter | None = None,
    judge_backend: Callable[..., Any] | None = None,
    lane_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[EvalRunResult]:
    """Run ``n_trials`` eval runs of one prompt; completed trials are skipped."""
    if prompt_file is not None:
        prompt_text = Path(prompt_file).read_text(encoding="utf-8")
    prompt_text = str(prompt_text or "")
    if not prompt_text.strip():
        raise ValueError("eval prompt text must be non-empty")
    lane_names = _normalize_lanes(lanes)
    if int(n_trials) < 1:
        raise ValueError(f"n_trials must be >= 1; got {n_trials!r}")
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    writer = writer or EventWriter(EventLog(state_dir / "events.jsonl"))
    digest = eval_run.prompt_digest(prompt_text, prompt_version=prompt_version)
    results: list[EvalRunResult] = []
    for trial in range(1, int(n_trials) + 1):
        try:
            results.append(_run_trial(
                state_dir=state_dir, prompt_text=prompt_text,
                prompt_version=prompt_version, prompt_digest=digest,
                trial=trial, lane_names=lane_names, backend=backend,
                channel_backend=channel_backend, timeout_s=timeout_s,
                config=config, project_root=project_root, writer=writer,
                judge_backend=judge_backend, lane_overrides=lane_overrides,
            ))
        except Exception as exc:
            results.append(EvalRunResult(
                run_id=eval_run.eval_run_id(prompt_digest=digest, trial=trial),
                trial=trial,
                prompt_digest=digest,
                status="failed",
                error=f"{type(exc).__name__}: {exc}"[:500],
            ))
    return results


# --------------------------------------------------------------------- trial


def _run_trial(
    *,
    state_dir: Path,
    prompt_text: str,
    prompt_version: str,
    prompt_digest: str,
    trial: int,
    lane_names: list[str],
    backend: str,
    channel_backend: str,
    timeout_s: float,
    config: Any | None,
    project_root: Path | None,
    writer: EventWriter,
    judge_backend: Callable[..., Any] | None,
    lane_overrides: dict[str, dict[str, Any]] | None,
) -> EvalRunResult:
    run_id = eval_run.eval_run_id(prompt_digest=prompt_digest, trial=trial)
    run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=prompt_digest, trial=trial)
    if eval_run.run_manifest_path(run_dir).exists():
        manifest = eval_run.load_manifest(state_dir, run_id)
        if manifest.status == eval_run.RUN_STATUS_COMPLETED:
            return _result_from_run_dir(state_dir, run_id, skipped=True)
    else:
        manifest = eval_run.start_eval_run(
            state_dir, prompt_text=prompt_text, prompt_version=prompt_version,
            trial=trial, lanes=lane_names, writer=writer,
        )
    _drive_lanes(
        state_dir=state_dir, run_id=run_id, run_dir=run_dir,
        prompt_text=prompt_text, backend=backend, channel_backend=channel_backend,
        timeout_s=timeout_s, writer=writer, lane_overrides=lane_overrides,
    )
    manifest = eval_run.load_manifest(state_dir, run_id)
    evaluation = _evaluate_run(
        run_dir=run_dir, run_id=run_id, manifest=manifest,
        prompt_text=prompt_text, config=config, project_root=project_root,
        judge_backend=judge_backend, judge_backend_name=backend,
    )
    conclusion = _write_conclusion(
        run_dir=run_dir, manifest=manifest, evaluation=evaluation,
    )
    report = _write_reports(
        run_dir=run_dir, run_id=run_id, manifest=manifest,
        evaluation=evaluation, conclusion=conclusion,
    )
    eval_run.complete_run(state_dir, run_id, writer=writer)
    return EvalRunResult(
        run_id=run_id,
        trial=trial,
        prompt_digest=prompt_digest,
        status=eval_run.RUN_STATUS_COMPLETED,
        run_dir=str(run_dir),
        report_path=str(report),
        conclusion_path=str(conclusion_path(run_dir)),
        conclusion_status=str(conclusion.get("status") or ""),
        lanes=conclusion.get("lanes") or {},
    )


def _result_from_run_dir(state_dir: Path, run_id: str, *, skipped: bool) -> EvalRunResult:
    digest, trial = eval_run.parse_eval_run_id(run_id)
    run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
    manifest = eval_run.load_manifest(state_dir, run_id)
    conclusion = _read_json(conclusion_path(run_dir)) or {}
    report = report_path(run_dir, run_id)
    return EvalRunResult(
        run_id=run_id,
        trial=trial,
        prompt_digest=digest,
        status=manifest.status,
        skipped=skipped,
        run_dir=str(run_dir),
        report_path=str(report) if report.is_file() else "",
        conclusion_path=str(conclusion_path(run_dir)) if conclusion_path(run_dir).is_file() else "",
        conclusion_status=str(conclusion.get("status") or ""),
        lanes=conclusion.get("lanes") or {
            lane: {"status": lane_state.status, "error": lane_state.error}
            for lane, lane_state in manifest.lanes.items()
        },
    )


# --------------------------------------------------------------------- lanes


def _normalize_lanes(lanes: Iterable[str]) -> list[str]:
    names: list[str] = []
    for raw in lanes:
        lane = canonical_lane_id(raw)
        if not lane:
            raise ValueError(
                f"unknown eval lane {raw!r}; expected one of {list(eval_run.DEFAULT_LANES)}"
            )
        if lane not in names:
            names.append(lane)
    if not names:
        raise ValueError("eval run requires at least one lane")
    return names


def _lane_kwargs(
    lane: str,
    *,
    backend: str,
    channel_backend: str,
    lane_overrides: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if lane == LANE_CHANNEL:
        selected = channel_backend or backend or "claude-headless"
        kwargs["backend"] = selected
    elif backend:
        kwargs["backend"] = backend
    kwargs.update((lane_overrides or {}).get(lane) or {})
    return kwargs


def _drive_lanes(
    *,
    state_dir: Path,
    run_id: str,
    run_dir: Path,
    prompt_text: str,
    backend: str,
    channel_backend: str,
    timeout_s: float,
    writer: EventWriter,
    lane_overrides: dict[str, dict[str, Any]] | None,
) -> None:
    manifest = eval_run.load_manifest(state_dir, run_id)
    open_lanes = [
        lane for lane, lane_state in manifest.lanes.items()
        if lane_state.status not in eval_run.LANE_TERMINAL_STATUSES
    ]
    if not open_lanes:
        return

    def work(lane: str) -> None:
        _drive_lane(
            state_dir=state_dir,
            run_id=run_id,
            run_dir=run_dir,
            lane=lane,
            prompt_text=prompt_text,
            backend=backend,
            channel_backend=channel_backend,
            timeout_s=timeout_s,
            writer=writer,
            lane_overrides=lane_overrides,
        )

    with ThreadPoolExecutor(max_workers=len(open_lanes), thread_name_prefix="zf-eval-lane") as pool:
        list(pool.map(work, open_lanes))


def _drive_lane(
    *,
    state_dir: Path,
    run_id: str,
    run_dir: Path,
    lane: str,
    prompt_text: str,
    backend: str,
    channel_backend: str,
    timeout_s: float,
    writer: EventWriter,
    lane_overrides: dict[str, dict[str, Any]] | None,
) -> None:
    try:
        lane_state = eval_run.load_manifest(state_dir, run_id).lanes[lane]
        if lane_state.status in eval_run.LANE_TERMINAL_STATUSES:
            return
        prd = eval_run.lane_prd_path(run_dir, lane)
        if lane_state.status == eval_run.LANE_STATUS_PENDING:
            eval_run.mark_lane_running(state_dir, run_id, lane)
        elif (
            lane_state.status == eval_run.LANE_STATUS_RUNNING
            and prd.is_file()
            and prd.read_text(encoding="utf-8").strip()
        ):
            # A previous driver died after the lane transcribed its PRD but
            # before completing the lane; bind the existing artifact instead
            # of re-running the provider.
            eval_run.complete_lane(
                state_dir, run_id, lane,
                status=eval_run.LANE_STATUS_PRD_FROZEN, writer=writer,
            )
            return
        result = run_lane(
            lane,
            lane_dir=eval_run.lane_dir(run_dir, lane),
            prompt=prompt_text,
            timeout_s=timeout_s,
            **_lane_kwargs(
                lane, backend=backend, channel_backend=channel_backend,
                lane_overrides=lane_overrides,
            ),
        )
        _complete_lane_from_result(state_dir, run_id, lane, result, writer=writer)
    except Exception as exc:
        try:
            eval_run.complete_lane(
                state_dir, run_id, lane,
                status=eval_run.LANE_STATUS_FAILED,
                error=f"driver:{type(exc).__name__}: {exc}"[:300],
                writer=writer,
            )
        except ValueError:
            pass


def _complete_lane_from_result(
    state_dir: Path,
    run_id: str,
    lane: str,
    result: LaneResult,
    *,
    writer: EventWriter,
) -> None:
    if result.ok:
        eval_run.complete_lane(
            state_dir, run_id, lane,
            status=eval_run.LANE_STATUS_PRD_FROZEN, writer=writer,
        )
        return
    error = (
        eval_run.LANE_ERROR_TIMEOUT
        if result.status == "timeout"
        else (result.reason or result.status or "failed")
    )
    eval_run.complete_lane(
        state_dir, run_id, lane,
        status=eval_run.LANE_STATUS_FAILED, error=error[:300], writer=writer,
    )


# ---------------------------------------------------------------- evaluation


def _evaluate_run(
    *,
    run_dir: Path,
    run_id: str,
    manifest: eval_run.EvalRunManifest,
    prompt_text: str,
    config: Any | None,
    project_root: Path | None,
    judge_backend: Callable[..., Any] | None,
    judge_backend_name: str,
) -> dict[str, Any]:
    """Freeze packets, drive the trial-local scheduler, collect records."""
    trial_id = f"trial-{manifest.trial}"
    trial_writer = EventWriter(EventLog(run_dir / "events.jsonl"))
    backend = judge_backend or headless_judge_backend(
        judge_backend_name, workspace=run_dir / "judge-workspace",
    )
    scheduler = scheduler_for_state(run_dir, inline=True)
    packets: dict[str, EvaluationPacket] = {}
    runner = _stage_runner(
        run_dir=run_dir, project_root=project_root, config=config,
        writer=trial_writer, packets=packets, backend=backend,
    )
    try:
        task_packet = build_lane_task_type_packet(
            run_id=run_id, workflow_run_id=run_id, trial_id=trial_id,
            prompt_text=prompt_text,
        )
        task_entry = _enqueue_packet(
            scheduler, packets, run_dir, task_packet,
            judge_type=TASK_TYPE_JUDGE_TYPE, channel_id=f"{run_id}:task-type",
        )
        scheduler.tick(runner)
        task_record = _read_record(
            run_dir, task_packet.workflow_run_id, TASK_TYPE_JUDGE_TYPE, task_packet,
        )
        task_result = (task_record or {}).get("result") or {}
        classified = str(task_result.get("task_type") or "")
        confidence = task_result.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = None

        frozen_lanes = [
            lane for lane, lane_state in manifest.lanes.items()
            if lane_state.status == eval_run.LANE_STATUS_PRD_FROZEN
        ]
        lane_entries: dict[str, EvaluationQueueEntry] = {}
        lane_packets: dict[str, EvaluationPacket] = {}
        for lane in frozen_lanes:
            lane_wf = eval_run.lane_eval_run_id(
                prompt_digest=manifest.prompt_digest, trial=manifest.trial, lane=lane,
            )
            prd_body = eval_run.lane_prd_path(run_dir, lane).read_text(encoding="utf-8")
            packet = build_lane_prd_fidelity_packet(
                run_id=run_id, workflow_run_id=lane_wf, trial_id=trial_id,
                prompt_text=prompt_text, prd_body=prd_body,
                prd_digest=manifest.lanes[lane].prd_digest,
                task_type=classified, task_type_confidence=confidence,
            )
            lane_entries[lane] = _enqueue_packet(
                scheduler, packets, run_dir, packet,
                judge_type=LANE_PRD_FIDELITY_JUDGE_TYPE,
                channel_id=f"{run_id}:lane-{lane}",
                depends_on=[task_entry.job_id],
            )
            lane_packets[lane] = packet
        scheduler.tick(runner)
        lane_records = {
            lane: _read_record(
                run_dir, lane_packets[lane].workflow_run_id,
                LANE_PRD_FIDELITY_JUDGE_TYPE, lane_packets[lane],
            )
            for lane in frozen_lanes
        }

        labels = assign_candidate_labels(frozen_lanes, seed=run_id) if frozen_lanes else {}
        candidate_lanes = [
            lane for lane in frozen_lanes
            if str((lane_records.get(lane) or {}).get("status") or "") == "scored"
        ]
        ordered = sorted(candidate_lanes, key=lambda lane: labels[lane])
        candidates = [
            {
                "label": labels[lane],
                "prd_body": eval_run.lane_prd_path(run_dir, lane).read_text(encoding="utf-8"),
                "prd_digest": manifest.lanes[lane].prd_digest,
                "blind": blind_summary_from_record(lane_records[lane] or {}),
            }
            for lane in ordered
        ]
        cross_record: dict[str, Any] | None = None
        if len(candidates) >= 2:
            cross_packet = build_cross_compare_packet(
                run_id=run_id, workflow_run_id=run_id, trial_id=trial_id,
                prompt_text=prompt_text, candidates=candidates,
                task_type=classified,
            )
            _enqueue_packet(
                scheduler, packets, run_dir, cross_packet,
                judge_type=CROSS_COMPARE_JUDGE_TYPE,
                channel_id=f"{run_id}:cross-compare",
                depends_on=[lane_entries[lane].job_id for lane in ordered],
            )
            scheduler.tick(runner)
            cross_record = _read_record(
                run_dir, cross_packet.workflow_run_id,
                CROSS_COMPARE_JUDGE_TYPE, cross_packet,
            )
    finally:
        scheduler.close()
    return {
        "trial_id": trial_id,
        "task_type": task_record,
        "lane_records": lane_records,
        "cross_compare": cross_record,
        "labels": labels,
        "candidate_lanes": ordered,
        "skipped_compare_lanes": [
            lane for lane in frozen_lanes if lane not in candidate_lanes
        ],
    }


def _enqueue_packet(
    scheduler: EvaluationScheduler,
    packets: dict[str, EvaluationPacket],
    run_dir: Path,
    packet: EvaluationPacket,
    *,
    judge_type: str,
    channel_id: str,
    depends_on: Iterable[str] = (),
) -> EvaluationQueueEntry:
    """Persist the immutable packet and enqueue its deterministic job id."""
    path, digest = write_immutable_packet(run_dir, packet)
    entry = EvaluationQueueEntry(
        job_id=evaluation_request_id(
            workflow_run_id=packet.workflow_run_id,
            trial_id=packet.trial_id,
            judge_type=judge_type,
            source_snapshot_digest=packet.source_snapshot_digest,
            prompt_version=prompt_version_for_stage(judge_type),
        ),
        channel_id=channel_id,
        workflow_run_id=packet.workflow_run_id,
        trial_id=packet.trial_id,
        judge_type=judge_type,
        source_snapshot_digest=packet.source_snapshot_digest,
        input_ref=path.relative_to(run_dir).as_posix(),
        input_digest=digest,
        depends_on=list(depends_on),
        evaluator_profile=packet.evaluator_profile or judge_type,
    )
    packets[entry.job_id] = packet
    scheduler.enqueue(entry)
    return entry


def _stage_runner(
    *,
    run_dir: Path,
    project_root: Path | None,
    config: Any | None,
    writer: EventWriter,
    packets: dict[str, EvaluationPacket],
    backend: Callable[..., Any],
) -> Callable[[EvaluationQueueEntry, int], dict[str, Any]]:
    def runner(entry: EvaluationQueueEntry, slot: int) -> dict[str, Any]:
        packet = packets.get(entry.job_id)
        if packet is None and entry.input_digest:
            packet = read_packet(run_dir, entry.input_digest)
        if packet is None:
            return {
                "status": "inconclusive",
                "error": f"eval packet missing for {entry.job_id}",
            }
        record = run_judge(
            state_dir=run_dir, project_root=project_root, config=config,
            writer=writer, workflow_run_id=entry.workflow_run_id,
            trial_id=entry.trial_id, judge_type=entry.judge_type, packet=packet,
            backend=backend, slot_id=slot, channel_id=entry.channel_id,
        )
        status = str(record.get("status") or "")
        if status in _VERDICT_STATUSES:
            return {
                "status": "completed",
                "request_id": str(record.get("request_id") or ""),
                "record_status": status,
            }
        return {
            "status": "inconclusive",
            "error": str(record.get("error") or status or "judge did not finish"),
        }

    return runner


def _read_record(
    run_dir: Path,
    workflow_run_id: str,
    judge_type: str,
    packet: EvaluationPacket,
) -> dict[str, Any] | None:
    return read_judge_result(
        run_dir,
        workflow_run_id=workflow_run_id,
        judge_type=judge_type,
        request_id=evaluation_request_id(
            workflow_run_id=workflow_run_id,
            trial_id=packet.trial_id,
            judge_type=judge_type,
            source_snapshot_digest=packet.source_snapshot_digest,
            prompt_version=prompt_version_for_stage(judge_type),
        ),
        expected_trial_id=packet.trial_id,
        expected_source_snapshot_digest=packet.source_snapshot_digest,
    )


def headless_judge_backend(
    backend: str = "",
    *,
    workspace: Path | None = None,
) -> Callable[..., JudgeTurnObservation]:
    """Judge turn over the headless provider CLI (the eval driver has no tmux).

    The runner hands judges a file-reference prompt; this adapter inlines the
    rendered packet sidecar as the turn prompt and runs it read-only in a
    private workspace so judges never touch the host project.
    """
    from zf.web.headless_agent import (
        ClaudeHeadlessBackend,
        CodexHeadlessBackend,
        canonical_headless_backend,
    )

    backend_id = canonical_headless_backend(backend) or "claude-headless"
    adapter = {
        "claude-headless": ClaudeHeadlessBackend,
        "codex-headless": CodexHeadlessBackend,
    }[backend_id]()
    workdir = Path(workspace) if workspace is not None else None

    def judge(**kwargs: Any) -> JudgeTurnObservation:
        if not adapter.available():
            return JudgeTurnObservation(
                status="provider_error",
                error=f"{backend_id} command is unavailable",
            )
        packet_path = Path(kwargs["packet_path"])
        try:
            prompt_text = packet_path.read_text(encoding="utf-8")
        except OSError as exc:
            return JudgeTurnObservation(
                status="provider_error", error=f"judge packet unreadable: {exc}",
            )
        budgets = kwargs.get("evaluator_budgets") or {}
        timeouts = budgets.get("timeouts") if isinstance(budgets, dict) else {}
        timeout_s = float((timeouts or {}).get("turn_seconds") or 1800)
        cwd = workdir or packet_path.parent
        cwd.mkdir(parents=True, exist_ok=True)
        turn = adapter.run_turn(
            prompt=prompt_text,
            cwd=cwd,
            system_prompt=str(kwargs.get("system_prompt") or ""),
            thread_id=f"eval-judge-{kwargs.get('request_id') or 'unknown'}",
            provider_session_id="",
            on_session_id=lambda _session_id: None,
            on_message=None,
            timeout_s=timeout_s,
            execution_deadline=time.monotonic() + timeout_s,
            permission_profile="read_only",
        )
        if turn.status == "timeout":
            return JudgeTurnObservation(
                status="timeout", error="eval judge turn timed out",
            )
        if turn.ok and turn.reply.strip():
            return JudgeTurnObservation(
                status="completed",
                reply=turn.reply,
                turn_id=turn.thread_id,
                session_id=turn.provider_session_id,
            )
        return JudgeTurnObservation(
            status="provider_error",
            error=turn.error or f"headless judge turn {turn.status}",
        )

    return judge


# --------------------------------------------------------------- conclusion


def conclusion_path(run_dir: Path) -> Path:
    return Path(run_dir) / CONCLUSION_FILENAME


def labels_path(run_dir: Path) -> Path:
    return Path(run_dir) / LABELS_FILENAME


def _write_conclusion(
    *,
    run_dir: Path,
    manifest: eval_run.EvalRunManifest,
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic aggregation: blind means + cross_compare recommendation."""
    labels: dict[str, str] = evaluation.get("labels") or {}
    label_to_lane = {label: lane for lane, label in labels.items()}
    lane_records: dict[str, dict[str, Any] | None] = evaluation.get("lane_records") or {}
    lanes_info: dict[str, dict[str, Any]] = {}
    for lane, lane_state in manifest.lanes.items():
        info: dict[str, Any] = {
            "status": lane_state.status,
            "error": lane_state.error,
            "prd_ref": lane_state.prd_ref,
            "prd_digest": lane_state.prd_digest,
            "label": labels.get(lane, ""),
            "blind_status": "",
            "blind_mean": None,
            "dimensions": {},
        }
        record = lane_records.get(lane)
        if record:
            result = record.get("result") or {}
            info["blind_status"] = str(record.get("status") or "")
            info["blind_mean"] = result.get("lane_prd_fidelity_score")
            info["dimensions"] = {
                str(item.get("dimension_id")): item.get("score")
                for item in result.get("dimensions") or []
                if isinstance(item, dict)
            }
        lanes_info[lane] = info

    task_record = evaluation.get("task_type") or {}
    task_result = task_record.get("result") or {}
    cross_record = evaluation.get("cross_compare")
    cross_info: dict[str, Any] = {}
    recommendation: dict[str, Any] = {}
    if cross_record:
        cross_result = cross_record.get("result") or {}
        recommendation = cross_result.get("recommendation") or {}
        cross_info = {
            "status": str(cross_record.get("status") or ""),
            "request_id": str(cross_record.get("request_id") or ""),
            "dimension_rankings": cross_result.get("dimension_rankings") or [],
            "recommendation": recommendation,
        }
    ranking_labels = [str(label) for label in recommendation.get("ranking") or []]
    ranking = [
        {"label": label, "lane": label_to_lane.get(label, "")}
        for label in ranking_labels
    ]
    top_label = str(recommendation.get("top") or "")
    top_lane = label_to_lane.get(top_label, "")

    missing_lanes = sorted(
        lane for lane, lane_state in manifest.lanes.items()
        if lane_state.status != eval_run.LANE_STATUS_PRD_FROZEN
    )
    unjudged_lanes = sorted(
        lane for lane, info in lanes_info.items()
        if info["status"] == eval_run.LANE_STATUS_PRD_FROZEN
        and info["blind_status"] != "scored"
    )
    problems: list[str] = []
    if missing_lanes:
        problems.append(
            "lane 未产出 PRD: " + ", ".join(
                f"{lane}({manifest.lanes[lane].error or 'failed'})" for lane in missing_lanes
            )
        )
    if unjudged_lanes:
        problems.append("lane blind 评分未完成: " + ", ".join(unjudged_lanes))
    scored_frozen = [
        lane for lane, info in lanes_info.items()
        if info["status"] == eval_run.LANE_STATUS_PRD_FROZEN
        and info["blind_status"] == "scored"
    ]
    cross_status = str((cross_record or {}).get("status") or "")
    if len(scored_frozen) >= 2 and cross_status != "scored":
        problems.append(
            "cross_compare 未得分"
            + (f"（status: {cross_status}）" if cross_status else "（候选不足或未执行）")
        )
    complete = (
        not problems
        and str(task_record.get("status") or "") in {"scored", "ambiguous"}
        and (len(scored_frozen) < 2 or cross_status == "scored")
    )
    if complete:
        means = ", ".join(
            f"{lane}={info['blind_mean']}"
            for lane, info in sorted(lanes_info.items())
        )
        compare_note = (
            f"cross_compare 推荐 {top_label}（lane={top_lane}，"
            f"置信 {recommendation.get('confidence')}）。"
            if cross_status == "scored" else "候选不足两条，未做横向对比。"
        )
        summary_zh = (
            f"{len(scored_frozen)} 条 lane 均产出 PRD 并完成 blind 评分"
            f"（均分 {means}）；{compare_note}"
        )
    else:
        summary_zh = "评测不完整：" + "；".join(problems or ["未知原因"])
    conclusion = {
        "schema_version": CONCLUSION_SCHEMA_VERSION,
        "run_id": manifest.run_id,
        "prompt_digest": manifest.prompt_digest,
        "prompt_version": manifest.prompt_version,
        "trial": manifest.trial,
        "status": "complete" if complete else "incomplete",
        "task_type": str(task_result.get("task_type") or ""),
        "task_type_status": str(task_record.get("status") or ""),
        "lanes": lanes_info,
        "missing_lanes": missing_lanes,
        "cross_compare": cross_info,
        "recommendation": {
            "ranking": ranking,
            "top_label": top_label,
            "top_lane": top_lane,
            "confidence": recommendation.get("confidence"),
            "rationale": str(recommendation.get("rationale") or ""),
        },
        "problems": problems,
        "summary_zh": summary_zh,
    }
    atomic_write_text(
        conclusion_path(run_dir),
        json.dumps(conclusion, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write_text(
        labels_path(run_dir),
        json.dumps({
            "schema_version": LABELS_SCHEMA_VERSION,
            "run_id": manifest.run_id,
            "seed": manifest.run_id,
            "lane_to_label": labels,
            "label_to_lane": label_to_lane,
        }, ensure_ascii=False, indent=2) + "\n",
    )
    return conclusion


def _write_reports(
    *,
    run_dir: Path,
    run_id: str,
    manifest: eval_run.EvalRunManifest,
    evaluation: dict[str, Any],
    conclusion: dict[str, Any],
) -> Path:
    trial_id = str(evaluation.get("trial_id") or f"trial-{manifest.trial}")
    judges: dict[str, dict[str, Any]] = {}
    if evaluation.get("task_type"):
        judges["task_type"] = evaluation["task_type"]
    if evaluation.get("cross_compare"):
        judges["cross_compare"] = evaluation["cross_compare"]
    body = render_report(
        workflow_run_id=run_id,
        trial_id=trial_id,
        channel_id="",
        freeze={"task_id": run_id, "prd_ref": "", "event_type": "benchmark.eval.run"},
        judges=judges,
    )
    body += _conclusion_markdown(conclusion)
    run_report = write_report(run_dir, run_id, body, trial_id=trial_id, final=True)
    lane_records: dict[str, dict[str, Any] | None] = evaluation.get("lane_records") or {}
    for lane, record in lane_records.items():
        if record is None:
            continue
        lane_wf = eval_run.lane_eval_run_id(
            prompt_digest=manifest.prompt_digest, trial=manifest.trial, lane=lane,
        )
        lane_judges: dict[str, dict[str, Any]] = {LANE_PRD_FIDELITY_JUDGE_TYPE: record}
        if evaluation.get("task_type"):
            lane_judges["task_type"] = evaluation["task_type"]
        lane_body = render_report(
            workflow_run_id=lane_wf,
            trial_id=trial_id,
            channel_id="",
            freeze={
                "task_id": run_id,
                "prd_ref": manifest.lanes[lane].prd_ref,
                "event_type": eval_run.LANE_COMPLETED_EVENT,
            },
            judges=lane_judges,
        )
        write_report(run_dir, lane_wf, lane_body, trial_id=trial_id, final=True)
    return run_report


def _conclusion_markdown(conclusion: dict[str, Any]) -> str:
    lines = [
        "",
        "## Eval Run Conclusion（确定性汇总，无 provider）",
        "",
        f"- status: `{conclusion.get('status')}`",
        f"- task_type: `{conclusion.get('task_type') or 'unknown'}`"
        f"（classifier status: {conclusion.get('task_type_status') or 'unknown'}）",
        f"- 汇总：{conclusion.get('summary_zh') or ''}",
    ]
    for problem in conclusion.get("problems") or []:
        lines.append(f"- 问题: {problem}")
    lines += ["", "### Lane 身份与 blind 评分", ""]
    for lane, info in sorted((conclusion.get("lanes") or {}).items()):
        dims = info.get("dimensions") or {}
        dims_text = ", ".join(f"{key}={value}" for key, value in sorted(dims.items()))
        mean = info.get("blind_mean")
        lines.append(
            f"- lane `{lane}` = 候选 **{info.get('label') or '-'}** — "
            f"status `{info.get('status')}`, blind `{info.get('blind_status') or '-'}`, "
            f"均分 {mean if mean is not None else '-'}"
            + (f"（{dims_text}）" if dims_text else "")
            + (f"，error: {info.get('error')}" if info.get("error") else "")
        )
    cross = conclusion.get("cross_compare") or {}
    recommendation = conclusion.get("recommendation") or {}
    if cross:
        label_to_lane = {
            info.get("label"): lane
            for lane, info in (conclusion.get("lanes") or {}).items()
            if info.get("label")
        }

        def reveal(label: str) -> str:
            return f"{label}({label_to_lane.get(label) or '?'})"

        lines += ["", "### Cross Compare 结论（lane 身份已揭示）", ""]
        lines.append(f"- cross_compare status: `{cross.get('status')}`")
        ranking = recommendation.get("ranking") or []
        if ranking:
            order = " > ".join(reveal(str(item.get("label"))) for item in ranking)
            lines.append(
                f"- 推荐排名: {order}（置信 {recommendation.get('confidence')}）"
                f"：{recommendation.get('rationale')}"
            )
            lines.append(f"- top lane: `{recommendation.get('top_lane')}`")
        for entry in cross.get("dimension_rankings") or []:
            revealed = " > ".join(
                reveal(str(label)) for label in entry.get("ranking") or []
            )
            lines.append(f"- 维度 `{entry.get('dimension_id')}`: {revealed}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- helpers


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def run_summary(state_dir: Path, run_id: str) -> dict[str, Any]:
    """Read projection for `zf eval show` / batch summaries (never raises on
    missing optional sidecars; a missing manifest still raises)."""
    manifest = eval_run.load_manifest(state_dir, run_id)
    digest, trial = eval_run.parse_eval_run_id(run_id)
    run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
    report = report_path(run_dir, run_id)
    lane_wfs = {
        lane: eval_run.lane_eval_run_id(
            prompt_digest=digest, trial=trial, lane=lane,
        )
        for lane in manifest.lanes
    }
    return {
        "manifest": manifest.to_dict(),
        "run_dir": str(run_dir),
        "conclusion": _read_json(conclusion_path(run_dir)),
        "labels": _read_json(labels_path(run_dir)),
        "report_path": str(report) if report.is_file() else "",
        "lane_report_paths": {
            lane: str(path)
            for lane, wf in lane_wfs.items()
            if (path := report_path(run_dir, wf)).is_file()
        },
    }


__all__ = [
    "CONCLUSION_FILENAME",
    "CONCLUSION_SCHEMA_VERSION",
    "EvalRunResult",
    "LABELS_FILENAME",
    "LABELS_SCHEMA_VERSION",
    "TASK_TYPE_JUDGE_TYPE",
    "conclusion_path",
    "headless_judge_backend",
    "labels_path",
    "run_eval",
    "run_summary",
]
