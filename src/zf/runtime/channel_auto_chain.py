"""Automatic Channel pipeline: discuss → freeze the canonical PRD.

Owner approval of the ``channel-create-and-start`` Plan is the only human
step. After that an auto Channel runs without owner intervention:

1. Question resolution — an edge agent reads the phase-2 ledger and records
   resolutions for open questions (owner decisions included; the owner
   delegation was granted up front by approving the auto template Plan).
2. Consensus — auto sessions reach consensus on the full roster signature
   (see ``channel_discussion._consensus_reached``); readiness still gates
   planning downstream.
3. Completion — ``channel.consensus.reached`` remains the terminal discussion
   event and is followed by the informational ``channel.prd.frozen`` handoff.
   The latter records the reviewed, immutable canonical PRD for downstream
   evaluation; Task and Workflow planning remain explicit owner actions.

Every step is idempotent against the event ledger so restarts do not duplicate
discussion or resolution work.
"""

from __future__ import annotations

import json
import os
from functools import partial
from pathlib import Path
from typing import Any

from zf.core.events.writer import EventWriter
from zf.runtime.channel_contracts import channel_discussion_auto_enabled
from zf.runtime.channel_prd_context import canonical_channel_prd_authority
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_question_gate import question_dedup_gate_state
from zf.runtime.channel_sidecar import channel_message_event_payload
from zf.runtime.control_actions import ControlledActionService

AUTO_CHAIN_ACTOR = "channel-auto-chain"
AUTO_RESOLVER_REF = "owner:auto-resolver"
_RESOLUTION_KINDS = {"answered", "assumption", "out_of_scope", "evidence"}
_OWNER_QUESTION_KINDS = {"owner_decision", "tradeoff", "clarification"}

AUTO_CHAIN_QUESTION_EVENT_TYPES = (
    "channel.questions.frozen",
    "channel.question.dedup.applied",
    "channel.question.opened",
    # A blocked consensus causes the discussion driver to append a new
    # blocker question and move the thread back to phase2 in the same tick.
    # Observe the source event as well so the auto resolver cannot miss that
    # internally-created question before the next external event arrives.
    "channel.consensus.blocked",
    # Relay-liveness re-arm: question resolution defers while a member turn
    # is in flight (2026-09-18 test_dev5: fresh questions were resolved 20
    # seconds into a relay round, before the discussants could weigh in).
    # A member turn completing or the moderator publishing a verdict are the
    # round-boundary moments where no turn is active — those re-arm the
    # deferred resolution.
    "channel.agent.reply.completed",
    "channel.moderator.verdict.proposed",
)


def register_auto_chain_handlers(registry: Any, host: Any) -> None:
    """Wire host-aware auto-chain reactors into the event registry.

    ``EventActionRegistry`` invokes handlers with the event only.  Bind the
    orchestrator host here instead of registering the two-argument reactor
    functions directly (which would be swallowed as a side-effect TypeError).
    """
    question_handler = partial(react_channel_auto_question_event, host)
    # A consensus event freezes the canonical PRD.  Task/workflow handoff is
    # an explicit downstream owner action, so the auto discussion path does
    # not schedule it here.
    for event_type in AUTO_CHAIN_QUESTION_EVENT_TYPES:
        registry.register(
            event_type,
            question_handler,
            source="builtin",
        )
    registry.register(
        "channel.auto_chain.retry.requested",
        partial(react_channel_auto_retry_requested, host),
        source="builtin",
    )


def react_channel_consensus_reached(host: Any, event: Any) -> None:
    from zf.runtime.channel_auto_recovery import schedule_auto_step

    payload = event.payload if isinstance(event.payload, dict) else {}
    schedule_auto_step(
        host, str(payload.get("channel_id") or event.correlation_id or ""),
        str(payload.get("thread_id") or "main"), "handoff",
    )


def react_channel_auto_question_event(host: Any, event: Any) -> None:
    from zf.runtime.channel_auto_recovery import schedule_auto_step

    payload = event.payload if isinstance(event.payload, dict) else {}
    schedule_auto_step(
        host, str(payload.get("channel_id") or event.correlation_id or ""),
        str(payload.get("thread_id") or "main"), "resolve",
    )


