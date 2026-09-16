"""Channel discussion moderator (主持人): round steering + conditional freeze.

The moderator is an edge KanbanHeadlessAgent turn (same bounded pattern as
the auto-chain resolver), not a Channel member: it never enters the roster,
so the auto consensus gate (full roster signature) is unchanged.

Responsibilities, per discussion config flag ``moderator: true``:

1. At discussion start, classify requirement complexity and decide the
   reply-dispatch budget (``channel.discussion.complexity.decided``).
2. At each phase2 round boundary (the thread is quiet: no pending/running
   replies, no open questions), request a verdict: ``continue`` with the
   next round's speakers + focus, or ``conclude`` the discussion.
3. On conclude, the kernel gate (zero open questions + completed
   cross-reviews) must still pass before synthesis is requested — semantic
   judgment lives here, invariants stay in the kernel.

Bounded by construction: the complexity budget force-concludes the
discussion once exhausted, every LLM failure falls back deterministically
(``channel.moderator.verdict.fallback``), and the phase wall-clock deadline
still closes stalled threads. A moderated discussion therefore cannot loop
forever, whatever the LLM does.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from zf.runtime.channel_auto_chain import agent_json_turn
from zf.runtime.channel_contracts import (
    channel_discussion_moderator_enabled,
    default_debate_max_rounds,
)

MODERATOR_ACTOR = "channel-moderator"

MODERATOR_EVENT_TYPES = (
    "channel.discussion.complexity.decided",
    "channel.discussion.round.opened",
    "channel.moderator.verdict.requested",
    "channel.moderator.verdict.proposed",
    "channel.moderator.verdict.fallback",
)

COMPLEXITY_LEVELS = ("simple", "standard", "complex")

_IN_FLIGHT: set[str] = set()


# ---------------------------------------------------------------------------
# fold accessors
# ---------------------------------------------------------------------------

def discussion_round(channel: dict[str, Any], thread_id: str) -> int:
    """Current phase2 round: 1 at phase2 entry, +1 per accepted continue."""
    rounds = [
        item for item in (channel.get("moderator_rounds") or [])
        if isinstance(item, dict)
        and str(item.get("thread_id") or "main") == thread_id
    ]
    return len(rounds) + 1


def complexity_decision(channel: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
    decisions = (channel or {}).get("complexity_decisions")
    if not isinstance(decisions, dict):
        return None
    item = decisions.get(thread_id)
    return item if isinstance(item, dict) else None


def complexity_budget_max_rounds(channel: dict[str, Any], thread_id: str) -> int:
    """Reply-dispatch budget from the complexity decision, 0 when undecided."""
    item = complexity_decision(channel, thread_id)
    if not item:
        return 0
    try:
        return max(0, int(item.get("max_rounds") or 0))
    except (TypeError, ValueError):
        return 0


def _reply_items(channel: dict[str, Any]) -> list[dict[str, Any]]:
    raw = (channel.get("reply_requests") or [])
    items = raw.values() if isinstance(raw, dict) else raw
    return [item for item in items if isinstance(item, dict)]


def _replies_used(channel: dict[str, Any], thread_id: str, session: dict[str, Any]) -> int:
    session_start = str(session.get("started_at") or "")
    return len([
        item for item in _reply_items(channel)
        if isinstance(item, dict)
        and str(item.get("thread_id") or "main") == thread_id
        and str(item.get("status") or "") not in {"failed", "cancelled", "superseded"}
        and (not session_start or str(item.get("created_at") or "") >= session_start)
    ])


def _round_quiet(channel: dict[str, Any], thread_id: str) -> bool:
    """No reply_request for the thread is still pending or running."""
    for item in _reply_items(channel):
        if str(item.get("thread_id") or "main") != thread_id:
            continue
        if str(item.get("status") or "") in {"pending", "running"}:
            return False
    return True


def _pending_verdict(channel: dict[str, Any], thread_id: str, round_no: int) -> dict[str, Any] | None:
    """Latest proposed verdict/fallback for ``round_no`` not yet consumed."""
    latest: dict[str, Any] | None = None
    for item in channel.get("moderator_verdicts") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("thread_id") or "main") != thread_id:
            continue
        if int(item.get("round") or 0) != round_no:
            continue
        if str(item.get("kind") or "") not in {"proposed", "fallback"}:
            continue
        latest = item
    return latest


def _verdict_requested(channel: dict[str, Any], thread_id: str, round_no: int) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("thread_id") or "main") == thread_id
        and int(item.get("round") or 0) == round_no
        and str(item.get("kind") or "") == "requested"
        for item in channel.get("moderator_verdicts") or []
    )


def round_ledger_digest(channel: dict[str, Any], thread_id: str, round_no: int) -> str:
    """Digest the moderator must echo back on its verdict.

    Binds a verdict to the ledger snapshot it decided on: recent message
    event ids + open-question state + round counter.
    """
    message_ids = [
        str(item.get("event_id") or "")
        for item in (channel.get("recent_messages") or [])[-20:]
        if isinstance(item, dict)
    ]
    open_questions = [
        str(item.get("question_id") or "")
        for item in (channel.get("open_questions") or [])
        if isinstance(item, dict)
        and str(item.get("thread_id") or "main") == thread_id
        and str(item.get("status") or "") == "open"
    ]
    raw = "|".join([thread_id, str(round_no), *message_ids, *sorted(open_questions)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# kernel-side round advance (called from channel_discussion.advance_discussion)
# ---------------------------------------------------------------------------

def advance_moderator_round(
    writer: Any,
    channel: dict[str, Any],
    session: dict[str, Any],
    *,
    actor: str,
    source: str,
    channel_id: str,
    thread_id: str,
    state_dir: Path,
) -> list[str]:
    """Phase2 advance for moderated discussions.

    Returns emitted event types; ``[]`` means "wait" (replies flowing,
    questions open, or verdict already requested and not yet answered).
    """
    from zf.runtime.channel_discussion import (
        _thread_consensus,
        _thread_has_open_questions,
        request_synthesis_and_phase3,
    )

    emitted: list[str] = []
    round_no = discussion_round(channel, thread_id)
    budget = complexity_budget_max_rounds(channel, thread_id)
    replies_used = _replies_used(channel, thread_id, session)
    roster = [str(m) for m in session.get("roster") or [] if str(m)]

    pending = _pending_verdict(channel, thread_id, round_no)
    if pending is not None:
        action = str(pending.get("action") or "")
        if pending.get("digest") and str(pending.get("digest")) != round_ledger_digest(channel, thread_id, round_no):
            action = "continue"  # stale snapshot: safe default, budget-bounded
        if action == "continue" and budget > 0 and replies_used >= budget:
            action = "conclude"  # budget exhausts moderator discretion
        if action == "conclude":
            if _conclude_gate(channel, thread_id):
                emitted.extend(request_synthesis_and_phase3(
                    writer, state_dir, channel, session,
                    actor=actor, source=source,
                    channel_id=channel_id, thread_id=thread_id,
                    reason=str(pending.get("kind") or "proposed") == "fallback"
                        and "moderator_fallback" or "moderator_concluded",
                    gate=_conclude_gate_3,
                ))
            # Gate blocked (questions reopened): hold; question events re-tick.
            return emitted
        # continue: open the next round and post the moderator's plan.
        writer.emit(
            "channel.message.posted",
            actor=MODERATOR_ACTOR,
            correlation_id=channel_id,
            payload={
                "channel_id": channel_id,
                "thread_id": thread_id,
                "message_id": f"moderator-round-{round_no + 1}",
                "member_id": MODERATOR_ACTOR,
                "role": "moderator",
                "text": _round_plan_text(pending, roster),
                "source": source,
            },
        )
        emitted.append("channel.message.posted")
        writer.emit(
            "channel.discussion.round.opened",
            actor=actor,
            correlation_id=channel_id,
            payload={
                "channel_id": channel_id,
                "thread_id": thread_id,
                "round": round_no + 1,
                "speakers": list(pending.get("speakers") or []),
                "focus": str(pending.get("focus") or ""),
                "fallback": str(pending.get("kind") or "") == "fallback",
                "source": source,
            },
        )
        emitted.append("channel.discussion.round.opened")
        return emitted

    if _thread_has_open_questions(channel, thread_id):
        return emitted  # resolver / owner path owns the thread for now
    if complexity_decision(channel, thread_id) is None:
        return emitted  # classification turn still running; it re-ticks on decide
    if not _round_quiet(channel, thread_id):
        return emitted  # relay still flowing

    if budget > 0 and replies_used >= budget:
        # Budget exhausted: the moderator is bypassed for the freeze decision.
        if _conclude_gate(channel, thread_id):
            emitted.extend(request_synthesis_and_phase3(
                writer, state_dir, channel, session,
                actor=actor, source=source,
                channel_id=channel_id, thread_id=thread_id,
                reason="budget_exhausted",
                gate=_conclude_gate_3,
            ))
        return emitted

    if _verdict_requested(channel, thread_id, round_no):
        return emitted  # verdict LLM turn still running

    writer.emit(
        "channel.moderator.verdict.requested",
        actor=actor,
        correlation_id=channel_id,
        payload={
            "channel_id": channel_id,
            "thread_id": thread_id,
            "round": round_no,
            "ledger_digest": round_ledger_digest(channel, thread_id, round_no),
            "replies_used": replies_used,
            "budget_max_rounds": budget,
            "roster": roster,
            "trigger": str(session.get("trigger") or ""),
            "source": source,
        },
    )
    emitted.append("channel.moderator.verdict.requested")
    return emitted


def _conclude_gate_3(channel: dict[str, Any], session: dict[str, Any], thread_id: str) -> bool:
    """3-arg adapter matching request_synthesis_and_phase3's gate callable."""
    return _conclude_gate(channel, thread_id)


