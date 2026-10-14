"""Lane orchestration for the three-lane PRD eval driver.

Split from ``eval_driver`` by responsibility: this module owns parallel lane
driving and lane-result -> manifest transitions; ``eval_driver`` owns the
trial lifecycle and the judge chain.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from zf.core.events import EventWriter
from zf.runtime import eval_run
from zf.runtime.eval_lanes import LANE_CHANNEL, LaneResult, run_lane


def _lane_kwargs(
    lane: str,
    *,
    backend: str,
    channel_backend: str,
    lane_overrides: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if lane == LANE_CHANNEL:
        selected = channel_backend or backend or "claude-headless"
        kwargs["backend"] = selected
    elif backend:
        kwargs["backend"] = backend
    kwargs.update((lane_overrides or {}).get(lane) or {})
    return kwargs


def _drive_lanes(
    *,
    state_dir: Path,
    run_id: str,
    run_dir: Path,
    prompt_text: str,
    backend: str,
    channel_backend: str,
    channel_shared_state: bool,
    timeout_s: float,
    writer: EventWriter,
    lane_overrides: dict[str, dict[str, Any]] | None,
) -> None:
    manifest = eval_run.load_manifest(state_dir, run_id)
    open_lanes = [
        lane for lane, lane_state in manifest.lanes.items()
        if lane_state.status not in eval_run.LANE_TERMINAL_STATUSES
    ]
    if not open_lanes:
        return

    def work(lane: str) -> None:
        _drive_lane(
            state_dir=state_dir,
            run_id=run_id,
            run_dir=run_dir,
            lane=lane,
            prompt_text=prompt_text,
            backend=backend,
            channel_backend=channel_backend,
            channel_shared_state=channel_shared_state,
            timeout_s=timeout_s,
            writer=writer,
            lane_overrides=lane_overrides,
        )

    with ThreadPoolExecutor(max_workers=len(open_lanes), thread_name_prefix="zf-eval-lane") as pool:
        list(pool.map(work, open_lanes))


def _drive_lane(
    *,
    state_dir: Path,
    run_id: str,
    run_dir: Path,
    lane: str,
    prompt_text: str,
    backend: str,
    channel_backend: str,
    channel_shared_state: bool,
    timeout_s: float,
    writer: EventWriter,
    lane_overrides: dict[str, dict[str, Any]] | None,
) -> None:
    try:
        lane_state = eval_run.load_manifest(state_dir, run_id).lanes[lane]
        if lane_state.status in eval_run.LANE_TERMINAL_STATUSES:
            return
        prd = eval_run.lane_prd_path(run_dir, lane)
        if lane_state.status == eval_run.LANE_STATUS_PENDING:
            eval_run.mark_lane_running(state_dir, run_id, lane)
        elif (
            lane_state.status == eval_run.LANE_STATUS_RUNNING
            and prd.is_file()
            and prd.read_text(encoding="utf-8").strip()
        ):
            # A previous driver died after the lane transcribed its PRD but
            # before completing the lane; bind the existing artifact instead
            # of re-running the provider.
            eval_run.complete_lane(
                state_dir, run_id, lane,
                status=eval_run.LANE_STATUS_PRD_FROZEN, writer=writer,
            )
            return
        lane_kwargs = _lane_kwargs(
            lane, backend=backend, channel_backend=channel_backend,
            lane_overrides=lane_overrides,
        )
        if channel_shared_state and lane == LANE_CHANNEL:
            # Run the channel on the workspace ledger so a watching Web
            # dashboard shows the discussion live; scope by per-trial id.
            trial = eval_run.parse_eval_run_id(run_id)[1]
            lane_kwargs["state_dir"] = state_dir
            lane_kwargs["channel_id"] = f"eval-prd-t{trial}"
        result = run_lane(
            lane,
            lane_dir=eval_run.lane_dir(run_dir, lane),
            prompt=prompt_text,
            timeout_s=timeout_s,
            **lane_kwargs,
        )
        _complete_lane_from_result(state_dir, run_id, lane, result, writer=writer)
    except Exception as exc:
        try:
            eval_run.complete_lane(
                state_dir, run_id, lane,
                status=eval_run.LANE_STATUS_FAILED,
                error=f"driver:{type(exc).__name__}: {exc}"[:300],
                writer=writer,
            )
        except ValueError:
            pass


def _complete_lane_from_result(
    state_dir: Path,
    run_id: str,
    lane: str,
    result: LaneResult,
    *,
    writer: EventWriter,
) -> None:
    if result.ok:
        eval_run.complete_lane(
            state_dir, run_id, lane,
            status=eval_run.LANE_STATUS_PRD_FROZEN, writer=writer,
        )
        return
    error = (
        eval_run.LANE_ERROR_TIMEOUT
        if result.status == "timeout"
        else (result.reason or result.status or "failed")
    )
    eval_run.complete_lane(
        state_dir, run_id, lane,
        status=eval_run.LANE_STATUS_FAILED, error=error[:300], writer=writer,
    )
