#!/usr/bin/env python3
"""Thin batch driver for the three-lane PRD eval over a JSONL prompt dataset.

Dataset lines are JSON objects: ``{"prompt": "..."}`` or
``{"prompt_file": "..."}`` (relative paths resolve against the dataset file).
Completed ``(prompt_digest, trial)`` pairs are skipped, so re-running the
script resumes an interrupted batch; one failing prompt never aborts the rest.
No kernel state machine lives here — the script only loops
``zf.runtime.eval_driver.run_eval`` with a concurrency cap.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def _load_prompts(dataset: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lineno, raw in enumerate(
        dataset.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{dataset.name}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{dataset.name}:{lineno}: expected a JSON object")
        text = str(row.get("prompt") or "")
        prompt_file = str(row.get("prompt_file") or "")
        if prompt_file:
            path = Path(prompt_file)
            if not path.is_absolute():
                path = dataset.parent / path
            text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"{dataset.name}:{lineno}: empty prompt")
        rows.append({"line": lineno, "prompt_text": text})
    if not rows:
        raise ValueError(f"{dataset}: no prompts")
    return rows


def _parse_lanes(value: str) -> list[str]:
    from zf.runtime.eval_lanes import canonical_lane_id

    lanes: list[str] = []
    unknown: list[str] = []
    for raw in value.split(","):
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


def _pending_trials(state_dir: Path, digest: str, n: int) -> list[int]:
    from zf.runtime import eval_run

    pending: list[int] = []
    for trial in range(1, n + 1):
        run_id = eval_run.eval_run_id(prompt_digest=digest, trial=trial)
        run_dir = eval_run.eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
        if not eval_run.run_manifest_path(run_dir).is_file():
            pending.append(trial)
            continue
        manifest = eval_run.load_manifest(state_dir, run_id)
        if manifest.status != eval_run.RUN_STATUS_COMPLETED:
            pending.append(trial)
    return pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="JSONL prompts dataset")
    parser.add_argument("--n", type=int, default=1, help="trials per prompt (default 1)")
    parser.add_argument("--max-concurrent", type=int, default=1,
                        help="concurrent prompts; each prompt already runs its lanes in parallel")
    parser.add_argument("--lanes", default="terminal,kanban,channel")
    parser.add_argument("--backend", default="claude-headless")
    parser.add_argument("--channel-backend", default="")
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    parser.add_argument("--prompt-version", default="eval-prompt.v1")
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args(argv)

    from zf.core.config.project_context import resolve_state_dir
    from zf.core.events import EventLog, EventWriter
    from zf.runtime import eval_run
    from zf.runtime.eval_driver import run_eval, run_summary

    try:
        rows = _load_prompts(args.dataset)
        lanes = _parse_lanes(args.lanes)
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.n < 1 or args.max_concurrent < 1 or args.timeout_s <= 0:
        print("error: --n/--max-concurrent must be >= 1 and --timeout-s > 0",
              file=sys.stderr)
        return 2
    state_dir = (
        Path(args.state_dir).resolve()
        if args.state_dir
        else resolve_state_dir(cwd=Path.cwd())
    )
    state_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        row["digest"] = eval_run.prompt_digest(
            row["prompt_text"], prompt_version=args.prompt_version,
        )
        row["pending"] = _pending_trials(state_dir, row["digest"], args.n)
    planned = [row for row in rows if row["pending"]]
    print(
        f"dataset: {len(rows)} prompts, {sum(len(r['pending']) for r in rows)} "
        f"trials to run ({args.n} per prompt; "
        f"{len(rows) - len(planned)} prompts already complete)"
    )

    def work(row: dict[str, Any]) -> None:
        writer = EventWriter(EventLog(state_dir / "events.jsonl"))
        try:
            results = run_eval(
                prompt_text=row["prompt_text"],
                n_trials=args.n,
                lanes=lanes,
                backend=args.backend,
                channel_backend=args.channel_backend,
                timeout_s=args.timeout_s,
                prompt_version=args.prompt_version,
                state_dir=state_dir,
                writer=writer,
            )
            row["results"] = [item.to_dict() for item in results]
            row["error"] = ""
        except Exception as exc:
            row["results"] = []
            row["error"] = f"{type(exc).__name__}: {exc}"

    if planned:
        with ThreadPoolExecutor(
            max_workers=args.max_concurrent, thread_name_prefix="zf-eval-batch"
        ) as pool:
            list(pool.map(work, planned))

    failures = 0
    print("\n==== eval batch summary ====")
    for row in rows:
        digest = row["digest"]
        head = f"line {row['line']}  {digest[:16]}…"
        if row.get("error"):
            failures += 1
            print(f"{head}  ERROR: {row['error']}")
            continue
        driver_failed = [
            item for item in row.get("results") or [] if item.get("status") != "completed"
        ]
        if driver_failed:
            failures += 1
            print(f"{head}  driver failures: "
                  f"{[item.get('run_id') for item in driver_failed]}")
            continue
        trials: list[dict[str, Any]] = []
        for trial in range(1, args.n + 1):
            run_id = eval_run.eval_run_id(prompt_digest=digest, trial=trial)
            try:
                summary = run_summary(state_dir, run_id)
            except (ValueError, FileNotFoundError):
                trials.append({"trial": trial, "status": "missing"})
                continue
            conclusion = summary.get("conclusion") or {}
            lanes_info = conclusion.get("lanes") or {}
            trials.append({
                "trial": trial,
                "status": summary["manifest"]["status"],
                "conclusion": conclusion.get("status") or "",
                "means": {
                    lane: info.get("blind_mean")
                    for lane, info in lanes_info.items()
                    if info.get("blind_mean") is not None
                },
                "top_lane": (conclusion.get("recommendation") or {}).get("top_lane") or "",
            })
        done = sum(1 for item in trials if item["status"] == "completed")
        incomplete = sum(1 for item in trials if item.get("conclusion") == "incomplete")
        print(
            f"{head}  trials {done}/{args.n} completed"
            + (f" ({incomplete} incomplete)" if incomplete else "")
        )
        for item in trials:
            means = " ".join(
                f"{lane}={mean}" for lane, mean in sorted(item["means"].items())
            )
            print(
                f"    trial {item['trial']}: {item['status']} {item.get('conclusion')}"
                + (f" top={item['top_lane']}" if item.get("top_lane") else "")
                + (f"  均分 {means}" if means else "")
            )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
