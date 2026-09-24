"""Deterministic evaluation observer: trigger, dedup, queue, report.

Watches ``events.jsonl`` for the PRD §8.1 trigger conditions, assembles
immutable packets from the channel projection, dedups by the §8.2 identity
(so restarts and rescans never re-submit), enqueues ordered stages on bounded
independent slots, and refreshes the ``report.md`` reading projection after every change.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event
from typing import Any

from zf.core.events import EventLog, EventWriter
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.evaluation_scheduler import EvaluationQueueEntry, scheduler_for_state
from zf.runtime.evaluation_types import EvaluationPacket
from zf.runtime.evaluation_packets import (
    EVALUATION_ARTIFACT_ROOT,
    build_packet,
    evaluation_run_id,
    message_evidence_ref,
    stable_digest,
    write_immutable_packet,
)
from zf.runtime.evaluation_runner import (
    is_completed_judge_record,
    read_conclusion_result,
    read_judge_result,
    run_conclusion,
    run_judge,
    write_report,
)
from zf.runtime.evaluation_schemas import (
    communication_score,
    overall_score,
    prd_result_score,
)

FREEZE_TRIGGER_EVENTS = frozenset({
    "channel.consensus.reached",
    "channel.result.receipt.recorded",
})
WORKFLOW_BIND_EVENTS = frozenset({
    "workflow.invoke.requested",
    "workflow.invoke.accepted",
})

DEFAULT_POLL_INTERVAL_S = 60.0


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
    stats = {"judges_run": 0, "channels_seen": len(freeze_points), "queued": 0, "errors": []}
    for channel_id, freeze in freeze_points.items():
        try:
            context = _prepare_channel(
                state_dir=state_dir, events=events, channel_id=channel_id,
                freeze=freeze, bound_workflows=bound_workflows.get(channel_id) or [],
            )
            if context is not None:
                stats["queued"] += _enqueue_channel(state_dir, scheduler, context)
        except Exception as exc:
            stats["errors"].append(f"{channel_id}: {exc}")
            log(f"evaluation discovery failed for {channel_id}: {exc}")
    if dispatch:
        results = scheduler.tick(lambda entry, slot_id: _evaluate_stage(
            state_dir=state_dir, project_root=project_root, config=config,
            entry=entry, slot_id=slot_id, backend=backend, log=log, stop_event=scheduler.stop_event,
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


def _prepare_channel(
    *, state_dir: Path, events: list[Any], channel_id: str,
    freeze: dict[str, Any], bound_workflows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Capture immutable inputs before any asynchronous provider work."""

    from zf.runtime.channel_projection import project_channel
    from zf.runtime.channel_prd_render import channel_requirement_text
    from zf.runtime.channel_sidecar import hydrate_channel_message_text

    # Bind the projection to the freeze ledger occurrence, not live messages,
    # member presence or later discussion changes.
    frozen_events = []
    for event in events:
        frozen_events.append(event)
        if event.id == freeze.get("event_id"):
            break
    channel = project_channel(state_dir, channel_id, events=frozen_events) or {}
    thread_id = str(freeze.get("thread_id") or "main")
    prd_revision = int(freeze.get("prd_revision") or 0)
    workflow_run_id = evaluation_run_id(
        channel_id=channel_id, thread_id=thread_id, prd_revision=prd_revision,
    )

    messages = _packet_messages(
        channel, freeze_event_id=str(freeze.get("event_id") or ""),
        hydrate=lambda message: hydrate_channel_message_text(state_dir, message),
    )
    if not messages:
        return None

    prd_body = _read_json_artifact(state_dir, str(freeze.get("prd_ref") or ""))
    readiness = _read_json_artifact(state_dir, str(freeze.get("readiness_ref") or ""))
    if prd_body is None:
        return None  # PRD §8.1: frozen PRD must be readable or we do not trigger

    frozen_prd = {
        "prd_ref": str(freeze.get("prd_ref") or ""),
        "prd_digest": str(freeze.get("prd_digest") or ""),
        "body": prd_body,
        "readiness_ref": str(freeze.get("readiness_ref") or ""),
        "readiness_verdict": str(readiness.get("verdict") or "") if readiness else "",
        "gaps": readiness.get("gaps") if isinstance(readiness, dict) else [],
        "risks": readiness.get("risks") if isinstance(readiness, dict) else [],
    }
    bound_prd = _bound_prd_payload(bound_workflows, frozen_prd)

    task_statement = channel_requirement_text(state_dir, channel, thread_id)
    task = {
        "task_id": str(channel.get("origin_task_id") or channel_id),
        "statement": task_statement,
    }
    task_details = {
        "statement": task_statement,
        "source": "channel-requirement",
        "evidence_keys": [],
    }
    profiles = _frozen_profiles(channel, thread_id, messages)
    iteration_summary = {
        "member_count": len(profiles),
        "prd_revision": prd_revision,
        "message_count": len(messages),
        "complexity": str(
            (channel.get("complexity_decisions") or {})
            .get(thread_id, {})
            .get("complexity")
            or ""
        ),
    }

    # Identity covers the FROZEN inputs only. ``bound_prd`` is excluded so a
    # later workflow bind does not re-run milestone/communication; prd_result
    # folds a bound marker into its own digest instead (it re-runs on bind).
    snapshot_source = {
        "task": task,
        "task_details": task_details,
        "agent_profiles": profiles,
        "messages": messages,
        "frozen_prd": frozen_prd,
    }
    source_snapshot_digest = stable_digest(snapshot_source)
    bound_digest = stable_digest(bound_prd or {})
    trial_id = source_snapshot_digest[:16]

    packets = {}
    for judge_type in ("milestone", "communication", "prd_result"):
        digest = source_snapshot_digest if judge_type != "prd_result" else stable_digest({
            "source": source_snapshot_digest, "bound": bound_digest,
        })
        packets[judge_type] = build_packet(
            stage=judge_type, run_id=workflow_run_id, workflow_run_id=workflow_run_id,
            trial_id=digest[:16], source_snapshot_digest=digest, task=task,
            task_details=task_details, agent_profiles=profiles, agent_relationships=[],
            iteration_summary=iteration_summary, channel_messages=messages,
            channel_frozen_prd=frozen_prd, workflow_bound_prd=bound_prd or {},
        ).to_dict()
    return {
        "channel_id": channel_id, "workflow_run_id": workflow_run_id,
        "trial_id": trial_id, "freeze": freeze, "packets": packets,
        "source_snapshot_digest": packets["prd_result"]["source_snapshot_digest"],
    }


