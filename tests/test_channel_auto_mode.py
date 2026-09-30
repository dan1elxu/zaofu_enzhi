"""Auto Channel mode: template, kernel deltas, and the automatic chain."""

from __future__ import annotations

import hashlib

import pytest
from types import SimpleNamespace
from pathlib import Path

from tests.channel_prd_fixture import persist_test_prd

from zf.core.config.schema import (
    FanoutAggregateConfig,
    ProjectConfig,
    RoleConfig,
    WorkflowConfig,
    WorkflowStageConfig,
    ZfConfig,
)
from zf.core.events import EventLog, EventWriter
from zf.core.reactor.registry import EventActionRegistry
from zf.core.task.store import TaskStore
from zf.runtime import channel_auto_chain
from zf.runtime.channel_contract_artifacts import (
    persist_channel_conclusion,
    persist_channel_prd,
    persist_channel_prd_readiness,
)
from zf.runtime.channel_contracts import channel_discussion_auto_enabled
from zf.runtime.channel_discussion import advance_discussion
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_templates import materialize_channel_template
from zf.runtime.control_actions import ControlledActionService
from zf.runtime.orchestrator import _kernel_owns_liveness_event

CH = "ch-auto"


def _writer(tmp_path: Path) -> tuple[Path, EventWriter]:
    state_dir = tmp_path / ".zf"
    state_dir.mkdir(exist_ok=True)
    return state_dir, EventWriter(EventLog(state_dir / "events.jsonl"))


def _events(writer: EventWriter):
    return writer.event_log.read_all()


def _auto_channel(writer: EventWriter, state_dir: Path, tmp_path: Path) -> None:
    """Create an auto channel with three agent members, up to an idle thread."""
    service = ControlledActionService(
        state_dir,
        writer,
        config=ZfConfig(project=ProjectConfig(name="auto-test")),
        project_root=tmp_path,
        actor="web",
        source="web",
        surface="web",
    )
    requested = writer.emit(
        "web.action.requested", actor="web", payload={"action": "channel-create"}
    )
    service.execute(
        action="channel-create",
        requested_action="channel-create",
        payload={"channel_id": CH, "name": "Auto", "owner_actor_ref": "web"},
        requested=requested,
    )
    for member, role in (("pm-1", "product_pm"), ("arch-1", "arch"), ("critic-1", "critic")):
        req = writer.emit(
            "web.action.requested", actor="web", payload={"action": "channel-invite-member"}
        )
        service.execute(
            action="channel-invite-member",
            requested_action="channel-invite-member",
            payload={
                "channel_id": CH,
                "member_id": member,
                "provider": "fake",
                "member_type": "provider_agent",
                "channel_role": role,
                "permissions": ["read", "message", "summarize"],
            },
            requested=req,
        )
    writer.emit(
        "channel.discussion.mode.set",
        actor="web",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "mode": "multi_lens",
            "auto": True,
            "synthesizer": "pm-1",
            "participants": ["pm-1", "arch-1", "critic-1"],
            "source": "web",
        },
    )


def _open_owner_question(writer: EventWriter) -> None:
    writer.emit(
        "channel.question.dedup.requested",
        actor="runtime",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "request_id": "dedup-1",
            "ledger_digest": "ledger-1",
            "source": "runtime",
        },
    )
    writer.emit(
        "channel.question.dedup.applied",
        actor="pm-1",
        correlation_id=CH,
        payload={"channel_id": CH, "thread_id": "main", "request_id": "dedup-1"},
    )
    writer.emit(
        "channel.question.opened",
        actor="arch-1",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "question_id": "q-owner-1",
            "kind": "owner_decision",
            "question": "Which login method should the PRD require?",
            "target_member_id": "owner",
            "recommended_answer": "Session cookie",
            "status": "open",
            "asked_by": "arch-1",
            "source": "runtime",
        },
    )
    writer.emit(
        "channel.discussion.phase.changed",
        actor="runtime",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "phase": "phase2_relay",
            "reason": "test_setup",
            "source": "runtime",
        },
    )


def _consensus(writer: EventWriter, kind: str, actor: str, **extra) -> None:
    writer.emit(
        f"channel.consensus.{kind}",
        actor=actor,
        correlation_id=CH,
        payload={"channel_id": CH, "thread_id": "main", **extra},
    )


# ---------------------------------------------------------------------------
# Template + contracts
# ---------------------------------------------------------------------------