def react_channel_auto_retry_requested(host: Any, event: Any) -> None:
    from zf.runtime.channel_auto_recovery import schedule_auto_step

    payload = event.payload if isinstance(event.payload, dict) else {}
    schedule_auto_step(
        host, str(payload.get("channel_id") or ""),
        str(payload.get("thread_id") or "main"), str(payload.get("purpose") or ""),
    )


def run_auto_chain(
    *,
    state_dir: Path,
    writer: EventWriter,
    config: Any | None,
    project_root: Path | None,
    channel_id: str,
    thread_id: str,
    cause_event_id: str,
    report_failure: bool = True,
) -> dict[str, Any]:
    """Create the Task + publish the Workflow Plan for one frozen auto PRD."""
    cause = _event_by_id(writer, cause_event_id)
    channel = project_channel(state_dir, channel_id) or {}
    if not channel_discussion_auto_enabled(channel):
        return {"ok": False, "reason": "channel is not auto"}
    authority = canonical_channel_prd_authority(
        state_dir, channel_id=channel_id, thread_id=thread_id
    )
    if not authority:
        return _fail(
            writer, cause, channel_id, thread_id,
            "channel_prd_not_planning_authorized",
            "canonical PRD is missing or its consensus is no longer current",
            report_failure=report_failure,
        )
    source_digest = str(authority.get("source_digest") or "")
    if _handoff_already_recorded(writer, channel_id, source_digest):
        return {"ok": True, "status": "existing"}

    prd_text = _hydrate_artifact_text(state_dir, str(authority.get("source_ref") or ""))
    routes = _route_choices(config)
    prompt = _HANDOFF_PROMPT.format(
        channel_id=channel_id,
        thread_id=thread_id,
        prd_ref=str(authority.get("source_ref") or ""),
        prd_digest=source_digest,
        prd_revision=int(authority.get("prd_revision") or 0),
        prd_text=prd_text or "(PRD artifact body unavailable; trust ref/digest only)",
        routes=routes,
    )
    thread_key = f"channel-auto-handoff:{channel_id}:{int(authority.get('prd_revision') or 0)}"
    json_repair_hint = (
        "Return exactly one fenced JSON object with keys title, objective, "
        "scope, acceptance_criteria, explicit_non_goals, skills_required, "
        "workflow_plan (question plus two or three options with route_id, "
        "label, description, recommended, parameters)."
    )

    from zf.web.channel_task_plan import normalize_channel_task_submit_payload

    # Bounded contract repair (2026-09-16 auto-e2e): the create-task gate may
    # reject an otherwise parseable payload (verification command shape,
    # workflow option parameters). Feed the rejection reason back into one
    # re-turn before giving up; the kernel gate stays deterministic.
    result: dict[str, Any] | None = None
    fail_status = ""
    fail_reason = ""
    for attempt in (1, 2):
        turn_prompt = prompt
        if attempt == 2 and fail_reason:
            turn_prompt = (
                f"{prompt}\n\nYour previous answer was rejected by the kernel "
                f"task-creation gate: {fail_reason}\n"
                "Fix exactly that and return the full fenced JSON object again."
            )
        parsed, error = _agent_json_turn(
            state_dir=state_dir,
            project_root=project_root,
            config=config,
            thread_key=thread_key,
            prompt=turn_prompt,
            repair_hint=json_repair_hint,
        )
        if parsed is None:
            return _fail(writer, cause, channel_id, thread_id, "handoff_agent_failed", error,
                         report_failure=report_failure)

        payload, _, normalize_error = normalize_channel_task_submit_payload({
            "title": parsed.get("title"),
            "objective": parsed.get("objective"),
            "scope": parsed.get("scope"),
            "acceptance_criteria": parsed.get("acceptance_criteria"),
            "explicit_non_goals": parsed.get("explicit_non_goals"),
            "skills_required": parsed.get("skills_required"),
            "priority": parsed.get("priority"),
            "channel_authority": authority,
            "workflow_plan": parsed.get("workflow_plan"),
        })
        if normalize_error:
            return _fail(writer, cause, channel_id, thread_id, "handoff_payload_invalid", normalize_error,
                         report_failure=report_failure)

        result = _execute_action(
            state_dir=state_dir,
            writer=writer,
            config=config,
            project_root=project_root,
            action="create-task",
            payload=payload,
            requested=cause,
            correlation_id=channel_id,
        )
        if bool(result.get("ok")):
            break
        fail_status = "create_task_failed"
        fail_reason = str(result.get("reason") or result.get("status") or "unknown")
        result = None
    if result is None:
        return _fail(writer, cause, channel_id, thread_id, fail_status, fail_reason,
                     report_failure=report_failure)
    writer.emit(
        "channel.auto_chain.completed",
        actor=AUTO_CHAIN_ACTOR,
        causation_id=cause_event_id,
        correlation_id=channel_id,
        payload={
            "channel_id": channel_id,
            "thread_id": thread_id,
            "task_id": str(result.get("task_id") or ""),
            "source_ref": str(authority.get("source_ref") or ""),
            "source_digest": source_digest,
            "workflow_plan_event_id": str(
                result.get("workflow_plan_event_id") or ""
            ),
            "status": "completed",
        },
    )
    return {"ok": True, "task_id": str(result.get("task_id") or "")}


