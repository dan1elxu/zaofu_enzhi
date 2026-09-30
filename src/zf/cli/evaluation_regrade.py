"""zf evaluation-regrade — explicit manual evaluation replay."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "evaluation-regrade", help="手动重跑一个已冻结的 Channel PRD 评测"
    )
    parser.add_argument("--input-ref", required=True, help="canonical queue-input JSON relative path")
    parser.add_argument("--regrade-id", default=None, help="optional stable manual regrade id")
    parser.set_defaults(func=run_evaluation_regrade)


def run_evaluation_regrade(args: argparse.Namespace) -> int:
    from zf.core.config.loader import load_config
    from zf.core.config.project_context import resolve_project_context
    from zf.runtime.evaluation_regrade import regrade_once
    try:
        ctx = resolve_project_context(cwd=Path.cwd())
        config = load_config(ctx.config_path)
        state_dir = Path(os.environ.get("ZF_STATE_DIR") or ctx.state_dir)
        result = regrade_once(
            state_dir=state_dir, input_ref=args.input_ref, config=config,
            project_root=ctx.project_root, regrade_id=args.regrade_id,
        )
    except Exception as exc:
        print(f"Error: evaluation regrade failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = ["register"]
