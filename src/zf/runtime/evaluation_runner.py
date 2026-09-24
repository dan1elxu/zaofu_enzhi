"""Judge runner: packet -> fixed prompt -> bounded turn -> validated sidecar.

Per the evaluation PRD: agents only produce semantic judgments; this module
owns prompt assembly, the one bounded retry on invalid output (§11), the
timeout-vs-invalid distinction, deterministic score overrides (§4), sidecar
persistence under ``artifacts/benchmark-evaluations/`` (§8.3), the two
``benchmark.evaluation.*.completed`` events, and the ``report.md`` reading
projection (§9).
"""

from __future__ import annotations

import json
import os
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
)
from zf.runtime.evaluation_prompts import (
    EVALUATION_PROMPT_VERSION,
    STAGE_PROMPTS,
    SYSTEM_PROMPT,
)
from zf.runtime.evaluation_schemas import (
    SCHEMA_VERSIONS,
    communication_score,
    normalize_judge_output,
    overall_score,
    prd_result_score,
)
from zf.runtime.evaluation_types import (
    EvaluationPacket,
    JudgeRecord,
)

JUDGE_COMPLETED_EVENT = "benchmark.evaluation.judge.completed"
CONCLUSION_COMPLETED_EVENT = "benchmark.evaluation.conclusion.completed"

