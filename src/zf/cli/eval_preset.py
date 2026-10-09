"""zf eval preset — static `zf.yaml` evaluation before real E2E (doc 68 S4)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("eval", help="Static evaluation of zaofu config")
    sub = parser.add_subparsers(dest="eval_cmd")

    preset = sub.add_parser("preset", help="Statically evaluate a zf.yaml preset")
    preset.add_argument("yaml_path", help="Path to a zf.yaml to evaluate")
    preset.add_argument("--format", choices=["text", "json"], default="text")
    preset.set_defaults(func=run_eval_preset)

    output = sub.add_parser("output", help="Independently evaluate a frozen text output")
    output.add_argument("--kind", required=True, choices=["response_output", "prd_output", "markdown_output", "prompt_design"])
    output.add_argument("--objective", required=True)
    output.add_argument("--target", required=True, type=Path)
    output.add_argument("--source", type=Path)
    output.add_argument("--decisions", type=Path)
    output.add_argument("--evidence", type=Path)
    output.add_argument("--scope", choices=["full", "focused"], default="full")
    output.add_argument("--risk-focus", action="append", default=[])
    output.add_argument("--backend", choices=["codex", "claude"], default="codex")
    output.add_argument("--timeout", type=int, default=1800)
    output.add_argument("--state-dir")
    output.set_defaults(func=run_eval_output)

    parser.set_defaults(func=lambda args: _show_help(parser))


def _show_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


def run_eval_preset(args: argparse.Namespace) -> int:
    from zf.core.config.loader import ConfigError, load_config
    from zf.core.config.preset_eval import evaluate_preset

    path = Path(args.yaml_path)
    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        return 1
    try:
        config = load_config(path)
    except ConfigError as e:
        print(f"Error: invalid config: {e}", file=sys.stderr)
        return 1

    report = evaluate_preset(config)
    if getattr(args, "format", "text") == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Preset Evaluation: {path}\n")
        for c in report["checks"]:
            mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}.get(c["status"], c["status"])
            line = f"{mark} {c['name']}"
            if c["detail"]:
                line += f" - {c['detail']}"
            print(line)
        s = report["summary"]
        print(f"\n{s['pass']} pass, {s['warn']} warn, {s['fail']} fail")
    # nonzero exit on any FAIL so CI / scripts can gate on it
    return 0 if report["ok"] else 2


def run_eval_output(args: argparse.Namespace) -> int:
    """Operator CLI edge: freeze all inputs before a read-only provider turn."""
    import uuid

    from zf.core.config.loader import ConfigError
    from zf.core.config.project_context import resolve_project_context
    from zf.core.events import EventWriter
    from zf.core.events.factory import event_log_from_project
    from zf.runtime.output_evaluation import EvaluationInput, evaluate_output
    from zf.web.headless_agent import ClaudeHeadlessBackend, CodexHeadlessBackend

    def read(path: Path | None) -> str:
        return path.read_text(encoding="utf-8") if path else ""

    if not 1 <= args.timeout <= 3600:
        print("Error: timeout must be 1..3600 seconds", file=sys.stderr)
        return 2
    try:
        context = resolve_project_context(explicit_state_dir=args.state_dir)
        item = EvaluationInput(
            kind=args.kind, objective=args.objective,
            source_text=read(args.source), target_text=read(args.target),
            decision_text=read(args.decisions), evidence_text=read(args.evidence),
            scope=args.scope, risk_focus=tuple(args.risk_focus),
            source_ref=str(args.source.resolve()) if args.source else "",
            target_ref=str(args.target.resolve()),
            context={"project": str(context.project_root)},
        )
        manifest = item.manifest()
    except (ConfigError, OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    request_id = str(uuid.uuid4())
    writer = EventWriter(event_log_from_project(context.state_dir, config=context.config))
    requested = writer.emit("output.evaluation.requested", actor="operator:cli",
                            correlation_id=request_id,
                            payload={"request_id": request_id, "manifest": manifest})
    backend = CodexHeadlessBackend() if args.backend == "codex" else ClaudeHeadlessBackend()
    result = evaluate_output(state_dir=context.state_dir, writer=writer, item=item,
                             request_id=request_id, backend=backend,
                             timeout_s=args.timeout, causation_id=requested.id)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("verdict") == "PASS" else 1
