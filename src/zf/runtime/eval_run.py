"""Three-lane PRD eval run state model (docs/ideas/three-lane-prd-eval.md).

One eval run is one ``(prompt packet, trial)`` pair rooted at
``artifacts/benchmark-evaluations/runs/<prompt_digest>/trial-<n>/`` under the
project state dir.  Every lane works in an isolated sibling directory and
honors a single contract: its PRD lands at ``lanes/<lane>/prd/prd.md``.

Run lifecycle is mirrored to the main event ledger through informational
``benchmark.eval.*`` events (projection-only, never wake-worthy).  The
``run.json`` manifest inside the run directory is the canonical state;
lane-internal events stay inside the lane's own state dir and never enter
the main ledger.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from zf.core.events import EventWriter
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.evaluation_packets import (
    EVALUATION_ARTIFACT_ROOT,
    _safe_segment,
    stable_digest,
)

RUN_MANIFEST_SCHEMA_VERSION = "benchmark.eval.run.v1"
PROMPT_PACKET_SCHEMA_VERSION = "benchmark.eval.prompt.v1"
DEFAULT_PROMPT_VERSION = "eval-prompt.v1"

RUN_STARTED_EVENT = "benchmark.eval.run.started"
LANE_COMPLETED_EVENT = "benchmark.eval.lane.completed"
RUN_COMPLETED_EVENT = "benchmark.eval.run.completed"
EVAL_RUN_EVENT_TYPES = frozenset({
    RUN_STARTED_EVENT,
    LANE_COMPLETED_EVENT,
    RUN_COMPLETED_EVENT,
})

DEFAULT_ACTOR = "eval-driver"

LANE_TERMINAL = "terminal"
LANE_KANBAN = "kanban"
LANE_CHANNEL = "channel"
DEFAULT_LANES: tuple[str, ...] = (LANE_TERMINAL, LANE_KANBAN, LANE_CHANNEL)

LANE_STATUS_PENDING = "pending"
LANE_STATUS_RUNNING = "running"
LANE_STATUS_PRD_FROZEN = "prd_frozen"
LANE_STATUS_FAILED = "failed"
LANE_STATUSES = frozenset({
    LANE_STATUS_PENDING,
    LANE_STATUS_RUNNING,
    LANE_STATUS_PRD_FROZEN,
    LANE_STATUS_FAILED,
})
LANE_TERMINAL_STATUSES = frozenset({LANE_STATUS_PRD_FROZEN, LANE_STATUS_FAILED})
# A timed-out lane is recorded as ``failed`` with this reason; retries are new
# trials, never a re-open of the same lane.
LANE_ERROR_TIMEOUT = "timeout"

RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"

MANIFEST_FILENAME = "run.json"
PROMPT_PACKET_FILENAME = "prompt.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------- ids


def prompt_digest(prompt_text: str, *, prompt_version: str) -> str:
    """Prompt packet identity: body + version via the stable_digest mechanism."""
    text = str(prompt_text)
    if not text.strip():
        raise ValueError("eval prompt text must be non-empty")
    version = str(prompt_version or "").strip()
    if not version:
        raise ValueError("eval prompt version must be non-empty")
    return stable_digest({
        "schema_version": PROMPT_PACKET_SCHEMA_VERSION,
        "prompt_text": text,
        "prompt_version": version,
    })


def eval_run_id(*, prompt_digest: str, trial: int) -> str:
    """Deterministic run identity; maps 1:1 onto the run directory."""
    digest = str(prompt_digest or "").strip()
    if not digest:
        raise ValueError("eval run requires a prompt digest")
    return f"{digest}-trial-{_trial_number(trial)}"


def parse_eval_run_id(run_id: str) -> tuple[str, int]:
    """Inverse of :func:`eval_run_id`; raises ValueError on malformed ids."""
    digest, sep, trial = str(run_id or "").rpartition("-trial-")
    if not sep or not digest or not trial.isdigit() or int(trial) < 1:
        raise ValueError(f"invalid eval run id: {run_id!r}")
    return digest, int(trial)


def lane_eval_run_id(*, prompt_digest: str, trial: int, lane: str) -> str:
    """``workflow_run_id`` for one lane's evaluation chain.

    Sister of ``evaluation_packets.evaluation_run_id``: deterministic and
    unique per (prompt packet, trial, lane), so per-trial schedulers and
    judge request identities never collide across lanes or trials.
    """
    _validate_lane(lane)
    return (
        f"lane-{_safe_segment(lane)}-{_safe_segment(str(prompt_digest).strip())}"
        f"-trial-{_trial_number(trial)}"
    )


# --------------------------------------------------------------------- paths


def eval_runs_root(state_dir: Path) -> Path:
    return Path(state_dir) / EVALUATION_ARTIFACT_ROOT / "runs"


def run_group_dir(state_dir: Path, *, prompt_digest: str) -> Path:
    return eval_runs_root(state_dir) / _safe_segment(str(prompt_digest).strip())


def prompt_packet_path(state_dir: Path, *, prompt_digest: str) -> Path:
    return run_group_dir(state_dir, prompt_digest=prompt_digest) / PROMPT_PACKET_FILENAME


def eval_run_dir(state_dir: Path, *, prompt_digest: str, trial: int) -> Path:
    return run_group_dir(state_dir, prompt_digest=prompt_digest) / f"trial-{_trial_number(trial)}"


def run_manifest_path(run_dir: Path) -> Path:
    return Path(run_dir) / MANIFEST_FILENAME


def lane_dir(run_dir: Path, lane: str) -> Path:
    return Path(run_dir) / "lanes" / _safe_segment(lane)


def lane_prd_path(run_dir: Path, lane: str) -> Path:
    """The lane contract: every lane's PRD must land at this exact path."""
    return lane_dir(run_dir, lane) / "prd" / "prd.md"


