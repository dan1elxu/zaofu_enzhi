"""zf evaluation-watch — resident PRD evaluation observer (tmux window)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "evaluation-watch",
        help="Run the Kanban->Channel->PRD evaluation observer loop (tmux window)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="poll interval seconds (default: ZF_EVALUATION_WATCH_INTERVAL_S or 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one observer pass and exit (diagnostics/dry runs)",
    )
    parser.set_defaults(func=run_evaluation_watch)


def run_evaluation_watch(args: argparse.Namespace) -> int:
    import os

    from zf.core.config.loader import load_config
    from zf.core.config.project_context import resolve_project_context
    from zf.runtime.evaluation_observer import (
        DEFAULT_POLL_INTERVAL_S,
        observe_loop,
        observe_once,
    )

    try:
        ctx = resolve_project_context(cwd=Path.cwd())
        config = load_config(ctx.config_path)
    except Exception as exc:
        print(f"Error: not inside a ZaoFu project: {exc}", file=sys.stderr)
        return 1
    state_dir = Path(os.environ.get("ZF_STATE_DIR") or ctx.state_dir)
    interval = args.interval
    if interval is None:
        interval = float(
            os.environ.get(
                "ZF_EVALUATION_WATCH_INTERVAL_S", DEFAULT_POLL_INTERVAL_S,
            )
        )
    if args.once:
        stats = observe_once(
            state_dir=state_dir, project_root=ctx.project_root, config=config,
        )
        print(stats)
        return 0 if not stats["errors"] else 1
    observe_loop(
        state_dir=state_dir,
        project_root=ctx.project_root,
        config=config,
        poll_interval_s=interval,
    )
    return 0


__all__ = ["register"]
