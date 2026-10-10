"""Channel reply capacity coordination shared by routing and dispatch."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
from typing import Any, Callable

from zf.core.state.locks import locked_path
from zf.runtime.channel_contracts import channel_max_parallel_replies


RUNNING_REPLY_STATUSES = frozenset({"running", "started"})


def channel_dispatch_lock(state_dir: Path, channel_id: str):
    lock_key = hashlib.sha1(channel_id.encode("utf-8")).hexdigest()[:16]
    return locked_path(state_dir / "locks" / f"channel-dispatch-{lock_key}")


def channel_dispatch_capacity(channel: dict[str, Any]) -> int:
    running = sum(
        1
        for item in channel.get("reply_requests") or []
        if isinstance(item, dict)
        and str(item.get("status") or "") in RUNNING_REPLY_STATUSES
    )
    return max(channel_max_parallel_replies(channel) - running, 0)


def dispatch_candidate_waves(
    candidates: list[dict[str, Any]],
    *,
    channel_id: str,
    channel_loader: Callable[[], dict[str, Any]],
    dispatch_one: Callable[[dict[str, Any]], Any],
    max_dispatch: int,
) -> list[Any]:
    """Drain candidates in bounded waves while terminal progress frees slots."""

    attempted: set[str] = set()
    collected: list[Any] = []
    while True:
        current = channel_loader()
        remaining = [
            item
            for item in candidates
            if str(item.get("request_id") or "") not in attempted
        ]
        capacity = min(max_dispatch, channel_dispatch_capacity(current))
        busy = {str(row.get("target_member_id") or "") for row in current.get("reply_requests", [])
                if row.get("status") in RUNNING_REPLY_STATUSES}
        batch = []
        for item in remaining:
            if len(batch) >= capacity:
                break
            member = str(item.get("target_member_id") or item.get("request_id") or "")
            if member in busy:
                continue
            batch.append(item)
            busy.add(member)
        if not batch:
            break
        attempted.update(str(item.get("request_id") or "") for item in batch)
        if len(batch) == 1:
            results = [dispatch_one(batch[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=min(
                    len(batch),
                    channel_max_parallel_replies(current),
                ),
                thread_name_prefix=f"zf-channel-dispatch-{channel_id}",
            ) as pool:
                results = list(pool.map(dispatch_one, batch))
        collected.extend(results)
        if not any(
            bool(getattr(result, "completed", None))
            or bool(getattr(result, "failed", None))
            for result in results
        ):
            break
    return collected


__all__ = [
    "channel_dispatch_capacity",
    "channel_dispatch_lock",
    "dispatch_candidate_waves",
]