TERMINAL_JUDGE_STATUSES = frozenset({
    "scored",
    "evidence_missing",
    "abstain",
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
    "scored",
    "evidence_missing",
    "abstain",
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

    state_dir = Path(kwargs["state_dir"])
    project_root = Path(kwargs.get("project_root") or Path.cwd())
    config = kwargs.get("config")
    tmux_session = str(getattr(getattr(config, "session", None), "tmux_session", "") or "")
    if not tmux_session:
        return JudgeTurnObservation(status="provider_error", error="evaluation judge requires config.session.tmux_session")
    slot_id = kwargs.get("slot_id") or 0
    backend = evaluation_tmux.resolve_judge_backend(config)
    # Fresh IDs for retries and schema repairs prevent stale transcript reuse.
    launch_id = str(uuid.uuid4())
    launch_dir = state_dir / EVALUATION_ARTIFACT_ROOT / "sessions" / f"slot-{slot_id}" / launch_id
    launch_dir.mkdir(parents=True, exist_ok=True)
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

    def observe(observation: JudgeTurnObservation) -> None:
        binding.update(session_id=observation.session_id or binding["session_id"],
                       rollout_path=observation.transcript_ref, turn_id=observation.turn_id)
        atomic_write_text(binding_path, _canonical(binding))
        progress = kwargs.get("progress")
        if progress:
            progress(observation)

    observation = JudgeTurnObservation(status="provider_error", error="judge did not start")
    try:
        ready, error = evaluation_tmux.start_judge_agent(
            tmux_session, project_root, session_uuid=launch_id,
            system_prompt=str(kwargs.get("system_prompt") or ""), backend=backend,
            slot_id=slot_id, codex_home=codex_home,
            stop_event=kwargs.get("stop_event"),
            ready_timeout_s=float(os.environ.get("ZF_EVALUATION_JUDGE_START_TIMEOUT_S", "120")),
        )
        if not ready:
            return JudgeTurnObservation(status="provider_error", error=error)
        prompt = str(kwargs["prompt"])
        if backend == "codex":
            prompt = " ".join(f"{_CODEX_COMPACT_PERSONA} {prompt}".splitlines())
        marker = f"evaluation-turn:{launch_id}"
        started_at = time.time()
        evaluation_tmux.send_prompt_to_window(
            tmux_session, f"{marker} {prompt}", backend=backend, slot_id=slot_id,
        )
        observation = evaluation_tmux.read_judge_reply(
            project_root, launch_id,
            timeout_s=float(os.environ.get("ZF_EVALUATION_JUDGE_TIMEOUT_S", "1800")),
            backend=backend, started_at=started_at, request_marker=marker,
            codex_sessions_root=codex_home / "sessions" if codex_home else None,
            log=kwargs.get("log"), on_observation=observe,
            stop_event=kwargs.get("stop_event"),
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
    itself (bypass-mode provider, read-only persona scoped to the packet).
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


def _prompt_file_reference(path: Path, *, judge_type: str) -> str:
    schema = SCHEMA_VERSIONS.get(judge_type, f"{judge_type}_result.v1")
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
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """Run one judge request end to end and emit its completed event."""

    log = log or (lambda message: None)
    request_id = evaluation_request_id(
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        judge_type=judge_type,
        source_snapshot_digest=str(packet.get("source_snapshot_digest") or ""),
    )
    existing = read_judge_result(
        state_dir, workflow_run_id=workflow_run_id, judge_type=judge_type,
        request_id=request_id,
    )
    if is_completed_judge_record(existing):
        return existing  # §8.2: a real verdict is permanent for this identity

    prompt_view = packet_for_prompt(packet)
    rendered = STAGE_PROMPTS[judge_type].format(
        task=json.dumps(prompt_view.get("task"), ensure_ascii=False, default=str),
        task_details=json.dumps(prompt_view.get("task_details"), ensure_ascii=False, default=str),
        agent_profiles=json.dumps(prompt_view.get("agent_profiles"), ensure_ascii=False, default=str),
        agent_relationships=json.dumps(prompt_view.get("agent_relationships"), ensure_ascii=False, default=str),
        milestone_results=json.dumps(
            _prior_judge(prompt_view, "milestone"), ensure_ascii=False, default=str,
        ),
        channel_messages=json.dumps(prompt_view.get("channel_messages"), ensure_ascii=False, default=str),
        channel_frozen_prd=json.dumps(prompt_view.get("channel_frozen_prd"), ensure_ascii=False, default=str),
        workflow_bound_prd=json.dumps(prompt_view.get("workflow_bound_prd"), ensure_ascii=False, default=str),
        allowed_evidence_refs=json.dumps(
            prompt_view.get("allowed_evidence_refs"), ensure_ascii=False, default=str,
        ),
    )
    packet_path = _write_judge_prompt_sidecar(
        state_dir,
        workflow_run_id=workflow_run_id,
        judge_type=judge_type,
        request_id=request_id,
        rendered=rendered,
    )
    prompt_ref = _prompt_file_reference(packet_path, judge_type=judge_type)

    result = _run_request_turns(
        state_dir=state_dir, project_root=project_root, config=config, writer=writer,
        workflow_run_id=workflow_run_id, trial_id=trial_id, judge_type=judge_type,
        request_id=request_id, packet=packet, prompt_ref=prompt_ref, existing=existing,
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
            "prompt_version": EVALUATION_PROMPT_VERSION,
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
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """Run the conclusion agent once all three judges have terminal results."""

    log = log or (lambda message: None)
    request_id = evaluation_request_id(
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        judge_type="conclusion",
        source_snapshot_digest=stable_digest(
            {key: judges.get(key) for key in sorted(judges)}
        ),
    )
    existing = read_conclusion_result(state_dir, workflow_run_id, request_id)
    if existing is not None and str(existing.get("status") or "") in (
        _COMPLETED_CONCLUSION_STATUSES
    ):
        return existing

    comm = judges.get("communication") or {}
    prd = judges.get("prd_result") or {}
    det_comm = _deterministic(judges, "communication", communication_score)
    det_prd = _deterministic(judges, "prd_result", prd_result_score)
    det_overall = overall_score(communication=det_comm, prd_result=det_prd)

    packet = {
        "schema_version": "zaofu-prd-evaluation-input.v1",
        "run_id": workflow_run_id,
        "workflow_run_id": workflow_run_id,
        "trial_id": trial_id,
        "stage": "conclusion",
        "source_snapshot_digest": stable_digest({key: judges.get(key) for key in sorted(judges)}),
        "prompt_version": EVALUATION_PROMPT_VERSION,
        "milestone_result": judges.get("milestone"),
        "communication_result": comm,
        "prd_result": prd,
    }
    failed = {key: judges.get(key) or {} for key in ("milestone", "communication", "prd_result")
              if (judges.get(key) or {}).get("status") not in _COMPLETED_JUDGE_STATUSES
              or (judges.get(key) or {}).get("status") == "inconclusive"}
    if failed:
        error = "; ".join(f"{key}: {value.get('error') or value.get('status')}"
                          for key, value in failed.items())
        result = _judge_record(
            judge_type="conclusion", workflow_run_id=workflow_run_id,
            trial_id=trial_id, request_id=request_id, packet=packet,
            status="inconclusive", result={}, error=error, provider="deterministic", model="",
        )
    else:
        rendered = STAGE_PROMPTS["conclusion"].format(
            milestone_result=json.dumps(judges.get("milestone"), ensure_ascii=False, default=str),
            communication_result=json.dumps(comm, ensure_ascii=False, default=str),
            prd_result=json.dumps(prd, ensure_ascii=False, default=str),
        )
        packet_path = _write_judge_prompt_sidecar(
            state_dir, workflow_run_id=workflow_run_id, judge_type="conclusion",
            request_id=request_id, rendered=rendered,
        )
        result = _run_request_turns(
            state_dir=state_dir, project_root=project_root, config=config, writer=writer,
            workflow_run_id=workflow_run_id, trial_id=trial_id, judge_type="conclusion",
            request_id=request_id, packet=packet, existing=existing,
            prompt_ref=_prompt_file_reference(packet_path, judge_type="conclusion"),
            backend=backend or _visible_tmux_backend, log=log,
            slot_id=slot_id, channel_id=channel_id, stop_event=stop_event,
        )
    status, error = result["status"], result["error"]
    raw = result["result"]
    if status == "inconclusive":
        det_overall = None
        raw.update(schema_version="conclusion_result.v1", judge_type="conclusion",
                   status="inconclusive", conclusion_zh="Judge 评估未完成，无法形成确定性结论。",
                   next_actions=["排查 provider 失败原因；重新测评时保留旧报告。"])
    if raw:
        raw.update(communication_score=det_comm, prd_result_score=det_prd,
                   overall_score=det_overall)
        if status == "complete" and det_overall is None:
            status = raw["status"] = result["status"] = "incomplete"
    path = write_conclusion_result(state_dir, workflow_run_id, request_id, result)
    writer.emit(
        CONCLUSION_COMPLETED_EVENT,
        actor="evaluation-runner",
        correlation_id=workflow_run_id,
        payload={
            "schema_version": "benchmark.evaluation.conclusion.v1",
            "channel_id": channel_id,
            "judge_type": "conclusion",
            "attempt": result["attempt"],
            "max_attempts": result["max_attempts"],
            "request_id": request_id,
            "workflow_run_id": workflow_run_id,
            "trial_id": trial_id,
            "status": status,
            "communication_score": det_comm,
            "prd_result_score": det_prd,
            "overall_score": det_overall,
            "artifact_ref": _rel(path, state_dir),
            "artifact_digest": stable_digest(result),
            "error": error,
        },
    )
    return result


def judge_result_path(
    state_dir: Path,
    *,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
) -> Path:
    return (
        Path(state_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "judges"
        / _safe(workflow_run_id)
        / _safe(judge_type)
        / f"{_safe(request_id)}.json"
    )


def conclusion_result_path(
    state_dir: Path, workflow_run_id: str, request_id: str,
) -> Path:
    return (
        Path(state_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "conclusions"
        / _safe(workflow_run_id)
        / f"{_safe(request_id)}.json"
    )


def report_path(state_dir: Path, workflow_run_id: str) -> Path:
    return (
        Path(state_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "reports"
        / _safe(workflow_run_id)
        / "report.md"
    )


def read_judge_result(
    state_dir: Path, *, workflow_run_id: str, judge_type: str, request_id: str,
) -> dict[str, Any] | None:
    return _read_json(judge_result_path(
        state_dir, workflow_run_id=workflow_run_id,
        judge_type=judge_type, request_id=request_id,
    ))


def read_conclusion_result(
    state_dir: Path, workflow_run_id: str, request_id: str,
) -> dict[str, Any] | None:
    return _read_json(conclusion_result_path(state_dir, workflow_run_id, request_id))


def write_judge_result(
    state_dir: Path,
    *,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
    record: dict[str, Any],
) -> Path:
    path = judge_result_path(
        state_dir, workflow_run_id=workflow_run_id,
        judge_type=judge_type, request_id=request_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _canonical(record))
    return path


def write_conclusion_result(
    state_dir: Path, workflow_run_id: str, request_id: str, record: dict[str, Any],
) -> Path:
    path = conclusion_result_path(state_dir, workflow_run_id, request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _canonical(record))
    return path


def versioned_report_path(
    state_dir: Path, workflow_run_id: str, prompt_version: str, *,
    trial_id: str = "", source_snapshot_digest: str = "",
) -> Path:
    suffix = f"-{_safe(trial_id)}-{_safe(source_snapshot_digest)}" if trial_id else ""
    return report_path(state_dir, workflow_run_id).with_name(f"report-{_safe(prompt_version)}{suffix}.md")


def _archive_report(path: Path, body: str) -> Path:
    # Same identity can acquire a new conclusion after recovering a legacy
    # timeout. Preserve both bodies, including the first archived report.
    if path.exists() and path.read_text(encoding="utf-8") != body:
        path = path.with_name(f"{path.stem}-{stable_digest(body)[:16]}.md")
    if not path.exists():
        atomic_write_text(path, body)
    return path


def write_report(
    state_dir: Path, workflow_run_id: str, body: str, *,
    prompt_version: str | None = None, trial_id: str = "",
    source_snapshot_digest: str = "", final: bool = True,
) -> Path:
    """Immutable archives + latest projection; preserve legacy latest on upgrade."""
    from zf.core.state.locks import locked_path

    path = report_path(state_dir, workflow_run_id)
    with locked_path(path):
        if path.exists():
            previous = path.read_text(encoding="utf-8")
            if previous != body:
                _archive_report(path.with_name(f"report-snapshot-{stable_digest(previous)[:16]}.md"), previous)
        if prompt_version and final:
            _archive_report(versioned_report_path(
                state_dir, workflow_run_id, prompt_version,
                trial_id=trial_id, source_snapshot_digest=source_snapshot_digest,
            ), body)
        atomic_write_text(path, body)
    return path


def _write_turn_sidecars(
    state_dir: Path,
    *,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
    attempts_log: list[tuple[int, str, str, str]],
) -> None:
    """Persist the exact prompt seen and raw replies returned per attempt.

    These are the human-readable audit trail: the record JSON only carries a
    prompt_digest and a normalized result, so without these files one cannot
    re-read what the judge was actually shown (rendered packet included).
    Each entry is (attempt, system_prompt, user_prompt, raw_reply).
    """

    if not attempts_log:
        return
    if judge_type == "conclusion":
        directory = conclusion_result_path(
            state_dir, workflow_run_id, request_id,
        ).parent
    else:
        directory = judge_result_path(
            state_dir, workflow_run_id=workflow_run_id,
            judge_type=judge_type, request_id=request_id,
        ).parent
    directory.mkdir(parents=True, exist_ok=True)
    _attempt, system_prompt, user_prompt, _reply = attempts_log[-1]
    header = (
        f"# judge turn prompt — {judge_type} {request_id}\n\n"
        f"attempts: {len(attempts_log)} (below is the final attempt)\n\n"
        "## system prompt\n\n"
        f"{system_prompt}\n\n"
        "## user prompt (rendered packet)\n\n"
        "---\n\n"
    )
    atomic_write_text(
        directory / f"{request_id}.prompt.md", header + user_prompt + "\n",
    )
    replies = [
        f"## attempt {attempt}\n\n{reply.strip() or '(empty reply)'}\n"
        for attempt, _system, _prompt, reply in attempts_log
        if reply.strip()
    ]
    if replies:
        atomic_write_text(
            directory / f"{request_id}.raw.md",
            f"# judge raw replies — {judge_type} {request_id}\n\n"
            + "\n".join(replies),
        )


def _run_request_turns(
    *, state_dir: Path, project_root: Path | None, config: Any | None,
    writer: EventWriter, workflow_run_id: str, trial_id: str, judge_type: str,
    request_id: str, packet: Any, prompt_ref: str, existing: dict[str, Any] | None,
    backend: JudgeBackend, log: Any, slot_id: int | None, channel_id: str,
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """Persist transport attempts before launch; schema repair has its own cap."""
    from zf.runtime.evaluation_scheduler import PROGRESS_EVENT
    from zf.runtime.evaluation_tmux import resolve_judge_backend

    max_attempts = 2
    previous = int((existing or {}).get("attempt") or (1 if existing else 0))
    path = (conclusion_result_path(state_dir, workflow_run_id, request_id)
            if judge_type == "conclusion" else judge_result_path(
                state_dir, workflow_run_id=workflow_run_id, judge_type=judge_type, request_id=request_id))
    provider = f"tmux-{resolve_judge_backend(config)}" if backend is _visible_tmux_backend else "injected"
    record = _judge_record(
        judge_type=judge_type, workflow_run_id=workflow_run_id, trial_id=trial_id,
        request_id=request_id, packet=packet, status="inconclusive", result=None,
        error=str((existing or {}).get("error") or "provider retry budget exhausted"),
        provider=provider, model="",
    )
    record.update(attempt=min(previous, max_attempts), max_attempts=max_attempts,
                  retryable=False, inconclusive_after=max_attempts)
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
            turn_path = path.parent / "attempts" / f"{request_id}-attempt-{attempt}-schema-{schema_attempt}.json"
            turn = {"attempt": attempt, "schema_attempt": schema_attempt,
                    "system_prompt": SYSTEM_PROMPT, "prompt": prompt, "status": "running"}
            atomic_write_text(turn_path, _canonical(turn))
            try:
                observed = backend(
                    state_dir=state_dir, project_root=project_root, config=config,
                    workflow_run_id=workflow_run_id, judge_type=judge_type,
                    request_id=request_id, system_prompt=SYSTEM_PROMPT, prompt=prompt,
                    log=log, attempt=attempt, schema_attempt=schema_attempt,
                    slot_id=slot_id, progress=progress,
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
            record.update(turn_id=observed.turn_id, transcript_ref=observed.transcript_ref)
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
            normalized, error = (normalize_judge_output(
                judge_type=judge_type, raw=parsed,
                allowed_evidence_refs=list(packet.get("allowed_evidence_refs") or []),
            ) if parsed is not None else (None, f"judge returned no single JSON object: {observed.reply[:240]}"))
            record.update(status="invalid", error=error)
            if normalized is not None:
                record.update(result=normalized.to_dict(), error="",
                              status="complete" if judge_type == "conclusion" else str(normalized.status or "scored"))
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
    candidates = [stripped] if stripped.startswith("{") else []
    candidates.extend(
        block.strip()[4:].strip()
        for block in stripped.split("```")
        if block.strip().lower().startswith("json")
    )
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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
    return JudgeRecord(
        schema_version="benchmark.evaluation.record.v1",
        judge_type=judge_type,
        status=status,
        request_id=request_id,
        run_id=str(packet_dict.get("run_id") or workflow_run_id),
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        source_snapshot_digest=str(packet_dict.get("source_snapshot_digest") or ""),
        prompt_version=EVALUATION_PROMPT_VERSION,
        prompt_digest=stable_digest(STAGE_PROMPTS[judge_type]),
        provider=provider,
        model=model,
        created_at=datetime.now(timezone.utc).isoformat(),
        error=error,
        result=result or {},
        evidence_refs=list(packet_dict.get("allowed_evidence_refs") or []),
    ).to_dict()


def _deterministic(
    judges: dict[str, dict[str, Any]],
    judge_type: str,
    scorer: Any,
) -> float | None:
    record = judges.get(judge_type) or {}
    result = record.get("result") or {}
    if str(record.get("status") or "") != "scored":
        return None
    return scorer(result.get("dimensions") or [])


def _prior_judge(prompt_view: dict[str, Any], judge_type: str) -> Any:
    for item in prompt_view.get("prior_judges") or []:
        if isinstance(item, dict) and item.get("judge_type") == judge_type:
            return item.get("result")
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"


def _rel(path: Path, state_dir: Path) -> str:
    try:
        return str(Path(path).relative_to(Path(state_dir)))
    except ValueError:
        return str(path)


def _safe(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value))
    return safe.strip(".-") or "unknown"


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
