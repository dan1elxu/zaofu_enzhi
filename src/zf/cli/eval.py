"""zf eval — three-lane PRD eval runs (run/list/show) and static evaluation.

This module owns the shared ``eval`` command group: the three-lane PRD eval
subcommands (docs/ideas/three-lane-prd-eval.md Phase B1) live here, and the
static preset/output evaluators from ``eval_preset.py`` attach to the same
group via ``register_static``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "eval",
        help="Three-lane PRD eval runs (run/list/show) and static evaluation",
    )
    sub = parser.add_subparsers(dest="eval_cmd")

    run = sub.add_parser("run", help="Run a three-lane PRD eval run-group")
    run.add_argument("--prompt-file", required=True, type=Path,
                     help="path to the entry prompt text file")
    run.add_argument("--n", type=int, default=1, help="trials per prompt (default 1)")
    run.add_argument("--lanes", default="terminal,kanban,channel",
                     help="comma-separated lane subset (channel-auto alias accepted)")
    run.add_argument("--backend", default="claude-headless",
                     help="headless provider for terminal/kanban lanes and judges")
    run.add_argument("--channel-backend", default="",
                     help="channel lane provider override (default: mapped from --backend)")
    run.add_argument("--channel-poll-s", type=float, default=2.0,
                     help="channel lane pump poll interval in seconds (default 2)")
    run.add_argument("--channel-state", choices=["isolated", "shared"],
                     default="isolated",
                     help="channel lane ledger: isolated lane dir (default) or the "
                          "workspace state dir so the discussion is visible in "
                          "the Web Channels page")
    run.add_argument("--timeout-s", type=float, default=1800.0,
                     help="per-lane timeout budget in seconds (default 1800)")
    run.add_argument("--prompt-version", default="eval-prompt.v1",
                     help="prompt packet version identity")
    run.add_argument("--state-dir", default=None, help="override the runtime state dir")
    run.add_argument("--format", choices=["text", "json"], default="text")
    run.set_defaults(func=run_eval_run)

    list_parser = sub.add_parser("list", help="List eval runs grouped by prompt digest")
    list_parser.add_argument("--prompt-digest", default="", help="restrict to one run-group")
    list_parser.add_argument("--state-dir", default=None)
    list_parser.add_argument("--format", choices=["text", "json"], default="text")
    list_parser.set_defaults(func=run_eval_list)

    show = sub.add_parser("show", help="Show one eval run's status and conclusion")
    show.add_argument("run_id")
    show.add_argument("--state-dir", default=None)
    show.add_argument("--format", choices=["text", "json"], default="text")
    show.set_defaults(func=run_eval_show)

    from zf.cli import eval_preset

    eval_preset.register_static(sub)
    parser.set_defaults(func=lambda args: _show_help(parser))


def _show_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


def _resolve_context(args: argparse.Namespace):
    from zf.core.config.loader import ConfigError
    from zf.core.config.project_context import resolve_project_context

    try:
        return resolve_project_context(
            cwd=Path.cwd(),
            explicit_state_dir=getattr(args, "state_dir", None),
            load_config_with_explicit=True,
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def _parse_lanes(value: str) -> list[str]:
    from zf.runtime.eval_lanes import canonical_lane_id

    lanes: list[str] = []
    unknown: list[str] = []
    for raw in str(value or "").split(","):
        lane = canonical_lane_id(raw)
        if lane and lane not in lanes:
            lanes.append(lane)
        elif not lane and raw.strip():
            unknown.append(raw.strip())
    if unknown:
        raise ValueError(f"unknown lanes: {', '.join(unknown)}")
    if not lanes:
        raise ValueError("at least one lane is required")
    return lanes


def run_eval_run(args: argparse.Namespace) -> int:
    from zf.core.events import EventWriter
    from zf.core.events.factory import event_log_from_project
    from zf.runtime.eval_driver import run_eval

    prompt_file = Path(args.prompt_file)
    if not prompt_file.is_file():
        print(f"Error: prompt file not found: {prompt_file}", file=sys.stderr)
        return 2
    try:
        prompt_text = prompt_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"Error: cannot read prompt file: {exc}", file=sys.stderr)
        return 2
    try:
        lanes = _parse_lanes(args.lanes)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.n < 1:
        print("Error: --n must be >= 1", file=sys.stderr)
        return 2
    if args.timeout_s <= 0:
        print("Error: --timeout-s must be positive", file=sys.stderr)
        return 2
    context = _resolve_context(args)
    if context is None:
        return 2
    writer = EventWriter(event_log_from_project(context.state_dir, config=context.config))
    results = run_eval(
        prompt_text=prompt_text,
        n_trials=args.n,
        lanes=lanes,
        backend=args.backend,
        channel_backend=args.channel_backend,
        channel_shared_state=args.channel_state == "shared",
        timeout_s=args.timeout_s,
        prompt_version=args.prompt_version,
        state_dir=context.state_dir,
        config=context.config,
        project_root=context.project_root,
        writer=writer,
        lane_overrides={"channel": {"poll_interval_s": args.channel_poll_s}},
    )
    if args.format == "json":
        print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2))
    else:
        for item in results:
            lane_bits = []
            for lane, info in sorted(item.lanes.items()):
                bit = f"{lane}={info.get('status')}"
                if info.get("blind_mean") is not None:
                    bit += f"(blind {info['blind_mean']})"
                lane_bits.append(bit)
            line = f"trial {item.trial}: run_id={item.run_id} status={item.status}"
            if item.skipped:
                line += " (skipped: already completed)"
            if item.conclusion_status:
                line += f" conclusion={item.conclusion_status}"
            if lane_bits:
                line += " lanes: " + " ".join(lane_bits)
            if item.error:
                line += f" error={item.error}"
            print(line)
            if item.report_path:
                print(f"  report: {item.report_path}")
    return 0 if all(item.status == "completed" for item in results) else 1


def run_eval_list(args: argparse.Namespace) -> int:
    from zf.runtime.eval_run import list_eval_runs

    context = _resolve_context(args)
    if context is None:
        return 2
    try:
        manifests = list_eval_runs(
            context.state_dir,
            prompt_digest=str(args.prompt_digest or "").strip(),
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(
            [manifest.to_dict() for manifest in manifests],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not manifests:
        print("no eval runs")
        return 0
    current_digest = ""
    for manifest in manifests:
        if manifest.prompt_digest != current_digest:
            current_digest = manifest.prompt_digest
            print(f"prompt_digest {current_digest[:16]}…")
        lanes = " ".join(
            f"{lane}={lane_state.status}"
            for lane, lane_state in sorted(manifest.lanes.items())
        )
        print(
            f"  {manifest.run_id}  trial={manifest.trial}  status={manifest.status}"
            f"  lanes: {lanes}  created={manifest.created_at}"
        )
    return 0


def run_eval_show(args: argparse.Namespace) -> int:
    from zf.runtime.eval_driver import run_summary

    context = _resolve_context(args)
    if context is None:
        return 2
    try:
        summary = run_summary(context.state_dir, args.run_id)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0
    manifest = summary["manifest"]
    conclusion = summary.get("conclusion") or {}
    conclusion_lanes = conclusion.get("lanes") or {}
    print(
        f"run: {manifest['run_id']}  status={manifest['status']}"
        f"  trial={manifest['trial']}"
    )
    print(
        f"prompt_digest: {manifest['prompt_digest']}"
        f"  prompt_version: {manifest['prompt_version']}"
    )
    for lane, lane_state in sorted((manifest.get("lanes") or {}).items()):
        info = conclusion_lanes.get(lane) or {}
        line = f"  lane {lane}: {lane_state.get('status')}"
        if lane_state.get("error"):
            line += f" (error: {lane_state['error']})"
        if info.get("blind_mean") is not None:
            line += f"  blind均分={info['blind_mean']}"
        if info.get("label"):
            line += f"  候选={info['label']}"
        print(line)
    if conclusion:
        print(f"conclusion: {conclusion.get('status')} — {conclusion.get('summary_zh')}")
        recommendation = conclusion.get("recommendation") or {}
        if recommendation.get("ranking"):
            order = " > ".join(
                f"{item['label']}({item.get('lane') or '?'})"
                for item in recommendation["ranking"]
            )
            print(f"推荐排名: {order}")
    if summary.get("report_path"):
        print(f"report: {summary['report_path']}")
    elif not conclusion:
        print("evaluation not finished; rerun `zf eval run` to resume")
    return 0


__all__ = ["register"]