def _enqueue_channel(state_dir: Path, scheduler: Any, context: dict[str, Any]) -> int:
    input_path = state_dir / EVALUATION_ARTIFACT_ROOT / "queue-inputs" / f"{stable_digest(context)}.json"
    if not input_path.exists():
        atomic_write_text(input_path, json.dumps(context, ensure_ascii=False, indent=2) + "\n")
    run_id = context["workflow_run_id"]
    deps: list[str] = []
    queued = 0
    repair_conclusion = False
    for stage in ("milestone", "communication", "prd_result", "conclusion"):
        packet = context["packets"].get(stage) or context
        digest, trial = packet["source_snapshot_digest"], packet["trial_id"]
        job_id = _request_id(run_id, trial, stage, digest)
        stage_repair = False
        if stage != "conclusion":
            existing = read_judge_result(state_dir, workflow_run_id=run_id,
                                         judge_type=stage, request_id=job_id)
            old_entry = scheduler.entries.get(job_id)
            stage_repair = bool(
                old_entry and old_entry.status == "completed"
                and not is_completed_judge_record(existing)
            )
            repair_conclusion = repair_conclusion or stage_repair
        else:
            stage_repair = repair_conclusion
        queued += int(scheduler.enqueue(EvaluationQueueEntry(
            job_id=job_id, channel_id=context["channel_id"], workflow_run_id=run_id,
            trial_id=trial, judge_type=stage, source_snapshot_digest=digest,
            input_ref=str(input_path.relative_to(state_dir)), depends_on=list(deps),
        ), repair=stage_repair))
        deps.append(job_id)
    return queued


