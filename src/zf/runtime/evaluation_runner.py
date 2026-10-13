"""Judge runner: packet -> fixed prompt -> bounded turn -> validated sidecar.

Per the evaluation PRD: agents only produce semantic judgments; this module
owns prompt assembly, the one bounded retry on invalid output (§11), the
timeout-vs-invalid distinction, deterministic score overrides (§4), provider
turn execution, and the two ``benchmark.evaluation.*.completed`` events.
Durable result/report sidecars live in the sibling ``evaluation_artifacts``
module so path identity and read validation stay isolated from transport.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable

from zf.runtime.evaluation_turns import EvaluationCancelled, JudgeTurnObservation
from zf.core.events import EventWriter
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.evaluation_packets import (
    EVALUATION_ARTIFACT_ROOT,
    evaluation_request_id,
    packet_for_prompt,
    stable_digest,
    write_immutable_packet,
)
from zf.runtime.evaluation_artifacts import (
    _canonical,
    _read_json,
    _recover_attempt_archive,
    _recover_sidecar_attempt,
    _rel,
    _safe,
    _write_turn_sidecars,
    conclusion_result_path,
    judge_result_path,
    read_conclusion_result,
    read_judge_result,
    report_path,
    versioned_report_path,
    write_conclusion_result,
    write_judge_result,
    write_report,
)
from zf.runtime.evaluation_prompts import (
    EVALUATION_PROMPT_VERSION,
    JUDGE_TYPES,
    LANE_JUDGE_TYPES,
    SYSTEM_PROMPT,
    evaluation_contract_for_stage,
    prompt_for_stage,
    prompt_version_for_stage,
    require_stage_prompt_version,
)
from zf.runtime import evaluation_lane_packets as lane_packets
from zf.runtime.evaluation_schemas import SCHEMA_VERSIONS, normalize_judge_output
from zf.runtime.evaluation_output import normalize_provider_output
from zf.runtime.evaluation_types import (
    EvaluationPacket,
    JudgeRecord,
)
from zf.runtime.evaluation_config import capture_evaluator_budgets, observed_evaluator_config
from zf.runtime.evaluation_role_binding import role_binding_error

JUDGE_COMPLETED_EVENT = "benchmark.evaluation.judge.completed"
CONCLUSION_COMPLETED_EVENT = "benchmark.evaluation.conclusion.completed"
SUPPORTED_JUDGE_TYPES = frozenset(JUDGE_TYPES) | frozenset(LANE_JUDGE_TYPES)

TERMINAL_JUDGE_STATUSES = frozenset({
    "scored",
    "ambiguous",
    "evidence_missing",
    "abstain",
    "conflict",
    "invalid",
    "judge_timeout",
    "inconclusive",
})

# Records that hold a REAL judge verdict. ``judge_timeout`` is a transport
# failure — nothing was judged — so it must stay retryable; otherwise one
# bad spawn (e.g. the provider never booted) blocks the request identity
# forever and no restart can ever make the evaluation happen
# (2026-09-17 test_dev3 live incident: yesterday's "judge claude did not
# start" records deduped today's entire run).
_COMPLETED_JUDGE_STATUSES = frozenset({
    "inconclusive",
    "ambiguous",
    "scored",
    "evidence_missing",
    "abstain",
    "conflict",
    "invalid",
})
_COMPLETED_CONCLUSION_STATUSES = frozenset({"complete", "invalid", "incomplete", "inconclusive"})


def is_completed_judge_record(record: dict[str, Any] | None) -> bool:
    """True only when the record carries a genuine verdict (not a transport
    failure); completed records are what §8.2 identity dedup protects."""

    return str((record or {}).get("status") or "") in _COMPLETED_JUDGE_STATUSES


def _retry_hint(error: object) -> str:
    """Compact retry feedback for the bounded second attempt.

    The full _RETRY_SUFFIX text pushed the codex turn prompt past the TUI's
    ~1000-char "[Pasted Content]" fold threshold once the error text was
    inlined (2026-09-21), so the retry never submitted. Keep the whole
    retry prompt (hint + packet reference) well under the fold limit.
    """
    return (
        f"Your previous answer failed validation ({str(error)[:60]}). "
        "Return exactly one JSON object matching the requested schema "
        "again. Output only the JSON object."
    )

# Compact persona for codex judges: the interactive TUI folds any input past
# ~1000 chars into a "[Pasted Content]" placeholder (2026-09-21), so the full
# SYSTEM_PROMPT cannot be delivered inline. Schema details live in the packet
# sidecar the prompt points at.
_CODEX_COMPACT_PERSONA = (
    "You are an independent read-only evaluator for ZaoFu PRD generation. "
    "Read only the packet file named below. Cite only evidence_refs listed "
    "inside it; confidence below 0.60 means abstain; missing required "
    "evidence means evidence_missing. Return exactly one JSON object "
    "matching the requested schema; no Markdown, no commentary."
)

JudgeBackend = Callable[..., tuple[str, str] | JudgeTurnObservation]


def _visible_tmux_backend(**kwargs: Any) -> JudgeTurnObservation:
    """One isolated slot/session/rollout per provider launch."""
    from zf.runtime import evaluation_tmux

    state_dir = Path(kwargs["state_dir"]).resolve()
    config = kwargs.get("config")
    tmux_session = str(getattr(getattr(config, "session", None), "tmux_session", "") or "")
    if not tmux_session:
        return JudgeTurnObservation(status="provider_error", error="evaluation judge requires config.session.tmux_session")
    slot_id = kwargs.get("slot_id") or 0
    backend = evaluation_tmux.resolve_judge_backend(config)
    budgets = kwargs.get("evaluator_budgets") or capture_evaluator_budgets()
    # Fresh IDs for retries and schema repairs prevent stale transcript reuse.
    launch_id = str(uuid.uuid4())
    launch_dir = state_dir / EVALUATION_ARTIFACT_ROOT / "sessions" / f"slot-{slot_id}" / launch_id
    launch_dir.mkdir(parents=True, exist_ok=True)
    workspace = launch_dir / "input"
    workspace.mkdir()
    packet_path = Path(kwargs["packet_path"])
    local_packet = workspace / "packet.txt"
    atomic_write_text(local_packet, packet_path.read_text(encoding="utf-8"))
    local_packet.chmod(0o400)
    codex_home = None
    if backend == "codex":
        codex_home = state_dir / "workdirs" / "evaluation" / f"slot-{slot_id}" / launch_id
        codex_home.mkdir(parents=True, mode=0o700)
        # Reuse the configured provider/auth, but never its sessions directory.
        source_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        for name in ("auth.json", "config.toml", "version.json"):
            source = source_home / name
            if source.is_file():
                shutil.copy2(source, codex_home / name)
                (codex_home / name).chmod(0o600)
    binding = {
        "request_id": kwargs["request_id"], "session_uuid": launch_id,
        "session_id": launch_id if backend != "codex" else "",
        "backend": backend, "slot_id": slot_id, "turn_id": "", "rollout_path": "",
        "attempt": kwargs.get("attempt", 1), "schema_attempt": kwargs.get("schema_attempt", 1),
    }
    binding_path = launch_dir / "binding.json"
    atomic_write_text(binding_path, _canonical(binding))
    prompt = str(kwargs["prompt"]).replace(str(packet_path), str(local_packet))

    def observe(observation: JudgeTurnObservation) -> None:
        binding.update(session_id=observation.session_id or binding["session_id"],
                       rollout_path=observation.transcript_ref, turn_id=observation.turn_id)
        atomic_write_text(binding_path, _canonical(binding))
        progress = kwargs.get("progress")
        if progress:
            progress(observation)

    observation = JudgeTurnObservation(status="provider_error", error="judge did not start")
    marker = f"evaluation-turn:{launch_id}"
    if backend == "codex":
        prompt = " ".join(f"{_CODEX_COMPACT_PERSONA} {prompt}".splitlines())
    prompt = f"{marker} {prompt}"
    started_at = time.time()
    try:
        ready, error = evaluation_tmux.start_judge_agent(
            tmux_session, workspace, session_uuid=launch_id,
            system_prompt=str(kwargs.get("system_prompt") or ""), backend=backend,
            slot_id=slot_id, codex_home=codex_home,
            stop_event=kwargs.get("stop_event"),
            initial_prompt=prompt if backend == "codex" else "",
            ready_timeout_s=float(budgets["timeouts"]["start_seconds"]),
        )
        if not ready:
            return JudgeTurnObservation(status="provider_error", error=error)
        if backend != "codex":
            started_at = time.time()
            evaluation_tmux.send_prompt_to_window(
                tmux_session, prompt, backend=backend, slot_id=slot_id,
            )
        observation = evaluation_tmux.read_judge_reply(
            workspace, launch_id,
            timeout_s=float(budgets["timeouts"]["turn_seconds"]),
            backend=backend, started_at=started_at, request_marker=marker,
            codex_sessions_root=codex_home / "sessions" if codex_home else None,
            log=kwargs.get("log"), on_observation=observe,
            stop_event=kwargs.get("stop_event"),
            evaluator_budgets=budgets,
        )
        observe(observation)
        return observation
    finally:
        # A timed-out/runaway turn must actually stop before its slot is reused.
        if observation.status != "completed":
            evaluation_tmux.stop_judge_agent(tmux_session, backend=backend, slot_id=slot_id)


def _write_judge_prompt_sidecar(
    state_dir: Path,
    *,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
    rendered: str,
) -> Path:
    """Persist the rendered judge prompt; the TUI backend reads the FILE.

    Codex's TUI replaces any large pasted input with a literal
    "[Pasted Content N chars]" placeholder (observed from ~2 KiB up) — the
    model then judges the placeholder, not the packet. Delivering a short
    path reference keeps the TUI input tiny; the judge reads the sidecar
    itself from a private input directory with read-only provider permissions.
    """
    directory = (
        Path(state_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "judges"
        / _safe(workflow_run_id)
        / _safe(judge_type)
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe(request_id)}.packet.txt"
    atomic_write_text(path, rendered)
    return path


def _prompt_file_reference(
    path: Path, *, judge_type: str, schema_version: str | None = None,
) -> str:
    schema = schema_version or SCHEMA_VERSIONS.get(judge_type, f"{judge_type}_result.v2")
    # Single line on purpose: codex's input box treats Enter as newline when
    # the draft contains line breaks (submit needs Ctrl+Enter there), while
    # a single-line draft submits on plain Enter — which is the only submit
    # key tmux can send uniformly across backends.
    return (
        f"Read the evaluation packet rendered at this exact file path: "
        f"{path} — that file is the complete, immutable review input. "
        f"Base every judgment strictly on its contents, cite only "
        f"evidence_refs listed inside it, and return exactly one JSON "
        f"object matching the {schema} schema. Do not inspect any other "
        f"file."
    )


def _prompt_template(judge_type: str, packet: Any) -> str:
    del packet
    return prompt_for_stage(judge_type)


def _schema_version(judge_type: str, packet: Any) -> str:
    del packet
    try:
        return SCHEMA_VERSIONS[judge_type]
    except KeyError as exc:
        raise KeyError(f"unknown evaluation judge type: {judge_type}") from exc


def run_judge(
    *,
    state_dir: Path,
    project_root: Path | None,
    config: Any | None,
    writer: EventWriter,
    workflow_run_id: str,
    trial_id: str,
    judge_type: str,
    packet: EvaluationPacket | dict[str, Any],
    backend: JudgeBackend | None = None,
    log: Any | None = None,
    slot_id: int | None = None,
    channel_id: str = "",
    expected_role_ref: str = "",
    expected_role_digest: str = "",
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """Run one judge request end to end and emit its completed event."""

    if judge_type not in SUPPORTED_JUDGE_TYPES:
        raise ValueError(
            f"unsupported evaluation judge type retired; not dispatched: {judge_type}"
        )
    log = log or (lambda message: None)
    prompt_version = require_stage_prompt_version(judge_type, packet)
    evaluation_contract = evaluation_contract_for_stage(judge_type)
    request_id = evaluation_request_id(
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        judge_type=judge_type,
        source_snapshot_digest=str(packet.get("source_snapshot_digest") or ""),
        prompt_version=prompt_version,
    )
    existing = read_judge_result(
        state_dir, workflow_run_id=workflow_run_id, judge_type=judge_type,
        request_id=request_id,
        expected_trial_id=trial_id,
        expected_source_snapshot_digest=str(packet.get("source_snapshot_digest") or ""),
        expected_role_ref=expected_role_ref,
        expected_role_digest=expected_role_digest,
    )
    if is_completed_judge_record(existing):
        return existing  # §8.2: a real verdict is permanent for this identity
    raw_existing = _read_json(judge_result_path(
        state_dir, workflow_run_id=workflow_run_id, judge_type=judge_type,
        request_id=request_id,
    ))
    recovery = _recover_sidecar_attempt(
        raw_existing, workflow_run_id=workflow_run_id, judge_type=judge_type,
        request_id=request_id, trial_id=trial_id,
        source_snapshot_digest=str(packet.get("source_snapshot_digest") or ""),
    )
    if recovery is not None and recovery[0] >= recovery[1]:
        result = _judge_record(
            judge_type=judge_type, workflow_run_id=workflow_run_id,
            trial_id=trial_id, request_id=request_id, packet=packet,
            status="inconclusive", result={},
            error="invalid judge sidecar; bounded recovery exhausted",
            provider="deterministic", model="unknown",
        )
        result.update(attempt=recovery[0], max_attempts=recovery[1],
                      retryable=False, inconclusive_after=recovery[1])
        path = write_judge_result(
            state_dir, workflow_run_id=workflow_run_id,
            judge_type=judge_type, request_id=request_id, record=result,
        )
        writer.emit(
            JUDGE_COMPLETED_EVENT, actor="evaluation-runner",
            correlation_id=workflow_run_id,
            payload={
                "schema_version": "benchmark.evaluation.judge.v1",
                "evaluation_contract": evaluation_contract,
                "request_id": request_id, "channel_id": channel_id,
                "attempt": result["attempt"], "max_attempts": result["max_attempts"],
                "judge_type": judge_type, "workflow_run_id": workflow_run_id,
                "trial_id": trial_id, "run_id": str(packet.get("run_id") or ""),
                "status": result["status"],
                "source_snapshot_digest": str(packet.get("source_snapshot_digest") or ""),
                "prompt_version": prompt_version,
                "freeze_event_id": str((packet.get("task_details") or {}).get("freeze_event_id") or ""),
                "artifact_ref": _rel(path, state_dir),
                "artifact_digest": stable_digest(result), "error": result["error"],
            },
        )
        return result
    if existing is None and recovery is not None:
        existing = raw_existing

    prompt_view = packet_for_prompt(packet)
    prompt_template = _prompt_template(judge_type, packet)
    rendered = prompt_template.format(
        task=json.dumps(prompt_view.get("task"), ensure_ascii=False, default=str),
        task_type=json.dumps(prompt_view.get("task_type") or "", ensure_ascii=False, default=str),
        role_ref=json.dumps(prompt_view.get("role_ref") or "", ensure_ascii=False, default=str),
        task_details=json.dumps(prompt_view.get("task_details"), ensure_ascii=False, default=str),
        agent_profiles=json.dumps(prompt_view.get("agent_profiles"), ensure_ascii=False, default=str),
        agent_relationships=json.dumps(prompt_view.get("agent_relationships"), ensure_ascii=False, default=str),
        contribution_result=json.dumps(
            _prior_judge(prompt_view, "contribution"), ensure_ascii=False, default=str,
        ),
        discussion_result=json.dumps(
            _prior_judge(prompt_view, "discussion"), ensure_ascii=False, default=str,
        ),
        prd_fidelity_result=json.dumps(
            _prior_judge(prompt_view, "prd_fidelity"), ensure_ascii=False, default=str,
        ),
        channel_messages=json.dumps(prompt_view.get("channel_messages"), ensure_ascii=False, default=str),
        channel_frozen_prd=json.dumps(prompt_view.get("channel_frozen_prd"), ensure_ascii=False, default=str),
        workflow_bound_prd=json.dumps(prompt_view.get("workflow_bound_prd"), ensure_ascii=False, default=str),
        allowed_evidence_refs=json.dumps(
            prompt_view.get("allowed_evidence_refs"), ensure_ascii=False, default=str,
        ),
        **lane_packets.lane_template_values(prompt_view),
    )
    packet_path = _write_judge_prompt_sidecar(
        state_dir,
        workflow_run_id=workflow_run_id,
        judge_type=judge_type,
        request_id=request_id,
        rendered=rendered,
    )
    prompt_ref = _prompt_file_reference(
        packet_path, judge_type=judge_type,
        schema_version=_schema_version(judge_type, packet),
    )

    missing = lane_packets.missing_stage_inputs(judge_type, packet)
    if missing:
        result = _judge_record(
            judge_type=judge_type, workflow_run_id=workflow_run_id, trial_id=trial_id,
            request_id=request_id, packet=packet, status="evidence_missing",
            result=lane_packets.missing_input_result(
                judge_type, schema_version=_schema_version(judge_type, packet), missing=missing,
            ),
            error="missing evaluation input: " + ", ".join(missing),
            provider="deterministic", model="unknown",
        )
        result["attempt"] = 0
    else:
        result = _run_request_turns(
            state_dir=state_dir, project_root=project_root, config=config, writer=writer,
            workflow_run_id=workflow_run_id, trial_id=trial_id, judge_type=judge_type,
            request_id=request_id, packet=packet, prompt_ref=prompt_ref, packet_path=packet_path, existing=existing,
            backend=backend or _visible_tmux_backend, log=log,
            slot_id=slot_id, channel_id=channel_id, stop_event=stop_event,
        )
    status, error = result["status"], result["error"]
    path = write_judge_result(
        state_dir, workflow_run_id=workflow_run_id,
        judge_type=judge_type, request_id=request_id, record=result,
    )
    writer.emit(
        JUDGE_COMPLETED_EVENT,
        actor="evaluation-runner",
        correlation_id=workflow_run_id,
        payload={
            "schema_version": "benchmark.evaluation.judge.v1",
            "evaluation_contract": evaluation_contract,
            "request_id": request_id,
            "channel_id": channel_id,
            "attempt": result["attempt"],
            "max_attempts": result["max_attempts"],
            "judge_type": judge_type,
            "workflow_run_id": workflow_run_id,
            "trial_id": trial_id,
            "run_id": str(packet.get("run_id") or ""),
            "status": status,
            "source_snapshot_digest": str(packet.get("source_snapshot_digest") or ""),
            "prompt_version": prompt_version,
            "freeze_event_id": str((packet.get("task_details") or {}).get("freeze_event_id") or ""),
            "artifact_ref": _rel(path, state_dir),
            "artifact_digest": stable_digest(result),
            "error": error,
        },
    )
    return result


def run_conclusion(
    *,
    state_dir: Path,
    project_root: Path | None,
    config: Any | None,
    writer: EventWriter,
    workflow_run_id: str,
    trial_id: str,
    judges: dict[str, dict[str, Any]],
    backend: JudgeBackend | None = None,
    log: Any | None = None,
    slot_id: int | None = None,
    channel_id: str = "",
    freeze_event_id: str = "",
    queue_job_id: str = "",
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """Run the conclusion agent once all three judges have terminal results."""

    log = log or (lambda message: None)
    snapshot_digest = stable_digest({key: judges.get(key) for key in sorted(judges)})
    request_id = evaluation_request_id(
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        judge_type="conclusion",
        source_snapshot_digest=snapshot_digest,
    )
    existing = read_conclusion_result(state_dir, workflow_run_id, request_id)
    if existing is not None and str(existing.get("status") or "") in (
        _COMPLETED_CONCLUSION_STATUSES
    ):
        return existing
    raw_existing = _read_json(conclusion_result_path(state_dir, workflow_run_id, request_id))
    recovery = _recover_sidecar_attempt(
        raw_existing, workflow_run_id=workflow_run_id, judge_type="conclusion",
        request_id=request_id, trial_id=trial_id, source_snapshot_digest=snapshot_digest,
    )
    if recovery is None:
        raw_existing = _recover_attempt_archive(
            conclusion_result_path(state_dir, workflow_run_id, request_id),
            workflow_run_id=workflow_run_id, judge_type="conclusion",
            request_id=request_id, trial_id=trial_id,
            source_snapshot_digest=snapshot_digest,
        )
        recovery = _recover_sidecar_attempt(
            raw_existing, workflow_run_id=workflow_run_id, judge_type="conclusion",
            request_id=request_id, trial_id=trial_id, source_snapshot_digest=snapshot_digest,
        )
    if existing is None and recovery is not None:
        existing = raw_existing

    discussion = judges.get("discussion") or {}
    prd_fidelity = judges.get("prd_fidelity") or {}
    contribution = judges.get("contribution") or {}
    classification = (judges.get("task_type") or {}).get("result") or {}
    role_record = next(
        (
            judges.get(stage) or {}
            for stage in ("contribution", "discussion", "prd_fidelity")
            if str((judges.get(stage) or {}).get("role_ref") or "").strip()
            or str((judges.get(stage) or {}).get("role_digest") or "").strip()
        ),
        {},
    )
    role_result = role_record.get("result") if isinstance(role_record.get("result"), dict) else {}
    packet = {
        "schema_version": "zaofu-prd-evaluation-input.v1",
        "run_id": workflow_run_id,
        "workflow_run_id": workflow_run_id,
        "trial_id": trial_id,
        "stage": "conclusion",
        "source_snapshot_digest": stable_digest({key: judges.get(key) for key in sorted(judges)}),
        "prompt_version": EVALUATION_PROMPT_VERSION,
        "contribution_result": contribution,
        "discussion_result": discussion,
        "prd_fidelity_result": prd_fidelity,
        "task_type": str(classification.get("task_type") or role_result.get("task_type") or ""),
        "task_type_confidence": classification.get("confidence"),
        "role_ref": str(
            role_result.get("role_ref")
            or (role_result.get("role_metadata") or {}).get("role_ref")
            or role_record.get("role_ref")
            or role_result.get("role_id")
            or ""
        ),
        "role_digest": str(
            role_result.get("role_digest")
            or (role_result.get("role_metadata") or {}).get("role_digest")
            or role_record.get("role_digest")
            or ""
        ),
        "score_scale": "0-4",
    }
    conclusion_evidence_refs = sorted({
        ref
        for value in judges.values()
        for ref in (value.get("evidence_refs") or [])
        if isinstance(ref, str) and ref
    } | {
        ref
        for value in judges.values()
        for item in ((value.get("result") or {}).get("dimensions") or [])
                    + ((value.get("result") or {}).get("agent_findings") or [])
        for ref in (item.get("evidence_refs") or [])
        if isinstance(ref, str) and ref
    })
    conclusion_input = EvaluationPacket(
        schema_version="zaofu-prd-evaluation-input.v1",
        run_id=workflow_run_id,
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        stage="conclusion",
        source_snapshot_digest=packet["source_snapshot_digest"],
        prompt_version=EVALUATION_PROMPT_VERSION,
        task={"task_type": packet.get("task_type") or ""},
        task_details={
            "source": "main-judge-aggregation",
            "task_type": packet.get("task_type") or "",
            "role_ref": packet.get("role_ref") or "",
            "freeze_event_id": freeze_event_id,
        },
        prior_judges=[
            judges.get(stage) or {}
            for stage in ("contribution", "discussion", "prd_fidelity")
        ],
        allowed_evidence_refs=conclusion_evidence_refs,
        task_type=str(packet.get("task_type") or ""),
        task_type_confidence=packet.get("task_type_confidence"),
        role_ref=str(packet.get("role_ref") or ""),
        role_digest=str(packet.get("role_digest") or ""),
        evaluator_profile="main_judge",
        subagent_scope="conclusion",
    )
    conclusion_input_path, conclusion_input_digest = write_immutable_packet(
        state_dir, conclusion_input,
    )
    packet["input_ref"] = _rel(conclusion_input_path, state_dir)
    packet["input_digest"] = conclusion_input_digest
    failed = {key: judges.get(key) or {} for key in ("contribution", "discussion", "prd_fidelity")
              if (judges.get(key) or {}).get("status") != "scored"}
    conclusion_fields = {
        "discussion": ("discussion_meaningful", "discussion_progressed"),
        "prd_fidelity": ("prd_ready_for_next_stage",),
    }
    copied_team_findings: dict[str, bool] = {}
    for stage, fields in conclusion_fields.items():
        record = judges.get(stage) or {}
        child = record.get("result") if isinstance(record.get("result"), dict) else {}
        missing = [field for field in fields if type(child.get(field)) is not bool]
        if missing:
            failed[stage] = {
                **record,
                "error": str(record.get("error") or "")
                or "missing role-bound conclusion field: " + ", ".join(missing),
            }
            continue
        copied_team_findings.update({field: child[field] for field in fields})
    if failed:
        error = "; ".join(f"{key}: {value.get('error') or value.get('status')}"
                          for key, value in failed.items())
        status = "inconclusive" if any(
            value.get("status") not in _COMPLETED_JUDGE_STATUSES
            or value.get("status") == "inconclusive" for value in failed.values()
        ) else "incomplete"
        result = _judge_record(
            judge_type="conclusion", workflow_run_id=workflow_run_id,
            trial_id=trial_id, request_id=request_id, packet=packet,
            status=status, result={}, error=error, provider="deterministic", model="unknown",
        )
    else:
        rendered = _prompt_template("conclusion", packet).format(
            task_type=json.dumps(packet.get("task_type") or "", ensure_ascii=False),
            role_ref=json.dumps(packet.get("role_ref") or "", ensure_ascii=False),
            contribution_result=json.dumps(contribution, ensure_ascii=False, default=str),
            discussion_result=json.dumps(discussion, ensure_ascii=False, default=str),
            prd_fidelity_result=json.dumps(prd_fidelity, ensure_ascii=False, default=str),
        )
        packet_path = _write_judge_prompt_sidecar(
            state_dir, workflow_run_id=workflow_run_id, judge_type="conclusion",
            request_id=request_id, rendered=rendered,
        )
        result = _run_request_turns(
            state_dir=state_dir, project_root=project_root, config=config, writer=writer,
            workflow_run_id=workflow_run_id, trial_id=trial_id, judge_type="conclusion",
            request_id=request_id, packet=packet, existing=existing,
            prompt_ref=_prompt_file_reference(
                packet_path, judge_type="conclusion",
                schema_version=_schema_version("conclusion", packet),
            ),
            packet_path=packet_path,
            backend=backend or _visible_tmux_backend, log=log,
            slot_id=slot_id, channel_id=channel_id, stop_event=stop_event,
        )
    status, error = result["status"], result["error"]
    raw = result["result"]
    if failed or status == "inconclusive":
        raw.update(schema_version=_schema_version("conclusion", packet), judge_type="conclusion",
                   status=status, score_scale="0-4", agent_scores=[],
                   conclusion_zh="Judge 结果未全部通过评分门禁，暂不形成汇总结论。",
                   team_findings={"problems": [error],
                                  "underperformed_roles": [], "unnecessary_roles": []},
                   next_actions=["检查失败状态和缺失证据；修复后重新测评，保留旧报告。"],
                   judge_refs=[], evidence_refs=[])
    if raw:
        contribution = judges.get("contribution") or {}
        findings = (contribution.get("result") or {}).get("agent_findings") or []
        raw["agent_scores"] = [{
            "agent_id": item.get("agent_id"),
            "score": item.get("score") if contribution.get("status") == "scored" and item.get("status") == "scored" else None,
            "necessity": item.get("necessity"),
            "necessity_status": item.get("necessity_status") or "complete",
        } for item in findings]
        team = raw.get("team_findings")
        if not failed and isinstance(team, dict):
            # These determinations belong to their role-bound subagents.  The
            # main Judge may narrate them, but cannot replace their values.
            team.update(copied_team_findings)
        raw["judge_refs"] = [value["request_id"] for value in judges.values() if value.get("request_id")]
        raw["evidence_refs"] = sorted({ref for value in judges.values()
            for item in ((value.get("result") or {}).get("dimensions") or [])
                        + ((value.get("result") or {}).get("agent_findings") or [])
            for ref in item.get("evidence_refs") or [] if item.get("status") == "scored"})
    result["packet_ref"] = packet["input_ref"]
    result["packet_digest"] = packet["input_digest"]
    # Keep the immutable input binding in the conclusion sidecar itself; the
    # semantic scores remain untouched.
    path = write_conclusion_result(state_dir, workflow_run_id, request_id, result)
    writer.emit(
        CONCLUSION_COMPLETED_EVENT,
        actor="evaluation-runner",
        correlation_id=workflow_run_id,
        payload={
            "schema_version": "benchmark.evaluation.conclusion.v1",
            "evaluation_contract": "channel-prd-eval.v1",
            "channel_id": channel_id,
            "job_id": queue_job_id,
            "judge_type": "conclusion",
            "attempt": result["attempt"],
            "max_attempts": result["max_attempts"],
            "request_id": request_id,
            "workflow_run_id": workflow_run_id,
            "trial_id": trial_id,
            "source_snapshot_digest": str(packet.get("source_snapshot_digest") or ""),
            "prompt_version": EVALUATION_PROMPT_VERSION,
            "freeze_event_id": freeze_event_id,
            "status": status,
            "artifact_ref": _rel(path, state_dir),
            "artifact_digest": stable_digest(result),
            "error": error,
        },
    )
    return result


def _run_request_turns(
    *, state_dir: Path, project_root: Path | None, config: Any | None,
    writer: EventWriter, workflow_run_id: str, trial_id: str, judge_type: str,
    request_id: str, packet: Any, prompt_ref: str, packet_path: Path, existing: dict[str, Any] | None,
    backend: JudgeBackend, log: Any, slot_id: int | None, channel_id: str,
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """Persist transport attempts before launch; schema repair has its own cap."""
    from zf.runtime.evaluation_scheduler import PROGRESS_EVENT
    from zf.runtime.evaluation_tmux import resolve_judge_backend

    prompt_version = str(packet.get("prompt_version") or "") or prompt_version_for_stage(judge_type)
    max_attempts = 2
    previous = int((existing or {}).get("attempt") or (1 if existing else 0))
    path = (conclusion_result_path(state_dir, workflow_run_id, request_id)
            if judge_type == "conclusion" else judge_result_path(
                state_dir, workflow_run_id=workflow_run_id, judge_type=judge_type, request_id=request_id))
    provider = f"tmux-{resolve_judge_backend(config)}" if backend is _visible_tmux_backend else "injected"
    backend_name = resolve_judge_backend(config) if backend is _visible_tmux_backend else "injected"
    budgets = capture_evaluator_budgets()
    base_config = observed_evaluator_config(
        backend=backend_name, provider=provider, prompt_version=prompt_version,
        prompt_digest=stable_digest(_prompt_template(judge_type, packet)),
        system_prompt_digest=stable_digest(SYSTEM_PROMPT),
        role_ref=str(packet.get("role_ref") or ""), role_digest=str(packet.get("role_digest") or ""),
        max_attempts=max_attempts, schema_max_attempts=2, budgets=budgets,
    )
    record = _judge_record(
        judge_type=judge_type, workflow_run_id=workflow_run_id, trial_id=trial_id,
        request_id=request_id, packet=packet, status="inconclusive", result=None,
        error=str((existing or {}).get("error") or "provider retry budget exhausted"),
        provider=provider, model="unknown",
    )
    record.update(attempt=min(previous, max_attempts), max_attempts=max_attempts,
                  retryable=False, inconclusive_after=max_attempts)
    record.update(evaluator_config=base_config, config_digest=base_config["config_digest"], model_source="unknown")
    attempts_log: list[tuple[int, str, str, str]] = []

    def progress(observed: JudgeTurnObservation | None = None) -> None:
        payload = {key: record[key] for key in (
            "request_id", "workflow_run_id", "trial_id", "judge_type", "status",
            "attempt", "max_attempts", "retryable", "error",
        )}
        payload.update(channel_id=channel_id, slot_id=slot_id)
        if observed:
            payload.update(turn_id=observed.turn_id, phase=observed.phase,
                           last_activity_at=observed.last_activity_at,
                           tool_calls=observed.tool_calls, transcript_bytes=observed.transcript_bytes)
        writer.emit(PROGRESS_EVENT, actor="evaluation-runner",
                    correlation_id=workflow_run_id, payload=payload)

    for attempt in range(previous + 1, max_attempts + 1):
        if stop_event is not None and stop_event.is_set():
            raise EvaluationCancelled("evaluation observer stopped")
        record.update(status="running", attempt=attempt, retryable=False,
                      inconclusive_after=max_attempts, error="", result={},
                      retry_of=f"{request_id}:attempt-{attempt - 1}" if attempt > 1 else "")
        # Crash recovery consumes this attempt even if the process dies before
        # returning a verdict. Repeated observer restarts cannot reset budgets.
        atomic_write_text(path, _canonical(record))
        progress()
        transport_failed = False
        for schema_attempt in (1, 2):
            prompt = prompt_ref if schema_attempt == 1 else f"{_retry_hint(record['error'])} {prompt_ref}"
            record.update(status="running", error="")
            record.pop("output_repair", None)
            turn_path = path.parent / "attempts" / f"{request_id}-attempt-{attempt}-schema-{schema_attempt}.json"
            turn = {"attempt": attempt, "schema_attempt": schema_attempt,
                    "system_prompt": SYSTEM_PROMPT, "prompt": prompt, "status": "running"}
            atomic_write_text(turn_path, _canonical(turn))
            try:
                observed = backend(
                    state_dir=state_dir, project_root=project_root, config=config,
                    workflow_run_id=workflow_run_id, judge_type=judge_type,
                    request_id=request_id, system_prompt=SYSTEM_PROMPT, prompt=prompt, packet_path=packet_path,
                    log=log, attempt=attempt, schema_attempt=schema_attempt,
                    slot_id=slot_id, progress=progress,
                    evaluator_budgets=budgets,
                    stop_event=stop_event,
                )
                if not isinstance(observed, JudgeTurnObservation):
                    reply, error = observed
                    observed = JudgeTurnObservation(status="provider_error" if error else "completed",
                                                    reply=str(reply or ""), error=str(error or ""))
            except EvaluationCancelled:
                raise
            except Exception as exc:
                observed = JudgeTurnObservation(status="provider_error", error=f"judge backend error: {exc}")
            turn_config = observed_evaluator_config(
                backend=backend_name, provider=provider,
                observed_model=observed.observed_model,
                observed_model_source=observed.observed_model_source,
                prompt_version=prompt_version,
                prompt_digest=stable_digest(_prompt_template(judge_type, packet)),
                system_prompt_digest=stable_digest(SYSTEM_PROMPT),
                role_ref=str(packet.get("role_ref") or ""), role_digest=str(packet.get("role_digest") or ""),
                max_attempts=max_attempts, schema_max_attempts=2, budgets=budgets,
            )
            record.update(turn_id=observed.turn_id, transcript_ref=observed.transcript_ref,
                          model=turn_config["model"], model_source=turn_config["model_source"],
                          evaluator_config=turn_config, config_digest=turn_config["config_digest"])
            turn["evaluator_config"] = turn_config
            turn.update(vars(observed))
            atomic_write_text(turn_path, _canonical(turn))
            attempts_log.append((attempt * 10 + schema_attempt, SYSTEM_PROMPT, prompt, observed.reply))
            if observed.status != "completed":
                transport_failed = True
                record.update(status="judge_timeout" if observed.status == "timeout" else observed.status,
                              error=observed.error or f"provider turn {observed.status}",
                              retryable=attempt < max_attempts)
                break
            parsed = _parse_json_object(observed.reply)
            profiles = packet.get("agent_profiles") or []
            participants = [item.get("agent_id") for item in profiles if item.get("agent_id")]
            if not profiles:
                participants = sorted({item["member_id"] for item in packet.get("channel_messages") or []
                                       if item.get("member_id")})
            normalized, error, repair = (normalize_provider_output(
                judge_type=judge_type, raw=parsed,
                allowed_evidence_refs=list(packet.get("allowed_evidence_refs") or []),
                expected_agent_ids=participants if judge_type == "contribution" else None,
                workflow_bound=bool(packet.get("workflow_bound_prd")),
                expected_schema_version=_schema_version(judge_type, packet),
                cross_compare_context=lane_packets.cross_compare_context_from_packet(packet),
            ) if parsed is not None else (None, f"judge returned no single JSON object: {observed.reply[:240]}", {}))
            if repair:
                turn["output_repair"] = record["output_repair"] = repair
                atomic_write_text(turn_path, _canonical(turn))
            if normalized is not None and judge_type in {"contribution", "discussion", "prd_fidelity"}:
                role_error = role_binding_error(normalized.to_dict(), packet)
                if role_error:
                    normalized = None
                    error = role_error
            record.update(status="invalid", error=error)
            if normalized is not None:
                record.update(result=normalized.to_dict(), status=normalized.status,
                              error="invalid evidence references" if normalized.status == "invalid" else "")
                if normalized.status != "invalid":
                    break
        archive = path.parent / "attempts" / f"{request_id}-attempt-{attempt}.json"
        if not archive.exists():
            atomic_write_text(archive, _canonical(record))
        if transport_failed:
            record.update(status="retryable_failure" if attempt < max_attempts else "inconclusive",
                          inconclusive_after=max_attempts)
            if attempt < max_attempts:
                atomic_write_text(path, _canonical(record))
            progress(observed)
        if not transport_failed:
            break
    _write_turn_sidecars(state_dir, workflow_run_id=workflow_run_id, judge_type=judge_type,
                         request_id=request_id, attempts_log=attempts_log)
    return record


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1)

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON key")
        return result

    def invalid_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"non-finite JSON number: {value}")
        return parsed

    try:
        parsed = json.loads(stripped, object_pairs_hook=unique_keys,
                            parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _judge_record(
    *,
    judge_type: str,
    workflow_run_id: str,
    trial_id: str,
    request_id: str,
    packet: EvaluationPacket | dict[str, Any],
    status: str,
    result: dict[str, Any] | None,
    error: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    packet_dict = packet.to_dict() if isinstance(packet, EvaluationPacket) else packet
    prompt_version = str(packet_dict.get("prompt_version") or "") or prompt_version_for_stage(judge_type)
    record = JudgeRecord(
        schema_version="benchmark.evaluation.record.v1",
        judge_type=judge_type,
        status=status,
        request_id=request_id,
        run_id=str(packet_dict.get("run_id") or workflow_run_id),
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        source_snapshot_digest=str(packet_dict.get("source_snapshot_digest") or ""),
        prompt_version=prompt_version,
        prompt_digest=stable_digest(_prompt_template(judge_type, packet_dict)),
        provider=provider,
        model=model,
        created_at=datetime.now(timezone.utc).isoformat(),
        error=error,
        result=result or {},
        evidence_refs=list(packet_dict.get("allowed_evidence_refs") or []),
    ).to_dict()
    record.update(
        evaluation_contract=evaluation_contract_for_stage(judge_type),
        freeze_event_id=str((packet_dict.get("task_details") or {}).get("freeze_event_id") or ""),
        role_ref=str(packet_dict.get("role_ref") or ""),
        role_digest=str(packet_dict.get("role_digest") or ""),
        evaluator_profile=str(packet_dict.get("evaluator_profile") or ""),
        task_type=str(packet_dict.get("task_type") or ""),
    )
    if provider == "deterministic":
        audit = observed_evaluator_config(
            backend=provider, provider=provider, observed_model_source="not_applicable",
            prompt_version=prompt_version, prompt_digest=record["prompt_digest"],
            system_prompt_digest=stable_digest(SYSTEM_PROMPT),
            role_ref=record["role_ref"], role_digest=record["role_digest"],
        )
        record.update(evaluator_config=audit, config_digest=audit["config_digest"],
                      model_source="not_applicable")
    return record


def _prior_judge(prompt_view: dict[str, Any], judge_type: str) -> Any:
    for item in prompt_view.get("prior_judges") or []:
        if isinstance(item, dict) and item.get("judge_type") == judge_type:
            return item.get("result")
    return None


__all__ = [
    "CONCLUSION_COMPLETED_EVENT",
    "JUDGE_COMPLETED_EVENT",
    "TERMINAL_JUDGE_STATUSES",
    "conclusion_result_path",
    "judge_result_path",
    "read_conclusion_result",
    "read_judge_result",
    "report_path",
    "run_conclusion",
    "run_judge",
    "versioned_report_path",
    "write_conclusion_result",
    "write_judge_result",
    "write_report",
]