def run_question_resolution(
    *,
    state_dir: Path,
    writer: EventWriter,
    config: Any | None,
    project_root: Path | None,
    channel_id: str,
    thread_id: str,
    report_failure: bool = True,
) -> dict[str, Any]:
    """Resolve open phase-2 questions from the discussion, then inform the Channel."""
    channel = project_channel(state_dir, channel_id) or {}
    if not channel_discussion_auto_enabled(channel):
        return {"ok": False, "reason": "channel is not auto"}
    session = (channel.get("discussions") or {}).get(thread_id) or {}
    if str(session.get("state") or "") != "phase2_relay":
        return {"ok": True, "status": "skipped_not_relay"}
    # Defer while any member turn is in flight. A resolution that lands
    # mid-round answers the question before the discussants can weigh in
    # (2026-09-18 test_dev5: questions opened during a relay round were
    # resolved 20 seconds in, while the first discussant was still
    # running). Round-boundary events (reply.completed / verdict.proposed)
    # re-arm the deferred pass once no turn is active.
    if any(
        isinstance(request, dict)
        and str(request.get("thread_id") or "main") == thread_id
        and str(request.get("status") or "") in {"requested", "started", "pending", "running"}
        for request in channel.get("reply_requests") or []
    ):
        return {"ok": True, "status": "skipped_relay_in_flight"}
    if question_dedup_gate_state(channel, thread_id=thread_id) != "open":
        return {"ok": True, "status": "skipped_gate_closed"}
    questions = _open_questions(channel, thread_id)
    if not questions:
        return {"ok": True, "status": "nothing_open"}

    prompt = _RESOLVER_PROMPT.format(
        channel_id=channel_id,
        questions=json.dumps(questions, ensure_ascii=True, indent=1),
        discussion=_recent_discussion_text(writer, channel_id, thread_id),
    )
    parsed, error = _agent_json_turn(
        state_dir=state_dir,
        project_root=project_root,
        config=config,
        thread_key=f"channel-auto-resolve:{channel_id}:{thread_id}",
        prompt=prompt,
        repair_hint=(
            "Return exactly one fenced JSON object {\"resolutions\": [...]} "
            "where each item has question_id, resolution "
            "(answered|assumption|out_of_scope|evidence), answer, and "
            "optional evidence_refs."
        ),
    )
    if parsed is None:
        return _fail(
            writer, None, channel_id, thread_id, "question_resolver_failed", error,
            report_failure=report_failure,
        )

    raw_resolutions = parsed.get("resolutions")
    if not isinstance(raw_resolutions, list):
        return _fail(writer, None, channel_id, thread_id, "question_resolver_failed",
                     "自动答题返回缺少 resolutions 列表", report_failure=report_failure)

    resolved: list[dict[str, str]] = []
    rejected: list[str] = []
    cause = None
    for raw in raw_resolutions:
        if not isinstance(raw, dict):
            continue
        question_id = str(raw.get("question_id") or "").strip()
        resolution = str(raw.get("resolution") or "").strip()
        answer = str(raw.get("answer") or "").strip()
        if not question_id or resolution not in _RESOLUTION_KINDS:
            continue
        if resolution in {"answered", "assumption"} and not answer:
            continue
        question = next(
            (item for item in questions if item["question_id"] == question_id),
            None,
        )
        if question is None:
            continue
        resolved_by = AUTO_RESOLVER_REF
        if resolution == "evidence":
            target = str(question.get("target_member_id") or "").strip()
            if target and target != "owner":
                resolved_by = target
        if cause is None:
            cause = _latest_event(writer, channel_id)
        result = _execute_action(
            state_dir=state_dir,
            writer=writer,
            config=config,
            project_root=project_root,
            action="channel-question-resolve",
            payload={
                "channel_id": channel_id,
                "thread_id": thread_id,
                "question_id": question_id,
                "resolution": resolution,
                "answer": answer,
                "resolved_by": resolved_by,
                "evidence_refs": raw.get("evidence_refs")
                if isinstance(raw.get("evidence_refs"), list)
                else [],
            },
            requested=cause,
            correlation_id=channel_id,
        )
        if bool(result.get("ok")):
            resolved.append({
                "question_id": question_id,
                "resolution": resolution,
                "answer": answer,
            })
        else:
            rejected.append(str(result.get("reason") or result.get("status") or "解答被拒绝"))

    if resolved:
        _post_resolver_summary(
            writer, channel_id, thread_id, resolved,
            config=config, project_root=project_root,
        )
    if _open_questions(project_channel(state_dir, channel_id) or {}, thread_id):
        reason = "; ".join(rejected) or "自动答题未解决全部待答问题"
        return _fail(writer, None, channel_id, thread_id, "question_resolver_failed", reason,
                     report_failure=report_failure)
    return {"ok": True, "resolved": len(resolved)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_questions(channel: dict[str, Any], thread_id: str) -> list[dict[str, Any]]:
    raw = channel.get("open_questions") or []
    items = raw.values() if isinstance(raw, dict) else raw
    questions: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("thread_id") or "main") != thread_id:
            continue
        if str(item.get("status") or "") != "open":
            continue
        questions.append({
            "question_id": str(item.get("question_id") or ""),
            "kind": str(item.get("kind") or ""),
            "question": str(item.get("question") or item.get("text") or ""),
            "target_member_id": str(item.get("target_member_id") or ""),
            "recommended_answer": str(item.get("recommended_answer") or ""),
            "asked_by": str(item.get("asked_by") or item.get("member_id") or ""),
        })
    return questions