def _conclude_gate(channel: dict[str, Any], thread_id: str) -> bool:
    """Mechanical freeze gate: no open questions, cross-reviews completed."""
    from zf.runtime.channel_discussion import _thread_has_open_questions

    if _thread_has_open_questions(channel, thread_id):
        return False
    cross_reviews = channel.get("cross_reviews") or []
    candidates = (
        list(cross_reviews.values())
        if isinstance(cross_reviews, dict)
        else list(cross_reviews)
    )
    return not any(
        isinstance(item, dict)
        and str(item.get("thread_id") or "main") == thread_id
        and str(item.get("status") or "") != "completed"
        for item in candidates
    )


def _round_plan_text(verdict: dict[str, Any], roster: list[str]) -> str:
    speakers = [
        str(s) for s in (verdict.get("speakers") or []) if str(s) in roster
    ]
    focus = str(verdict.get("focus") or "").strip()
    if speakers:
        mentions = " ".join(f"@{s}" for s in speakers)
        body = f"下一轮({mentions})"
    else:
        body = "下一轮(open relay,自由接力)"
    if focus:
        body += f":{focus}"
    rationale = str(verdict.get("rationale") or "").strip()
    if rationale:
        body += f"\n(主持人理由:{rationale})"
    return body


# ---------------------------------------------------------------------------
# edge runners (LLM turns; directly callable for tests)
# ---------------------------------------------------------------------------