def _evaluate_stage(
    *, state_dir: Path, project_root: Path | None, config: Any | None,
    entry: EvaluationQueueEntry, slot_id: int, backend: Any | None, log: Any,
    stop_event: Event | None = None,
) -> dict[str, Any]:
    context = json.loads((state_dir / entry.input_ref).read_text(encoding="utf-8"))
    run_id, stage = entry.workflow_run_id, entry.judge_type
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    judges = {}
    for judge_type, packet in context["packets"].items():
        request_id = _request_id(run_id, packet["trial_id"], judge_type,
                                 packet["source_snapshot_digest"])
        record = read_judge_result(state_dir, workflow_run_id=run_id,
                                  judge_type=judge_type, request_id=request_id)
        if record is not None:
            judges[judge_type] = record
    conclusion = None
    if stage == "conclusion":
        # An exhausted queue callback can have no verdict sidecar. Treat its
        # absence mechanically, never ask a provider to invent the missing score.
        for judge_type in context["packets"]:
            judges.setdefault(judge_type, {"status": "inconclusive", "result": {},
                                           "error": "Judge 未产出结果"})
        conclusion, ran = _pending_or_run_conclusion(
            state_dir=state_dir, project_root=project_root, config=config,
            writer=writer, backend=backend, log=log, workflow_run_id=run_id,
            trial_id=context["trial_id"], judges=judges, slot_id=slot_id,
            channel_id=entry.channel_id, stop_event=stop_event,
        )
        record = conclusion or {}
    else:
        packet = dict(context["packets"][stage])
        stages = ("milestone", "communication", "prd_result")
        packet["prior_judges"] = [
            {"judge_type": key, "result": (judges[key].get("result") or {})}
            for key in stages[:stages.index(stage)] if key in judges
        ]
        packet_path, _ = write_immutable_packet(state_dir, EvaluationPacket.from_dict(packet))
        ran = not is_completed_judge_record(judges.get(stage))
        if ran:
            log(f"running {stage} judge for {run_id} (slot {slot_id}, packet {packet_path.name})")
        record = run_judge(
            state_dir=state_dir, project_root=project_root, config=config,
            writer=writer, workflow_run_id=run_id, trial_id=entry.trial_id,
            judge_type=stage, packet=packet, backend=backend, log=log,
            slot_id=slot_id, channel_id=entry.channel_id, stop_event=stop_event,
        )
        judges[stage] = record
    # A completed report must never be replaced by an intermediate rescan.
    if ran or conclusion:
        _refresh_report(
            state_dir, workflow_run_id=run_id, trial_id=context["trial_id"],
            channel_id=entry.channel_id, freeze=context["freeze"], judges=judges,
            conclusion=conclusion, source_snapshot_digest=context["source_snapshot_digest"],
        )
    record_status = str(record.get("status") or "")
    return {"judges_run": int(ran), "status": (
        "inconclusive" if record_status == "inconclusive"
        else "completed"
    ), "error": str(record.get("error") or "")}


def render_report(
    *,
    workflow_run_id: str,
    trial_id: str,
    channel_id: str,
    freeze: dict[str, Any],
    judges: dict[str, dict[str, Any]],
    conclusion: dict[str, Any] | None = None,
) -> str:
    """Fixed-order Markdown report per PRD §9 (short, links to raw JSON)."""

    comm = (judges.get("communication") or {}).get("result") or {}
    prd = (judges.get("prd_result") or {}).get("result") or {}
    milestone = (judges.get("milestone") or {}).get("result") or {}
    comm_score = communication_score(comm.get("dimensions") or [])
    prd_score = prd_result_score(prd.get("dimensions") or [])
    overall = overall_score(communication=comm_score, prd_result=prd_score)

    lines = [
        f"# PRD Evaluation Report — {workflow_run_id}",
        "",
        f"- run: `{workflow_run_id}`  trial: `{trial_id}`",
        f"- channel: `{channel_id}`  prd: `{freeze.get('prd_ref') or ''}`",
        f"- status: {(_judge_line_status(judges, conclusion))}",
        "",
        "## 1. Run / Task / Channel / PRD",
        "",
        f"- task_id: `{freeze.get('task_id') or ''}`",
        f"- prd_revision: {freeze.get('prd_revision') or 0}",
        "",
        "## 2. 逐 Agent 作用分析（Milestone Judge）",
        "",
    ]
    findings = milestone.get("agent_findings") or []
    if findings:
        for item in findings:
            lines.append(
                f"- **{item.get('agent_id')}** — score {item.get('score')}, "
                f"necessity `{item.get('necessity')}`, status `{item.get('status')}`: "
                f"{item.get('role_summary')}（{item.get('rationale')}）"
            )
    else:
        lines.append(f"- 无可用结论（status: {judges.get('milestone', {}).get('status', 'pending')}）")
    lines += ["", "## 3. Communication Score（五维度）", ""]
    dims = comm.get("dimensions") or []
    if dims:
        for item in dims:
            lines.append(
                f"- `{item.get('dimension_id')}`: {item.get('score')} "
                f"（{item.get('rationale')}）"
            )
        lines.append(f"- **communication_score: {comm_score}**")
    else:
        lines.append(f"- 无可用分数（status: {judges.get('communication', {}).get('status', 'pending')}）")
    lines += ["", "## 4. PRD Result Score（两项对齐）", ""]
    prd_dims = prd.get("dimensions") or []
    if prd_dims:
        for item in prd_dims:
            lines.append(
                f"- `{item.get('dimension_id')}`: {item.get('score')} "
                f"verdict={item.get('verdict')}（{item.get('rationale')}）"
            )
        for diff in prd.get("differences") or []:
            lines.append(f"- difference: {diff}")
        lines.append(f"- **prd_result_score: {prd_score}**")
    else:
        lines.append(f"- 无可用分数（status: {judges.get('prd_result', {}).get('status', 'pending')}）")
    lines += ["", "## 5. Conclusion / 缺失证据 / 下一步", ""]
    if conclusion:
        result = conclusion.get("result") or {}
        lines.append(f"- status: `{conclusion.get('status')}`")
        lines.append(f"- overall_score: **{result.get('overall_score')}**")
        lines.append(f"- 结论：{result.get('conclusion_zh') or '（无）'}")
        for action in result.get("next_actions") or []:
            lines.append(f"- next: {action}")
    else:
        lines.append("- conclusion：等待三个 Judge 终态")
    missing = _collect_missing(judges)
    if missing:
        lines += ["", "### missing", ""]
        for item in missing:
            lines.append(f"- required: {item['required']}")
            lines.append(f"  reason: {item['reason']}")
    lines += [
        "",
        "## 6. 原始 Judge JSON",
        "",
        f"- judges dir: `artifacts/benchmark-evaluations/judges/{workflow_run_id}/`",
        f"- conclusions dir: `artifacts/benchmark-evaluations/conclusions/{workflow_run_id}/`",
        "",
    ]
    return "\n".join(lines)


