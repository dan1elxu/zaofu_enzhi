"""Durable bounded queue for independent channels and ordered Judge stages."""
from __future__ import annotations

import fcntl
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from zf.core.events import EventLog, EventWriter
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.evaluation_turns import EvaluationCancelled

QUEUE_RELATIVE_PATH = Path("artifacts/benchmark-evaluations/scheduler.json")
DEFAULT_SLOT_COUNT = 2
MAX_SLOT_COUNT = 8
PROGRESS_EVENT = "benchmark.evaluation.progress"
TERMINAL = {"completed", "inconclusive"}


def configured_slot_count() -> int:
    try:
        return max(1, min(MAX_SLOT_COUNT, int(os.environ.get(
            "ZF_EVALUATION_JUDGE_SLOTS", str(DEFAULT_SLOT_COUNT),
        ))))
    except ValueError:
        return DEFAULT_SLOT_COUNT


@dataclass
class EvaluationQueueEntry:
    job_id: str
    channel_id: str
    workflow_run_id: str
    trial_id: str = ""
    judge_type: str = ""
    source_snapshot_digest: str = ""
    input_ref: str = ""
    freeze_event_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "queued"
    slot_id: int = -1
    error: str = ""
    updated_at: str = ""


StageRunner = Callable[[EvaluationQueueEntry, int], dict[str, Any]]


class EvaluationScheduler:
    """One queue owner per state dir; never block discovery on a provider.

    Inputs and dependencies are durable. Only runnable entries enter the pool,
    so blocked same-channel work cannot occupy another channel's slot. A worker
    schedules its successor immediately, without waiting for an observer scan.
    """

    def __init__(self, state_dir: Path, *, max_slots: int | None = None,
                 inline: bool = False) -> None:
        self.state_dir = Path(state_dir)
        self.queue_path = self.state_dir / QUEUE_RELATIVE_PATH
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self._owner = self.queue_path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._owner.close()
            raise RuntimeError("evaluation scheduler already active for this state dir") from None
        self.inline = inline
        self.max_slots = max(1, min(MAX_SLOT_COUNT, max_slots or configured_slot_count()))
        self._lock = threading.RLock()
        self._entries: dict[str, EvaluationQueueEntry] = {}
        self._active: dict[int, str] = {}
        self._results: list[dict[str, Any]] = []
        self._runner: StageRunner | None = None
        self._closed = False
        self.stop_event = threading.Event()
        try:
            if self.queue_path.exists():
                # Fail closed on corrupt state instead of resetting budgets.
                payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
                entry_fields = {item.name for item in fields(EvaluationQueueEntry)}
                self._entries = {
                    raw["job_id"]: EvaluationQueueEntry(**{
                        key: value for key, value in raw.items() if key in entry_fields
                    })
                    for raw in payload["entries"]
                }
            for entry in self._entries.values():
                if entry.status == "running":
                    # Runner resumes from its persisted provider attempt count.
                    entry.status, entry.slot_id = "queued", -1
            self._persist()
        except Exception:
            self._owner.close()
            raise
        self._executor = None if inline else ThreadPoolExecutor(
            max_workers=self.max_slots, thread_name_prefix="zf-evaluation-slot",
        )

    @property
    def entries(self) -> dict[str, EvaluationQueueEntry]:
        with self._lock:
            return {key: EvaluationQueueEntry(**asdict(value))
                    for key, value in self._entries.items()}

    def enqueue(self, entry: EvaluationQueueEntry, *, repair: bool = False) -> bool:
        with self._lock:
            old = self._entries.get(entry.job_id)
            if old is not None and not (repair and old.status in TERMINAL):
                return False
            self._entries[entry.job_id] = entry
            self._transition(entry)
            return True

    def tick(self, runner: StageRunner) -> list[dict[str, Any]]:
        with self._lock:
            self._runner = runner
            self._dispatch()
            results, self._results = self._results, []
            return results

    def _dispatch(self) -> None:
        while not self._closed and self._runner and len(self._active) < self.max_slots:
            active_channels = {self._entries[job].channel_id for job in self._active.values()}
            entry = next((item for item in self._entries.values()
                          if item.status == "queued" and item.channel_id not in active_channels
                          and all(dep in self._entries and self._entries[dep].status in TERMINAL
                                  for dep in item.depends_on)), None)
            if entry is None:
                return
            slot = next(i for i in range(self.max_slots) if i not in self._active)
            self._active[slot] = entry.job_id
            entry.status, entry.slot_id = "running", slot
            self._transition(entry)
            if self.inline:
                self._run(entry, slot, self._runner)
            else:
                assert self._executor is not None
                self._executor.submit(self._run, entry, slot, self._runner)

    def _run(self, entry: EvaluationQueueEntry, slot: int, runner: StageRunner) -> None:
        try:
            result = runner(entry, slot)
        except EvaluationCancelled:
            result = {"status": "queued"}
        except Exception as exc:
            # Transport/schema retries belong to the runner. A broken stage
            # callback is inconclusive, allowing deterministic conclusion.
            result = {"error": str(exc), "status": "inconclusive"}
        with self._lock:
            status = str(result.get("status") or "completed")
            entry.status = status if status in {"inconclusive", "queued"} else "completed"
            entry.error = str(result.get("error") or "")
            self._results.append({"job_id": entry.job_id, **result})
            self._active.pop(slot, None)
            self._transition(entry)
            # Inline mode drains iteratively in _dispatch, without recursion.
            if not self.inline:
                self._dispatch()

    def _persist(self) -> None:
        atomic_write_text(self.queue_path, json.dumps({
            "schema_version": "benchmark.evaluation.scheduler.v1",
            "max_slots": self.max_slots,
            "entries": [asdict(item) for item in self._entries.values()],
        }, ensure_ascii=False, indent=2) + "\n")

    def _transition(self, entry: EvaluationQueueEntry) -> None:
        entry.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist()
        EventWriter(EventLog(self.state_dir / "events.jsonl")).emit(
            PROGRESS_EVENT, actor="evaluation-scheduler",
            correlation_id=entry.workflow_run_id,
            payload={key: value for key, value in asdict(entry).items()
                     if key not in {"input_ref", "depends_on"}},
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.stop_event.set()
        if self._executor:
            self._executor.shutdown(wait=True)
        self._owner.close()


_SCHEDULERS: dict[tuple[str, bool], EvaluationScheduler] = {}
_SCHEDULERS_LOCK = threading.Lock()


def scheduler_for_state(state_dir: Path, *, inline: bool = False) -> EvaluationScheduler:
    key = (str(Path(state_dir).resolve()), inline)
    with _SCHEDULERS_LOCK:
        scheduler = _SCHEDULERS.get(key)
        if scheduler is None or scheduler._closed:
            scheduler = EvaluationScheduler(state_dir, inline=inline)
            _SCHEDULERS[key] = scheduler
        return scheduler