def test_auto_template_materializes_with_auto_flag(tmp_path: Path) -> None:
    materialized, error = materialize_channel_template("auto")
    assert error == ""
    assert materialized is not None
    assert materialized["discussion"]["mode"] == "multi_lens"
    assert materialized["discussion"]["auto"] is True
    assert materialized["discussion"]["synthesizer"] == "synthesizer"
    digest_a = materialized["materialization_digest"]
    materialized_b, _ = materialize_channel_template("auto")
    assert materialized_b["materialization_digest"] == digest_a


def test_discussion_mode_auto_override_sets_flag(tmp_path: Path) -> None:
    materialized, error = materialize_channel_template(
        "prd-clarification", overrides={"discussion_mode": "auto"}
    )
    assert error == ""
    assert materialized["discussion"]["mode"] == "multi_lens"
    assert materialized["discussion"]["auto"] is True
    _, error = materialize_channel_template(
        "prd-clarification", overrides={"discussion_mode": "sideways"}
    )
    assert "multi_lens, or auto" in error


def test_fold_records_auto_flag(tmp_path: Path) -> None:
    state_dir, writer = _writer(tmp_path)
    _auto_channel(writer, state_dir, tmp_path)
    channel = project_channel(state_dir, CH) or {}
    assert channel_discussion_auto_enabled(channel) is True


def test_auto_chain_handlers_bind_the_orchestrator_host(
    tmp_path: Path, monkeypatch,
) -> None:
    """The event registry passes only ``event`` to side-effect handlers."""
    state_dir, writer = _writer(tmp_path)
    host = SimpleNamespace(state_dir=state_dir)
    seen: list[tuple[object, str]] = []

    def _record(received_host, event):
        seen.append((received_host, event.type))

    monkeypatch.setattr(channel_auto_chain, "react_channel_auto_question_event", _record)
    registry = EventActionRegistry()
    channel_auto_chain.register_auto_chain_handlers(registry, host)

    question = writer.emit("channel.question.opened", correlation_id=CH)
    blocked = writer.emit("channel.consensus.blocked", correlation_id=CH)
    registry.resolve("channel.question.opened")[0].handler(question)
    registry.resolve("channel.consensus.blocked")[0].handler(blocked)
    assert not registry.resolve("channel.consensus.reached")

    assert seen == [
        (host, "channel.question.opened"),
        (host, "channel.consensus.blocked"),
    ]


def test_auto_handoff_event_is_kernel_owned_even_with_layer2() -> None:
    assert _kernel_owns_liveness_event(None, "channel.consensus.reached") is True


# ---------------------------------------------------------------------------
# Consensus gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("readiness", ["ready", "needs_multi_lens", "needs_owner"])
def test_auto_consensus_reached_without_human(tmp_path: Path, readiness: str) -> None:
    state_dir, writer = _writer(tmp_path)
    _auto_channel(writer, state_dir, tmp_path)
    writer.emit(
        "channel.discussion.phase.changed",
        actor="runtime",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "phase": "phase3_synthesis",
            "reason": "test_setup",
            "source": "runtime",
        },
    )
    artifact = persist_test_prd(state_dir, CH, verdict=readiness)
    frozen_files = {
        ref: (state_dir / ref).read_bytes()
        for ref in (artifact["artifact_ref"], artifact["readiness_ref"], artifact["conclusion_ref"])
    }
    _consensus(
        writer, "proposed", "pm-1",
        **artifact,
        required_signers=["pm-1", "arch-1", "critic-1"],
        proposed_by="pm-1",
    )
    for member in ("pm-1", "arch-1", "critic-1"):
        _consensus(writer, "signed", member, member_id=member)
    advance_discussion(state_dir, writer, channel_id=CH, thread_id="main")
    advance_discussion(state_dir, writer, channel_id=CH, thread_id="main")
    types = [event.type for event in _events(writer)]
    assert types.count("channel.consensus.reached") == 1
    assert "channel.discussion.closed" in types
    assert "task.created" not in types
    assert "workflow.request.proposed" not in types
    assert project_channel(state_dir, CH)["consensus"]["main"]["readiness_verdict"] == readiness
    assert all((state_dir / ref).read_bytes() == body for ref, body in frozen_files.items())


