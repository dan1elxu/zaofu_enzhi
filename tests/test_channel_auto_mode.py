"""Auto Channel mode: template, kernel deltas, and the automatic chain."""

from __future__ import annotations

import hashlib
from pathlib import Path

from zf.core.config.schema import (
    FanoutAggregateConfig,
    ProjectConfig,
    RoleConfig,
    WorkflowConfig,
    WorkflowStageConfig,
    ZfConfig,
)
from zf.core.events import EventLog, EventWriter
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


# ---------------------------------------------------------------------------
# Consensus gate
# ---------------------------------------------------------------------------


def test_auto_consensus_reached_without_human(tmp_path: Path) -> None:
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
    assert "channel.consensus.reached" in types
    assert "channel.discussion.closed" in types


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


def test_handoff_fails_when_readiness_not_ready(tmp_path: Path, monkeypatch) -> None:
    state_dir, writer = _writer(tmp_path)
    _ready_auto_prd(tmp_path, writer, state_dir, readiness_verdict="needs_owner")
    monkeypatch.setattr(channel_auto_chain, "_agent_json_turn", _fake_handoff_turn)
    cause = _events(writer)[-1]
    result = channel_auto_chain.run_auto_chain(
        state_dir=state_dir, writer=writer, config=_chain_config(),
        project_root=tmp_path, channel_id=CH, thread_id="main",
        cause_event_id=cause.id,
    )
    assert result["ok"] is False
    failed = [
        event for event in _events(writer)
        if event.type == "channel.auto_chain.failed"
    ]
    assert failed and failed[-1].payload["status"] == "channel_prd_not_planning_authorized"


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
