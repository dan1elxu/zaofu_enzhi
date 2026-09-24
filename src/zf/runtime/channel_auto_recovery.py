"""Bounded recovery for hidden auto Channel steps; events own retry state.

See docs/impl/channel-auto-recovery.md for the operator retry contract.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from zf.core.security.redaction import redact_obj
from zf.core.state.locks import FileLock

MAX_ATTEMPTS = 3
_IN_FLIGHT: set[tuple[str, str, str, str]] = set()
_SCHEDULING_LOCK = threading.Lock()
STEP_EVENT = "channel.auto_chain.step"
RETRY_EVENT = "channel.auto_chain.retry.requested"


def step_lock(state_dir: Path, channel_id: str, thread_id: str, purpose: str) -> FileLock:
    identity = json.dumps([channel_id, thread_id, purpose])
    name = hashlib.sha256(identity.encode()).hexdigest()
    return FileLock(Path(state_dir) / "locks/channel-auto" / f"{name}.lock", timeout_seconds=0)


def latest_step(channel: dict[str, Any], thread_id: str, purpose: str) -> dict[str, Any]:
    return next((dict(row) for row in channel.get("auto_chain") or []
                 if row.get("thread_id") == thread_id and row.get("purpose") == purpose), {})


def _source(channel: dict[str, Any], thread_id: str, purpose: str, *, require_ready: bool = True) -> tuple[str, str]:
    from zf.runtime.channel_auto_chain import _open_questions
    from zf.runtime.channel_contracts import channel_discussion_auto_enabled
    from zf.runtime.channel_question_gate import question_dedup_gate_state

    if channel.get("status") != "open" or not channel_discussion_auto_enabled(channel):
        return "", ""
    if purpose == "handoff":
        consensus = (channel.get("consensus") or {}).get(thread_id) or {}
        event_id = str(consensus.get("reached_event_id") or "")
        return (event_id, event_id) if consensus.get("status") == "reached" else ("", "")
    if purpose != "resolve":
        return "", ""
    session = (channel.get("discussions") or {}).get(thread_id) or {}
    questions = _open_questions(channel, thread_id)
    if require_ready and (session.get("state") != "phase2_relay" or not questions
            or question_dedup_gate_state(channel, thread_id=thread_id) != "open"
            or any(str(row.get("thread_id") or "main") == thread_id
                   and row.get("status") in {"requested", "started", "pending", "running"}
                   for row in channel.get("reply_requests") or [])):
        return "", ""
    # Partial resolution must not reset a persisted retry budget.
    question_ids = sorted(str(row.get("question_id")) for row in channel.get("open_questions") or []
                          if str(row.get("thread_id") or "main") == thread_id)
    identity = [session.get("started_event_id"), question_ids]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(), ""


def _emit(writer: Any, step: dict[str, Any], status: str, reason: str = "") -> None:
    step.update(status=status, reason=reason, retryable=status in {"interrupted", "retrying"})
    writer.emit(STEP_EVENT, actor="channel-auto-chain", correlation_id=step["channel_id"],
                payload=redact_obj(dict(step)))


def schedule_auto_step(host: Any, channel_id: str, thread_id: str, purpose: str) -> bool:
    """Persist before spawning; workers acquire their own ordinary file lock."""
    from zf.runtime.channel_projection import project_channel

    if not channel_id or purpose not in {"resolve", "handoff"}:
        return False
    key = (str(Path(host.state_dir).resolve()), channel_id, thread_id, purpose)
    with _SCHEDULING_LOCK:
        if key in _IN_FLIGHT:
            return False
        try:
            with step_lock(host.state_dir, channel_id, thread_id, purpose):
                channel = project_channel(host.state_dir, channel_id) or {}
                step = latest_step(channel, thread_id, purpose)
                recovering = step.get("status") in {"queued", "running", "retrying", "interrupted"}
                source_id, cause_id = _source(channel, thread_id, purpose, require_ready=not recovering)
                if not source_id:
                    return False
                if step.get("source_id") == source_id and step.get("status") in {"completed", "failed"}:
                    return False
                if step.get("source_id") != source_id:
                    step = {
                        "channel_id": channel_id, "thread_id": thread_id, "purpose": purpose,
                        "source_id": source_id, "cause_event_id": cause_id,
                        "operation_id": uuid.uuid4().hex, "attempt": 0, "max_attempts": MAX_ATTEMPTS,
                    }
                    _emit(host.event_writer, step, "queued")
            _IN_FLIGHT.add(key)
            try:
                threading.Thread(target=_run_guarded, args=(host, step, key), daemon=True,
                                 name=f"channel-auto-{purpose}").start()
            except Exception:
                _IN_FLIGHT.discard(key)
                raise  # The queued event is recoverable on the next tick.
            return True
        except TimeoutError:
            return False


def _run_guarded(host: Any, step: dict[str, Any], key: tuple[str, str, str, str]) -> None:
    from zf.runtime.channel_projection import project_channel

    try:
        with step_lock(host.state_dir, step["channel_id"], step["thread_id"], step["purpose"]):
            current = latest_step(project_channel(host.state_dir, step["channel_id"]) or {},
                                  step["thread_id"], step["purpose"])
            if current.get("operation_id") != step["operation_id"] or current.get("status") in {"completed", "failed"}:
                return
            if current["purpose"] == "resolve":
                from zf.runtime.channel_auto_chain import _open_questions

                channel = project_channel(host.state_dir, step["channel_id"]) or {}
                if not _open_questions(channel, step["thread_id"]):
                    _emit(host.event_writer, current, "completed")
                    return
            _run_step(host, current)
    except TimeoutError:
        pass  # Another live watcher/provider still owns the operation.
    finally:
        _IN_FLIGHT.discard(key)


def _interrupt_lost_thread(host: Any, step: dict[str, Any], *, error: str = "") -> bool:
    """A free provider lock proves its previous caller has gone away.

    This also handles legacy hidden calls that predate auto-chain step events.
    Never infer a dead owner from elapsed time alone.
    """
    from zf.web.headless_agent import HeadlessThreadStore, HeadlessTurnResult
    from zf.runtime.channel_projection import project_channel

    channel_id, thread_id = step["channel_id"], step["thread_id"]
    if step["purpose"] == "resolve":
        key = f"channel-auto-resolve:{channel_id}:{thread_id}"
    else:
        channel = project_channel(host.state_dir, channel_id) or {}
        revision = ((channel.get("consensus") or {}).get(thread_id) or {}).get("prd_revision") or 0
        key = f"channel-auto-handoff:{channel_id}:{revision}"
    store = HeadlessThreadStore(state_dir=host.state_dir,
                                project_root=Path(getattr(host, "project_root", None) or Path(host.state_dir).parent))
    thread = store.load(thread_key=key)
    with FileLock(store.path_for(thread["thread_id"]).with_suffix(".lock"), timeout_seconds=0):
        thread = store.load(thread_key=key)
        if thread.get("status") != "running":
            return False
        reason = error or "调用已中断：执行进程已退出，未收到 provider 最终响应；原因未知。"
        for backend, provider in thread.get("providers", {}).items():
            if provider.get("status") == "running":
                store.record_turn(thread, result=HeadlessTurnResult(
                    ok=False, status="failed" if error else "interrupted", backend=backend, thread_id=thread["thread_id"],
                    provider_session_id=provider.get("provider_session_id", ""),
                    reply="", messages=[], usage={}, error=reason,
                ), workdir=str(getattr(host, "project_root", None) or ""))
        return True


def _run_step(host: Any, step: dict[str, Any]) -> None:
    from zf.runtime import channel_auto_chain as chain

    writer = host.event_writer
    lost = _interrupt_lost_thread(host, step)
    if lost or step.get("status") == "running":
        step["attempt"] = max(1, int(step.get("attempt") or 0))
        _emit(writer, step, "interrupted", "调用已中断：执行进程已退出，未收到步骤终态；具体原因未知。")
    while int(step.get("attempt") or 0) < MAX_ATTEMPTS:
        step["attempt"] = int(step.get("attempt") or 0) + 1
        _emit(writer, step, "running")
        kwargs = dict(state_dir=Path(host.state_dir), writer=writer,
                      config=getattr(host, "config", None), project_root=getattr(host, "project_root", None),
                      channel_id=step["channel_id"], thread_id=step["thread_id"], report_failure=False)
        try:
            if step["purpose"] == "resolve":
                result = chain.run_question_resolution(**kwargs)
            else:
                result = chain.run_auto_chain(**kwargs, cause_event_id=step["cause_event_id"])
        except Exception as exc:
            result = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
            _interrupt_lost_thread(host, step, error=result["reason"])
        if result.get("ok"):
            if result.get("status") in {"skipped_relay_in_flight", "skipped_gate_closed"}:
                step["attempt"] -= 1
                _emit(writer, step, "queued")
            else:
                _emit(writer, step, "completed")
            return
        reason = str(result.get("reason") or result.get("status") or "自动步骤失败，未提供原因")
        if step["attempt"] < MAX_ATTEMPTS:
            _emit(writer, step, "retrying", reason)
            time.sleep(step["attempt"])
        else:
            step["reason"] = reason
    _emit(writer, step, "failed", str(step.get("reason") or "自动重试次数已用完"))
    writer.emit("channel.auto_chain.failed", actor=chain.AUTO_CHAIN_ACTOR,
                correlation_id=step["channel_id"], payload=redact_obj(dict(step)))


def request_auto_retry(service: Any, *, requested: Any, action: str,
                       requested_action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Controlled action: only enqueue; never run a provider from the Web request."""
    from zf.runtime.channel_projection import project_channel

    channel_id = str(payload.get("channel_id") or "")
    thread_id = str(payload.get("thread_id") or "main")
    purpose = str(payload.get("purpose") or "")
    try:
        with step_lock(service.state_dir, channel_id, thread_id, purpose):
            channel = project_channel(service.state_dir, channel_id) or {}
            step = latest_step(channel, thread_id, purpose)
            source_id, _ = _source(channel, thread_id, purpose, require_ready=False)
            if (not source_id or step.get("source_id") != source_id
                    or step.get("status") != "failed"
                    or step.get("operation_id") != payload.get("operation_id")):
                raise ValueError("该自动步骤已更新、尚在执行或没有可重试的失败")
            step.update(status="queued", attempt=0, reason="", retryable=False, retry_of=step["operation_id"],
                        operation_id=uuid.uuid4().hex)
            event = service.writer.emit(RETRY_EVENT, actor=service.actor, causation_id=requested.id,
                                        correlation_id=channel_id, payload=redact_obj(step))
    except (TimeoutError, ValueError) as exc:
        return service._failed(requested=requested, action=action, requested_action=requested_action,
                               task_id=None, reason=str(exc), status_code=409, status="retry_unavailable")
    extra = {"channel_id": channel_id, "operation_id": step["operation_id"]}
    service._completed(requested=requested, event=event, action=action,
                       requested_action=requested_action, status="queued", task_id=None, extra=extra)
    return {"_status_code": 202, "ok": True, "status": "queued", "action": action,
            "requested_action": requested_action, "event_id": event.id, **extra}


def reconcile_auto_chain(*, state_dir: Path, writer: Any, config: Any,
                         project_root: Path | None) -> int:
    """Periodic discovery recovers consumed triggers without replaying old events."""
    from zf.runtime.channel_contracts import channel_discussion_auto_enabled
    from zf.runtime.channel_projection import project_channels

    host = SimpleNamespace(state_dir=state_dir, event_writer=writer, config=config,
                           project_root=project_root or Path(state_dir).parent)
    scheduled = 0
    for channel in project_channels(state_dir).get("channels") or []:
        if not channel_discussion_auto_enabled(channel):
            continue
        threads = set(channel.get("discussions") or {}) | set(channel.get("consensus") or {})
        for thread_id in threads:
            for purpose in ("resolve", "handoff"):
                try:
                    scheduled += schedule_auto_step(host, channel["channel_id"], thread_id, purpose)
                except Exception:
                    logging.getLogger(__name__).exception("auto Channel recovery failed")
    return scheduled
