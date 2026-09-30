"""Provider transcript identity and liveness for a single evaluation turn."""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable

from zf.runtime.evaluation_config import capture_evaluator_budgets


class EvaluationCancelled(Exception):
    """Observer shutdown: leave the consumed attempt durable for recovery."""


@dataclass(frozen=True)
class JudgeTurnObservation:
    status: str
    reply: str = ""
    error: str = ""
    turn_id: str = ""
    last_activity_at: str = ""
    tool_calls: int = 0
    transcript_bytes: int = 0
    phase: str = ""
    transcript_ref: str = ""
    session_id: str = ""
    activity_bytes: int = 0
    same_tool_calls: int = 0
    tool_wall_time: float = 0.0
    observed_model: str = "unknown"
    observed_model_source: str = "unknown"


def _entries(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partially flushed trailing record is not an error
        if isinstance(entry, dict):
            result.append(entry)
    return result


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(block.get("text") or block.get("output_text") or "")
                     for block in content if isinstance(block, dict)
                     and block.get("type") in {"text", "input_text", "output_text"})


def _stamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _scoped_entries(entries: list[dict[str, Any]], *, backend: str,
                    current_turn_id: str, request_marker: str,
                    started_at: float | None) -> tuple[str, list[dict[str, Any]]]:
    active = ""
    selected = current_turn_id
    scoped: list[tuple[str, dict[str, Any]]] = []
    for entry in entries:
        payload = entry.get("payload") or {}
        message = entry.get("message") or {}
        if not isinstance(payload, dict) or not isinstance(message, dict):
            continue
        explicit = str(payload.get("turn_id") or message.get("turn_id")
                       or entry.get("turn_id") or "")
        user_text = ""
        if backend == "codex":
            if entry.get("type") == "turn_context" or (
                entry.get("type") == "event_msg"
                and payload.get("type") in {"task_started", "turn_started"}
            ):
                active = explicit
            if entry.get("type") == "event_msg" and payload.get("type") == "user_message":
                user_text = str(payload.get("message") or "")
            elif entry.get("type") == "response_item" and payload.get("role") == "user":
                user_text = _text(payload.get("content"))
        elif entry.get("type") == "user":
            # Tool results are user records too, but do not start a new turn.
            user_text = _text(message.get("content"))
            if user_text:
                active = explicit or str(entry.get("uuid") or "")
        turn = explicit or active
        ts = _stamp(entry.get("timestamp") or entry.get("ts"))
        after = started_at is None or ts is None or ts >= started_at
        if not selected and turn and after:
            if (request_marker and request_marker in user_text) or not request_marker:
                selected = turn
        scoped.append((turn, entry))
    return selected, [entry for turn, entry in scoped if selected and turn == selected]


