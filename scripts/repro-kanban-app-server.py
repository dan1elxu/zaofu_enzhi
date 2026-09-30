#!/usr/bin/env python3
"""Reproduce the ZaoFu Kanban -> Codex app-server provider turn.

This is a diagnostic script. It does not write the selected project's runtime
state; Codex state is isolated under a temporary CODEX_HOME and removed on
exit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT = Path("/home/uisee/projects/test_dev6")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one Kanban-shaped Codex app-server turn against CPA."
    )
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        default=DEFAULT_PROJECT,
        help=f"project root (default: {DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--model",
        default="kimi-k2.8-code",
        help="Codex model from ~/.codex/config.toml (default: kimi-k2.8-code)",
    )
    parser.add_argument(
        "--message",
        default="hi",
        help="fallback user message when no historical Kanban prompt is found",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=280.0,
        help="maximum wait for the provider turn in seconds (default: 280)",
    )
    parser.add_argument(
        "--fresh-prompt",
        action="store_true",
        help="ignore saved Kanban prompts and build a small fresh prompt",
    )
    return parser.parse_args()


def _load_project_context(project: Path) -> tuple[Path, Path]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from zf.core.config.project_context import resolve_project_context

    context = resolve_project_context(cwd=project, require_config=False)
    return context.project_root, context.state_dir


def _latest_kanban_prompt(project: Path) -> str | None:
    sessions_root = Path.home() / ".codex" / "sessions"
    paths = sorted(
        # Codex stores sessions as <year>/<month>/<day>/rollout-*.jsonl.
        sessions_root.glob("*/*/*/rollout-*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths[:500]:
        originator = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    data = json.loads(raw)
                    payload = data.get("payload", {})
                    if data.get("type") == "session_meta":
                        originator = str(payload.get("originator") or "")
                    if (
                        originator != "zaofu-kanban-agent"
                        or data.get("type") != "event_msg"
                        or payload.get("type") != "item_completed"
                    ):
                        continue
                    item = payload.get("item", {})
                    if item.get("type") != "UserMessage":
                        continue
                    text = "".join(
                        part.get("text", "")
                        for part in item.get("content", [])
                        if isinstance(part, dict)
                    )
                    if text.startswith("ZaoFu Kanban Agent turn") and str(project) in text:
                        return text
        except (OSError, ValueError, TypeError):
            continue
    return None


def _make_prompt(agent: object, project: Path, message: str, use_history: bool) -> tuple[str, str]:
    if use_history:
        historical = _latest_kanban_prompt(project)
        if historical:
            return historical, "historical-kanban-prompt"
    prompt = agent._build_prompt(  # type: ignore[attr-defined]
        message=message,
        task_id="",
        context={
            "project_id": project.name,
            "conversation_id": f"kanban:{project.name}",
            "thread_key": f"kanban:{project.name}",
            "operation_kind": "kanban_turn",
            "workflow_context": {},
        },
    )
    return prompt, "fresh-kanban-prompt"


def main() -> int:
    args = _parse_args()
    project = args.project.expanduser().resolve()
    if not project.is_dir():
        print(f"ERROR: project directory does not exist: {project}", file=sys.stderr)
        return 2
    if not os.environ.get("CPA_API_KEY"):
        print(
            "ERROR: CPA_API_KEY is missing. Run: source ~/.config/cpa/defaults.env",
            file=sys.stderr,
        )
        return 2

    project, state_dir = _load_project_context(project)
    from zf.web.headless_agent import CodexHeadlessBackend, KanbanHeadlessAgent

    temp_home = Path(tempfile.mkdtemp(prefix="zf-kanban-codex-home-"))
    try:
        source_config = Path.home() / ".codex" / "config.toml"
        if not source_config.is_file():
            print(f"ERROR: Codex config not found: {source_config}", file=sys.stderr)
            return 2
        config_text = source_config.read_text(encoding="utf-8")
        config_text, replacements = re.subn(
            r'(?m)^model\s*=\s*"[^"]*"\s*$',
            f'model = {json.dumps(args.model)}',
            config_text,
            count=1,
        )
        if replacements != 1:
            print("ERROR: top-level model setting not found in Codex config", file=sys.stderr)
            return 2
        (temp_home / "config.toml").write_text(config_text, encoding="utf-8")
        os.environ["CODEX_HOME"] = str(temp_home)
        os.environ["NO_PROXY"] = "10.0.169.15,localhost,127.0.0.1"
        os.environ["no_proxy"] = os.environ["NO_PROXY"]

        agent = KanbanHeadlessAgent(state_dir=state_dir, project_root=project)
        prompt, prompt_source = _make_prompt(
            agent,
            project,
            args.message,
            use_history=not args.fresh_prompt,
        )
        system_prompt = agent._system_prompt("dangerous_full")
        backend = CodexHeadlessBackend()
        print("COMMAND:", " ".join(backend.build_args()))
        print("PROJECT:", project)
        print("STATE_DIR:", state_dir)
        print("MODEL:", args.model)
        print("ENDPOINT: http://10.0.169.15:8317/v1/responses")
        print("PROMPT_SOURCE:", prompt_source)
        print("PROMPT_CHARS:", len(prompt))
        print("SYSTEM_CHARS:", len(system_prompt))
        print("START:", time.strftime("%Y-%m-%d %H:%M:%S %z"), flush=True)

        started = time.monotonic()
        result = backend.run_turn(
            prompt=prompt,
            cwd=project,
            system_prompt=system_prompt,
            thread_id="manual-kanban-repro",
            provider_session_id="",
            on_session_id=lambda sid: print(f"PROVIDER_SESSION: {sid}", flush=True),
            on_message=lambda msg: print(
                f"EVENT: {msg.type} {msg.content[:120]!r}", flush=True
            ),
            timeout_s=args.timeout,
            thinking_level="medium",
            run_id="manual-kanban-repro",
            run_thread_id="manual-kanban-repro",
            project_id=project.name,
            conversation_id=f"kanban:{project.name}",
            permission_profile="dangerous_full",
        )
        elapsed = time.monotonic() - started
        print("END:", time.strftime("%Y-%m-%d %H:%M:%S %z"))
        print("ELAPSED_S:", round(elapsed, 2))
        print("OK:", result.ok)
        print("STATUS:", result.status)
        print("ERROR:", result.error or "<none>")
        print("REPLY:", (result.reply or "")[:500])
        return 0 if result.ok else 1
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