def _refresh_report(
    state_dir: Path,
    *,
    workflow_run_id: str,
    trial_id: str,
    channel_id: str,
    freeze: dict[str, Any],
    judges: dict[str, dict[str, Any]],
    conclusion: dict[str, Any] | None = None,
    source_snapshot_digest: str = "",
) -> None:
    from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION

    write_report(
        state_dir, workflow_run_id,
        render_report(
            workflow_run_id=workflow_run_id, trial_id=trial_id,
            channel_id=channel_id, freeze=freeze,
            judges=judges, conclusion=conclusion,
        ),
        prompt_version=EVALUATION_PROMPT_VERSION,
        trial_id=trial_id, source_snapshot_digest=source_snapshot_digest,
        final=conclusion is not None,
    )


def _freeze_points(events: list[Any]) -> dict[str, dict[str, Any]]:
    """Latest PRD freeze point per channel (consensus.reached wins)."""

    points: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type not in FREEZE_TRIGGER_EVENTS or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if event.type == "channel.result.receipt.recorded" and (
            str(payload.get("receipt_kind") or "") != "prd_confirmed"
        ):
            continue
        channel_id = str(payload.get("channel_id") or event.correlation_id or "")
        if not channel_id:
            continue
        current = points.get(channel_id) or {}
        if event.type == "channel.consensus.reached" or not current:
            points[channel_id] = {
                "event_id": event.id,
                "ts": event.ts or "",
                "thread_id": str(payload.get("thread_id") or "main"),
                "prd_ref": str(payload.get("prd_ref") or payload.get("artifact_ref") or ""),
                "prd_digest": str(payload.get("prd_digest") or payload.get("artifact_digest") or ""),
                "prd_revision": int(payload.get("prd_revision") or payload.get("revision") or 0),
                "readiness_ref": str(payload.get("readiness_ref") or ""),
                "task_id": str(payload.get("task_id") or ""),
            }
    return points