def test_non_auto_consensus_still_requires_human(tmp_path: Path) -> None:
    state_dir, writer = _writer(tmp_path)
    _auto_channel(writer, state_dir, tmp_path)
    writer.emit(
        "channel.discussion.mode.set",
        actor="web",
        correlation_id=CH,
        payload={"channel_id": CH, "mode": "multi_lens", "auto": False, "source": "web"},
    )
    writer.emit(
        "channel.discussion.phase.changed",
        actor="runtime",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "phase": "phase3_synthesis",
            "reason": "test_setup",
            "source": "runtime",
        },
    )
    _consensus(
        writer, "proposed", "pm-1",
        artifact_ref="channels/ch-auto/prd/r1.json",
        artifact_digest="d1", prd_ref="channels/ch-auto/prd/r1.json",
        prd_digest="d1", prd_revision=1,
        readiness_ref="channels/ch-auto/prd/r1-readiness.json",
        readiness_digest="rd1", readiness_verdict="ready",
        implementation_start=False,
        required_signers=["pm-1", "arch-1", "critic-1"],
        proposed_by="pm-1",
    )
    for member in ("pm-1", "arch-1", "critic-1"):
        _consensus(writer, "signed", member, member_id=member)
    advance_discussion(state_dir, writer, channel_id=CH, thread_id="main")
    types = [event.type for event in _events(writer)]
    assert "channel.consensus.reached" not in types


# ---------------------------------------------------------------------------
# Question resolver
# ---------------------------------------------------------------------------


