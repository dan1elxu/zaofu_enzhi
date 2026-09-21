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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
})

# Records that hold a REAL judge verdict. ``judge_timeout`` is a transport
# failure — nothing was judged — so it must stay retryable; otherwise one
# bad spawn (e.g. the provider never booted) blocks the request identity
# forever and no restart can ever make the evaluation happen
# (2026-09-17 test_dev3 live incident: yesterday's "judge claude did not
# start" records deduped today's entire run).
_COMPLETED_JUDGE_STATUSES = frozenset({
    "scored",
    "evidence_missing",
    "abstain",
    "invalid",
})
_COMPLETED_CONCLUSION_STATUSES = frozenset({"complete", "invalid", "incomplete"})


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

JudgeBackend = Callable[..., tuple[str, str]]


def _visible_tmux_backend(**kwargs: Any) -> tuple[str, str]:
    """Default judge backend: the persistent ``judges`` tmux window.

    Every judge turn runs visibly in ONE shared window (the "judge page")
    inside the project's workflow session. The agent is spawned exactly
    like an orchestrator bypass worker (adapter-built provider argv and
    evaluator persona), readiness is proven against the provider's TUI
    prompt before the packet is pasted, and the provider transcript is
    polled for the final completed reply.
    Interactive blockers (trust/auth/quota) surface as readiness errors
    instead of silently hanging the judge.
    """

    from zf.runtime import evaluation_tmux

    project_root = Path(kwargs.get("project_root") or Path.cwd())
    config: Any | None = kwargs.get("config")
    workflow_run_id = str(kwargs["workflow_run_id"])
    judge_type = str(kwargs["judge_type"])
    request_id = str(kwargs["request_id"])
    prompt = str(kwargs["prompt"])
    system_prompt = str(kwargs.get("system_prompt") or "")
    log = kwargs.get("log") or (lambda message: print(message, flush=True))

    tmux_session = str(
        getattr(getattr(config, "session", None), "tmux_session", "") or ""
    ).strip()
    if not tmux_session:
        return "", "evaluation judge requires config.session.tmux_session"

    judge_backend = evaluation_tmux.resolve_judge_backend(config)
    started_at = time.time()
    session_uuid = evaluation_tmux.judge_session_uuid(
        project_root, workflow_run_id, judge_type, request_id,
    )
    ready, error = evaluation_tmux.start_judge_agent(
        tmux_session,
        project_root,
        session_uuid=session_uuid,
        system_prompt=system_prompt,
        backend=judge_backend,
    )
    if not ready:
        return "", error
    try:
        try:
            # Codex has no interactive equivalent of Claude's
            # ``--append-system-prompt``.  Keep the same fixed persona by
            # putting it before the immutable packet in the first turn.
            prompt_to_send = prompt
            if judge_backend == "codex":
                # Codex TUI quirks, both learned live (2026-09-18/21):
                # 1. A MULTI-LINE draft submits on Ctrl+Enter only; plain
                #    Enter inserts a newline and the turn never fires.
                # 2. Any input past ~1000 chars is folded into a literal
                #    "[Pasted Content N chars]" placeholder that replaces
                #    the real text in the model context.
                # The whole turn prompt must therefore be ONE SHORT LINE:
                # newlines become spaces, and the persona is the compact
                # variant below (schema details live in the packet sidecar
                # the prompt points at).
                persona = _CODEX_COMPACT_PERSONA if system_prompt.strip() else ""
                prompt_to_send = " ".join(
                    f"{persona} {prompt}".splitlines()
                )
            evaluation_tmux.send_prompt_to_window(
                tmux_session, prompt_to_send, backend=judge_backend,
            )
        except Exception as exc:
            return "", f"failed to deliver judge prompt: {exc}"
        timeout_s = float(os.environ.get("ZF_EVALUATION_JUDGE_TIMEOUT_S", "1800"))
        reply = evaluation_tmux.read_judge_reply(
            project_root, session_uuid,
            timeout_s=timeout_s, log=log,
            backend=judge_backend, started_at=started_at,
        )
        if not reply.strip():
            return "", f"judge timed out after {int(timeout_s)}s"
        return reply, ""
    finally:
        # The judge agent stays alive in the judges window (idle at its
        # prompt) so the operator can scroll the deliberation; the next
        # turn/attempt replaces it via stop_judge_agent before spawning.
        pass


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

    run_backend = backend or _visible_tmux_backend
    raw: dict[str, Any] | None = None
    status = "invalid"
    error = ""
    provider = ""
    model = ""
    attempts_log: list[tuple[int, str, str, str]] = []  # (n, system, prompt, reply)
    for attempt in (1, 2):
        user_prompt = prompt_ref if attempt == 1 else f"{_retry_hint(error)} {prompt_ref}"
        reply, turn_error, provider, model = _judge_turn(
            state_dir=state_dir,
            project_root=project_root,
            config=config,
            workflow_run_id=workflow_run_id,
            judge_type=judge_type,
            request_id=request_id,
            system_prompt=SYSTEM_PROMPT,
            prompt=user_prompt,
            backend=run_backend,
            log=log,
        )
        attempts_log.append((attempt, SYSTEM_PROMPT, user_prompt, reply))
        if turn_error:
            # Transport-level failure / timeout: judge_timeout, not a score.
            status = "judge_timeout"
            error = turn_error
            break
        parsed = _parse_json_object(reply)
        if parsed is None:
            status = "invalid"
            error = f"judge returned no single JSON object: {reply[:240]}"
            continue
        normalized, normalize_error = normalize_judge_output(
            judge_type=judge_type,
            raw=parsed,
            allowed_evidence_refs=list(packet.get("allowed_evidence_refs") or []),
        )
        if normalized is None:
            status = "invalid"
            error = normalize_error
            continue
        raw = normalized.to_dict()
        status = str(normalized.status or "scored")
        error = ""
        break

    _write_turn_sidecars(
        state_dir,
        workflow_run_id=workflow_run_id,
        judge_type=judge_type,
        request_id=request_id,
        attempts_log=attempts_log,
    )

    result = _judge_record(
        judge_type=judge_type,
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        request_id=request_id,
        packet=packet,
        status=status,
        result=raw,
        error=error,
        provider=provider,
        model=model,
    )
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
    rendered = STAGE_PROMPTS["conclusion"].format(
        milestone_result=json.dumps(judges.get("milestone"), ensure_ascii=False, default=str),
        communication_result=json.dumps(comm, ensure_ascii=False, default=str),
        prd_result=json.dumps(prd, ensure_ascii=False, default=str),
    )
    packet_path = _write_judge_prompt_sidecar(
        state_dir,
        workflow_run_id=workflow_run_id,
        judge_type="conclusion",
        request_id=request_id,
        rendered=rendered,
    )
    prompt_ref = _prompt_file_reference(packet_path, judge_type="conclusion")
    run_backend = backend or _visible_tmux_backend
    raw: dict[str, Any] | None = None
    status = "invalid"
    error = ""
    provider = ""
    model = ""
    attempts_log: list[tuple[int, str, str, str]] = []
    for attempt in (1, 2):
        user_prompt = prompt_ref if attempt == 1 else f"{_retry_hint(error)} {prompt_ref}"
        reply, turn_error, provider, model = _judge_turn(
            state_dir=state_dir,
            project_root=project_root,
            config=config,
            workflow_run_id=workflow_run_id,
            judge_type="conclusion",
            request_id=request_id,
            system_prompt=SYSTEM_PROMPT,
            prompt=user_prompt,
            backend=run_backend,
            log=log,
        )
        attempts_log.append((attempt, SYSTEM_PROMPT, user_prompt, reply))
        if turn_error:
            status = "judge_timeout"
            error = turn_error
            break
        parsed = _parse_json_object(reply)
        if parsed is None:
            status = "invalid"
            error = f"conclusion returned no single JSON object: {reply[:240]}"
            continue
        normalized, normalize_error = normalize_judge_output(
            judge_type="conclusion",
            raw=parsed,
            allowed_evidence_refs=[],
        )
        if normalized is None:
            status = "invalid"
            error = normalize_error
            continue
        raw = normalized.to_dict()
        status = "complete"
        error = ""
        break

    # Deterministic score ownership: agent numbers are ignored/overridden.
    if raw is not None:
        raw["communication_score"] = det_comm
        raw["prd_result_score"] = det_prd
        raw["overall_score"] = det_overall
        if det_overall is None:
            raw["status"] = "incomplete"
            status = "incomplete"
    _write_turn_sidecars(
        state_dir,
        workflow_run_id=workflow_run_id,
        judge_type="conclusion",
        request_id=request_id,
        attempts_log=attempts_log,
    )
    result = _judge_record(
        judge_type="conclusion",
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        request_id=request_id,
        packet=packet,
        status=status,
        result=raw,
        error=error,
        provider=provider,
        model=model,
    )
    path = write_conclusion_result(state_dir, workflow_run_id, request_id, result)
    writer.emit(
        CONCLUSION_COMPLETED_EVENT,
        actor="evaluation-runner",
        correlation_id=workflow_run_id,
        payload={
            "schema_version": "benchmark.evaluation.conclusion.v1",
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
    state_dir: Path, workflow_run_id: str, prompt_version: str,
) -> Path:
    return (
        Path(state_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "reports"
        / _safe(workflow_run_id)
        / f"report-{_safe(prompt_version)}.md"
    )


def write_report(
    state_dir: Path,
    workflow_run_id: str,
    body: str,
    *,
    prompt_version: str | None = None,
) -> Path:
    """Refresh ``report.md`` (latest pointer) and archive per prompt version.

    Judge records are immutable per request identity, but the report path
    used to carry no identity — every prompt version rewrote the same
    ``report.md`` and earlier evaluation rounds lost their readable summary
    (2026-09-17: v1/v2/v3 reports were overwritten by v4 on test_dev3).
    With ``prompt_version`` each round keeps its own ``report-<version>.md``
    archive; ``report.md`` stays the backwards-compatible latest view.
    """

    if prompt_version:
        archived = versioned_report_path(state_dir, workflow_run_id, prompt_version)
        archived.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(archived, body)
    path = report_path(state_dir, workflow_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _judge_turn(
    *,
    state_dir: Path,
    project_root: Path | None,
    config: Any | None,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
    system_prompt: str,
    prompt: str,
    backend: JudgeBackend,
    log: Any | None = None,
) -> tuple[str, str, str, str]:
    """One judge turn. Returns (reply, transport_error, provider, model)."""

    try:
        reply, error = backend(
            state_dir=state_dir,
            project_root=project_root,
            config=config,
            workflow_run_id=workflow_run_id,
            judge_type=judge_type,
            request_id=request_id,
            system_prompt=system_prompt,
            prompt=prompt,
            log=log,
        )
    except Exception as exc:  # provider blew up: timeout class, not a score
        return "", f"judge backend error: {exc}", "", ""
    provider = "tmux-claude"
    if backend is _visible_tmux_backend:
        try:
            from zf.runtime.evaluation_tmux import resolve_judge_backend

            provider = f"tmux-{resolve_judge_backend(config)}"
        except Exception:
            # Provider naming is telemetry only; never turn a valid judge
            # response into a transport failure because metadata resolution
            # failed.
            pass
    return reply, error, provider, ""


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
