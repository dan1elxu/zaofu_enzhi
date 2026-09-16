"""Channel moderator (主持人): round steering, conditional freeze, complexity budget.

Regression discipline: the dispatch tests drive events through the real
`Orchestrator.run_once` loop under an L2-active config — the 2026-09-16
auto-e2e bug class was wiring that only surfaced when task-less channel
events were dispatched live (direct reactor calls never exercise the
`_react_to_events` gating).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from zf.core.config.schema import (
    ProjectConfig,
    RoleConfig,
    SessionConfig,
    ZfConfig,
)
from zf.core.events.log import EventLog
from zf.core.events.model import ZfEvent
from zf.core.events.writer import EventWriter
from zf.core.state.session import SessionStore
from zf.runtime import channel_moderator as moderator
from zf.runtime.channel_discussion import advance_discussion
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_router import _debate_round_guard_reason
from zf.runtime.orchestrator import Orchestrator
from zf.runtime.tmux import TmuxSession
from zf.runtime.transport import TmuxTransport

CHANNEL_ID = "ch-mod"
TRIGGER = "m-req"
ROSTER = ["pm-1", "arch-1", "critic-1"]


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / ".zf"
    sd.mkdir()
    (sd / "memory").mkdir()
    EventLog(sd / "events.jsonl").append(ZfEvent(type="loop.started", actor="zf-cli"))
    SessionStore(sd / "session.yaml").create(project_root=str(tmp_path))
    (sd / "kanban.json").write_text("[]\n")
    return sd


@pytest.fixture
def config() -> ZfConfig:
    return ZfConfig(
        project=ProjectConfig(name="t"),
        session=SessionConfig(tmux_session="t"),
        roles=[
            RoleConfig(name="orchestrator", backend="mock"),
            RoleConfig(name="dev", backend="mock"),
        ],
    )


@pytest.fixture
def transport() -> TmuxTransport:
    return TmuxTransport(TmuxSession(session_name="t", dry_run=True))


def _seed_phase2_moderated(log: EventLog, *, budget: int = 20) -> None:
    log.append(ZfEvent(
        type="channel.created",
        actor="web",
        payload={"channel_id": CHANNEL_ID, "name": "mod", "source": "web"},
        correlation_id=CHANNEL_ID,
    ))
    log.append(ZfEvent(
        type="channel.discussion.mode.set",
        actor="web",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "mode": "multi_lens",
            "auto": True,
            "moderator": True,
        },
        correlation_id=CHANNEL_ID,
    ))
    for member in ROSTER:
        log.append(ZfEvent(
            type="channel.member.added",
            actor="web",
            payload={
                "channel_id": CHANNEL_ID,
                "thread_id": "main",
                "member_id": member,
                "member_type": "provider_agent",
                "provider": "mock",
                "backend": "mock",
                "permissions": ["read", "message"],
            },
            correlation_id=CHANNEL_ID,
        ))
    log.append(ZfEvent(
        type="channel.message.posted",
        actor="web",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "message_id": TRIGGER,
            "member_id": "web",
            "role": "user",
            "text": "做一个 CLI todo 工具",
        },
        correlation_id=CHANNEL_ID,
    ))
    log.append(ZfEvent(
        type="channel.discussion.started",
        actor="channel-discussion",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "trigger": "做一个 CLI todo 工具",
            "roster": ROSTER,
            "synthesizer": "pm-1",
            "requirement_message_id": TRIGGER,
        },
        correlation_id=CHANNEL_ID,
    ))
    log.append(ZfEvent(
        type="channel.discussion.phase.changed",
        actor="channel-discussion",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "phase": "phase2_relay",
            "reason": "blind_round_complete",
        },
        correlation_id=CHANNEL_ID,
    ))
    log.append(ZfEvent(
        type="channel.discussion.complexity.decided",
        actor=moderator.MODERATOR_ACTOR,
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "complexity": "standard",
            "max_rounds": budget,
            "member_count": len(ROSTER),
            "source": "moderator-llm",
        },
        correlation_id=CHANNEL_ID,
    ))


def _advance(state_dir: Path, writer: EventWriter) -> list[str]:
    return advance_discussion(
        state_dir,
        writer,
        channel_id=CHANNEL_ID,
        thread_id="main",
        source="test",
    )


def test_quiet_phase2_requests_moderator_verdict(state_dir: Path) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log)
    writer = EventWriter(EventLog(state_dir / 'events.jsonl'))
    emitted = _advance(state_dir, writer)
    assert "channel.moderator.verdict.requested" in emitted
    channel = project_channel(state_dir, CHANNEL_ID)
    request = channel["moderator_verdicts"][-1]
    assert request["kind"] == "requested"
    assert request["round"] == 1
    # Idempotent: a second tick must not double-request the same round.
    emitted2 = _advance(state_dir, writer)
    assert "channel.moderator.verdict.requested" not in emitted2


def test_verdict_continue_opens_round_with_moderator_message(state_dir: Path) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log)
    writer = EventWriter(EventLog(state_dir / 'events.jsonl'))
    _advance(state_dir, writer)
    channel = project_channel(state_dir, CHANNEL_ID)
    digest = moderator.round_ledger_digest(channel, "main", 1)
    writer.emit(
        "channel.moderator.verdict.proposed",
        actor=moderator.MODERATOR_ACTOR,
        correlation_id=CHANNEL_ID,
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "round": 1,
            "ledger_digest": digest,
            "action": "continue",
            "speakers": ["arch-1", "critic-1"],
            "focus": "敲定存储格式",
            "rationale": "存储格式是剩余唯一未决点",
        },
    )
    emitted = _advance(state_dir, writer)
    assert "channel.discussion.round.opened" in emitted
    assert "channel.message.posted" in emitted
    channel = project_channel(state_dir, CHANNEL_ID)
    assert channel["discussions"]["main"]["state"] == "phase2_relay"
    assert moderator.discussion_round(channel, "main") == 2
    last_msg = channel["recent_messages"][-1]
    assert last_msg["member_id"] == moderator.MODERATOR_ACTOR
    assert "@arch-1" in last_msg["text"] and "存储格式" in last_msg["text"]


def test_verdict_conclude_enters_phase3_via_synthesis(state_dir: Path) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log)
    writer = EventWriter(EventLog(state_dir / 'events.jsonl'))
    _advance(state_dir, writer)
    channel = project_channel(state_dir, CHANNEL_ID)
    digest = moderator.round_ledger_digest(channel, "main", 1)
    writer.emit(
        "channel.moderator.verdict.proposed",
        actor=moderator.MODERATOR_ACTOR,
        correlation_id=CHANNEL_ID,
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "round": 1,
            "ledger_digest": digest,
            "action": "conclude",
            "speakers": [],
            "focus": "",
        },
    )
    emitted = _advance(state_dir, writer)
    assert "channel.synthesis.requested" in emitted
    assert "channel.discussion.phase.changed" in emitted
    channel = project_channel(state_dir, CHANNEL_ID)
    assert channel["discussions"]["main"]["state"] == "phase3_synthesis"


def test_open_questions_hold_verdict_for_resolver(state_dir: Path) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log)
    log.append(ZfEvent(
        type="channel.question.opened",
        actor="pm-1",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "question_id": "q-1",
            "question": "验收标准?",
            "target_member_id": "owner",
        },
        correlation_id=CHANNEL_ID,
    ))
    writer = EventWriter(EventLog(state_dir / 'events.jsonl'))
    emitted = _advance(state_dir, writer)
    assert "channel.moderator.verdict.requested" not in emitted


def test_budget_exhaustion_concludes_without_moderator(state_dir: Path) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log, budget=1)
    log.append(ZfEvent(
        type="channel.agent.reply.requested",
        actor="router",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "request_id": "reply-1",
            "target_member_id": "arch-1",
        },
        correlation_id=CHANNEL_ID,
    ))
    log.append(ZfEvent(
        type="channel.agent.reply.completed",
        actor="arch-1",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "request_id": "reply-1",
            "target_member_id": "arch-1",
        },
        correlation_id=CHANNEL_ID,
    ))
    writer = EventWriter(EventLog(state_dir / 'events.jsonl'))
    emitted = _advance(state_dir, writer)
    assert "channel.synthesis.requested" in emitted
    channel = project_channel(state_dir, CHANNEL_ID)
    assert channel["discussions"]["main"]["state"] == "phase3_synthesis"


def test_complexity_budget_enforces_router_round_guard(state_dir: Path) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log, budget=2)
    log.append(ZfEvent(
        type="channel.agent.reply.requested",
        actor="router",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "request_id": "reply-1",
            "target_member_id": "arch-1",
        },
        correlation_id=CHANNEL_ID,
    ))
    channel = project_channel(state_dir, CHANNEL_ID)
    # First dispatch is within the budget.
    assert _debate_round_guard_reason(channel, thread_id="main") == ""
    log.append(ZfEvent(
        type="channel.agent.reply.completed",
        actor="arch-1",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "request_id": "reply-1",
            "target_member_id": "arch-1",
        },
        correlation_id=CHANNEL_ID,
    ))
    # Second dispatch would exceed the simple-task budget.
    log.append(ZfEvent(
        type="channel.agent.reply.requested",
        actor="router",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "request_id": "reply-2",
            "target_member_id": "critic-1",
        },
        correlation_id=CHANNEL_ID,
    ))
    channel = project_channel(state_dir, CHANNEL_ID)
    assert _debate_round_guard_reason(channel, thread_id="main") == "debate_round_limit_reached"


def test_verdict_llm_failure_emits_deterministic_fallback(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log)
    writer = EventWriter(EventLog(state_dir / 'events.jsonl'))
    _advance(state_dir, writer)
    monkeypatch.setattr(
        moderator, "agent_json_turn",
        lambda **kwargs: (None, "no parseable JSON"),
    )
    result = moderator.run_moderator_verdict(
        state_dir=state_dir,
        writer=writer,
        channel=project_channel(state_dir, CHANNEL_ID),
        channel_id=CHANNEL_ID,
        thread_id="main",
        request_event={
            "round": 1,
            "ledger_digest": "x",
            "replies_used": 0,
            "budget_max_rounds": 20,
            "roster": ROSTER,
            "trigger": "做一个 CLI todo 工具",
        },
    )
    assert result["ok"] is False
    channel = project_channel(state_dir, CHANNEL_ID)
    fallback = channel["moderator_verdicts"][-1]
    assert fallback["kind"] == "fallback"
    assert fallback["action"] == "continue"
    # The fallback still drives the round (default open relay), no stall.
    emitted = _advance(state_dir, writer)
    assert "channel.discussion.round.opened" in emitted


def test_classification_failure_falls_back_to_standard_budget(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log)
    monkeypatch.setattr(
        moderator, "agent_json_turn",
        lambda **kwargs: (None, "provider down"),
    )
    result = moderator.run_complexity_classification(
        state_dir=state_dir,
        writer=EventWriter(EventLog(state_dir / 'events.jsonl')),
        channel=project_channel(state_dir, CHANNEL_ID),
        channel_id=CHANNEL_ID,
        thread_id="main",
    )
    assert result["complexity"] == "standard"
    assert result["source"] == "fallback"
    channel = project_channel(state_dir, CHANNEL_ID)
    assert moderator.complexity_budget_max_rounds(channel, "main") > 0


def test_verdict_request_dispatches_through_run_once_l2_active(
    state_dir: Path, config: ZfConfig, transport: TmuxTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = EventLog(state_dir / "events.jsonl")
    _seed_phase2_moderated(log)
    orch = Orchestrator(state_dir, config, transport)
    assert orch._find_role_by_name("orchestrator") is not None

    calls: list[dict] = []
    monkeypatch.setattr(
        moderator, "run_moderator_verdict",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )
    event = orch.event_writer.emit(
        "channel.moderator.verdict.requested",
        actor="channel-discussion",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "round": 1,
            "ledger_digest": "d",
            "replies_used": 0,
            "budget_max_rounds": 20,
            "roster": ROSTER,
            "trigger": "做一个 CLI todo 工具",
        },
        correlation_id=CHANNEL_ID,
    )
    orch.run_once(events=[event])
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    assert calls, "moderator verdict reactor did not run through run_once dispatch"
    assert calls[0]["channel_id"] == CHANNEL_ID


def test_discussion_started_dispatches_classification_through_run_once(
    state_dir: Path, config: ZfConfig, transport: TmuxTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = EventLog(state_dir / "events.jsonl")
    log.append(ZfEvent(
        type="channel.created",
        actor="web",
        payload={"channel_id": CHANNEL_ID, "name": "mod", "source": "web"},
        correlation_id=CHANNEL_ID,
    ))
    log.append(ZfEvent(
        type="channel.discussion.mode.set",
        actor="web",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "mode": "multi_lens",
            "auto": True,
            "moderator": True,
        },
        correlation_id=CHANNEL_ID,
    ))
    for member in ROSTER:
        log.append(ZfEvent(
            type="channel.member.added",
            actor="web",
            payload={
                "channel_id": CHANNEL_ID,
                "thread_id": "main",
                "member_id": member,
                "member_type": "provider_agent",
                "provider": "mock",
                "backend": "mock",
                "permissions": ["read", "message"],
            },
            correlation_id=CHANNEL_ID,
        ))
    orch = Orchestrator(state_dir, config, transport)
    calls: list[dict] = []
    monkeypatch.setattr(
        moderator, "run_complexity_classification",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )
    event = orch.event_writer.emit(
        "channel.discussion.started",
        actor="channel-discussion",
        payload={
            "channel_id": CHANNEL_ID,
            "thread_id": "main",
            "trigger": "做一个 CLI todo 工具",
            "roster": ROSTER,
            "synthesizer": "pm-1",
            "requirement_message_id": TRIGGER,
        },
        correlation_id=CHANNEL_ID,
    )
    orch.run_once(events=[event])
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    assert calls, "classification reactor did not run through run_once dispatch"
