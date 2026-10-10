"""Startup reconciliation for daemon-owned Kanban Agent turns."""

from __future__ import annotations

import json
from pathlib import Path

from zf.core.config.schema import ZfConfig
from zf.core.events import EventWriter
from zf.core.events.factory import event_log_from_project
from zf.core.state.atomic_io import atomic_write_text
from zf.web.projections.request_util import reconcile_pending_idempotency_keys
from zf.web.agent_turn_store import AgentTurnStore, TURN_TERMINAL_STATES
from zf.web.webchat_runtime import durable_webchat_enabled


def reconcile_kanban_startup(
    state_dir: Path,
    *,
    config: ZfConfig | None,
) -> dict[str, int]:
    writer = EventWriter(event_log_from_project(state_dir, config=config))
    turn_store = AgentTurnStore(state_dir) if durable_webchat_enabled() else None
    durable_active = {
        str(item["turn_id"])
        for item in turn_store.list_turns(limit=500)
        if str(item.get("state") or "") not in TURN_TERMINAL_STATES
    } if turn_store is not None else set()
    durable_recovered = turn_store.reconcile_expired() if turn_store is not None else []
    if turn_store is not None:
        turn_store.reconcile_queue_claims()
    for item in durable_recovered:
        writer.emit(
            "agent.session.turn.recovery_queued",
            actor="web",
            payload={
                "turn_id": str(item.get("turn_id") or ""),
                "lost_attempt_id": str(item.get("attempt_id") or ""),
                "reason": "lost_owner_lease_expired",
            },
        )
    legacy = reconcile_interrupted_headless_turns(
        state_dir,
        writer,
        durable_active_turn_ids=durable_active,
    )
    return {
        **legacy,
        "turns": int(legacy["turns"]) + len(durable_recovered),
        "idempotency_keys": reconcile_pending_idempotency_keys(state_dir),
    }


def reconcile_interrupted_headless_turns(
    state_dir: Path,
    writer: EventWriter,
    *,
    durable_active_turn_ids: set[str] | None = None,
) -> dict[str, int]:
    """Fail turns that were running before this Web process started."""
    durable_active_turn_ids = durable_active_turn_ids or set()
    events = writer.event_log.read_all()
    terminal_turn_ids = {
        str((event.payload or {}).get("turn_id") or "")
        for event in events
        if event.type in {
            "kanban.agent.turn.completed",
            "kanban.agent.turn.failed",
        }
        and isinstance(event.payload, dict)
    }
    starts = {}
    for event in events:
        if event.type != "kanban.agent.turn.started":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        turn_id = str(payload.get("turn_id") or "")
        if turn_id:
            starts[turn_id] = event

    recovered = 0
    for turn_id, started in starts.items():
        if turn_id in terminal_turn_ids or turn_id in durable_active_turn_ids:
            continue
        payload = started.payload if isinstance(started.payload, dict) else {}
        failed = writer.emit(
            "kanban.agent.turn.failed",
            actor="web",
            task_id=started.task_id,
            causation_id=started.id,
            correlation_id=started.correlation_id,
            payload={
                "turn_id": turn_id,
                "thread_key": str(payload.get("thread_key") or ""),
                "project_id": str(payload.get("project_id") or ""),
                "conversation_id": str(payload.get("conversation_id") or ""),
                "backend": str(payload.get("backend") or ""),
                "permission_profile": str(
                    payload.get("permission_profile") or "read_only"
                ),
                "status": "interrupted",
                "reason": "web_server_restart_interrupted_in_flight_turn",
                "message_event_id": str(payload.get("message_event_id") or ""),
                "delta_count": 0,
            },
        )
        writer.emit(
            "runtime.action.failed",
            actor="web",
            task_id=started.task_id,
            causation_id=failed.id,
            correlation_id=started.correlation_id,
            payload={
                "action": "chat-orchestrator",
                "requested_action": str(
                    payload.get("requested_action") or "chat-orchestrator"
                ),
                "status": "interrupted",
                "reason": "web_server_restart_interrupted_in_flight_turn",
                "turn_id": turn_id,
            },
        )
        writer.emit(
            "web.action.failed",
            actor="web",
            task_id=started.task_id,
            causation_id=failed.id,
            correlation_id=started.correlation_id,
            payload={
                "action": "chat-orchestrator",
                "requested_action": str(
                    payload.get("requested_action") or "chat-orchestrator"
                ),
                "status": "interrupted",
                "reason": "web_server_restart_interrupted_in_flight_turn",
                "turn_id": turn_id,
            },
        )
        recovered += 1
    threads = _reconcile_thread_sidecars(Path(state_dir))
    return {"turns": recovered, "threads": threads}


def _reconcile_thread_sidecars(state_dir: Path) -> int:
    thread_dir = state_dir / "operator" / "threads"
    if not thread_dir.exists():
        return 0
    updated = 0
    for path in thread_dir.glob("*.json"):
        try:
            thread = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(thread, dict):
            continue
        changed = False
        if thread.get("status") == "running":
            thread["status"] = "interrupted"
            thread["last_error"] = "web_server_restart_interrupted_in_flight_turn"
            changed = True
        providers = thread.get("providers")
        if isinstance(providers, dict):
            for provider in providers.values():
                if isinstance(provider, dict) and provider.get("status") == "running":
                    provider["status"] = "interrupted"
                    provider["error"] = (
                        "web_server_restart_interrupted_in_flight_turn"
                    )
                    changed = True
        if changed:
            atomic_write_text(
                path,
                json.dumps(thread, ensure_ascii=False, indent=2) + "\n",
            )
            updated += 1
    return updated