def _agent_json_turn(
    *,
    state_dir: Path,
    project_root: Path | None,
    config: Any | None,
    thread_key: str,
    prompt: str,
    repair_hint: str,
) -> tuple[dict[str, Any] | None, str]:
    """Run one headless agent turn and parse its single fenced JSON object."""
    from zf.web.headless_agent import (
        ClaudeHeadlessBackend,
        CodexHeadlessBackend,
        KanbanHeadlessAgent,
        canonical_headless_backend,
    )

    backend = canonical_headless_backend(
        str(os.environ.get("ZF_KANBAN_AGENT_BACKEND", "")).strip()
        or str(getattr(getattr(config, "orchestrator", None), "backend", "")).strip()
    )
    if not backend:
        backend = "claude-headless"
    auto_step = thread_key.startswith(("channel-auto-resolve:", "channel-auto-handoff:"))
    options: dict[str, Any] = {}
    if auto_step:
        # These short read-only turns must fail on stream silence, including
        # a silent tool. Other Kanban/provider timeout policies are unchanged.
        timeout_s = max(1.0, float(os.environ.get("ZF_CHANNEL_AUTO_IDLE_TIMEOUT_S", "300")))
        options = {"timeout_s": timeout_s, "backends": {
            "claude-headless": ClaudeHeadlessBackend(),
            "codex-headless": CodexHeadlessBackend(tool_timeout_s=timeout_s),
        }}
    agent = KanbanHeadlessAgent(state_dir=state_dir, project_root=project_root or state_dir.parent, **options)
    last_reply = ""
    for attempt in (1, 2):
        message = prompt if attempt == 1 else f"{prompt}\n\n{repair_hint}"
        result = agent.run_turn(
            backend=backend,
            message=message,
            scope="project",
            thread_key=thread_key,
            context={"conversation_id": thread_key},
            permission_profile="read_only",
            retry_transport=not auto_step,
        )
        if not result.ok or result.incomplete:
            return None, f"{result.status}: {result.error or result.completion_reason or 'provider 未返回完整结果'}"
        last_reply = str(getattr(result, "reply", "") or "").strip()
        parsed = _parse_single_fenced_json(last_reply)
        if parsed is not None:
            return parsed, ""
    return None, f"agent did not return one parseable JSON object: {last_reply[:300]}"


# Public alias: channel_moderator reuses the same bounded headless-JSON turn
# pattern without importing a private name across sibling modules.
agent_json_turn = _agent_json_turn