def lane_state_dir(run_dir: Path, lane: str) -> Path:
    """Isolated per-lane state dir; never shares state with the main project."""
    return lane_dir(run_dir, lane) / ".zf"


# --------------------------------------------------------------------- model


@dataclass(frozen=True)
class LaneState:
    lane: str
    status: str = LANE_STATUS_PENDING
    error: str = ""
    prd_ref: str = ""
    prd_digest: str = ""
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LaneState":
        return cls(
            lane=str(value.get("lane") or ""),
            status=str(value.get("status") or LANE_STATUS_PENDING),
            error=str(value.get("error") or ""),
            prd_ref=str(value.get("prd_ref") or ""),
            prd_digest=str(value.get("prd_digest") or ""),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
        )


@dataclass(frozen=True)
class EvalRunManifest:
    """Canonical ``run.json`` state for one eval run."""

    schema_version: str
    run_id: str
    prompt_digest: str
    prompt_version: str
    prompt_ref: str
    trial: int
    status: str = RUN_STATUS_RUNNING
    lanes: dict[str, LaneState] = field(default_factory=dict)
    created_at: str = ""
    completed_at: str = ""
    started_event_id: str = ""
    # Source run_id when this trial reuses another trial's lane PRDs and only
    # re-runs judging; empty means lanes genuinely ran in this trial.
    artifacts_source: str = ""

    @property
    def all_lanes_prd_frozen(self) -> bool:
        return bool(self.lanes) and all(
            lane.status == LANE_STATUS_PRD_FROZEN for lane in self.lanes.values()
        )

    @property
    def all_lanes_terminal(self) -> bool:
        return bool(self.lanes) and all(
            lane.status in LANE_TERMINAL_STATUSES for lane in self.lanes.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "prompt_digest": self.prompt_digest,
            "prompt_version": self.prompt_version,
            "prompt_ref": self.prompt_ref,
            "trial": self.trial,
            "status": self.status,
            "lanes": {key: lane.to_dict() for key, lane in self.lanes.items()},
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "started_event_id": self.started_event_id,
            "artifacts_source": self.artifacts_source,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvalRunManifest":
        raw_lanes = value.get("lanes")
        lanes: dict[str, LaneState] = {}
        if isinstance(raw_lanes, dict):
            for key, item in raw_lanes.items():
                if isinstance(item, dict):
                    lanes[str(key)] = LaneState.from_dict(item)
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            run_id=str(value.get("run_id") or ""),
            prompt_digest=str(value.get("prompt_digest") or ""),
            prompt_version=str(value.get("prompt_version") or ""),
            prompt_ref=str(value.get("prompt_ref") or ""),
            trial=int(value.get("trial") or 0),
            status=str(value.get("status") or RUN_STATUS_RUNNING),
            lanes=lanes,
            created_at=str(value.get("created_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            started_event_id=str(value.get("started_event_id") or ""),
            artifacts_source=str(value.get("artifacts_source") or ""),
        )


# --------------------------------------------------------------- persistence


def freeze_prompt_packet(
    state_dir: Path,
    *,
    prompt_text: str,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> dict[str, Any]:
    """Write-once immutable prompt packet shared by every trial of a prompt.

    Returns the packet dict.  An existing packet must recompute to the same
    digest; a mismatch is fail-closed, mirroring the observer's queue-input
    guard, so a corrupted identity anchor never silently splits a run group.
    """
    digest = prompt_digest(prompt_text, prompt_version=prompt_version)
    packet = {
        "schema_version": PROMPT_PACKET_SCHEMA_VERSION,
        "prompt_text": str(prompt_text),
        "prompt_version": str(prompt_version).strip(),
        "prompt_digest": digest,
    }
    path = prompt_packet_path(state_dir, prompt_digest=digest)
    if not path.exists():
        atomic_write_text(path, json.dumps(packet, ensure_ascii=False, indent=2) + "\n")
        return packet
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"eval prompt packet is unreadable: {path.name}") from exc
    if not isinstance(existing, dict):
        raise ValueError(f"eval prompt packet is not an object: {path.name}")
    existing_digest = prompt_digest(
        str(existing.get("prompt_text") or ""),
        prompt_version=str(existing.get("prompt_version") or ""),
    )
    if existing.get("prompt_digest") != digest or existing_digest != digest:
        raise ValueError(f"eval prompt packet digest mismatch: {path.name}")
    return existing


def start_eval_run(
    state_dir: Path,
    *,
    prompt_text: str,
    trial: int,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    lanes: Iterable[str] = DEFAULT_LANES,
    artifacts_source: str = "",
    writer: EventWriter | None = None,
    actor: str = DEFAULT_ACTOR,
    now: Callable[[], str] = _now_iso,
) -> EvalRunManifest:
    """Freeze the prompt packet, create the run directory + manifest, emit started.

    Re-starting an existing (packet, trial) run is fail-closed: a new attempt
    is a new trial, never an overwrite of the run directory.
    """
    state_dir = Path(state_dir)
    packet = freeze_prompt_packet(
        state_dir, prompt_text=prompt_text, prompt_version=prompt_version,
    )
    digest = packet["prompt_digest"]
    run_id = eval_run_id(prompt_digest=digest, trial=trial)
    lane_names = _validate_lanes(lanes)
    run_dir = eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
    with _run_lock(run_dir):
        manifest_path = run_manifest_path(run_dir)
        if manifest_path.exists():
            raise ValueError(f"eval run already exists: {run_id}")
        manifest = EvalRunManifest(
            schema_version=RUN_MANIFEST_SCHEMA_VERSION,
            run_id=run_id,
            prompt_digest=digest,
            prompt_version=packet["prompt_version"],
            prompt_ref=_rel(prompt_packet_path(state_dir, prompt_digest=digest), state_dir),
            trial=_trial_number(trial),
            lanes={lane: LaneState(lane=lane) for lane in lane_names},
            created_at=now(),
            artifacts_source=str(artifacts_source or ""),
        )
        _write_manifest(run_dir, manifest)
        if writer is not None:
            event = writer.emit(
                RUN_STARTED_EVENT,
                actor=actor,
                correlation_id=run_id,
                payload={
                    "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
                    "run_id": run_id,
                    "prompt_digest": digest,
                    "prompt_version": manifest.prompt_version,
                    "prompt_ref": manifest.prompt_ref,
                    "trial": manifest.trial,
                    "lanes": lane_names,
                    "artifacts_source": manifest.artifacts_source,
                    "run_dir": _rel(run_dir, state_dir),
                },
            )
            manifest = replace(manifest, started_event_id=event.id)
            _write_manifest(run_dir, manifest)
    return manifest


def mark_lane_running(
    state_dir: Path,
    run_id: str,
    lane: str,
    *,
    now: Callable[[], str] = _now_iso,
) -> EvalRunManifest:
    """Transition one lane pending -> running and stamp ``started_at``."""
    digest, trial = parse_eval_run_id(run_id)
    run_dir = eval_run_dir(Path(state_dir), prompt_digest=digest, trial=trial)
    with _run_lock(run_dir):
        manifest = _read_manifest(run_dir)
        _require_run_running(manifest)
        lane_state = _require_lane(manifest, lane)
        if lane_state.status != LANE_STATUS_PENDING:
            raise ValueError(
                f"lane {lane!r} of eval run {run_id} is not pending "
                f"(status={lane_state.status!r})"
            )
        updated = replace(
            lane_state, status=LANE_STATUS_RUNNING, started_at=now(),
        )
        manifest = _with_lane(manifest, updated)
        _write_manifest(run_dir, manifest)
    return manifest


def complete_lane(
    state_dir: Path,
    run_id: str,
    lane: str,
    *,
    status: str,
    error: str = "",
    writer: EventWriter | None = None,
    actor: str = DEFAULT_ACTOR,
    now: Callable[[], str] = _now_iso,
) -> EvalRunManifest:
    """Record one lane's terminal outcome and emit ``lane.completed``.

    ``prd_frozen`` requires the lane-contract PRD at ``lanes/<lane>/prd/prd.md``
    and binds its digest into the manifest.  ``failed`` requires a non-empty
    reason (``LANE_ERROR_TIMEOUT`` for timeouts).  Double completion of one
    lane is fail-closed.
    """
    if status not in LANE_TERMINAL_STATUSES:
        raise ValueError(
            f"lane terminal status must be one of {sorted(LANE_TERMINAL_STATUSES)}; "
            f"got {status!r}"
        )
    error = str(error or "").strip()
    if status == LANE_STATUS_FAILED and not error:
        raise ValueError("failed lanes must record a reason")
    state_dir = Path(state_dir)
    digest, trial = parse_eval_run_id(run_id)
    run_dir = eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
    with _run_lock(run_dir):
        manifest = _read_manifest(run_dir)
        _require_run_running(manifest)
        lane_state = _require_lane(manifest, lane)
        if lane_state.status in LANE_TERMINAL_STATUSES:
            raise ValueError(
                f"lane {lane!r} of eval run {run_id} already completed "
                f"(status={lane_state.status!r})"
            )
        prd_ref = ""
        prd_digest = ""
        if status == LANE_STATUS_PRD_FROZEN:
            prd_path = lane_prd_path(run_dir, lane)
            if not prd_path.is_file():
                raise ValueError(
                    f"lane {lane!r} contract violated: missing PRD at "
                    f"{prd_path.relative_to(run_dir).as_posix()}"
                )
            prd_ref = prd_path.relative_to(run_dir).as_posix()
            prd_digest = hashlib.sha256(prd_path.read_bytes()).hexdigest()
        updated = replace(
            lane_state,
            status=status,
            error=error,
            prd_ref=prd_ref,
            prd_digest=prd_digest,
            completed_at=now(),
        )
        manifest = _with_lane(manifest, updated)
        _write_manifest(run_dir, manifest)
        if writer is not None:
            writer.emit(
                LANE_COMPLETED_EVENT,
                actor=actor,
                correlation_id=run_id,
                causation_id=manifest.started_event_id or None,
                payload={
                    "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
                    "run_id": run_id,
                    "prompt_digest": manifest.prompt_digest,
                    "trial": manifest.trial,
                    "lane": lane,
                    "status": status,
                    "error": error,
                    "prd_ref": prd_ref,
                    "prd_digest": prd_digest,
                },
            )
    return manifest


def complete_run(
    state_dir: Path,
    run_id: str,
    *,
    writer: EventWriter | None = None,
    actor: str = DEFAULT_ACTOR,
    now: Callable[[], str] = _now_iso,
) -> EvalRunManifest:
    """Close the run once every lane reached a terminal status.

    The completed event carries the per-lane outcome summary and the
    ``all_lanes_prd_frozen`` flag so projections can flag incomplete runs
    without reading the manifest.
    """
    state_dir = Path(state_dir)
    digest, trial = parse_eval_run_id(run_id)
    run_dir = eval_run_dir(state_dir, prompt_digest=digest, trial=trial)
    with _run_lock(run_dir):
        manifest = _read_manifest(run_dir)
        _require_run_running(manifest)
        open_lanes = sorted(
            key for key, lane in manifest.lanes.items()
            if lane.status not in LANE_TERMINAL_STATUSES
        )
        if open_lanes:
            raise ValueError(
                f"eval run {run_id} still has open lanes: {', '.join(open_lanes)}"
            )
        manifest = replace(
            manifest, status=RUN_STATUS_COMPLETED, completed_at=now(),
        )
        _write_manifest(run_dir, manifest)
        if writer is not None:
            writer.emit(
                RUN_COMPLETED_EVENT,
                actor=actor,
                correlation_id=run_id,
                causation_id=manifest.started_event_id or None,
                payload={
                    "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
                    "run_id": run_id,
                    "prompt_digest": manifest.prompt_digest,
                    "trial": manifest.trial,
                    "lanes": {
                        key: lane.status for key, lane in manifest.lanes.items()
                    },
                    "all_lanes_prd_frozen": manifest.all_lanes_prd_frozen,
                    "run_dir": _rel(run_dir, state_dir),
                },
            )
    return manifest


# -------------------------------------------------------------------- reads


def load_manifest(state_dir: Path, run_id: str) -> EvalRunManifest:
    digest, trial = parse_eval_run_id(run_id)
    return _read_manifest(
        eval_run_dir(Path(state_dir), prompt_digest=digest, trial=trial)
    )


def list_eval_runs(
    state_dir: Path,
    *,
    prompt_digest: str = "",
) -> list[EvalRunManifest]:
    """All manifests under the runs root, optionally one run group only.

    Corrupt manifests raise rather than being silently dropped: a state file
    that cannot be parsed is a runtime bug, not an empty row.
    """
    root = eval_runs_root(Path(state_dir))
    if prompt_digest:
        base = root / _safe_segment(str(prompt_digest).strip())
        pattern = "trial-*/" + MANIFEST_FILENAME
    else:
        base = root
        pattern = "*/trial-*/" + MANIFEST_FILENAME
    manifests: list[EvalRunManifest] = []
    if not base.is_dir():
        return manifests
    for manifest_path in sorted(base.glob(pattern)):
        manifests.append(_read_manifest(manifest_path.parent))
    manifests.sort(key=lambda manifest: (manifest.prompt_digest, manifest.trial))
    return manifests


# ------------------------------------------------------------------ internal


def _trial_number(trial: int) -> int:
    try:
        number = int(trial)
    except (TypeError, ValueError):
        raise ValueError(f"eval run trial must be an integer; got {trial!r}") from None
    if number < 1:
        raise ValueError(f"eval run trial starts at 1; got {trial!r}")
    return number


def _validate_lane(lane: str) -> str:
    name = str(lane or "").strip()
    if name not in DEFAULT_LANES:
        raise ValueError(
            f"unknown eval lane {lane!r}; expected one of {list(DEFAULT_LANES)}"
        )
    return name


def _validate_lanes(lanes: Iterable[str]) -> list[str]:
    names: list[str] = []
    for lane in lanes:
        name = _validate_lane(lane)
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("eval run requires at least one lane")
    return names


def _rel(path: Path, base: Path) -> str:
    return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()


def _with_lane(manifest: EvalRunManifest, lane_state: LaneState) -> EvalRunManifest:
    lanes = dict(manifest.lanes)
    lanes[lane_state.lane] = lane_state
    return replace(manifest, lanes=lanes)


def _require_run_running(manifest: EvalRunManifest) -> None:
    if manifest.status != RUN_STATUS_RUNNING:
        raise ValueError(
            f"eval run {manifest.run_id} is not running "
            f"(status={manifest.status!r})"
        )


def _require_lane(manifest: EvalRunManifest, lane: str) -> LaneState:
    name = _validate_lane(lane)
    lane_state = manifest.lanes.get(name)
    if lane_state is None:
        raise ValueError(f"eval run {manifest.run_id} has no lane {name!r}")
    return lane_state


def _read_manifest(run_dir: Path) -> EvalRunManifest:
    path = run_manifest_path(run_dir)
    if not path.is_file():
        raise FileNotFoundError(f"eval run manifest missing: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"eval run manifest is unreadable: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"eval run manifest is not an object: {path}")
    manifest = EvalRunManifest.from_dict(raw)
    if manifest.schema_version != RUN_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported eval run manifest schema: {manifest.schema_version!r}"
        )
    return manifest


def _write_manifest(run_dir: Path, manifest: EvalRunManifest) -> None:
    atomic_write_text(
        run_manifest_path(run_dir),
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )


@contextmanager
def _run_lock(run_dir: Path) -> Iterator[None]:
    """Serialize manifest read-modify-write across driver threads/processes."""
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    with (Path(run_dir) / f"{MANIFEST_FILENAME}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


__all__ = [
    "DEFAULT_ACTOR",
    "DEFAULT_LANES",
    "DEFAULT_PROMPT_VERSION",
    "EVAL_RUN_EVENT_TYPES",
    "EvalRunManifest",
    "LANE_CHANNEL",
    "LANE_COMPLETED_EVENT",
    "LANE_ERROR_TIMEOUT",
    "LANE_KANBAN",
    "LANE_STATUS_FAILED",
    "LANE_STATUS_PENDING",
    "LANE_STATUS_PRD_FROZEN",
    "LANE_STATUS_RUNNING",
    "LANE_STATUSES",
    "LANE_TERMINAL",
    "LANE_TERMINAL_STATUSES",
    "LaneState",
    "PROMPT_PACKET_SCHEMA_VERSION",
    "RUN_COMPLETED_EVENT",
    "RUN_MANIFEST_SCHEMA_VERSION",
    "RUN_STARTED_EVENT",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_RUNNING",
    "complete_lane",
    "complete_run",
    "eval_run_dir",
    "eval_run_id",
    "eval_runs_root",
    "freeze_prompt_packet",
    "lane_dir",
    "lane_eval_run_id",
    "lane_prd_path",
    "lane_state_dir",
    "list_eval_runs",
    "load_manifest",
    "mark_lane_running",
    "parse_eval_run_id",
    "prompt_digest",
    "prompt_packet_path",
    "run_group_dir",
    "run_manifest_path",
    "start_eval_run",
]
