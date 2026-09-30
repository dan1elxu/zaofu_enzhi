"""Deterministic evaluation observer: trigger, dedup, queue, report.

Watches ``events.jsonl`` for the PRD §8.1 trigger conditions, assembles
immutable packets from the channel projection, dedups by the §8.2 identity
(so restarts and rescans never re-submit), enqueues ordered stages on bounded
independent slots, and refreshes the ``report.md`` reading projection after every change.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from threading import Event
from typing import Any

from zf.core.events import EventLog, EventWriter
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.channel_contracts import normalize_product_discussion_mode
from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, scheduler_for_state
from zf.runtime.evaluation_admission import (
    admitted_context, binding_changed, require_admitted_trial, supersede_rubric_duplicates,
)
from zf.runtime.evaluation_artifacts import report_path
from zf.runtime.evaluation_types import EvaluationPacket, necessity_label
from zf.runtime.evaluation_packets import (
    EVALUATION_ARTIFACT_ROOT,
    PACKET_SCHEMA_VERSION,
    build_packet,
    evaluation_run_id,
    message_evidence_ref,
    stable_digest,
    write_immutable_packet,
)
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_runner import (
    conclusion_result_path,
    JUDGE_COMPLETED_EVENT,
    is_completed_judge_record,
    judge_result_path,
    read_conclusion_result,
    read_judge_result,
    run_conclusion,
    run_judge,
    write_report,
    write_judge_result,
)
from zf.runtime.evaluation_report import (
    _canonical_necessity,
    _collect_missing,
    _evaluation_status,
    _judge_line_status,
    _refresh_report,
    _write_audit_sidecars,
    render_report,
)
from zf.runtime.evaluation_inputs import (
    _freeze_points,
    _frozen_profiles,
    _packet_messages,
    _prepare_channel,
    _snapshot_evaluation_roles,
    _subagent_task_details,
    _frozen_discussion_events,
)
from zf.runtime.evaluation_workflow import (
    WORKFLOW_BIND_EVENTS,
    _bare_digest,
    _bound_prd_payload,
    _bound_workflows,
    _read_json_artifact,
    _require_artifact_identity,
    _revision_value,
    _strict_revision,
)

# Evaluation starts only after the immutable, explicit PRD freeze handoff.
# ``channel.consensus.reached`` remains an internal discussion event and is
# intentionally not an evaluation trigger.
FREEZE_TRIGGER_EVENTS = frozenset({"channel.prd.frozen"})

DEFAULT_POLL_INTERVAL_S = 60.0

TASK_TYPES = frozenset({"bugfix", "refactor", "greenfield", "ambiguous"})
_REVISION_RE = re.compile(r"^[1-9][0-9]*$")


class _ManualRegradeWriter(EventWriter):
    """Keep manual provider events out of the canonical Channel projection."""

    def emit(self, event_type: str, **kwargs: Any) -> Any:
        payload = dict(kwargs.pop("payload", None) or {})
        payload["evaluation_origin"] = "manual_regrade"
        return super().emit(event_type, payload=payload, **kwargs)


def observe_once(
    *,
    state_dir: Path,
    project_root: Path | None = None,
    config: Any | None = None,
    writer: EventWriter | None = None,
    backend: Any | None = None,
    log: Any | None = None,
    scheduler: Any | None = None,
    dispatch: bool = True,
) -> dict[str, Any]:
    """Discover/queue frozen inputs; production provider turns run in workers."""

    state_dir = Path(state_dir)
    writer = writer or EventWriter(EventLog(state_dir / "events.jsonl"))
    log = log or (lambda message: None)
    events = list(writer.event_log.read_all())
    freeze_points = _freeze_points(events)
    bound_workflows = _bound_workflows(events)
    scheduler = scheduler or scheduler_for_state(state_dir, inline=backend is not None)
    stats = {
        "judges_run": 0,
        "channels_seen": len({
            str(item.get("channel_id") or "")
            for item in freeze_points.values()
            if str(item.get("channel_id") or "")
        }),
        "queued": 0,
        "errors": [],
    }
    for _freeze_key, freeze in freeze_points.items():
        channel_id = str(freeze.get("channel_id") or "")
        try:
            # Once admitted, a freeze keeps its rubric. Deploying a role.md
            # update must not silently re-score an already frozen input.
            admitted = admitted_context(state_dir, scheduler.entries, events, freeze)
            changed_binding = bool(admitted and binding_changed(admitted, bound_workflows.get(channel_id) or []))
            role_catalog = (admitted["packets"]["task_type"]["task_details"]["evaluation_role_catalog"]
                            if admitted else None)
            try:
                context = _prepare_channel(
                    state_dir=state_dir, events=events, channel_id=channel_id,
                    freeze=freeze, bound_workflows=bound_workflows.get(channel_id) or [],
                    project_root=project_root,
                    role_catalog=role_catalog,
                )
            except ValueError:
                if not admitted or changed_binding:
                    raise
                # An admitted packet is digest-bound by the queue receipt;
                # resume it when live source files can no longer be rebuilt.
                # A new Workflow binding still needs its full input checks.
                context = admitted
            if context is None and admitted and not changed_binding:
                context = admitted
            if context is not None:
                if admitted and context["trial_id"] != admitted["trial_id"]:
                    raise ValueError("frozen evaluation evidence changed after admission")
                stats["queued"] += _enqueue_channel(state_dir, scheduler, context)
                supersede_rubric_duplicates(state_dir, scheduler, context)
        except Exception as exc:
            identity = ":".join(
                str(freeze.get(key) or "")
                for key in ("channel_id", "thread_id", "prd_revision", "event_id")
            )
            stats["errors"].append(f"{identity}: {exc}")
            log(f"evaluation discovery failed for {identity}: {exc}")
    if dispatch:
        results = scheduler.tick(lambda entry, slot_id: _evaluate_stage(
            state_dir=state_dir, project_root=project_root, config=config,
            entry=entry, slot_id=slot_id, backend=backend, log=log, stop_event=scheduler.stop_event,
            enforce_admission=True,
        ))
        stats["judges_run"] = sum(int(item.get("judges_run") or 0) for item in results)
        stats["errors"].extend(str(item["error"]) for item in results if item.get("error"))
    return stats


def observe_loop(
    *,
    state_dir: Path,
    project_root: Path | None = None,
    config: Any | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    backend: Any | None = None,
    log: Any | None = None,
) -> None:
    """Long-running observer used by the tmux window (``zf evaluation-watch``)."""

    log = log or (lambda message: print(message, flush=True))
    state_dir = Path(state_dir)
    log(f"evaluation observer watching {state_dir}")
    scheduler = scheduler_for_state(state_dir, inline=backend is not None)
    try:
        while True:
            try:
                stats = observe_once(
                    state_dir=state_dir,
                    project_root=project_root,
                    config=config,
                    backend=backend, scheduler=scheduler,
                    log=log,
                )
                if stats["judges_run"] or stats["errors"]:
                    log(
                        "evaluation pass: "
                        f"judges_run={stats['judges_run']} "
                        f"channels={stats['channels_seen']} "
                        f"errors={len(stats['errors'])}"
                    )
            except Exception as exc:
                log(f"evaluation observer tick failed: {exc}")
            time.sleep(max(5.0, float(poll_interval_s)))
    finally:
        scheduler.close()




def _enqueue_channel(state_dir: Path, scheduler: Any, context: dict[str, Any]) -> int:
    context_digest = stable_digest(context)
    input_path = state_dir / EVALUATION_ARTIFACT_ROOT / "queue-inputs" / f"{context_digest}.json"
    if not input_path.exists():
        atomic_write_text(input_path, json.dumps(context, ensure_ascii=False, indent=2) + "\n")
    else:
        try:
            existing = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"queue input is unreadable: {input_path.name}") from exc
        if not isinstance(existing, dict) or stable_digest(existing) != context_digest:
            raise ValueError(f"queue input digest mismatch: {input_path.name}")
    run_id = context["workflow_run_id"]
    classifier_job = ""
    judge_jobs: list[str] = []
    stages = ("task_type", "contribution", "discussion", "prd_fidelity", "conclusion")
    queued = 0
    repair_conclusion = False
    judge_records: dict[str, dict[str, Any] | None] = {}
    for stage in stages:
        packet = context["packets"].get(stage) or context
        digest, trial = packet["source_snapshot_digest"], packet["trial_id"]
        job_id = _request_id(run_id, trial, stage, digest)
        stage_repair = False
        if stage != "conclusion":
            existing = read_judge_result(state_dir, workflow_run_id=run_id,
                                         judge_type=stage, request_id=job_id,
                                         expected_trial_id=trial,
                                         expected_source_snapshot_digest=digest)
            judge_records[stage] = existing
            old_entry = scheduler.entries.get(job_id)
            stage_repair = bool(
                old_entry and old_entry.status == "completed"
                and not is_completed_judge_record(existing)
            )
            repair_conclusion = repair_conclusion or stage_repair
        else:
            # A terminal upstream queue row may have lost its sidecar.  Use
            # the same deterministic placeholder as the conclusion runner so
            # an absent conclusion can still be rebuilt without reopening an
            # exhausted provider request.
            for judge_type, packet in context["packets"].items():
                if judge_records.get(judge_type) is not None:
                    continue
                upstream_job = scheduler.entries.get(_request_id(
                    run_id, packet["trial_id"], judge_type, packet["source_snapshot_digest"],
                ))
                if upstream_job and upstream_job.status in {"completed", "inconclusive"}:
                    judge_records[judge_type] = {
                        "status": "inconclusive", "result": {},
                        "error": "Judge 未产出结果",
                    }
            _bind_quality_records_to_roles(
                context,
                {key: value for key, value in judge_records.items() if value is not None},
            )
            records_ready = all(
                is_completed_judge_record(judge_records.get(judge_type))
                for judge_type in context["packets"]
            )
            conclusion = None
            if records_ready:
                # The conclusion request is bound to the four verified Judge
                # records, not to its scheduler queue id.
                conclusion_digest = stable_digest({
                    key: judge_records.get(key) for key in sorted(judge_records)
                })
                conclusion_request = _request_id(
                    run_id, context["trial_id"], "conclusion", conclusion_digest,
                )
                conclusion = read_conclusion_result(state_dir, run_id, conclusion_request)
                if conclusion is not None and not _report_matches(
                    state_dir, context=context, judges=judge_records, conclusion=conclusion,
                ):
                    _refresh_evaluation_report(
                        state_dir, context=context, judges=judge_records, conclusion=conclusion,
                    )
            old_entry = scheduler.entries.get(job_id)
            stage_repair = repair_conclusion or bool(
                records_ready and old_entry and old_entry.status in {"completed", "inconclusive"}
                and conclusion is None
            )
        # Quality subagents share only the classifier dependency.  They do
        # not consume one another's results even if slot limits serialize
        # their provider turns.  Only the main Judge's conclusion joins all
        # three independent results.
        deps = ([classifier_job] if classifier_job else [])
        if stage == "task_type":
            deps = []
            classifier_job = job_id
        elif stage == "conclusion":
            deps = list(judge_jobs)
        else:
            judge_jobs.append(job_id)
        if stage in {"contribution", "discussion", "prd_fidelity"} and job_id not in judge_jobs:
            judge_jobs.append(job_id)
        queued += int(scheduler.enqueue(EvaluationQueueEntry(
            job_id=job_id, channel_id=context["channel_id"], workflow_run_id=run_id,
            trial_id=trial, judge_type=stage, source_snapshot_digest=digest,
            input_ref=str(input_path.relative_to(state_dir)), input_digest=context_digest,
            depends_on=list(deps),
            freeze_event_id=str(context["freeze"].get("event_id") or ""),
            evaluator_profile=stage,
            evaluation_origin=str(context.get("evaluation_origin") or ""),
        ), repair=stage_repair))
    return queued


def _report_matches(
    state_dir: Path,
    *,
    context: dict[str, Any],
    judges: dict[str, dict[str, Any] | None],
    conclusion: dict[str, Any],
) -> bool:
    """Return whether the latest report is the exact current projection.

    ``report.md`` is rebuildable, but a present file is not evidence that it
    belongs to these frozen Judge records.  Strict UTF-8 comparison detects
    deletion, byte corruption, and ordinary manual edits without adding a
    second report identity format.
    """

    expected = render_report(
        workflow_run_id=context["workflow_run_id"], trial_id=context["trial_id"],
        channel_id=context["channel_id"], freeze=context["freeze"],
        judges={key: value or {} for key, value in judges.items()},
        conclusion=conclusion,
        agent_profiles=(context.get("packets", {}).get("contribution", {})
                        .get("agent_profiles") or []),
    )
    try:
        return report_path(state_dir, context["workflow_run_id"]).read_text(
            encoding="utf-8"
        ) == expected
    except (OSError, UnicodeError):
        return False


def _refresh_evaluation_report(
    state_dir: Path,
    *,
    context: dict[str, Any],
    judges: dict[str, dict[str, Any] | None],
    conclusion: dict[str, Any],
) -> None:
    """Rebuild a damaged report solely from verified sidecars."""

    _refresh_report(
        state_dir, workflow_run_id=context["workflow_run_id"], trial_id=context["trial_id"],
        channel_id=context["channel_id"], freeze=context["freeze"],
        judges={key: value or {} for key, value in judges.items()}, conclusion=conclusion,
        source_snapshot_digest=context["source_snapshot_digest"],
        role_catalog=((context.get("packets", {}).get("task_type", {})
                       .get("task_details", {}) or {}).get("evaluation_role_catalog")),
        agent_profiles=(context.get("packets", {}).get("contribution", {})
                        .get("agent_profiles") or []),
    )


def _evaluate_stage(
    *, state_dir: Path, project_root: Path | None, config: Any | None,
    entry: EvaluationQueueEntry, slot_id: int, backend: Any | None, log: Any,
    stop_event: Event | None = None,
    enforce_admission: bool = False,
) -> dict[str, Any]:
    try:
        context = _load_queue_context(state_dir, entry)
        if enforce_admission:
            require_admitted_trial(state_dir, context)
    except ValueError as exc:
        return {"judges_run": 0, "status": "inconclusive", "error": str(exc)}
    run_id, stage = entry.workflow_run_id, entry.judge_type
    if context.get("evaluation_contract") != "channel-prd-eval.v1":
        # A queue row may survive an upgrade from the retired evaluator.  It
        # is allowed to settle for audit purposes, but its old packet must
        # never enter the canonical runner or start a provider turn.
        return {
            "judges_run": 0,
            "status": "inconclusive",
            "error": "legacy evaluation packet retired; not dispatched",
        }
    if stage not in {"task_type", "contribution", "discussion", "prd_fidelity", "conclusion"}:
        return {
            "judges_run": 0,
            "status": "inconclusive",
            "error": f"unsupported evaluation stage retired; not dispatched: {stage}",
        }
    writer_class = _ManualRegradeWriter if entry.evaluation_origin == "manual_regrade" else EventWriter
    writer = writer_class(EventLog(state_dir / "events.jsonl"))
    judges = {}
    for judge_type, packet in context["packets"].items():
        request_id = _request_id(run_id, packet["trial_id"], judge_type,
                                 packet["source_snapshot_digest"])
        record = read_judge_result(state_dir, workflow_run_id=run_id,
                                  judge_type=judge_type, request_id=request_id,
                                  expected_trial_id=packet.get("trial_id", ""),
                                  expected_source_snapshot_digest=packet.get(
                                      "source_snapshot_digest", ""),
                                  )
        if record is not None:
            judges[judge_type] = record
    conclusion = None
    if stage == "conclusion":
        # An exhausted queue callback can have no verdict sidecar. Treat its
        # absence mechanically, never ask a provider to invent the missing score.
        for judge_type in context["packets"]:
            judges.setdefault(judge_type, {"status": "inconclusive", "result": {},
                                           "error": "Judge 未产出结果"})
        _bind_quality_records_to_roles(context, judges)
        conclusion, ran = _pending_or_run_conclusion(
            state_dir=state_dir, project_root=project_root, config=config,
            writer=writer, backend=backend, log=log, workflow_run_id=run_id,
            trial_id=context["trial_id"], judges=judges, slot_id=slot_id,
            channel_id=entry.channel_id,
            freeze_event_id=str(context.get("freeze", {}).get("event_id") or ""),
            queue_job_id=entry.job_id,
            stop_event=stop_event,
        )
        record = conclusion or {}
    elif stage == "task_type":
        packet = dict(context["packets"][stage])
        packet_path, _ = write_immutable_packet(state_dir, EvaluationPacket.from_dict(packet))
        ran = not is_completed_judge_record(judges.get(stage))
        if ran:
            log(f"running task_type classifier for {run_id} (slot {slot_id}, packet {packet_path.name})")
        record = run_judge(
            state_dir=state_dir, project_root=project_root, config=config,
            writer=writer, workflow_run_id=run_id, trial_id=entry.trial_id,
            judge_type="task_type", packet=packet, backend=backend, log=log,
            slot_id=slot_id, channel_id=entry.channel_id, stop_event=stop_event,
        )
        judges[stage] = record
        if not _classifier_gate(record, context)[0]:
            # The queue marks this stage inconclusive so the scheduler cannot
            # release the three semantic quality providers.
            return {
                "judges_run": int(ran),
                "status": "inconclusive",
                "error": _classifier_gate(record, context)[1],
            }
    else:
        packet = dict(context["packets"][stage])
        # Never leak another subagent's score, rationale or conclusions.
        # Only the main Judge is allowed to consume their independent output.
        packet["prior_judges"] = []
        classifier_record = judges.get("task_type") or {}
        classification = classifier_record.get("result") or {}
        classifier_ok, classifier_error = _classifier_gate(classifier_record, context)
        if not classifier_ok:
            record = _classifier_blocked_record(
                packet, stage=stage, task_type=str(classification.get("task_type") or "ambiguous"),
                request_id=entry.job_id, error=classifier_error,
            )
            _persist_mechanical_judge(
                state_dir, writer=writer, channel_id=entry.channel_id,
                workflow_run_id=run_id, judge_type=stage,
                request_id=entry.job_id, record=record,
            )
            judges[stage] = record
            _refresh_report(
                state_dir, workflow_run_id=run_id, trial_id=context["trial_id"],
                channel_id=entry.channel_id, freeze=context["freeze"], judges=judges,
                conclusion=None, source_snapshot_digest=context["source_snapshot_digest"],
                role_catalog=((context.get("packets", {}).get("task_type", {})
                               .get("task_details", {}) or {}).get("evaluation_role_catalog")),
                agent_profiles=(context.get("packets", {}).get("contribution", {})
                                .get("agent_profiles") or []),
            )
            return {"judges_run": 1, "status": "inconclusive", "error": classifier_error}
        task_type = str(classification.get("task_type") or "ambiguous")
        classifier_details = (
            (context.get("packets", {}).get("task_type", {}) or {}).get("task_details") or {}
        )
        role = _select_evaluation_role(
            classifier_details, task_type, stage,
        ) if task_type != "ambiguous" else None
        if not _valid_role_profile(role, task_type, stage):
            role = None
        if role is None and "task_type" in context["packets"]:
            record = _semantic_role_missing_record(
                packet, stage=stage, task_type=task_type, request_id=entry.job_id,
            )
            _persist_mechanical_judge(
                state_dir, writer=writer, channel_id=entry.channel_id,
                workflow_run_id=run_id, judge_type=stage,
                request_id=entry.job_id, record=record,
            )
            judges[stage] = record
            ran = True
            _refresh_report(
                state_dir, workflow_run_id=run_id, trial_id=context["trial_id"],
                channel_id=entry.channel_id, freeze=context["freeze"], judges=judges,
                conclusion=None, source_snapshot_digest=context["source_snapshot_digest"],
                role_catalog=((context.get("packets", {}).get("task_type", {})
                               .get("task_details", {}) or {}).get("evaluation_role_catalog")),
                agent_profiles=(context.get("packets", {}).get("contribution", {})
                                .get("agent_profiles") or []),
            )
            return {"judges_run": 1, "status": "completed", "error": record.get("error", "")}
        if role:
            _attach_role_metadata(packet, classification, role, stage)
        packet_path, _ = write_immutable_packet(state_dir, EvaluationPacket.from_dict(packet))
        ran = not is_completed_judge_record(judges.get(stage))
        if ran:
            log(f"running {stage} judge for {run_id} (slot {slot_id}, packet {packet_path.name})")
        record = run_judge(
            state_dir=state_dir, project_root=project_root, config=config,
            writer=writer, workflow_run_id=run_id, trial_id=entry.trial_id,
            judge_type=stage, packet=packet, backend=backend, log=log,
            slot_id=slot_id, channel_id=entry.channel_id,
            expected_role_ref=str((role or {}).get("role_ref") or ""),
            expected_role_digest=str((role or {}).get("role_digest") or ""),
            stop_event=stop_event,
        )
        judges[stage] = record
    # A completed report must never be replaced by an intermediate rescan.
    if ran or conclusion:
        _refresh_report(
            state_dir, workflow_run_id=run_id, trial_id=context["trial_id"],
            channel_id=entry.channel_id, freeze=context["freeze"], judges=judges,
            conclusion=conclusion, source_snapshot_digest=context["source_snapshot_digest"],
            role_catalog=((context.get("packets", {}).get("task_type", {})
                           .get("task_details", {}) or {}).get("evaluation_role_catalog")),
            agent_profiles=(context.get("packets", {}).get("contribution", {})
                            .get("agent_profiles") or []),
        )
    record_status = str(record.get("status") or "")
    return {"judges_run": int(ran), "status": (
        "inconclusive" if record_status == "inconclusive"
        else "completed"
    ), "error": str(record.get("error") or "")}


def _load_queue_context(state_dir: Path, entry: EvaluationQueueEntry) -> dict[str, Any]:
    """Load and bind one immutable queue input to its durable queue row."""
    base = Path(state_dir).resolve()
    ref = Path(entry.input_ref)
    if not entry.input_ref or ref.is_absolute() or ".." in ref.parts:
        raise ValueError("evaluation queue input_ref must be a relative state path")
    path = (base / ref).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("evaluation queue input_ref escaped state_dir") from exc
    queue_root = (base / EVALUATION_ARTIFACT_ROOT / "queue-inputs").resolve()
    try:
        path.relative_to(queue_root)
    except ValueError as exc:
        raise ValueError("evaluation queue input_ref is outside queue-inputs") from exc
    if path.suffix != ".json" or not path.is_file():
        raise ValueError("evaluation queue input is missing")
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("evaluation queue input is unreadable") from exc
    if not isinstance(context, dict):
        raise ValueError("evaluation queue input must be an object")
    digest = stable_digest(context)
    if path.stem != digest or (entry.input_digest and entry.input_digest != digest):
        raise ValueError("evaluation queue input digest mismatch")
    if context.get("evaluation_contract") != "channel-prd-eval.v1":
        raise ValueError("unsupported evaluation contract in queue input")
    origin = str(context.get("evaluation_origin") or "")
    if origin not in {"", "manual_regrade"} or origin != entry.evaluation_origin:
        raise ValueError("queue evaluation_origin mismatch")
    if str(context.get("workflow_run_id") or "") != entry.workflow_run_id:
        raise ValueError("queue workflow_run_id mismatch")
    if str(context.get("channel_id") or "") != entry.channel_id:
        raise ValueError("queue channel_id mismatch")
    packets = context.get("packets")
    if not isinstance(packets, dict):
        raise ValueError("queue input packets are missing")
    for packet_stage, packet in packets.items():
        if not isinstance(packet, dict):
            raise ValueError(f"queue packet is not an object: {packet_stage}")
        if packet.get("schema_version") != PACKET_SCHEMA_VERSION:
            raise ValueError(f"queue packet schema mismatch: {packet_stage}")
        if packet.get("prompt_version") != EVALUATION_PROMPT_VERSION:
            raise ValueError(f"queue packet prompt mismatch: {packet_stage}")
        if str(packet.get("workflow_run_id") or "") != entry.workflow_run_id:
            raise ValueError(f"queue packet workflow mismatch: {packet_stage}")
        if str(packet.get("stage") or "") != packet_stage:
            raise ValueError(f"queue packet stage mismatch: {packet_stage}")
    packet = packets.get(entry.judge_type) if entry.judge_type != "conclusion" else context
    if not isinstance(packet, dict):
        raise ValueError(f"queue packet missing for stage: {entry.judge_type}")
    expected_trial = str(packet.get("trial_id") or "")
    expected_digest = str(packet.get("source_snapshot_digest") or "")
    if entry.trial_id != expected_trial or entry.source_snapshot_digest != expected_digest:
        raise ValueError(f"queue packet identity mismatch: {entry.judge_type}")
    freeze_event_id = str((context.get("freeze") or {}).get("event_id") or "")
    if entry.freeze_event_id and entry.freeze_event_id != freeze_event_id:
        raise ValueError("queue freeze_event_id mismatch")
    if entry.evaluator_profile and entry.evaluator_profile != entry.judge_type:
        raise ValueError("queue evaluator_profile mismatch")
    metadata = (packet.get("task_details") or {}).get("evaluation_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    for field in ("task_type", "role_ref", "role_digest"):
        expected = str(getattr(entry, field) or "")
        if expected and expected != str(metadata.get(field) or packet.get(field) or ""):
            raise ValueError(f"queue role metadata mismatch: {field}")
    expected_job = _request_id(entry.workflow_run_id, expected_trial, entry.judge_type, expected_digest)
    if entry.job_id != expected_job:
        raise ValueError(f"queue job identity mismatch: {entry.job_id}")
    return context


def _classifier_gate(record: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str]:
    result = record.get("result") if isinstance(record, dict) else None
    result = result if isinstance(result, dict) else {}
    status = str(record.get("status") or "") if isinstance(record, dict) else ""
    task_type = str(result.get("task_type") or "")
    confidence = result.get("confidence")
    if isinstance(confidence, bool):
        return False, f"task_type classifier confidence is not numeric: {confidence!r}"
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = -1.0
    result_status = str(result.get("status") or "")
    if status != "scored" or result_status != "scored":
        return False, (
            "task_type classifier status is not scored: "
            f"record={status or 'missing'}, result={result_status or 'missing'}"
        )
    if task_type not in {"bugfix", "refactor", "greenfield"}:
        return False, f"task_type classifier returned unsupported type: {task_type or 'missing'}"
    if not math.isfinite(confidence_value) or confidence_value < 0.60 or confidence_value > 1.0:
        return False, f"task_type classifier confidence below gate: {confidence!r}"
    details = ((context.get("packets", {}).get("task_type", {}) or {}).get("task_details") or {})
    for stage in ("contribution", "discussion", "prd_fidelity"):
        role = _select_evaluation_role(details, task_type, stage)
        if not _valid_role_profile(role, task_type, stage):
            return False, f"evaluation role unavailable for task_type={task_type!r}, stage={stage}"
    return True, ""


def _valid_role_profile(role: dict[str, Any] | None, task_type: str, stage: str) -> bool:
    if not isinstance(role, dict):
        return False
    required = ("profile_id", "scope", "task_type", "role_ref", "role_digest", "role_version", "body")
    if any(not str(role.get(key) or "").strip() for key in required):
        return False
    if str(role.get("task_type")) != task_type or str(role.get("scope")) != stage:
        return False
    if str(role.get("profile_id")) != f"{task_type}.{stage}":
        return False
    if str(role.get("role_ref")) != f"evaluation_roles/{task_type}.md":
        return False
    return True


def _classifier_blocked_record(
    packet: dict[str, Any], *, stage: str, task_type: str, request_id: str, error: str,
) -> dict[str, Any]:
    record = _semantic_role_missing_record(packet, stage=stage, task_type=task_type, request_id=request_id)
    record["error"] = error
    result = record.get("result") or {}
    result["missing_evidence"] = ["task_type.classifier"]
    record["result"] = result
    return record


def _bind_quality_records_to_roles(
    context: dict[str, Any], judges: dict[str, dict[str, Any]],
) -> None:
    """Invalidate a scored quality sidecar whose frozen role was changed."""
    classifier = judges.get("task_type") or {}
    result = classifier.get("result") if isinstance(classifier.get("result"), dict) else {}
    task_type = str(result.get("task_type") or "")
    details = ((context.get("packets", {}).get("task_type", {}) or {}).get("task_details") or {})
    for stage in ("contribution", "discussion", "prd_fidelity"):
        record = judges.get(stage)
        if not isinstance(record, dict) or record.get("status") != "scored":
            continue
        role = _select_evaluation_role(details, task_type, stage)
        if not _valid_role_profile(role, task_type, stage):
            record.update(status="invalid", error="evaluation role unavailable")
            continue
        if (
            str(record.get("role_ref") or "") != str(role.get("role_ref") or "")
            or str(record.get("role_digest") or "") != str(role.get("role_digest") or "")
            or str(record.get("task_type") or "") not in {"", task_type}
        ):
            record.update(status="invalid", error="judge role metadata mismatch")

def _select_evaluation_role(
    task_details: dict[str, Any], task_type: str, stage: str,
) -> dict[str, Any] | None:
    catalog = task_details.get("evaluation_role_catalog")
    if not isinstance(catalog, dict) or task_type not in TASK_TYPES - {"ambiguous"}:
        return None
    from zf.runtime.evaluation_roles import select_profile
    try:
        return select_profile(catalog, task_type, stage)
    except Exception:
        # Role tampering/malformed snapshots fail closed at the mechanical
        # boundary; the caller records evidence_missing and never starts a
        # provider turn.
        return None


def _attach_role_metadata(
    packet: dict[str, Any], classification: dict[str, Any], role: dict[str, Any], stage: str,
) -> None:
    """Bind the frozen role profile to one isolated subagent packet."""

    packet["task_type"] = str(classification.get("task_type") or role.get("task_type") or "")
    packet["task_type_confidence"] = classification.get("confidence")
    packet["role_ref"] = str(role.get("role_ref") or "")
    packet["role_digest"] = str(role.get("role_digest") or "")
    packet["evaluator_profile"] = str(role.get("profile_id") or stage)
    packet["subagent_scope"] = stage
    details = packet.setdefault("task_details", {})
    # The classifier packet carries the complete frozen allowlist, but a
    # semantic subagent receives only its selected task role.  Retain the
    # catalog digest for audit without exposing unrelated role bodies or
    # another subagent's rubric.
    catalog = details.pop("evaluation_role_catalog", None)
    if isinstance(catalog, dict):
        details["evaluation_role_catalog_ref"] = {
            "schema_version": catalog.get("schema_version") or catalog.get("catalog_version") or "",
            "catalog_version": catalog.get("catalog_version") or "",
            "catalog_digest": catalog.get("catalog_digest") or "",
        }
    metadata = details.setdefault("evaluation_metadata", {})
    metadata.update({
        "task_type": packet["task_type"],
        "task_type_confidence": packet["task_type_confidence"],
        "role_ref": packet["role_ref"],
        "role_digest": packet["role_digest"],
        "evaluator_profile": packet["evaluator_profile"],
        "subagent_scope": packet["subagent_scope"],
        "role_version": str(role.get("role_version") or ""),
        "role_body": str(role.get("body") or ""),
    })
    details["selected_evaluation_role"] = {
        "profile_id": packet["evaluator_profile"],
        "task_type": packet["task_type"],
        "role_ref": packet["role_ref"],
        "role_digest": packet["role_digest"],
        "role_version": metadata.get("role_version") if isinstance(metadata, dict) else "",
    }


def _persist_mechanical_judge(
    state_dir: Path,
    *,
    writer: EventWriter,
    channel_id: str,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
    record: dict[str, Any],
) -> None:
    """Persist a fail-closed result that never needed a provider turn."""

    path = write_judge_result(
        state_dir,
        workflow_run_id=workflow_run_id,
        judge_type=judge_type,
        request_id=request_id,
        record=record,
    )
    writer.emit(
        JUDGE_COMPLETED_EVENT,
        actor="evaluation-observer",
        correlation_id=workflow_run_id,
        payload={
            "schema_version": "benchmark.evaluation.judge.v1",
            "evaluation_contract": "channel-prd-eval.v1",
            "request_id": request_id,
            "channel_id": channel_id,
            "attempt": record.get("attempt", 0),
            "max_attempts": record.get("max_attempts", 0),
            "judge_type": judge_type,
            "workflow_run_id": workflow_run_id,
            "trial_id": record.get("trial_id", ""),
            "run_id": record.get("run_id", workflow_run_id),
            "status": record.get("status", "evidence_missing"),
            "source_snapshot_digest": record.get("source_snapshot_digest", ""),
            "prompt_version": record.get("prompt_version", EVALUATION_PROMPT_VERSION),
            "freeze_event_id": str(record.get("freeze_event_id") or ""),
            "artifact_ref": str(path.relative_to(Path(state_dir))),
            "artifact_digest": stable_digest(record),
            "error": record.get("error", ""),
        },
    )


def _semantic_role_missing_record(
    packet: dict[str, Any], *, stage: str, task_type: str, request_id: str,
) -> dict[str, Any]:
    """Mechanical fail-closed result for a missing/ambiguous role profile."""

    schema = {
        "contribution": "contribution_result.v2",
        "discussion": "discussion_result.v2",
        "prd_fidelity": "prd_fidelity_result.v2",
    }.get(stage, f"{stage}_result.v2")
    return {
        "schema_version": "benchmark.evaluation.record.v1",
        "evaluation_contract": "channel-prd-eval.v1",
        "judge_type": stage,
        "status": "evidence_missing",
        "request_id": request_id,
        "run_id": str(packet.get("run_id") or ""),
        "workflow_run_id": str(packet.get("workflow_run_id") or ""),
        "trial_id": str(packet.get("trial_id") or ""),
        "source_snapshot_digest": str(packet.get("source_snapshot_digest") or ""),
        "prompt_version": EVALUATION_PROMPT_VERSION,
        "freeze_event_id": str(
            (packet.get("task_details") or {}).get("freeze_event_id") or ""
        ),
        "provider": "deterministic",
        "model": "",
        "created_at": "",
        "result": {
            "schema_version": schema,
            "judge_type": stage,
            "status": "evidence_missing",
            "task_type": task_type,
            "score_scale": "0-4",
            "dimensions": [],
            "agent_findings": [],
            "evidence_refs": [],
            "missing_evidence": [f"evaluation_role:{task_type}"],
        },
        "error": f"evaluation role unavailable for task_type={task_type!r}",
        "attempt": 0,
        "max_attempts": 0,
        "retryable": False,
    }


def _request_id(
    workflow_run_id: str,
    trial_id: str,
    judge_type: str,
    source_snapshot_digest: str,
) -> str:
    from zf.runtime.evaluation_packets import evaluation_request_id

    return evaluation_request_id(
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        judge_type=judge_type,
        source_snapshot_digest=source_snapshot_digest,
    )


def _pending_or_run_conclusion(
    *,
    state_dir: Path,
    project_root: Path | None,
    config: Any | None,
    writer: EventWriter,
    backend: Any | None,
    workflow_run_id: str,
    trial_id: str,
    judges: dict[str, dict[str, Any]],
    log: Any | None = None,
    slot_id: int | None = None,
    channel_id: str = "",
    freeze_event_id: str = "",
    queue_job_id: str = "",
    stop_event: Event | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return (record, ran). An EXISTING conclusion record is returned too
    (ran=False) so the caller refreshes the report WITH the conclusion —
    returning None for the already-done case left stale "waiting for
    judges" reports behind whenever a later pass re-entered this branch
    after another pass had already refreshed a judge-only report (the
    refresh then raced and overwrote the good report).
    """

    from zf.runtime.evaluation_packets import (
        evaluation_request_id,
        stable_digest,
    )

    snapshot = stable_digest({key: judges.get(key) for key in sorted(judges)})
    request_id = evaluation_request_id(
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        judge_type="conclusion",
        source_snapshot_digest=snapshot,
    )
    existing = read_conclusion_result(state_dir, workflow_run_id, request_id)
    if existing is not None and existing.get("status") in {
        "complete", "incomplete", "inconclusive", "invalid",
    }:
        return existing, False
    return run_conclusion(
        state_dir=state_dir,
        project_root=project_root,
        config=config,
        writer=writer,
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        judges=judges,
        backend=backend,
        log=log, slot_id=slot_id, channel_id=channel_id,
        freeze_event_id=freeze_event_id, queue_job_id=queue_job_id, stop_event=stop_event,
    ), True












__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "FREEZE_TRIGGER_EVENTS",
    "observe_loop",
    "observe_once",
    "render_report",
]
