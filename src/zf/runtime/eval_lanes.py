"""Lane launchers for the three-lane PRD eval (docs/ideas/three-lane-prd-eval.md).

One interface per lane: input is the lane directory plus the entry prompt,
output is a ``LaneResult`` carrying either the lane-contract PRD path
(``<lane_dir>/prd/prd.md``) or a ``failed`` / ``timeout`` reason. Every lane
stays inside its own directory — the channel lane keeps a complete private
Channel ledger under ``<lane_dir>/.zf/`` — so lanes never touch the host
project's state dir, kanban, or ``events.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from zf.core.config.schema import ProjectConfig, ZfConfig
from zf.core.events import EventLog, EventWriter
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime import channel_auto_chain
from zf.runtime.channel_adapter import dispatch_pending_replies
from zf.runtime.channel_contracts import channel_discussion_moderator_enabled
from zf.runtime.channel_discussion import advance_discussion
from zf.runtime.channel_moderator import (
    run_complexity_classification,
    run_moderator_verdict,
)
from zf.runtime.channel_prd_freeze import PRD_FROZEN_EVENT
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_synthesis_reactor import (
    react_channel_consensus_proposed,
    react_channel_cross_review_requested,
    react_channel_question_dedup_requested,
    react_channel_synthesis_blocked,
    react_channel_synthesis_repair_requested,
    react_channel_synthesis_requested,
)
from zf.runtime.control_actions import ControlledActionService

LANE_TERMINAL = "terminal"
LANE_KANBAN = "kanban"
LANE_CHANNEL = "channel"
LANE_IDS = (LANE_TERMINAL, LANE_KANBAN, LANE_CHANNEL)
LANE_ALIASES = {"channel-auto": LANE_CHANNEL, "channel_auto": LANE_CHANNEL}

LANE_PRD_RELPATH = Path("prd") / "prd.md"
LANE_STATE_DIRNAME = ".zf"
DEFAULT_LANE_TIMEOUT_S = 30 * 60.0
DEFAULT_CHANNEL_POLL_INTERVAL_S = 2.0
EVAL_LANE_ACTOR = "eval-lane"

_PRD_CONTRACT_LINE = (
    "Write the complete PRD as one Markdown document to prd/prd.md relative "
    "to the current working directory (create the prd directory first)."
)

_TERMINAL_SYSTEM_PROMPT = (
    "You are a product engineer working in an empty scratch workspace. "
    + _PRD_CONTRACT_LINE
    + " Do not ask questions; make reasonable assumptions and record them in "
    "the PRD. The turn is complete only when prd/prd.md exists."
)


@dataclass(frozen=True)
class LaneResult:
    lane: str
    ok: bool
    status: str  # "completed" | "failed" | "timeout"
    prd_path: Path | None = None
    reason: str = ""
    started_at: str = ""
    finished_at: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "ok": self.ok,
            "status": self.status,
            "prd_path": str(self.prd_path) if self.prd_path else "",
            "reason": self.reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "detail": self.detail,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_lane_id(value: str) -> str:
    lane = str(value or "").strip()
    lane = LANE_ALIASES.get(lane, lane)
    return lane if lane in LANE_IDS else ""


def lane_prd_path(lane_dir: Path) -> Path:
    return Path(lane_dir) / LANE_PRD_RELPATH


def lane_state_dir(lane_dir: Path) -> Path:
    return Path(lane_dir) / LANE_STATE_DIRNAME


def run_lane(
    lane: str,
    *,
    lane_dir: Path,
    prompt: str,
    timeout_s: float = DEFAULT_LANE_TIMEOUT_S,
    **kwargs: Any,
) -> LaneResult:
    lane_id = canonical_lane_id(lane)
    if lane_id == LANE_TERMINAL:
        return run_terminal_lane(
            lane_dir=lane_dir, prompt=prompt, timeout_s=timeout_s, **kwargs
        )
    if lane_id == LANE_KANBAN:
        return run_kanban_lane(
            lane_dir=lane_dir, prompt=prompt, timeout_s=timeout_s, **kwargs
        )
    if lane_id == LANE_CHANNEL:
        return run_channel_lane(
            lane_dir=lane_dir, prompt=prompt, timeout_s=timeout_s, **kwargs
        )
    return LaneResult(
        lane=str(lane or ""),
        ok=False,
        status="failed",
        reason=f"unknown lane: {lane}",
        started_at=_utc_now(),
        finished_at=_utc_now(),
    )


# ---------------------------------------------------------------------------
# terminal / kanban lanes (headless provider backends)
# ---------------------------------------------------------------------------


def _resolve_headless_backend(value: str) -> str:
    from zf.web.headless_agent import canonical_headless_backend

    raw = str(value or "").strip() or os.environ.get(
        "ZF_KANBAN_AGENT_BACKEND", ""
    ).strip()
    return canonical_headless_backend(raw) or "claude-headless"


def _headless_adapters(lane_dir: Path) -> dict[str, Any]:
    from zf.web.headless_agent import ClaudeHeadlessBackend, CodexHeadlessBackend

    adapters: dict[str, Any] = {
        "claude-headless": ClaudeHeadlessBackend(),
        "codex-headless": CodexHeadlessBackend(),
    }
    state_dir = lane_state_dir(lane_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    # set_telemetry_env is the only per-call env channel into the provider
    # subprocess; pin the lane isolation vars through it.
    lane_env = {
        "ZF_PROJECT_ROOT": str(lane_dir),
        "ZF_STATE_DIR": str(state_dir),
    }
    for adapter in adapters.values():
        adapter.set_telemetry_env(lane_env)
    return adapters


def _terminal_prompt(prompt: str) -> str:
    return (
        "Produce a Product Requirements Document (PRD) for the requirement "
        f"below.\n{_PRD_CONTRACT_LINE}\n\nRequirement:\n\n{prompt.strip()}\n"
    )


def _kanban_message(prompt: str) -> str:
    return (
        "Produce a Product Requirements Document (PRD) for the requirement "
        f"below. {_PRD_CONTRACT_LINE} Do not propose controlled actions, "
        "plans, or tasks; the only deliverable is the prd/prd.md file.\n\n"
        f"Requirement:\n\n{prompt.strip()}\n"
    )


def _read_prd(prd: Path) -> str:
    try:
        return prd.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _headless_lane_result(
    lane: str,
    lane_dir: Path,
    turn: Any,
    *,
    started_at: str,
) -> LaneResult:
    detail = {
        "backend": str(getattr(turn, "backend", "") or ""),
        "provider_session_id": str(getattr(turn, "provider_session_id", "") or ""),
        "turn_status": str(getattr(turn, "status", "") or ""),
    }
    finished_at = _utc_now()
    if str(getattr(turn, "status", "") or "") == "timeout":
        return LaneResult(
            lane=lane, ok=False, status="timeout", reason="timeout",
            started_at=started_at, finished_at=finished_at, detail=detail,
        )
    prd = lane_prd_path(lane_dir)
    if bool(getattr(turn, "ok", False)) and _read_prd(prd):
        return LaneResult(
            lane=lane, ok=True, status="completed", prd_path=prd,
            started_at=started_at, finished_at=finished_at, detail=detail,
        )
    if bool(getattr(turn, "ok", False)):
        return LaneResult(
            lane=lane, ok=False, status="failed", reason="prd_missing",
            started_at=started_at, finished_at=finished_at, detail=detail,
        )
    error = str(getattr(turn, "error", "") or "provider turn failed")[:500]
    detail["error"] = error
    return LaneResult(
        lane=lane, ok=False, status="failed",
        reason=f"{getattr(turn, 'status', 'failed')}: {error}",
        started_at=started_at, finished_at=finished_at, detail=detail,
    )


def run_terminal_lane(
    *,
    lane_dir: Path,
    prompt: str,
    timeout_s: float = DEFAULT_LANE_TIMEOUT_S,
    backend: str = "",
    on_message: Any = None,
) -> LaneResult:
    """Raw headless provider turn in the lane workspace (workspace_writer)."""
    lane_dir = Path(lane_dir)
    lane_dir.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    backend_id = _resolve_headless_backend(backend)
    adapter = _headless_adapters(lane_dir)[backend_id]
    if not adapter.available():
        return LaneResult(
            lane=LANE_TERMINAL, ok=False, status="failed",
            reason=f"{backend_id} command is unavailable",
            started_at=started_at, finished_at=_utc_now(),
        )
    lane_prd_path(lane_dir).parent.mkdir(parents=True, exist_ok=True)
    turn = adapter.run_turn(
        prompt=_terminal_prompt(prompt),
        cwd=lane_dir,
        system_prompt=_TERMINAL_SYSTEM_PROMPT,
        thread_id=str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"zaofu-eval-terminal:{lane_dir.resolve()}")
        ),
        provider_session_id="",
        on_session_id=lambda _session_id: None,
        on_message=on_message,
        timeout_s=timeout_s,
        execution_deadline=time.monotonic() + timeout_s,
        permission_profile="workspace_writer",
    )
    return _headless_lane_result(LANE_TERMINAL, lane_dir, turn, started_at=started_at)


def run_kanban_lane(
    *,
    lane_dir: Path,
    prompt: str,
    timeout_s: float = DEFAULT_LANE_TIMEOUT_S,
    backend: str = "",
    on_message: Any = None,
) -> LaneResult:
    """Kanban-agent flow (KanbanHeadlessAgent thread store + system prompt)."""
    from zf.web.headless_agent import KanbanHeadlessAgent

    lane_dir = Path(lane_dir)
    lane_dir.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    backend_id = _resolve_headless_backend(backend)
    # KanbanHeadlessAgent plumbs only an idle timeout (no hard deadline); the
    # eval driver owns the per-lane wall-clock cap.
    agent = KanbanHeadlessAgent(
        state_dir=lane_state_dir(lane_dir),
        project_root=lane_dir,
        backends=_headless_adapters(lane_dir),
        timeout_s=timeout_s,
    )
    lane_prd_path(lane_dir).parent.mkdir(parents=True, exist_ok=True)
    turn = agent.run_turn(
        backend=backend_id,
        message=_kanban_message(prompt),
        thread_key=f"eval-lane:{lane_dir.name}",
        context={"conversation_id": f"eval-lane:{lane_dir.name}"},
        on_message=on_message,
        permission_profile="workspace_writer",
    )
    return _headless_lane_result(LANE_KANBAN, lane_dir, turn, started_at=started_at)


# ---------------------------------------------------------------------------
# channel-auto lane (manual pump over a lane-private Channel ledger)
# ---------------------------------------------------------------------------

_CHANNEL_REACTORS: dict[str, Callable[[Any, Any], None]] = {
    "channel.synthesis.requested": react_channel_synthesis_requested,
    "channel.discussion.next_round.proposed": react_channel_synthesis_requested,
    "channel.synthesis.repair.requested": react_channel_synthesis_repair_requested,
    "channel.synthesis.blocked": react_channel_synthesis_blocked,
    "channel.question.dedup.requested": react_channel_question_dedup_requested,
    "channel.cross_review.requested": react_channel_cross_review_requested,
    "channel.consensus.proposed": react_channel_consensus_proposed,
}

_CHANNEL_BACKENDS = {
    "claude": "claude-code",
    "claude-headless": "claude-code",
    "codex-headless": "codex",
}


def _run_guarded_step(
    warnings: list[str], label: str, fn: Callable[..., Any], **kwargs: Any
) -> Any:
    try:
        return fn(**kwargs)
    except Exception as exc:
        warnings.append(f"{label}: {exc}")
        return None


def _event_channel_id(event: Any) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return str(payload.get("channel_id") or "")


def _latest_discussion_closed(events: list[Any], *, channel_id: str) -> Any | None:
    for event in reversed(events):
        if (
            event.type == "channel.discussion.closed"
            and _event_channel_id(event) == channel_id
        ):
            return event
    return None


def run_channel_lane(
    *,
    lane_dir: Path,
    prompt: str,
    timeout_s: float = DEFAULT_LANE_TIMEOUT_S,
    backend: str = "fake",
    config: ZfConfig | None = None,
    template_id: str = "auto",
    channel_id: str = "eval-prd",
    channel_name: str = "Eval PRD",
    state_dir: Path | None = None,
    poll_interval_s: float = DEFAULT_CHANNEL_POLL_INTERVAL_S,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> LaneResult:
    """Run the Channel auto pipeline to ``channel.prd.frozen`` in the lane dir.

    The pump replaces the orchestrator's event wiring with a synchronous loop:
    reactor events (synthesis / dedup / cross-review / consensus) are replayed
    through the production reactor handlers, moderator turns run inline, and
    reply dispatch is drained each tick. ``dispatch_policy=trigger_only`` at
    creation makes this pump the only initial dispatcher.
    """
    lane_dir = Path(lane_dir)
    lane_dir.mkdir(parents=True, exist_ok=True)
    # An explicit state_dir shares the ledger with its owner (e.g. the
    # workspace the Web server projects), making the discussion visible
    # there; the default stays isolated inside the lane dir.
    state_dir = Path(state_dir) if state_dir is not None else lane_state_dir(lane_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    config = config if config is not None else ZfConfig(
        project=ProjectConfig(name="eval-lane")
    )
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    service = ControlledActionService(
        state_dir,
        writer,
        config=config,
        project_root=lane_dir,
        actor=EVAL_LANE_ACTOR,
        source=EVAL_LANE_ACTOR,
        surface=EVAL_LANE_ACTOR,
    )
    requested = writer.emit(
        "web.action.requested",
        actor=EVAL_LANE_ACTOR,
        correlation_id=channel_id,
        payload={"action": "channel-create-and-start"},
    )
    created = service.execute(
        action="channel-create-and-start",
        requested_action="channel-create-and-start",
        payload={
            "template_id": template_id,
            "channel_id": channel_id,
            "name": channel_name,
            "message": prompt,
            "overrides": {
                "backend": _CHANNEL_BACKENDS.get(backend, backend),
            },
            "dispatch_policy": "trigger_only",
        },
        requested=requested,
    )
    if not created.get("ok"):
        reason = str(created.get("reason") or created.get("status") or "unknown")
        return LaneResult(
            lane=LANE_CHANNEL, ok=False, status="failed",
            reason=f"channel_create_failed: {reason}",
            started_at=started_at, finished_at=_utc_now(),
            detail={"channel_id": channel_id},
        )
    # The controlled action owns the canonical id (e.g. "eval-prd" normalizes
    # to "ch-eval-prd"); every subsequent pump step must use it verbatim.
    channel_id = str(created.get("channel_id") or channel_id)

    host = SimpleNamespace(
        state_dir=state_dir,
        event_writer=writer,
        event_log=writer.event_log,
        project_root=lane_dir,
        config=config,
        openclaw_client=None,
    )
    deadline = time.monotonic() + timeout_s
    cursor = 0
    warnings: list[str] = []
    while True:
        events = writer.event_log.read_all()
        frozen = next(
            (
                event
                for event in events
                if event.type == PRD_FROZEN_EVENT
                and _event_channel_id(event) == channel_id
            ),
            None,
        )
        if frozen is not None:
            return _channel_lane_success(
                lane_dir, state_dir, channel_id, frozen,
                started_at=started_at, warnings=warnings,
            )
        closed = _latest_discussion_closed(events, channel_id=channel_id)
        if closed is not None:
            outcome = str(
                (closed.payload if isinstance(closed.payload, dict) else {}).get(
                    "outcome"
                )
                or ""
            )
            if outcome != "consensus":
                return LaneResult(
                    lane=LANE_CHANNEL, ok=False, status="failed",
                    reason=f"discussion_closed:{outcome or 'unknown'}",
                    started_at=started_at, finished_at=_utc_now(),
                    detail={"channel_id": channel_id, "warnings": warnings},
                )
        new_events = events[cursor:]
        cursor = len(events)
        for event in new_events:
            event_channel = _event_channel_id(event)
            if event_channel and event_channel != channel_id:
                # A shared ledger carries other channels' events too.
                continue
            if event.type == "channel.discussion.started":
                channel = project_channel(state_dir, channel_id) or {}
                if channel_discussion_moderator_enabled(channel):
                    _run_guarded_step(
                        warnings, "complexity_classification",
                        run_complexity_classification,
                        state_dir=state_dir, writer=writer, channel=channel,
                        channel_id=channel_id, thread_id="main",
                        config=config, project_root=lane_dir,
                    )
                continue
            if event.type == "channel.moderator.verdict.requested":
                channel = project_channel(state_dir, channel_id) or {}
                if channel_discussion_moderator_enabled(channel):
                    _run_guarded_step(
                        warnings, "moderator_verdict", run_moderator_verdict,
                        state_dir=state_dir, writer=writer, channel=channel,
                        channel_id=channel_id, thread_id="main",
                        request_event=dict(event.payload or {}),
                        config=config, project_root=lane_dir,
                    )
                continue
            handler = _CHANNEL_REACTORS.get(event.type)
            if handler is not None:
                try:
                    handler(host, event)
                except Exception as exc:
                    warnings.append(f"reactor:{event.type}: {exc}")
        _run_guarded_step(
            warnings, "dispatch", dispatch_pending_replies,
            state_dir=state_dir, writer=writer, channel_id=channel_id,
            actor=EVAL_LANE_ACTOR, source=EVAL_LANE_ACTOR,
            allow_queued=True, max_dispatch=64,
            project_root=lane_dir, config=config,
        )
        _run_guarded_step(
            warnings, "advance", advance_discussion,
            state_dir=state_dir, writer=writer,
            channel_id=channel_id, thread_id="main",
            actor=EVAL_LANE_ACTOR, source=EVAL_LANE_ACTOR,
        )
        _run_guarded_step(
            warnings, "question_resolution",
            channel_auto_chain.run_question_resolution,
            state_dir=state_dir, writer=writer, config=config,
            project_root=lane_dir, channel_id=channel_id, thread_id="main",
            report_failure=False,
        )
        if time.monotonic() >= deadline:
            return LaneResult(
                lane=LANE_CHANNEL, ok=False, status="timeout", reason="timeout",
                started_at=started_at, finished_at=_utc_now(),
                detail={"channel_id": channel_id, "warnings": warnings},
            )
        sleep_fn(poll_interval_s)


def _channel_lane_success(
    lane_dir: Path,
    state_dir: Path,
    channel_id: str,
    frozen: Any,
    *,
    started_at: str,
    warnings: list[str],
) -> LaneResult:
    payload = frozen.payload if isinstance(frozen.payload, dict) else {}
    prd_ref = str(payload.get("prd_ref") or payload.get("artifact_ref") or "")
    detail: dict[str, Any] = {
        "channel_id": channel_id,
        "prd_ref": prd_ref,
        "prd_digest": str(payload.get("prd_digest") or payload.get("artifact_digest") or ""),
        "prd_revision": payload.get("prd_revision"),
        "warnings": warnings,
    }

    def fail(reason: str) -> LaneResult:
        return LaneResult(
            lane=LANE_CHANNEL, ok=False, status="failed", reason=reason,
            started_at=started_at, finished_at=_utc_now(), detail=detail,
        )

    if not prd_ref:
        return fail("prd_ref_missing")
    sidecar = (state_dir / prd_ref).resolve()
    if not sidecar.is_relative_to(state_dir.resolve()):
        return fail("prd_ref_outside_state_dir")
    try:
        raw = sidecar.read_bytes()
    except OSError as exc:
        return fail(f"prd_sidecar_missing: {exc}")
    if detail["prd_digest"] and hashlib.sha256(raw).hexdigest() != detail["prd_digest"]:
        return fail("prd_digest_mismatch")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return fail(f"prd_sidecar_invalid: {exc}")
    body = data.get("body") if isinstance(data, dict) else None
    markdown = str((body or {}).get("markdown") or "").strip()
    if not markdown:
        return fail("prd_markdown_missing")
    target = lane_prd_path(lane_dir)
    atomic_write_text(target, markdown + "\n")
    return LaneResult(
        lane=LANE_CHANNEL, ok=True, status="completed", prd_path=target,
        started_at=started_at, finished_at=_utc_now(), detail=detail,
    )


__all__ = [
    "DEFAULT_CHANNEL_POLL_INTERVAL_S",
    "DEFAULT_LANE_TIMEOUT_S",
    "EVAL_LANE_ACTOR",
    "LANE_CHANNEL",
    "LANE_IDS",
    "LANE_KANBAN",
    "LANE_PRD_RELPATH",
    "LANE_TERMINAL",
    "LaneResult",
    "canonical_lane_id",
    "lane_prd_path",
    "lane_state_dir",
    "run_channel_lane",
    "run_kanban_lane",
    "run_lane",
    "run_terminal_lane",
]
