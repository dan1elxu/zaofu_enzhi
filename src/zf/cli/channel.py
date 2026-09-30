"""`zf channel say` — agent-facing edge to post a channel message (feishu-S5).

doc 98 §3: an agent posts to a channel (and thus, via the bridge's outbound
projection of channel.message.posted, to Feishu) WITHOUT holding any Feishu
credential and WITHOUT calling an MCP/transport directly — it goes through the
existing channel-post-message ControlledAction (same whitelist/audit gate as
Web/Feishu inbound). This is the ZaoFu-native agent-facing message edge.
"""

from __future__ import annotations

import argparse
import json
import sys

from zf.core.config.loader import ConfigError
from zf.core.config.project_context import resolve_project_context
from zf.core.events import EventWriter
from zf.core.events.factory import event_log_from_project
from zf.runtime.control_actions import ControlledActionService


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("channel", help="Channel operations")
    parser.set_defaults(func=_root)
    sub = parser.add_subparsers(dest="channel_command")

    say = sub.add_parser(
        "say", help="Post a message to a channel (gated; projected to Feishu)")
    say.add_argument("channel_id")
    say.add_argument("--text", required=True)
    say.add_argument("--member-id", default="agent",
                     help="Channel member posting as (agent identity)")
    say.add_argument("--mention", action="append", default=[],
                     metavar="MEMBER", help="@mention a member (repeatable)")
    say.add_argument("--state-dir", default=None)
    say.set_defaults(func=run_say)

    status = sub.add_parser(
        "status", help="Show one channel's live discussion state (phase, "
                       "who is running, question progress)")
    status.add_argument("channel_id")
    status.add_argument("--thread", default="main")
    status.add_argument("--json", action="store_true",
                        help="Emit the status payload as JSON")
    status.add_argument("--state-dir", default=None)
    status.set_defaults(func=run_status)

    retry = sub.add_parser("review-retry", help="Re-review an unchanged PRD after repairing a review failure")
    retry.add_argument("channel_id")
    retry.add_argument("--thread", default="main")
    retry.add_argument("--consensus-event-id", required=True)
    retry.add_argument("--reason", required=True)
    retry.add_argument("--actor", default="operator")
    retry.add_argument("--state-dir", default=None)
    retry.set_defaults(func=run_review_retry)


def run_review_retry(args: argparse.Namespace) -> int:
    try:
        context = resolve_project_context(explicit_state_dir=args.state_dir)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    payload = {
        "channel_id": args.channel_id, "thread_id": args.thread,
        "consensus_event_id": args.consensus_event_id, "reason": args.reason,
    }
    writer = EventWriter(event_log_from_project(context.state_dir, config=context.config))
    requested = writer.emit("control.action.requested", actor=args.actor, payload=payload)
    result = ControlledActionService(
        context.state_dir, writer, config=context.config, actor=args.actor,
        project_root=context.project_root, source="cli", surface="cli",
    ).execute(action="channel-consensus-review-retry", requested_action="zf channel review-retry",
              payload=payload, requested=requested)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def _root(args: argparse.Namespace) -> int:
    print("Usage: zf channel say <channel_id> --text ...", file=sys.stderr)
    return 2


def run_say(args: argparse.Namespace) -> int:
    try:
        context = resolve_project_context(
            explicit_state_dir=getattr(args, "state_dir", None))
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    actor = f"agent:{args.member_id}"
    payload = {
        "channel_id": args.channel_id,
        "member_id": args.member_id,
        "text": args.text,
        "mentions": list(args.mention),
    }
    writer = EventWriter(
        event_log_from_project(context.state_dir, config=context.config))
    requested = writer.emit("control.action.requested", actor=actor, payload=payload)
    result = ControlledActionService(
        context.state_dir, writer, config=context.config,
        actor=actor, source="cli", surface="cli",
    ).execute(action="channel-post-message", requested_action="zf channel say",
              payload=payload, requested=requested)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def run_status(args: argparse.Namespace) -> int:
    try:
        context = resolve_project_context(
            explicit_state_dir=getattr(args, "state_dir", None))
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    from zf.runtime.channel_discussion_contribution import valid_phase1_members
    from zf.runtime.channel_projection import project_channel

    channel = project_channel(context.state_dir, args.channel_id)
    if not channel:
        print(f"Error: unknown channel: {args.channel_id}", file=sys.stderr)
        return 1

    thread_id = str(args.thread or "main")
    session = (channel.get("discussions") or {}).get(thread_id) or {}
    roster = [str(m) for m in session.get("roster") or []]
    state = str(session.get("state") or "idle")

    active = []
    for request in channel.get("reply_requests") or []:
        if not isinstance(request, dict):
            continue
        status = str(request.get("status") or "")
        if status in {"requested", "started"}:
            active.append({
                "member_id": str(request.get("target_member_id") or ""),
                "status": status,
            })

    phase1_done: list[str] = []
    if state == "phase1_blind" and roster:
        replied = valid_phase1_members(channel, session, thread_id)
        phase1_done = sorted(replied)

    open_items = channel.get("open_questions") or []
    if isinstance(open_items, dict):
        open_items = list(open_items.values())
    question_counts: dict[str, int] = {}
    for item in open_items:
        if isinstance(item, dict):
            key = str(item.get("status") or "open")
            question_counts[key] = question_counts.get(key, 0) + 1

    frozen_map = (
        (channel.get("questions_frozen") or {}).get(thread_id) or {}
    )
    consensus = (channel.get("consensus") or {}).get(thread_id) or {}

    payload = {
        "channel_id": args.channel_id,
        "thread_id": thread_id,
        "state": state,
        "started_at": str(session.get("started_at") or ""),
        "roster": roster,
        "running": active,
        "phase1_progress": {
            "valid": len(phase1_done),
            "total": len(roster),
            "members": phase1_done,
        },
        "questions": {
            "total": len(open_items),
            "by_status": question_counts,
        },
        "frozen_members": sorted(frozen_map.keys()),
        "consensus": {
            "signed": len(consensus.get("signed") or {}),
            "blocked": len(consensus.get("blocked") or []),
            "proposed": bool(consensus.get("artifact_ref")),
        },
        "last_event_type": str(channel.get("last_event_type") or ""),
        "last_event_seq": int(channel.get("last_event_seq") or 0),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    lines = [
        f"channel: {payload['channel_id']} (thread {thread_id})",
        f"state:   {payload['state']}"
        + (f"  (started {payload['started_at'][:19]})"
           if payload["started_at"] else ""),
        f"roster:  {len(roster)} members: {', '.join(roster)}",
    ]
    if payload["running"]:
        running = ", ".join(
            f"{r['member_id']}({r['status']})" for r in payload["running"]
        )
        lines.append(f"running: {running}")
    else:
        lines.append("running: -")
    if state == "phase1_blind":
        p = payload["phase1_progress"]
        lines.append(
            f"phase1:  {p['valid']}/{p['total']} valid blind replies"
        )
    q = payload["questions"]
    by_status = ", ".join(
        f"{k}={v}" for k, v in sorted(q["by_status"].items())
    ) or "none"
    lines.append(f"questions: {q['total']} ({by_status})")
    lines.append(
        f"consensus: proposed={payload['consensus']['proposed']} "
        f"signed={payload['consensus']['signed']} "
        f"blocked={payload['consensus']['blocked']}"
    )
    print("\n".join(lines))
    return 0