def budget_for_complexity(complexity: str, member_count: int) -> int:
    standard = default_debate_max_rounds(member_count)
    if complexity == "simple":
        return max(4, member_count * 2)
    if complexity == "complex":
        return min(256, member_count * 8)
    return standard


def run_complexity_classification(
    *,
    state_dir: Path,
    writer: Any,
    channel: dict[str, Any],
    channel_id: str,
    thread_id: str,
    config: Any = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Classify requirement complexity → budget. Never blocks: LLM failure
    falls back to the standard budget (recorded as source="fallback")."""
    session = ((channel.get("discussions") or {}).get(thread_id) or {})
    trigger = str(session.get("trigger") or "")
    member_count = len(channel.get("members") or [])
    parsed: dict[str, Any] | None = None
    if trigger.strip():
        prompt = _CLASSIFY_PROMPT.format(
            requirement=trigger[:2000],
            member_count=member_count,
        )
        parsed, _ = agent_json_turn(
            state_dir=state_dir,
            project_root=project_root,
            config=config,
            thread_key=f"channel-moderator-classify:{channel_id}:{thread_id}",
            prompt=prompt,
            repair_hint='Return exactly one fenced JSON object: {"complexity": "simple|standard|complex"}.',
        )
    complexity = str((parsed or {}).get("complexity") or "").strip().lower()
    source = "moderator-llm"
    if complexity not in COMPLEXITY_LEVELS:
        complexity = "standard"
        source = "fallback"
    max_rounds = budget_for_complexity(complexity, member_count)
    writer.emit(
        "channel.discussion.complexity.decided",
        actor=MODERATOR_ACTOR,
        correlation_id=channel_id,
        payload={
            "channel_id": channel_id,
            "thread_id": thread_id,
            "complexity": complexity,
            "max_rounds": max_rounds,
            "member_count": member_count,
            "source": source,
        },
    )
    return {"ok": True, "complexity": complexity, "max_rounds": max_rounds, "source": source}


def run_moderator_verdict(
    *,
    state_dir: Path,
    writer: Any,
    channel: dict[str, Any],
    channel_id: str,
    thread_id: str,
    request_event: dict[str, Any],
    config: Any = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """One moderator verdict turn. Emits verdict.proposed, or
    verdict.fallback after bounded LLM/validation failure."""
    round_no = int(request_event.get("round") or 1)
    digest = str(request_event.get("ledger_digest") or "")
    replies_used = int(request_event.get("replies_used") or 0)
    budget = int(request_event.get("budget_max_rounds") or 0)
    roster = [str(m) for m in (request_event.get("roster") or []) if str(m)]
    trigger = str(request_event.get("trigger") or "")

    parsed: dict[str, Any] | None = None
    error = ""
    if roster:
        prompt = _VERDICT_PROMPT.format(
            trigger=trigger[:1500],
            round_no=round_no,
            replies_used=replies_used,
            budget=budget,
            roster=", ".join(roster),
            recent=_recent_discussion_text(channel, thread_id),
            digest=digest,
        )
        parsed, error = agent_json_turn(
            state_dir=state_dir,
            project_root=project_root,
            config=config,
            thread_key=f"channel-moderator-verdict:{channel_id}:{thread_id}:{round_no}",
            prompt=prompt,
            repair_hint=(
                "Return exactly one fenced JSON object: "
                '{"action": "continue|conclude", "speakers": ["<roster ids>"], '
                '"focus": "<next round topic>", "rationale": "<short>"}. '
                "speakers must be a subset of the roster."
            ),
        )
    action, speakers, focus, rationale, invalid = _validate_verdict(parsed, roster)
    if invalid or not roster:
        writer.emit(
            "channel.moderator.verdict.fallback",
            actor=MODERATOR_ACTOR,
            correlation_id=channel_id,
            payload={
                "channel_id": channel_id,
                "thread_id": thread_id,
                "round": round_no,
                "ledger_digest": digest,
                "fallback_action": "conclude" if (budget > 0 and replies_used >= budget) else "continue",
                "reason": invalid or (error[:200] if error else "empty roster"),
                "source": "fallback",
            },
        )
        return {"ok": False, "status": "fallback", "reason": invalid or error}
    writer.emit(
        "channel.moderator.verdict.proposed",
        actor=MODERATOR_ACTOR,
        correlation_id=channel_id,
        payload={
            "channel_id": channel_id,
            "thread_id": thread_id,
            "round": round_no,
            "ledger_digest": digest,
            "action": action,
            "speakers": speakers,
            "focus": focus,
            "rationale": rationale,
            "source": "moderator-llm",
        },
    )
    return {"ok": True, "action": action}


def _validate_verdict(
    parsed: dict[str, Any] | None,
    roster: list[str],
) -> tuple[str, list[str], str, str, str]:
    """Return (action, speakers, focus, rationale, invalid-reason)."""
    if not isinstance(parsed, dict):
        return "", [], "", "", "unparseable verdict"
    action = str(parsed.get("action") or "").strip().lower()
    if action not in {"continue", "conclude"}:
        return "", [], "", "", f"action must be continue|conclude, got {action!r}"
    raw_speakers = parsed.get("speakers") or []
    if not isinstance(raw_speakers, list):
        return "", [], "", "", "speakers must be a list"
    speakers = [str(s) for s in raw_speakers if str(s)]
    roster_set = set(roster)
    if any(s not in roster_set for s in speakers):
        return "", [], "", "", "speakers outside roster"
    if len(speakers) > 6:
        return "", [], "", "", "too many speakers"
    if action == "conclude":
        speakers = []
    focus = str(parsed.get("focus") or "").strip()[:500]
    rationale = str(parsed.get("rationale") or "").strip()[:500]
    return action, speakers, focus, rationale, ""


def _recent_discussion_text(channel: dict[str, Any], thread_id: str, limit: int = 12) -> str:
    lines: list[str] = []
    for item in (channel.get("recent_messages") or [])[-limit:]:
        if not isinstance(item, dict):
            continue
        if str(item.get("thread_id") or "main") != thread_id:
            continue
        author = str(item.get("member_id") or item.get("author") or "?")
        text = str(item.get("text") or "").strip().replace("\n", " ")
        if text:
            lines.append(f"- {author}: {text[:200]}")
    return "\n".join(lines) or "(no messages yet)"


# ---------------------------------------------------------------------------
# orchestrator reactor entries
# ---------------------------------------------------------------------------

def react_channel_discussion_started(host: Any, event: Any) -> None:
    """Discussion start → complexity classification turn (async thread)."""
    try:
        payload = event.payload if isinstance(event.payload, dict) else {}
        channel_id = str(payload.get("channel_id") or event.correlation_id or "")
        if not channel_id:
            return None
        channel = _project(host, channel_id)
        if not channel_discussion_moderator_enabled(channel):
            return None
        thread_id = str(payload.get("thread_id") or "main")
        key = f"{channel_id}:{thread_id}:classify"
        if key in _IN_FLIGHT:
            return None
        _IN_FLIGHT.add(key)
        threading.Thread(
            target=_run_guarded,
            args=(key, run_complexity_classification, {
                "state_dir": Path(host.state_dir),
                "writer": host.event_writer,
                "channel": channel,
                "channel_id": channel_id,
                "thread_id": thread_id,
                "config": getattr(host, "config", None),
                "project_root": getattr(host, "project_root", None),
            }),
            daemon=True,
        ).start()
    except Exception:
        return None
    return None


def react_channel_moderator_verdict_request(host: Any, event: Any) -> None:
    """Round boundary → moderator verdict turn (async thread)."""
    try:
        payload = event.payload if isinstance(event.payload, dict) else {}
        channel_id = str(payload.get("channel_id") or event.correlation_id or "")
        if not channel_id:
            return None
        channel = _project(host, channel_id)
        if not channel_discussion_moderator_enabled(channel):
            return None
        thread_id = str(payload.get("thread_id") or "main")
        round_no = int(payload.get("round") or 0)
        key = f"{channel_id}:{thread_id}:verdict:{round_no}"
        if key in _IN_FLIGHT:
            return None
        _IN_FLIGHT.add(key)
        threading.Thread(
            target=_run_guarded,
            args=(key, run_moderator_verdict, {
                "state_dir": Path(host.state_dir),
                "writer": host.event_writer,
                "channel": channel,
                "channel_id": channel_id,
                "thread_id": thread_id,
                "request_event": dict(payload),
                "config": getattr(host, "config", None),
                "project_root": getattr(host, "project_root", None),
            }),
            daemon=True,
        ).start()
    except Exception:
        return None
    return None


def _run_guarded(key: str, runner: Any, kwargs: dict[str, Any]) -> None:
    try:
        runner(**kwargs)
    except Exception:
        pass
    finally:
        _IN_FLIGHT.discard(key)


def _project(host: Any, channel_id: str) -> dict[str, Any]:
    from zf.runtime.channel_projection import project_channel

    return project_channel(Path(host.state_dir), channel_id) or {}


def register_moderator_handlers(registry: Any, host: Any) -> None:
    """Wire moderator reactors into the event registry.

    ``EventActionRegistry`` invokes handlers with the event only — bind the
    orchestrator host with partials (same pattern as register_auto_chain_handlers).
    """
    from functools import partial

    registry.register(
        "channel.discussion.started",
        partial(react_channel_discussion_started, host),
        source="builtin",
    )
    registry.register(
        "channel.moderator.verdict.requested",
        partial(react_channel_moderator_verdict_request, host),
        source="builtin",
    )


_CLASSIFY_PROMPT = """You are the moderator (主持人) of a ZaoFu Channel multi-agent PRD discussion.

Requirement from the owner:
---
{requirement}
---

Roster size: {member_count} members (product/arch/critic/security/synthesizer).

Classify the discussion complexity to size the round budget:
- simple: single-domain, few real tradeoffs, PRD is small (e.g. CLI tool, CRUD page)
- standard: multiple subsystems or real design tradeoffs (default)
- complex: cross-cutting architecture, security/compliance heavy, or broad scope

{budget_hint}Reply with exactly one fenced JSON object:
```json
{{"complexity": "simple|standard|complex"}}
```"""

_VERDICT_PROMPT = """You are the moderator (主持人) of a ZaoFu Channel multi-agent PRD discussion.

Original requirement:
---
{trigger}
---

Current state: round {round_no}, replies used {replies_used} of budget {budget}.
Roster: {roster}
Ledger digest: {digest}

Recent discussion:
{recent}

Decide the next step:
- "continue": keep discussing. Pick the speakers (subset of roster, max 6) who should speak next and state the focus (what to resolve in the next round). Prefer targeted @mentions over @all.
- "conclude": discussion is done, freeze the PRD and proceed to synthesis. Only conclude when the open questions are answered and the design is stable.

Reply with exactly one fenced JSON object:
```json
{{"action": "continue|conclude", "speakers": ["<roster ids>"], "focus": "<next round topic>", "rationale": "<one sentence>"}}
```
When concluding, use "speakers": [] and an empty focus."""

# Filled lazily to keep the classify prompt honest about budget consequences.
_CLASSIFY_PROMPT = _CLASSIFY_PROMPT.replace(
    "{budget_hint}",
    "Budget means reply-dispatch count; simple tasks must not burn rounds. ",
)