def test_resolver_answers_open_owner_question(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _writer(tmp_path)
    _auto_channel(writer, state_dir, tmp_path)
    _open_owner_question(writer)

    def _fake_turn(**_kwargs):
        return {
            "resolutions": [
                {
                    "question_id": "q-owner-1",
                    "resolution": "answered",
                    "answer": "Session cookie（按 recommended_answer 收敛）",
                }
            ]
        }, ""

    monkeypatch.setattr(channel_auto_chain, "_agent_json_turn", _fake_turn)
    result = channel_auto_chain.run_question_resolution(
        state_dir=state_dir,
        writer=writer,
        config=ZfConfig(project=ProjectConfig(name="auto-test")),
        project_root=tmp_path,
        channel_id=CH,
        thread_id="main",
    )
    assert result["ok"] is True
    channel = project_channel(state_dir, CH) or {}
    question = next(
        item for item in channel["open_questions"]
        if item["question_id"] == "q-owner-1"
    )
    assert question["status"] == "resolved"
    assert question["resolved_by"] == "owner:auto-resolver"
    assert not channel["rejected_resolutions"]
    texts = [
        str(event.payload.get("text") or "")
        for event in _events(writer)
        if event.type == "channel.message.posted"
        and str(event.payload.get("member_id") or "") == "operator"
    ]
    assert any("q-owner-1" in text for text in texts)


def test_resolver_skips_non_relay_phase(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _writer(tmp_path)
    _auto_channel(writer, state_dir, tmp_path)
    monkeypatch.setattr(
        channel_auto_chain,
        "_agent_json_turn",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no agent call")),
    )
    result = channel_auto_chain.run_question_resolution(
        state_dir=state_dir,
        writer=writer,
        config=None,
        project_root=tmp_path,
        channel_id=CH,
        thread_id="main",
    )
    assert result["status"] == "skipped_not_relay"


# ---------------------------------------------------------------------------
# Handoff chain
# ---------------------------------------------------------------------------


def _ready_auto_prd(
    tmp_path: Path,
    writer: EventWriter,
    state_dir: Path,
    *,
    readiness_verdict: str = "ready",
) -> dict:
    service = ControlledActionService(
        state_dir,
        writer,
        config=ZfConfig(project=ProjectConfig(name="auto-test")),
        project_root=tmp_path,
        actor="web",
        source="web",
        surface="web",
    )
    req = writer.emit(
        "web.action.requested", actor="web", payload={"action": "channel-create"}
    )
    service.execute(
        action="channel-create",
        requested_action="channel-create",
        payload={"channel_id": CH, "name": "Auto", "owner_actor_ref": "web"},
        requested=req,
    )
    for member, role in (("pm-1", "product_pm"), ("arch-1", "arch"), ("critic-1", "critic")):
        req = writer.emit(
            "web.action.requested", actor="web", payload={"action": "channel-invite-member"}
        )
        service.execute(
            action="channel-invite-member",
            requested_action="channel-invite-member",
            payload={
                "channel_id": CH,
                "member_id": member,
                "provider": "fake",
                "member_type": "provider_agent",
                "channel_role": role,
                "permissions": [
                    "read", "message", "summarize",
                    "propose_workflow" if member == "pm-1" else "message",
                ],
            },
            requested=req,
        )
    req = writer.emit(
        "web.action.requested", actor="web", payload={"action": "channel-set-leader"}
    )
    service.execute(
        action="channel-set-leader",
        requested_action="channel-set-leader",
        payload={"channel_id": CH, "leader_member_id": "pm-1", "idempotency_key": "l1"},
        requested=req,
    )
    writer.emit(
        "channel.discussion.mode.set",
        actor="web",
        correlation_id=CH,
        payload={"channel_id": CH, "mode": "multi_lens", "auto": True, "source": "web"},
    )

    spec_ref = "channel-artifacts/ch-auto/spec-r1.md"
    spec_path = state_dir / spec_ref
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = "# Auto PRD\n\n## Acceptance Criteria\n- `python -m pytest -q` passes.\n"
    spec_path.write_text(markdown, encoding="utf-8")
    spec_digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    readiness = persist_channel_prd_readiness(
        state_dir, channel_id=CH, thread_id="main", revision=1,
        body={
            "verdict": readiness_verdict,
            "implementation_start": True,
            "gaps": [],
            "risks": [],
            "evidence_refs": ["event:requirement"],
        },
        created_by="pm-1", source_event_id="evt-synth",
    )
    semantic = {
        "summary": "Deliver the auto-discussed PRD scope.",
        "acceptance_criteria": [
            {
                "id": "AC-01",
                "criterion": "Declared verification command passes.",
                "verification_command_refs": ["VC-01"],
                "producer_paths": ["tests/test_auto.py"],
            },
        ],
        "verification_commands": [
            {
                "id": "VC-01",
                "command": "python -m pytest -q",
                "covers": ["AC-01"],
                "producer_paths": ["tests/test_auto.py"],
            },
        ],
        "out_of_scope": ["YAML output"],
        "scope": ["src/**", "tests/**"],
        "affected_files": ["src/**", "tests/**"],
        "shared_files": [],
        "exclusive_files": ["src/**"],
    }
    prd = persist_channel_prd(
        state_dir, channel_id=CH, thread_id="main", revision=1,
        previous_ref="", previous_digest="",
        body={
            "summary": semantic["summary"],
            "title": "Auto delivery",
            "synthesis": semantic,
            "markdown": markdown,
            "spec_path": spec_ref,
            "spec_digest": spec_digest,
            "source_refs": ["event:requirement"],
            "evidence_refs": [],
        },
        readiness_descriptor=readiness,
        created_by="pm-1", source_event_id="evt-synth",
    )
    conclusion = persist_channel_conclusion(
        state_dir, channel_id=CH, thread_id="main", revision=1,
        prd_descriptor=prd, readiness_descriptor=readiness,
        summary=semantic["summary"], source_refs=["event:requirement"],
        created_by="pm-1", source_event_id="evt-synth",
    )
    artifact_payload = {
        "channel_id": CH,
        "thread_id": "main",
        "artifact_ref": prd["ref"],
        "artifact_digest": prd["sha256"],
        "prd_ref": prd["ref"],
        "prd_digest": prd["sha256"],
        "prd_revision": 1,
        "readiness_ref": readiness["ref"],
        "readiness_digest": readiness["sha256"],
        "readiness_verdict": readiness_verdict,
        "implementation_start": True,
        "conclusion_ref": conclusion["ref"],
        "conclusion_digest": conclusion["sha256"],
        "spec_path": spec_ref,
        "spec_digest": spec_digest,
        "source_refs": ["event:requirement"],
        "required_signers": ["pm-1", "arch-1", "critic-1"],
    }
    writer.emit(
        "channel.synthesis.proposed", actor="pm-1", correlation_id=CH,
        payload={**artifact_payload, "summary": semantic["summary"]},
    )
    writer.emit(
        "channel.consensus.proposed", actor="pm-1", correlation_id=CH,
        payload={**artifact_payload, "proposed_by": "pm-1"},
    )
    for member in ("pm-1", "arch-1", "critic-1"):
        writer.emit(
            "channel.consensus.signed", actor=member, correlation_id=CH,
            payload={"channel_id": CH, "thread_id": "main", "member_id": member},
        )
    writer.emit(
        "channel.consensus.reached", actor="channel-discussion", correlation_id=CH,
        payload={**artifact_payload, "confirmed_by": ""},
    )
    return artifact_payload


def _chain_config() -> ZfConfig:
    return ZfConfig(
        project=ProjectConfig(name="auto-test"),
        roles=[
            RoleConfig(name="dev-a", backend="mock", role_kind="reader"),
        ],
        workflow=WorkflowConfig(stages=[
            WorkflowStageConfig(
                id="implement",
                trigger="workflow.invoke.requested",
                topology="fanout_reader",
                roles=["dev-a"],
                target_ref="task/${task_id}",
                aggregate=FanoutAggregateConfig(
                    mode="wait_for_all",
                    success_event="review.approved",
                    failure_event="review.rejected",
                ),
            ),
        ]),
    )


def _fake_handoff_turn(**_kwargs):
    return {
        "title": "Auto delivery task",
        "objective": "Deliver the auto-discussed PRD scope.",
        "scope": ["src/**", "tests/**"],
        "acceptance_criteria": ["Declared verification command passes."],
        "explicit_non_goals": ["YAML output"],
        "skills_required": [],
        "workflow_plan": {
            "question": "Choose the delivery route",
            "options": [
                {
                    "route_id": "general:implement",
                    "label": "Standard PRD delivery",
                    "description": "Run the standard PRD scan route.",
                    "recommended": True,
                    "parameters": {},
                },
                {
                    "route_id": "general:implement",
                    "label": "Defer",
                    "description": "Do not run yet.",
                    "recommended": False,
                    "mode": "defer",
                },
            ],
        },
    }, ""


def test_handoff_creates_task_and_workflow_plan(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _writer(tmp_path)
    artifact = _ready_auto_prd(tmp_path, writer, state_dir)
    config = _chain_config()
    monkeypatch.setattr(channel_auto_chain, "_agent_json_turn", _fake_handoff_turn)

    cause = _events(writer)[-1]
    result = channel_auto_chain.run_auto_chain(
        state_dir=state_dir,
        writer=writer,
        config=config,
        project_root=tmp_path,
        channel_id=CH,
        thread_id="main",
        cause_event_id=cause.id,
    )
    assert result["ok"] is True
    store = TaskStore(state_dir / "kanban.json")
    task = store.get(result["task_id"])
    assert task is not None
    assert task.contract.source_mode == "channel_prd"
    types = [event.type for event in _events(writer)]
    assert "workflow.request.proposed" in types or "kanban.agent.plan.requested" in types
    assert "channel.auto_chain.completed" in types
    assert "workflow.invoke.requested" not in types


def test_handoff_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _writer(tmp_path)
    _ready_auto_prd(tmp_path, writer, state_dir)
    config = _chain_config()
    monkeypatch.setattr(channel_auto_chain, "_agent_json_turn", _fake_handoff_turn)
    cause = _events(writer)[-1]
    first = channel_auto_chain.run_auto_chain(
        state_dir=state_dir, writer=writer, config=config,
        project_root=tmp_path, channel_id=CH, thread_id="main",
        cause_event_id=cause.id,
    )
    second = channel_auto_chain.run_auto_chain(
        state_dir=state_dir, writer=writer, config=config,
        project_root=tmp_path, channel_id=CH, thread_id="main",
        cause_event_id=cause.id,
    )
    assert first["ok"] and second["ok"]
    assert second.get("status") == "existing"
    completed = [
        event for event in _events(writer)
        if event.type == "channel.auto_chain.completed"
    ]
    assert len(completed) == 1


def test_handoff_retries_once_with_gate_rejection_reason(tmp_path: Path, monkeypatch) -> None:
    # 2026-09-16 auto-e2e: create-task rejected a parseable payload (workflow
    # option missing executable parameters); the rejection reason must be fed
    # back into one bounded re-turn instead of failing the chain outright.
    state_dir, writer = _writer(tmp_path)
    _ready_auto_prd(tmp_path, writer, state_dir)
    config = _chain_config()
    prompts: list[str] = []
    turns = {"n": 0}

    def _recording_turn(*, prompt, **kwargs):
        prompts.append(prompt)
        turns["n"] += 1
        return _fake_handoff_turn()

    attempts = {"n": 0}

    def _flaky_execute_action(*, action, payload, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return {"ok": False, "reason": "missing executable parameter(s): target_root"}
        return {"ok": True, "task_id": "task-retry-1", "workflow_plan_event_id": "evt-wfp-1"}

    monkeypatch.setattr(channel_auto_chain, "_agent_json_turn", _recording_turn)
    monkeypatch.setattr(channel_auto_chain, "_execute_action", _flaky_execute_action)
    cause = _events(writer)[-1]
    result = channel_auto_chain.run_auto_chain(
        state_dir=state_dir, writer=writer, config=config,
        project_root=tmp_path, channel_id=CH, thread_id="main",
        cause_event_id=cause.id,
    )
    assert result["ok"] is True
    assert turns["n"] == 2
    assert "missing executable parameter(s): target_root" in prompts[1]
    types = [event.type for event in _events(writer)]
    assert "channel.auto_chain.completed" in types


def test_handoff_plans_task_with_confirmed_nonready_prd(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _writer(tmp_path)
    _ready_auto_prd(tmp_path, writer, state_dir, readiness_verdict="needs_owner")
    monkeypatch.setattr(channel_auto_chain, "_agent_json_turn", _fake_handoff_turn)
    cause = _events(writer)[-1]
    result = channel_auto_chain.run_auto_chain(
        state_dir=state_dir, writer=writer, config=_chain_config(),
        project_root=tmp_path, channel_id=CH, thread_id="main",
        cause_event_id=cause.id,
    )
    assert result["ok"] is True, result
    task = TaskStore(state_dir / "kanban.json").get(result["task_id"])
    assert task is not None
    assert task.contract.evidence_contract["readiness_verdict"] == "needs_owner"
    assert not any(event.type == "workflow.invoke.requested" for event in _events(writer))


def test_handoff_skips_non_auto_channel(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _writer(tmp_path)
    _ready_auto_prd(tmp_path, writer, state_dir)
    writer.emit(
        "channel.discussion.mode.set",
        actor="web",
        correlation_id=CH,
        payload={"channel_id": CH, "mode": "multi_lens", "auto": False, "source": "web"},
    )
    monkeypatch.setattr(channel_auto_chain, "_agent_json_turn", _fake_handoff_turn)
    cause = _events(writer)[-1]
    result = channel_auto_chain.run_auto_chain(
        state_dir=state_dir, writer=writer, config=_chain_config(),
        project_root=tmp_path, channel_id=CH, thread_id="main",
        cause_event_id=cause.id,
    )
    assert result == {"ok": False, "reason": "channel is not auto"}


def test_resolver_defers_while_relay_turn_in_flight(
    tmp_path: Path, monkeypatch,
) -> None:
    """2026-09-18 test_dev5: resolutions raced the relay — fresh questions
    were answered 20 seconds into a round, before discussants could weigh
    in. Resolution must defer while any member turn is active."""
    state_dir, writer = _writer(tmp_path)
    _auto_channel(writer, state_dir, tmp_path)
    _open_owner_question(writer)
    # A relay turn currently in flight (moderator-routed reply request).
    writer.emit(
        "channel.agent.reply.requested",
        actor="channel-moderator",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "request_id": "reply-relay-1",
            "message_id": "msg-relay-1",
            "target_member_id": "arch-1",
            "status": "started",
            "source": "runtime",
        },
    )
    monkeypatch.setattr(
        channel_auto_chain,
        "_agent_json_turn",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no agent call")),
    )
    result = channel_auto_chain.run_question_resolution(
        state_dir=state_dir,
        writer=writer,
        config=None,
        project_root=tmp_path,
        channel_id=CH,
        thread_id="main",
    )
    assert result["status"] == "skipped_relay_in_flight"


def test_resolver_runs_once_relay_turn_settles(
    tmp_path: Path, monkeypatch,
) -> None:
    """Once the in-flight turn completes, the same resolution pass runs."""
    state_dir, writer = _writer(tmp_path)
    _auto_channel(writer, state_dir, tmp_path)
    _open_owner_question(writer)
    writer.emit(
        "channel.agent.reply.requested",
        actor="channel-moderator",
        correlation_id=CH,
        payload={
            "channel_id": CH,
            "thread_id": "main",
            "request_id": "reply-relay-1",
            "message_id": "msg-relay-1",
            "target_member_id": "arch-1",
            "status": "completed",
            "source": "runtime",
        },
    )

    def _fake_turn(**_kwargs):
        return {
            "resolutions": [
                {
                    "question_id": "q-owner-1",
                    "resolution": "answered",
                    "answer": "fixed answer",
                }
            ]
        }, ""

    monkeypatch.setattr(channel_auto_chain, "_agent_json_turn", _fake_turn)
    result = channel_auto_chain.run_question_resolution(
        state_dir=state_dir,
        writer=writer,
        config=None,
        project_root=tmp_path,
        channel_id=CH,
        thread_id="main",
    )
    assert result["ok"] is True