def observe_transcript(path: Path, *, backend: str, current_turn_id: str = "",
                       request_marker: str = "", started_at: float | None = None,
                       now: float | None = None) -> JudgeTurnObservation:
    entries = _entries(path)
    session = next((str(entry["payload"].get("id") or "") for entry in entries
                    if entry.get("type") == "session_meta" and isinstance(entry.get("payload"), dict)), "")
    if backend != "codex":
        session = next((str(entry.get("sessionId") or "") for entry in entries
                        if entry.get("sessionId")), path.stem)
    turn_id, active = _scoped_entries(
        entries, backend=backend, current_turn_id=current_turn_id,
        request_marker=request_marker, started_at=started_at,
    )
    status, error, reply, phase, latest = "running", "", "", "", ""
    tool_fingerprints: dict[str, str] = {}
    pending: dict[str, float] = {}
    tool_wall = 0.0
    activity_bytes = 0
    observed_model = "unknown"
    observed_model_source = "unknown"
    now = time.time() if now is None else now
    for index, entry in enumerate(active):
        payload, message = entry.get("payload") or {}, entry.get("message") or {}
        if not isinstance(payload, dict) or not isinstance(message, dict):
            continue
        kind, payload_kind = entry.get("type"), payload.get("type")
        timestamp = str(entry.get("timestamp") or entry.get("ts") or "")
        stamp = _stamp(timestamp)
        calls, results = [], []
        meaningful = False
        if backend == "codex":
            if kind == "turn_context":
                model = payload.get("model")
                if isinstance(model, str) and model.strip():
                    observed_model = model.strip()
                    observed_model_source = "transcript.turn_context"
            if kind == "response_item":
                meaningful = payload.get("role") != "user"
                if payload_kind in {"function_call", "tool_call", "custom_tool_call", "computer_call"}:
                    calls.append((str(payload.get("call_id") or index), payload.get("name"),
                                  payload.get("arguments", payload.get("input"))))
                if payload_kind in {"function_call_output", "tool_result", "custom_tool_call_output"}:
                    results.append(str(payload.get("call_id") or ""))
                if payload_kind == "error" or payload.get("error") or payload.get("is_error"):
                    status, error = "provider_error", str(payload.get("error") or payload.get("message") or "tool error")
                if payload_kind == "message" and payload.get("role") == "assistant":
                    reply = _text(payload.get("content")) or reply
                    phase = "assistant"
            elif kind == "event_msg":
                meaningful = payload_kind in {
                    "agent_message", "agent_reasoning", "task_complete", "turn_complete",
                    "turn_aborted", "error", "exec_command_begin", "exec_command_end",
                    "mcp_tool_call_begin", "mcp_tool_call_end",
                }
                if payload_kind == "turn_aborted":
                    status, error = "aborted", str(payload.get("reason") or "turn aborted")
                elif payload_kind in {"task_complete", "turn_complete"}:
                    if payload.get("error"):
                        status, error = "provider_error", str(payload["error"])
                    elif status == "running":
                        status = "completed"
                        reply = str(payload.get("last_agent_message") or reply)
                elif payload_kind == "error":
                    status, error = "provider_error", str(payload.get("message") or payload.get("error") or "provider error")
                elif meaningful and not phase:
                    phase = "reasoning"
        else:
            if kind == "assistant":
                model = message.get("model")
                if isinstance(model, str) and model.strip():
                    observed_model = model.strip()
                    observed_model_source = "transcript.assistant.message"
            meaningful = kind in {"assistant", "error"}
            if kind == "error" or message.get("is_error") or entry.get("isApiErrorMessage"):
                status, error = "provider_error", str(entry.get("error") or message.get("error") or _text(message.get("content")) or "provider error")
            content = message.get("content") or []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls.append((str(block.get("id") or index), block.get("name"), block.get("input")))
                if block.get("type") == "tool_result":
                    meaningful = True
                    results.append(str(block.get("tool_use_id") or ""))
                    if block.get("is_error") is True:
                        status, error = "provider_error", str(block.get("content") or "tool error")
            if kind == "assistant":
                phase = "assistant"
                stop = message.get("stop_reason")
                if stop in {"error", "failed", "aborted", "cancelled", "canceled", "refusal", "max_tokens"}:
                    status = "aborted" if stop in {"aborted", "cancelled", "canceled"} else "provider_error"
                    error = str(stop)
                elif stop == "end_turn" and status == "running":
                    status, reply = "completed", _text(content)
        for call_id, name, arguments in calls:
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    pass
            tool_fingerprints[call_id] = json.dumps([name, arguments], sort_keys=True, default=str)
            if stamp is not None:
                pending.setdefault(call_id, stamp)
            phase = "tool"
        for call_id in results:
            if call_id in pending and stamp is not None:
                tool_wall = max(tool_wall, stamp - pending.pop(call_id))
            phase = "tool_result"
        if meaningful:
            latest = timestamp or latest
            activity_bytes += len(json.dumps(entry, ensure_ascii=False).encode())
    if pending:
        tool_wall = max(tool_wall, max(now - ts for ts in pending.values()))
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return JudgeTurnObservation(
        status=status, reply=reply, error=error, turn_id=turn_id,
        last_activity_at=latest, tool_calls=len(tool_fingerprints),
        transcript_bytes=size, transcript_ref=str(path), session_id=session,
        phase=phase, activity_bytes=activity_bytes,
        same_tool_calls=max(Counter(tool_fingerprints.values()).values(), default=0),
        tool_wall_time=max(0.0, tool_wall),
        observed_model=observed_model,
        observed_model_source=observed_model_source,
    )


