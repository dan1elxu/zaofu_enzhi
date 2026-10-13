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
from zf.runtime.evaluation_prompts import (
    evaluation_contract_for_stage,
    prompt_version_for_stage,
)
from zf.runtime.evaluation_packets import evaluation_request_id

QUEUE_RELATIVE_PATH = Path("artifacts/benchmark-evaluations/scheduler.json")
DEFAULT_SLOT_COUNT = 2
MAX_SLOT_COUNT = 8
PROGRESS_EVENT = "benchmark.evaluation.progress"
TERMINAL = {"completed", "inconclusive"}
# The observer is the only production producer of queue entries.  Keep this
# allowlist at the durable queue boundary as well, so a scheduler restart
# cannot revive entries written by the retired evaluation contract.  The two
# lane stages belong to the eval-run driver (trial-local queues).
SUPPORTED_JUDGE_TYPES = frozenset({
    "task_type", "contribution", "discussion", "prd_fidelity", "conclusion",
    "lane_prd_fidelity", "cross_compare",
})
# Aggregator stages may consume terminal dependency failures so they can
# record an incomplete verdict / leave a missing lane blank.
_AGGREGATOR_JUDGE_TYPES = frozenset({"conclusion", "cross_compare"})


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
    input_digest: str = ""
    freeze_event_id: str = ""
    evaluation_origin: str = ""
    # v1 evaluation metadata.  These fields are persisted with the queue so
    # a resumed observer cannot silently switch role/profile mid-run.
    evaluator_profile: str = ""
    task_type: str = ""
    role_ref: str = ""
    role_digest: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "queued"
    slot_id: int = -1
    error: str = ""
    # A duplicate queue row remains in the durable audit trail, but this
    # canonical job owns future scheduling.  It deliberately does not change
    # ``status``: completed/inconclusive rows retain their result evidence and
    # a running row is allowed to finish its already-issued provider request.
    superseded_by: str = ""
    updated_at: str = ""


StageRunner = Callable[[EvaluationQueueEntry, int], dict[str, Any]]