def _parse_single_fenced_json(text: str) -> dict[str, Any] | None:
    for candidate in text.split("```"):
        candidate = candidate.strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    if text.strip().startswith("{"):
        try:
            parsed = json.loads(text.strip())
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _execute_action(
    *,
    state_dir: Path,
    writer: EventWriter,
    config: Any | None,
    project_root: Path | None,
    action: str,
    payload: dict[str, Any],
    requested: Any | None,
    correlation_id: str,
) -> dict[str, Any]:
    service = ControlledActionService(
        state_dir,
        writer,
        config=config,
        project_root=project_root,
        actor=AUTO_CHAIN_ACTOR,
        source=AUTO_CHAIN_ACTOR,
        surface=AUTO_CHAIN_ACTOR,
    )
    if requested is None:
        requested = writer.emit(
            "channel.auto_chain.step",
            actor=AUTO_CHAIN_ACTOR,
            correlation_id=correlation_id,
            payload={"action": action, "channel_id": correlation_id},
        )
    try:
        return service.execute(
            action=action,
            requested_action=action,
            payload=payload,
            requested=requested,
        )
    except Exception as exc:  # defensive: action failures land in the event
        return {"ok": False, "reason": str(exc)}


def _post_resolver_summary(
    writer: EventWriter,
    channel_id: str,
    thread_id: str,
    resolved: list[dict[str, str]],
    *,
    config: Any | None,
    project_root: Path | None,
) -> None:
    """Post the resolutions into the Channel so the relay stays informed."""
    from zf.runtime.channel_router import route_channel_message

    lines = [
        f"[auto-resolver] Resolved {len(resolved)} question(s):",
        *[
            f"- {item['question_id']} → {item['resolution']}: {item['answer']}"
            for item in resolved
        ],
    ]
    text = "\n".join(lines)
    state_dir = _writer_state_dir(writer)
    message_payload = channel_message_event_payload(
        state_dir,
        {
            "channel_id": channel_id,
            "thread_id": thread_id,
            "member_id": "operator",
            "role": "user",
            "source": "runtime",
            "text": text,
            "mentions": [],
        },
        created_by=AUTO_CHAIN_ACTOR,
    )
    message = writer.emit(
        "channel.message.posted",
        actor=AUTO_CHAIN_ACTOR,
        correlation_id=channel_id,
        payload=message_payload,
    )
    route_channel_message(
        state_dir=state_dir,
        writer=writer,
        message_event=message,
        message_payload=message.payload,
        actor=AUTO_CHAIN_ACTOR,
        source="runtime",
        project_root=project_root,
        config=config,
        dispatch_inline=True,
    )


def _writer_state_dir(writer: EventWriter) -> Path:
    events_path = getattr(getattr(writer, "event_log", None), "path", None)
    if events_path is None:
        raise RuntimeError("cannot derive state_dir from event writer")
    return Path(events_path).parent


def _handoff_already_recorded(writer: EventWriter, channel_id: str, source_digest: str) -> bool:
    bare = source_digest.removeprefix("sha256:")
    for event in writer.event_log.read_all():
        if event.type == "channel.auto_chain.completed":
            payload = event.payload if isinstance(event.payload, dict) else {}
            if str(payload.get("source_digest") or "").removeprefix("sha256:") == bare:
                return True
        if event.type == "task.created":
            payload = event.payload if isinstance(event.payload, dict) else {}
            request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
            authority = (
                request.get("channel_authority")
                if isinstance(request.get("channel_authority"), dict)
                else {}
            )
            if str(authority.get("source_digest") or "").removeprefix("sha256:") == bare:
                return True
    return False


def _hydrate_artifact_text(state_dir: Path, ref: str) -> str:
    if not ref:
        return ""
    path = Path(ref)
    if path.is_absolute() or ".." in path.parts:
        return ""
    candidate = state_dir / path
    try:
        if not candidate.is_file():
            return ""
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:20000]