def _bound_workflows(events: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Workflow bindings per channel: invoke events carrying a channel task."""

    bound: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event.type not in WORKFLOW_BIND_EVENTS or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        channel_id = str(payload.get("channel_id") or payload.get("correlation_id") or "")
        if not channel_id:
            continue
        bound.setdefault(channel_id, []).append({
            "event_id": event.id,
            "workflow_run_id": str(payload.get("workflow_run_id") or payload.get("run_id") or ""),
            "task_id": str(payload.get("task_id") or ""),
            "prd_ref": str(payload.get("prd_ref") or payload.get("source_ref") or ""),
        })
    return bound


def _frozen_profiles(
    channel: dict[str, Any],
    thread_id: str,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Agent profiles from FROZEN facts only — never runtime state.

    The evaluation identity (§8.2) binds a source snapshot digest; if any
    runtime-volatile field (member connection status, typing, heartbeats)
    leaked in, every observer pass would mint a new digest and re-run the
    judges forever (2026-09-16 incident). Participation authority is the
    discussion roster recorded at discussion start; fallback is the sorted
    set of member ids present in the frozen message slice. Member metadata
    is limited to config-static keys.
    """

    session = (channel.get("discussions") or {}).get(thread_id) or {}
    roster = [
        str(member_id).strip()
        for member_id in (session.get("roster") or [])
        if str(member_id).strip()
    ]
    if not roster:
        roster = sorted({
            str(message.get("member_id") or "")
            for message in messages
            if str(message.get("member_id") or "").strip()
        })
    members = {
        str(member.get("member_id") or ""): member
        for member in (channel.get("members") or [])
        if isinstance(member, dict) and str(member.get("member_id") or "").strip()
    }
    profiles: list[dict[str, Any]] = []
    for member_id in roster:
        member = members.get(member_id) or {}
        profiles.append({
            "agent_id": member_id,
            "channel_role": str(
                member.get("channel_role") or member.get("role") or ""
            ),
            "backend": str(member.get("backend") or ""),
        })
    return profiles


def _packet_messages(channel: dict[str, Any], *, freeze_event_id: str, hydrate: Any) -> list[dict[str, Any]]:
    """Messages up to the freeze event, each bound to an evidence ref."""

    messages: list[dict[str, Any]] = []
    for message in channel.get("messages") or []:
        if not isinstance(message, dict):
            continue
        event_id = str(message.get("event_id") or "")
        if freeze_event_id and event_id == freeze_event_id:
            break
        text = hydrate(message)
        messages.append({
            "evidence_ref": message_evidence_ref(event_id),
            "event_id": event_id,
            "member_id": str(message.get("member_id") or ""),
            "role": str(message.get("role") or ""),
            "text": str(text or ""),
        })
    return messages


def _bound_prd_payload(
    bound_workflows: list[dict[str, Any]],
    frozen_prd: dict[str, Any],
) -> dict[str, Any] | None:
    """v1 workflow-bound PRD view; None until a workflow actually binds."""

    if not bound_workflows:
        return None
    latest = bound_workflows[-1]
    return {
        "workflow_run_id": latest.get("workflow_run_id") or "",
        "task_id": latest.get("task_id") or "",
        "prd_ref": str(latest.get("prd_ref") or frozen_prd.get("prd_ref") or ""),
        "bound_from_channel_prd_ref": frozen_prd.get("prd_ref") or "",
        "body": {"binding_event_id": latest.get("event_id") or ""},
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
        log=log, slot_id=slot_id, channel_id=channel_id, stop_event=stop_event,
    ), True


def _judge_line_status(
    judges: dict[str, dict[str, Any]],
    conclusion: dict[str, Any] | None,
) -> str:
    parts = [
        f"{judge_type}={str(record.get('status') or 'pending')}"
        for judge_type, record in sorted(judges.items())
    ]
    if conclusion:
        parts.append(f"conclusion={conclusion.get('status')}")
    return ", ".join(parts) if parts else "pending"


def _collect_missing(judges: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for judge_type, record in judges.items():
        result = record.get("result") or {}
        for ref in result.get("missing_evidence") or []:
            missing.append({
                "required": str(ref),
                "reason": f"{judge_type} judge reported missing evidence",
            })
        if str(record.get("status") or "") in {"judge_timeout", "inconclusive"}:
            missing.append({
                "required": judge_type,
                "reason": str(record.get("error") or "judge timed out"),
            })
    return missing


def _read_json_artifact(state_dir: Path, ref: str) -> dict[str, Any] | None:
    ref = str(ref or "").strip()
    if not ref:
        return None
    path = Path(state_dir) / ref
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Frozen PRD bodies are Markdown artifacts; keep them body-wrapped
        # so judges still receive the full text inline.
        return {"body": text} if text.strip() else None
    return payload if isinstance(payload, dict) else {"body": payload}


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "observe_loop",
    "observe_once",
    "render_report",
]