class EvaluationScheduler:
    """One queue owner per state dir; never block discovery on a provider.

    Inputs and dependencies are durable. Only runnable entries enter the pool,
    so blocked same-channel work cannot occupy another channel's slot. A worker
    schedules its successor immediately, without waiting for an observer scan.
    """

    def __init__(self, state_dir: Path, *, max_slots: int | None = None,
                 inline: bool = False, queue_path: Path | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.queue_path = Path(queue_path) if queue_path is not None else (
            self.state_dir / QUEUE_RELATIVE_PATH
        )
        self.queue_path.resolve().relative_to(self.state_dir.resolve())
        self.evaluation_origin = (
            "" if self.queue_path.resolve() == (self.state_dir / QUEUE_RELATIVE_PATH).resolve()
            else "manual_regrade"
        )
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
                if not isinstance(payload, dict) or payload.get("schema_version") != "benchmark.evaluation.scheduler.v1":
                    raise ValueError("unsupported evaluation scheduler schema")
                raw_entries = payload.get("entries")
                if not isinstance(raw_entries, list):
                    raise ValueError("evaluation scheduler entries must be a list")
                entry_fields = {item.name for item in fields(EvaluationQueueEntry)}
                for raw in raw_entries:
                    if not isinstance(raw, dict) or not str(raw.get("job_id") or "").strip():
                        raise ValueError("evaluation scheduler entry requires a job_id")
                    job_id = str(raw["job_id"])
                    if job_id in self._entries:
                        raise ValueError(f"duplicate evaluation scheduler job_id: {job_id}")
                    depends_on = raw.get("depends_on", [])
                    if (
                        not isinstance(depends_on, list)
                        or any(not isinstance(dep, str) or not dep.strip() for dep in depends_on)
                        or len(set(depends_on)) != len(depends_on)
                        or job_id in depends_on
                    ):
                        raise ValueError(f"invalid dependencies for evaluation job: {job_id}")
                    self._entries[job_id] = EvaluationQueueEntry(**{
                        key: value for key, value in raw.items() if key in entry_fields
                    })
                for entry in self._entries.values():
                    if entry.status not in {"queued", "running", "completed", "inconclusive"}:
                        entry.status = "inconclusive"
                        entry.slot_id = -1
                        entry.error = "unsupported evaluation queue status; not dispatched"
                        continue
                    if entry.trial_id and entry.source_snapshot_digest:
                        expected_job = evaluation_request_id(
                            workflow_run_id=entry.workflow_run_id,
                            trial_id=entry.trial_id,
                            judge_type=entry.judge_type,
                            source_snapshot_digest=entry.source_snapshot_digest,
                            prompt_version=prompt_version_for_stage(entry.judge_type),
                        )
                        if entry.job_id != expected_job:
                            entry.status = "inconclusive"
                            entry.slot_id = -1
                            entry.error = "evaluation job identity mismatch"
                            continue
                    missing = [dep for dep in entry.depends_on if dep not in self._entries]
                    if missing:
                        entry.status = "inconclusive"
                        entry.slot_id = -1
                        entry.error = "missing evaluation dependencies: " + ", ".join(missing)
                for job_id in self._cyclic_jobs():
                    entry = self._entries[job_id]
                    entry.status = "inconclusive"
                    entry.slot_id = -1
                    entry.error = "cyclic evaluation dependencies"
            for entry in self._entries.values():
                if entry.evaluation_origin != self.evaluation_origin:
                    entry.status, entry.slot_id = "inconclusive", -1
                    entry.error = "evaluation origin does not match scheduler queue"
                    continue
                if entry.judge_type not in SUPPORTED_JUDGE_TYPES:
                    # Preserve the old row for audit/recovery diagnostics, but
                    # make it terminal before dispatch.  Legacy entries must
                    # never reach a provider or the canonical runner.
                    entry.status = "inconclusive"
                    entry.slot_id = -1
                    entry.error = (
                        "unsupported evaluation stage retired; "
                        f"not dispatched: {entry.judge_type or '<empty>'}"
                    )
                    continue
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
            if entry.evaluation_origin != self.evaluation_origin:
                return False
            if entry.judge_type not in SUPPORTED_JUDGE_TYPES:
                # Do not let callers reintroduce the retired v1 stage names.
                # Returning False keeps this API non-throwing like duplicate
                # enqueue handling while making the rejection observable to
                # the caller.
                return False
            if not str(entry.job_id or "").strip() or not str(entry.workflow_run_id or "").strip():
                return False
            if entry.trial_id and entry.source_snapshot_digest:
                expected_job = evaluation_request_id(
                    workflow_run_id=entry.workflow_run_id,
                    trial_id=entry.trial_id,
                    judge_type=entry.judge_type,
                    source_snapshot_digest=entry.source_snapshot_digest,
                    prompt_version=prompt_version_for_stage(entry.judge_type),
                )
                if entry.job_id != expected_job:
                    return False
            if entry.input_ref:
                input_ref = Path(entry.input_ref)
                if input_ref.is_absolute() or ".." in input_ref.parts:
                    return False
                try:
                    (self.state_dir / input_ref).resolve().relative_to(self.state_dir.resolve())
                except ValueError:
                    return False
            if (
                any(not isinstance(dep, str) or not dep.strip() or dep == entry.job_id
                    for dep in entry.depends_on)
                or len(set(entry.depends_on)) != len(entry.depends_on)
                or any(dep not in self._entries for dep in entry.depends_on)
            ):
                return False
            old = self._entries.get(entry.job_id)
            # A duplicate may never be revived by a repair enqueue.  The
            # supersession is durable audit state rather than a terminal
            # result, so the usual terminal-row repair exception is unsafe.
            if old is not None and old.superseded_by:
                return False
            if old is not None and not (repair and old.status in TERMINAL):
                return False
            previous = self._entries.get(entry.job_id)
            self._entries[entry.job_id] = entry
            if self._cyclic_jobs():
                if previous is None:
                    self._entries.pop(entry.job_id, None)
                else:
                    self._entries[entry.job_id] = previous
                return False
            self._transition(entry)
            return True

    def supersede_jobs(self, mapping: dict[str, str]) -> bool:
        """Durably stop duplicate canonical-queue jobs from being dispatched.

        This is intentionally a narrow recovery primitive for the observer:
        both rows must describe the same frozen automatic evaluation stage,
        and the target must be a still-canonical row.  It never deletes a row,
        changes its result status, or attempts to interrupt an in-flight
        provider call.  Replaying an already-applied mapping is a no-op and
        emits no duplicate audit event.
        """
        with self._lock:
            if not mapping or not all(
                isinstance(duplicate_id, str) and duplicate_id.strip()
                and isinstance(canonical_id, str) and canonical_id.strip()
                for duplicate_id, canonical_id in mapping.items()
            ):
                return False
            normalized = {
                duplicate_id.strip(): canonical_id.strip()
                for duplicate_id, canonical_id in mapping.items()
            }
            if any(duplicate_id == canonical_id
                   for duplicate_id, canonical_id in normalized.items()):
                return False
            # A canonical target must not itself be another duplicate in this
            # operation.  This rules out chains and cycles before mutation.
            if set(normalized).intersection(normalized.values()):
                return False

            changed: list[EvaluationQueueEntry] = []
            for duplicate_id, canonical_id in normalized.items():
                duplicate = self._entries.get(duplicate_id)
                canonical = self._entries.get(canonical_id)
                if duplicate is None or canonical is None:
                    return False
                if duplicate.superseded_by:
                    if duplicate.superseded_by != canonical_id:
                        return False
                    # Preserve strict replay idempotence even if a later
                    # repair attempt made the old target invalid.
                    continue
                if (
                    canonical.superseded_by
                    or duplicate.evaluation_origin != ""
                    or canonical.evaluation_origin != ""
                    or not all((
                        duplicate.channel_id, duplicate.workflow_run_id,
                        duplicate.freeze_event_id, duplicate.judge_type,
                        canonical.channel_id, canonical.workflow_run_id,
                        canonical.freeze_event_id, canonical.judge_type,
                    ))
                    or not self._same_supersession_identity(duplicate, canonical)
                ):
                    return False
                changed.append(duplicate)

            for duplicate in changed:
                duplicate.superseded_by = normalized[duplicate.job_id]
                self._transition(duplicate)
            return bool(changed)

    def tick(self, runner: StageRunner) -> list[dict[str, Any]]:
        with self._lock:
            self._runner = runner
            self._dispatch()
            results, self._results = self._results, []
            return results

    def _dispatch(self) -> None:
        while not self._closed and self._runner and len(self._active) < self.max_slots:
            self._settle_blocked_entries()
            active_channels = {self._entries[job].channel_id for job in self._active.values()}
            entry = next((item for item in self._entries.values()
                          if not item.superseded_by and item.status == "queued"
                          and item.channel_id not in active_channels
                          and self._dependencies_satisfied(item)), None)
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

    def _dependencies_satisfied(self, entry: EvaluationQueueEntry) -> bool:
        """Return whether a queued entry may run.

        A failed classifier must never release semantic quality providers.
        Conclusions remain deterministic and may consume terminal quality
        failures so they can record an incomplete verdict.
        """
        if not entry.depends_on:
            return True
        classifier_failed = False
        for dep_id in entry.depends_on:
            dep = self._entries.get(dep_id)
            if dep is None or dep.superseded_by or dep.status not in TERMINAL:
                return False
            if entry.judge_type not in _AGGREGATOR_JUDGE_TYPES and dep.judge_type == "task_type":
                classifier_failed = dep.status != "completed"
        return not classifier_failed

    def _settle_blocked_entries(self) -> None:
        """Fail closed dependents of an inconclusive dependency."""
        changed = True
        while changed:
            changed = False
            for entry in self._entries.values():
                if (entry.superseded_by or entry.status != "queued"
                        or entry.judge_type in _AGGREGATOR_JUDGE_TYPES):
                    continue
                missing = [dep for dep in entry.depends_on if dep not in self._entries]
                failed = [
                    self._entries[dep] for dep in entry.depends_on
                    if dep in self._entries and (
                        self._entries[dep].status == "inconclusive"
                        or self._entries[dep].superseded_by
                    )
                ]
                if missing or failed:
                    entry.status = "inconclusive"
                    entry.slot_id = -1
                    entry.error = (
                        "missing evaluation dependencies: " + ", ".join(missing)
                        if missing else
                        "evaluation dependency inconclusive: " + ", ".join(
                            dep.job_id for dep in failed
                        )
                    )
                    self._transition(entry)
                    changed = True

    @staticmethod
    def _same_supersession_identity(
        duplicate: EvaluationQueueEntry, canonical: EvaluationQueueEntry,
    ) -> bool:
        return (
            duplicate.channel_id == canonical.channel_id
            and duplicate.workflow_run_id == canonical.workflow_run_id
            and duplicate.freeze_event_id == canonical.freeze_event_id
            and duplicate.judge_type == canonical.judge_type
        )

    def _cyclic_jobs(self) -> set[str]:
        cycles: set[str] = set()
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(job_id: str, path: list[str]) -> None:
            if job_id in visiting:
                try:
                    cycles.update(path[path.index(job_id):])
                except ValueError:
                    cycles.add(job_id)
                return
            if job_id in visited or job_id not in self._entries:
                return
            visiting.add(job_id)
            for dep in self._entries[job_id].depends_on:
                visit(dep, path + [job_id])
            visiting.remove(job_id)
            visited.add(job_id)

        for job_id in self._entries:
            visit(job_id, [])
        return cycles

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
            payload={
                "evaluation_contract": evaluation_contract_for_stage(entry.judge_type),
                "prompt_version": prompt_version_for_stage(entry.judge_type),
                **{key: value for key, value in asdict(entry).items()
                   if key not in {"input_ref", "depends_on"}},
            },
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.stop_event.set()
        if self._executor:
            self._executor.shutdown(wait=True)
        self._owner.close()


_SCHEDULERS: dict[tuple[str, bool, str], EvaluationScheduler] = {}
_SCHEDULERS_LOCK = threading.Lock()


def scheduler_for_state(
    state_dir: Path, *, inline: bool = False, queue_path: Path | None = None,
) -> EvaluationScheduler:
    resolved_queue = Path(queue_path) if queue_path is not None else Path(state_dir) / QUEUE_RELATIVE_PATH
    key = (str(Path(state_dir).resolve()), inline, str(resolved_queue.resolve()))
    with _SCHEDULERS_LOCK:
        scheduler = _SCHEDULERS.get(key)
        if scheduler is None or scheduler._closed:
            scheduler = EvaluationScheduler(state_dir, inline=inline, queue_path=queue_path)
            _SCHEDULERS[key] = scheduler
        return scheduler