def _find_rollout(root: Path, marker: str, started_at: float | None) -> Path | None:
    # The root belongs to one launch within one slot. Never search by project
    # cwd or consult the global latest session; native subagent rollouts must
    # not win either. The submitted request marker proves the root turn.
    matched = []
    for candidate in root.glob("*/*/*/rollout-*.jsonl"):
        observed = observe_transcript(candidate, backend="codex",
                                      request_marker=marker, started_at=started_at)
        if observed.session_id and observed.turn_id:
            matched.append(candidate)
    if len(matched) > 1:
        raise ValueError("ambiguous evaluation rollout identity")
    return matched[0] if matched else None


def read_judge_reply(
    project_root: Path, session_uuid: str, *, timeout_s: float, poll_s: float = 5.0,
    log: Any | None = None, backend: str = "claude-code", started_at: float | None = None,
    codex_sessions_root: Path | None = None, request_marker: str = "",
    on_observation: Callable[[JudgeTurnObservation], None] | None = None,
    stop_event: Event | None = None,
    evaluator_budgets: dict[str, Any] | None = None,
) -> JudgeTurnObservation:
    from zf.runtime.session_tailer import claude_session_path

    if backend == "codex" and (codex_sessions_root is None or not request_marker):
        return JudgeTurnObservation(status="provider_error", error="Codex judge requires isolated rollout root and request marker")
    path = None if backend == "codex" else claude_session_path(str(project_root), session_uuid)
    budgets = evaluator_budgets if evaluator_budgets is not None else capture_evaluator_budgets()
    timeouts, tool_budgets = budgets["timeouts"], budgets["tool_budgets"]
    first_limit = float(timeouts["first_progress_seconds"])
    idle_limit = float(timeouts["idle_seconds"])
    call_limit = float(tool_budgets["max_tool_calls"])
    same_limit = float(tool_budgets["max_same_tool_fingerprint"])
    wall_limit = float(tool_budgets["max_tool_wall_time_seconds"])
    start = last_growth = time.monotonic()
    heartbeat = start + 60
    observation = JudgeTurnObservation(status="running")
    last_activity = 0
    last_binding = ("", "", "")
    session_id = ""
    while True:
        if stop_event is not None and stop_event.is_set():
            raise EvaluationCancelled("evaluation observer stopped")
        if backend == "codex" and path is None:
            try:
                path = _find_rollout(codex_sessions_root, request_marker, started_at)
            except ValueError as exc:
                return replace(observation, status="provider_error", error=str(exc))
        if path is not None:
            observation = observe_transcript(
                path, backend=backend, current_turn_id=observation.turn_id,
                request_marker=request_marker, started_at=started_at,
            )
            if session_id and observation.session_id != session_id:
                return replace(observation, status="provider_error", error="provider session identity changed")
            session_id = observation.session_id or session_id
        now = time.monotonic()
        binding = (observation.session_id, observation.transcript_ref, observation.turn_id)
        if on_observation and binding != last_binding:
            on_observation(observation)
            last_binding = binding
        if observation.status != "running":
            return observation
        if observation.activity_bytes != last_activity:
            last_growth, last_activity = now, observation.activity_bytes
        error = ""
        if now - start >= max(0, timeout_s):
            error = f"judge timed out after {int(timeout_s)}s"
        elif not last_activity and first_limit and now - start >= first_limit:
            error = "provider made no effective transcript progress"
        elif last_activity and idle_limit and now - last_growth >= idle_limit:
            error = f"judge transcript idle for {int(idle_limit)}s"
        elif call_limit and observation.tool_calls > call_limit:
            error = f"judge exceeded max_tool_calls={int(call_limit)}"
        elif same_limit and observation.same_tool_calls > same_limit:
            error = f"judge exceeded max_same_tool_fingerprint={int(same_limit)}"
        elif wall_limit and observation.tool_wall_time > wall_limit:
            error = f"judge exceeded max_tool_wall_time={int(wall_limit)}s"
        if error:
            return replace(observation, status="timeout", error=error)
        if now >= heartbeat:
            if log:
                log(f"judge still running ({path.name if path else 'no transcript'}), {int(timeout_s - (now - start))}s left")
            if on_observation:
                on_observation(observation)
            heartbeat = now + 60
        time.sleep(max(0.01, min(poll_s, timeout_s - (now - start))))
