"""Fence blocking role activation and invoke acceptance against terminal Runs."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from zf.core.events.model import ZfEvent
from zf.core.state.locks import locked_path
from zf.core.state.role_sessions import RoleSessionRegistry
from zf.runtime.flow_roles import FlowRoleBindingError
from zf.runtime.run_admission import fold_terminal_run_scope


def require_live_activation_run(host: Any, payload: Mapping[str, Any]) -> None:
    run_id = str(payload.get("workflow_run_id") or "")
    if not run_id:
        return
    aliases, terminal = fold_terminal_run_scope(host.event_log.read_all())
    if aliases.get(run_id, run_id) in terminal:
        raise FlowRoleBindingError("run_terminal", f"workflow Run {run_id!r} is terminal")


@contextmanager
def live_run_acceptance(host: Any, payload: Mapping[str, Any]) -> Iterator[None]:
    # Run cancellation uses this same lock. Never hold it across provider startup.
    with locked_path(host.state_dir / "locks" / "run-admission"):
        require_live_activation_run(host, payload)
        yield


def reclaim_cancelled_activation_roles(
    host: Any, payload: Mapping[str, Any], spawned: list[str], roles: list[Any],
) -> None:
    from zf.runtime.flow_role_activation import (
        active_flow_role_instance_ids, _on_demand_resume_obligation,
    )

    with locked_path(host.state_dir / "locks" / "run-admission"):
        try:
            require_live_activation_run(host, payload)
        except FlowRoleBindingError:
            pass
        else:
            return
        shared = active_flow_role_instance_ids(host.config, host.event_log.read_all())
        registry = RoleSessionRegistry(
            host.state_dir / "role_sessions.yaml", project_root=str(host.project_root),
        )
        for role in roles:
            instance = role.instance_id
            if instance not in spawned or instance in shared:
                continue
            required, _task_id = _on_demand_resume_obligation(host, role)
            if required or not host.transport.is_alive(instance):
                continue
            host.transport.terminate(instance)
            event = host.event_writer.append(ZfEvent(
                type="role.lifecycle.suspended", actor="orchestrator",
                payload={"schema_version": "role-lifecycle.v1", "role": role.name,
                         "instance_id": instance, "backend": role.backend,
                         "from": "active", "to": "suspended",
                         "reason": "workflow Run terminated during role activation",
                         "workflow_run_id": str(payload.get("workflow_run_id") or ""),
                         "preserve_session": role.lifecycle.preserve_session,
                         "preserve_workdir": role.lifecycle.preserve_workdir},
                correlation_id=str(payload.get("workflow_run_id") or "") or None,
            ))
            registry.update_instance_meta(
                instance, lifecycle_state="suspended", lifecycle_transition_at=event.ts,
                lifecycle_suspended_at=event.ts,
            )
            host._set_worker_state(instance, "suspended", reason="Run terminal", force=True)