def _route_choices(config: Any | None) -> str:
    try:
        from zf.runtime.workflow_route_catalog import workflow_route_catalog

        catalog = workflow_route_catalog(config)
    except Exception:
        return "(route catalog unavailable)"
    rows = []
    for route in catalog.get("routes") or []:
        if not isinstance(route, dict) or not str(route.get("route_id") or route.get("id") or ""):
            continue
        row: dict[str, Any] = {
            "route_id": str(route.get("route_id") or route.get("id") or ""),
            "default_tier": str(route.get("default_tier") or ""),
        }
        if str(route.get("start_adapter") or "") in {
            "delivery_request_submit",
            "light_delivery_request_submit",
            "registered_general",
        }:
            try:
                from zf.core.workflow.request_policy import required_fields_for_kind

                row["required_parameters"] = required_fields_for_kind(
                    str(route.get("kind") or "workflow")
                )
            except Exception:
                pass
        rows.append(row)
    return json.dumps(rows, ensure_ascii=True)


def _recent_discussion_text(writer: EventWriter, channel_id: str, thread_id: str) -> str:
    messages: list[str] = []
    for event in reversed(writer.event_log.read_all()):
        if event.type != "channel.message.posted":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        if str(payload.get("channel_id") or "") != channel_id:
            continue
        if str(payload.get("thread_id") or "main") != thread_id:
            continue
        member = str(payload.get("member_id") or "")
        text = str(payload.get("text") or "")[:800]
        if text:
            messages.append(f"[{member}] {text}")
        if len(messages) >= 40:
            break
    return "\n".join(reversed(messages))


def _event_by_id(writer: EventWriter, event_id: str) -> Any | None:
    if not event_id:
        return None
    for event in reversed(writer.event_log.read_all()):
        if event.id == event_id:
            return event
    return None


def _latest_event(writer: EventWriter, channel_id: str) -> Any | None:
    for event in reversed(writer.event_log.read_all()):
        if str(event.correlation_id or "") == channel_id:
            return event
    return None


def _fail(
    writer: EventWriter,
    cause: Any | None,
    channel_id: str,
    thread_id: str,
    status: str,
    reason: str,
    *,
    report_failure: bool = True,
) -> dict[str, Any]:
    if report_failure:
        _emit_failed(writer, channel_id, thread_id, status, reason, cause_id=getattr(cause, "id", ""))
    return {"ok": False, "reason": reason}


def _emit_failed(
    writer: EventWriter,
    channel_id: str,
    thread_id: str,
    status: str,
    reason: str,
    *,
    cause_id: str = "",
) -> None:
    writer.emit(
        "channel.auto_chain.failed",
        actor=AUTO_CHAIN_ACTOR,
        causation_id=cause_id,
        correlation_id=channel_id,
        payload={
            "channel_id": channel_id,
            "thread_id": thread_id,
            "status": status,
            "reason": reason[:500],
        },
    )


_HANDOFF_PROMPT = """You are the Kanban Agent finalizing an automatic ZaoFu Channel handoff.

Channel: {channel_id} (thread {thread_id})
Frozen PRD: {prd_ref}
PRD digest: sha256:{prd_digest}
PRD revision: {prd_revision}

Read the frozen PRD below and propose the delivery package. Return a short
Markdown note, then exactly one fenced JSON object (no other fenced blocks)
with keys:
- title: concise Task title
- objective: one paragraph behavior/goal for the Task contract
- scope: list of repo-relative path/glob strings affected
- acceptance_criteria: list of concrete, checkable criteria (keep the PRD's)
- explicit_non_goals: list
- skills_required: list
- workflow_plan: {{"question": <short question>, "options": [2-3 of
  {{"route_id", "label", "description", "recommended", "parameters"}}]}};
  use only route_id values from this catalog: {routes}; mark exactly one
  option "recommended": true. Every option's "parameters" must include that
  route's "required_parameters" from the catalog (repo-relative paths or
  other concrete executable values, never prose).

Do not invent route ids. Do not propose actions outside this JSON.

Frozen PRD:
{prd_text}
"""

_RESOLVER_PROMPT = """You are the automatic owner-resolver for ZaoFu Channel {channel_id}.

The Channel discussion is fully automatic; you record the resolutions a human
owner would have recorded. Judge only from the discussion excerpt below.

Open questions:
{questions}

Recent discussion:
{discussion}

Return a short Markdown note, then exactly one fenced JSON object (no other
fenced blocks) {{"resolutions": [...]}}. Each item:
- question_id: exact id from the list
- resolution: answered | assumption | out_of_scope | evidence
  (evidence only when the discussion already contains the requested fact;
   otherwise prefer answered/assumption using the recommended answer)
- answer: the resolution text in Chinese, grounded in the discussion
- evidence_refs: optional list of event ids backing an evidence resolution

Resolve every open question. Do not invent question ids.
"""
