"""Durable evaluation result, report, and sidecar artifacts.

This module owns filesystem paths and immutable/read-side bindings for the
evaluation runner. Provider orchestration stays in ``evaluation_runner``;
these helpers remain a small sibling so artifact identity and persistence have
one implementation without growing the orchestration module.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from zf.core.events import EventLog
from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.evaluation_packets import (
    EVALUATION_ARTIFACT_ROOT,
    evaluation_request_id,
    stable_digest,
)
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION
from zf.runtime.evaluation_schemas import SCHEMA_VERSIONS


_RESULT_REQUIRED_STATUSES = frozenset({
    "scored",
    "ambiguous",
    "evidence_missing",
    "abstain",
    "conflict",
    "invalid",
    "complete",
    "incomplete",
})


def judge_result_path(
    state_dir: Path,
    *,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
) -> Path:
    return (
        Path(state_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "judges"
        / _safe(workflow_run_id)
        / _safe(judge_type)
        / f"{_safe(request_id)}.json"
    )


def conclusion_result_path(
    state_dir: Path, workflow_run_id: str, request_id: str,
) -> Path:
    return (
        Path(state_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "conclusions"
        / _safe(workflow_run_id)
        / f"{_safe(request_id)}.json"
    )


def report_path(state_dir: Path, workflow_run_id: str) -> Path:
    return (
        Path(state_dir)
        / EVALUATION_ARTIFACT_ROOT
        / "reports"
        / _safe(workflow_run_id)
        / "report.md"
    )


def read_judge_result(
    state_dir: Path, *, workflow_run_id: str, judge_type: str, request_id: str,
    expected_trial_id: str = "", expected_source_snapshot_digest: str = "",
    expected_role_ref: str = "", expected_role_digest: str = "",
) -> dict[str, Any] | None:
    record = _read_json(judge_result_path(
        state_dir, workflow_run_id=workflow_run_id,
        judge_type=judge_type, request_id=request_id,
    ))
    return _validate_read_record(
        record, state_dir=Path(state_dir), workflow_run_id=workflow_run_id, judge_type=judge_type,
        request_id=request_id, expected_trial_id=expected_trial_id,
        expected_source_snapshot_digest=expected_source_snapshot_digest,
        expected_role_ref=expected_role_ref, expected_role_digest=expected_role_digest,
    )


def read_conclusion_result(
    state_dir: Path, workflow_run_id: str, request_id: str,
) -> dict[str, Any] | None:
    record = _read_json(conclusion_result_path(state_dir, workflow_run_id, request_id))
    return _validate_read_record(
        record, state_dir=Path(state_dir), workflow_run_id=workflow_run_id, judge_type="conclusion",
        request_id=request_id,
    )


def _recover_sidecar_attempt(
    record: dict[str, Any] | None, *, workflow_run_id: str, judge_type: str,
    request_id: str, trial_id: str, source_snapshot_digest: str,
) -> tuple[int, int] | None:
    """Read only the durable retry counters from a malformed sidecar."""
    if not isinstance(record, dict):
        return None
    if (
        str(record.get("request_id") or "") != request_id
        or str(record.get("workflow_run_id") or "") != workflow_run_id
        or str(record.get("judge_type") or "") != judge_type
        or str(record.get("trial_id") or "") != trial_id
        or str(record.get("source_snapshot_digest") or "") != source_snapshot_digest
    ):
        return None
    try:
        attempt = int(record.get("attempt") or 0)
        maximum = int(record.get("max_attempts") or 2)
    except (TypeError, ValueError):
        return None
    if attempt <= 0 or maximum <= 0 or attempt > maximum:
        return None
    return attempt, maximum


def _recover_attempt_archive(
    path: Path, *, workflow_run_id: str, judge_type: str,
    request_id: str, trial_id: str, source_snapshot_digest: str,
) -> dict[str, Any] | None:
    """Recover a consumed retry count when the latest sidecar was deleted."""

    candidates: list[tuple[int, dict[str, Any]]] = []
    for archive in (path.parent / "attempts").glob(f"{request_id}-attempt-*.json"):
        record = _read_json(archive)
        recovery = _recover_sidecar_attempt(
            record, workflow_run_id=workflow_run_id, judge_type=judge_type,
            request_id=request_id, trial_id=trial_id,
            source_snapshot_digest=source_snapshot_digest,
        )
        if recovery is not None:
            candidates.append((recovery[0], record))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _validate_read_record(
    record: dict[str, Any] | None, *, state_dir: Path | None = None,
    workflow_run_id: str, judge_type: str,
    request_id: str, expected_trial_id: str = "", expected_source_snapshot_digest: str = "",
    expected_role_ref: str = "", expected_role_digest: str = "",
) -> dict[str, Any] | None:
    """Reject stale, hand-edited, or cross-request sidecars before reuse."""
    if not isinstance(record, dict):
        return None
    if record.get("schema_version") != "benchmark.evaluation.record.v1":
        return None
    if str(record.get("judge_type") or "") != judge_type:
        return None
    if str(record.get("request_id") or "") != request_id:
        return None
    if str(record.get("workflow_run_id") or "") != workflow_run_id:
        return None
    trial_id = str(record.get("trial_id") or "")
    source_digest = str(record.get("source_snapshot_digest") or "")
    if not trial_id or not source_digest:
        return None
    if expected_trial_id and trial_id != expected_trial_id:
        return None
    if expected_source_snapshot_digest and source_digest != expected_source_snapshot_digest:
        return None
    prompt_version = str(record.get("prompt_version") or "")
    if prompt_version != EVALUATION_PROMPT_VERSION:
        return None
    try:
        expected_request = evaluation_request_id(
            workflow_run_id=workflow_run_id, trial_id=trial_id,
            judge_type=judge_type, source_snapshot_digest=source_digest,
            prompt_version=prompt_version,
        )
    except Exception:
        return None
    if expected_request != request_id:
        return None
    if expected_role_ref or expected_role_digest:
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        metadata = result.get("role_metadata") if isinstance(result.get("role_metadata"), dict) else {}
        role_ref = str(record.get("role_ref") or result.get("role_ref") or metadata.get("role_ref") or "")
        role_digest = str(record.get("role_digest") or result.get("role_digest") or metadata.get("role_digest") or "")
        if expected_role_ref and role_ref != expected_role_ref:
            return None
        if expected_role_digest and role_digest != expected_role_digest:
            return None
    status = str(record.get("status") or "")
    if not status or status == "retryable_failure":
        return None
    result = record.get("result")
    if status in _RESULT_REQUIRED_STATUSES and not isinstance(result, dict):
        return None
    if status in _RESULT_REQUIRED_STATUSES and not result:
        return None
    if result not in (None, {}) and not isinstance(result, dict):
        return None
    if isinstance(result, dict) and result:
        expected_schema = SCHEMA_VERSIONS.get(judge_type)
        if result.get("schema_version") != expected_schema:
            return None
        declared_type = str(result.get("judge_type") or "")
        if declared_type and declared_type != judge_type:
            return None
        result_status = str(result.get("status") or "")
        if result_status and result_status != status:
            return None
    if state_dir is not None:
        events_path = Path(state_dir) / "events.jsonl"
        if events_path.exists():
            try:
                artifact = (
                    judge_result_path(state_dir, workflow_run_id=workflow_run_id,
                                      judge_type=judge_type, request_id=request_id)
                    if judge_type != "conclusion" else
                    conclusion_result_path(state_dir, workflow_run_id, request_id)
                )
                artifact_ref = str(artifact.relative_to(Path(state_dir)))
                matching = [
                    event for event in EventLog(events_path).read_all()
                    if isinstance(getattr(event, "payload", None), dict)
                    and event.payload.get("artifact_ref") == artifact_ref
                    and event.payload.get("request_id") == request_id
                ]
                digest_match = any(
                    event.payload.get("artifact_digest") == stable_digest(record)
                    for event in matching
                )
                # A terminal sidecar is reusable only when the append-only
                # ledger binds the exact body.  A hand-created JSON file must
                # not suppress a real evaluation merely because its fields
                # look complete.  Non-terminal transport records may exist
                # briefly before their progress event and are recovered from
                # the raw sidecar by ``run_judge``.
                if status in _RESULT_REQUIRED_STATUSES and not digest_match:
                    return None
            except (OSError, ValueError, TypeError):
                return None
    return record


def write_judge_result(
    state_dir: Path,
    *,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
    record: dict[str, Any],
) -> Path:
    path = judge_result_path(
        state_dir, workflow_run_id=workflow_run_id,
        judge_type=judge_type, request_id=request_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _canonical(record))
    return path


def write_conclusion_result(
    state_dir: Path, workflow_run_id: str, request_id: str, record: dict[str, Any],
) -> Path:
    path = conclusion_result_path(state_dir, workflow_run_id, request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _canonical(record))
    return path


def versioned_report_path(
    state_dir: Path, workflow_run_id: str, prompt_version: str, *,
    trial_id: str = "", source_snapshot_digest: str = "",
) -> Path:
    suffix = f"-{_safe(trial_id)}-{_safe(source_snapshot_digest)}" if trial_id else ""
    return report_path(state_dir, workflow_run_id).with_name(f"report-{_safe(prompt_version)}{suffix}.md")


def _archive_report(path: Path, body: str) -> Path:
    # Same identity can acquire a new conclusion after bounded recovery.
    # Preserve both bodies, including the first archived report.
    if path.exists() and path.read_text(encoding="utf-8") != body:
        path = path.with_name(f"{path.stem}-{stable_digest(body)[:16]}.md")
    if not path.exists():
        atomic_write_text(path, body)
    return path


def _archive_report_bytes(path: Path, body: bytes) -> Path:
    """Preserve an unreadable report before replacing its latest projection."""

    if path.exists() and path.read_bytes() != body:
        path = path.with_name(
            f"{path.stem}-{hashlib.sha256(body).hexdigest()[:16]}{path.suffix}"
        )
    if not path.exists():
        # Latin-1 maps every byte one-to-one, letting the existing atomic
        # text writer retain a corrupt UTF-8 report without lossy decoding.
        atomic_write_text(path, body.decode("latin-1"), encoding="latin-1")
    return path


def write_report(
    state_dir: Path, workflow_run_id: str, body: str, *,
    prompt_version: str | None = None, trial_id: str = "",
    source_snapshot_digest: str = "", final: bool = True,
) -> Path:
    """Write immutable archives and refresh the latest projection."""
    from zf.core.state.locks import locked_path

    path = report_path(state_dir, workflow_run_id)
    with locked_path(path):
        if path.exists():
            previous_bytes = path.read_bytes()
            try:
                previous = previous_bytes.decode("utf-8")
            except UnicodeDecodeError:
                _archive_report_bytes(
                    path.with_name(
                        "report-corrupt-"
                        f"{hashlib.sha256(previous_bytes).hexdigest()[:16]}.bin"
                    ),
                    previous_bytes,
                )
            else:
                if previous != body:
                    _archive_report(
                        path.with_name(f"report-snapshot-{stable_digest(previous)[:16]}.md"),
                        previous,
                    )
        if prompt_version and final:
            _archive_report(versioned_report_path(
                state_dir, workflow_run_id, prompt_version,
                trial_id=trial_id, source_snapshot_digest=source_snapshot_digest,
            ), body)
        atomic_write_text(path, body)
    return path


def _write_turn_sidecars(
    state_dir: Path,
    *,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
    attempts_log: list[tuple[int, str, str, str]],
) -> None:
    """Persist the exact prompt seen and raw replies returned per attempt.

    These are the human-readable audit trail: the record JSON only carries a
    prompt_digest and a normalized result, so without these files one cannot
    re-read what the judge was actually shown (rendered packet included).
    Each entry is (attempt, system_prompt, user_prompt, raw_reply).
    """

    if not attempts_log:
        return
    if judge_type == "conclusion":
        directory = conclusion_result_path(
            state_dir, workflow_run_id, request_id,
        ).parent
    else:
        directory = judge_result_path(
            state_dir, workflow_run_id=workflow_run_id,
            judge_type=judge_type, request_id=request_id,
        ).parent
    directory.mkdir(parents=True, exist_ok=True)
    _attempt, system_prompt, user_prompt, _reply = attempts_log[-1]
    header = (
        f"# judge turn prompt — {judge_type} {request_id}\n\n"
        f"attempts: {len(attempts_log)} (below is the final attempt)\n\n"
        "## system prompt\n\n"
        f"{system_prompt}\n\n"
        "## user prompt (rendered packet)\n\n"
        "---\n\n"
    )
    atomic_write_text(
        directory / f"{request_id}.prompt.md", header + user_prompt + "\n",
    )
    replies = [
        f"## attempt {attempt}\n\n{reply.strip() or '(empty reply)'}\n"
        for attempt, _system, _prompt, reply in attempts_log
        if reply.strip()
    ]
    if replies:
        atomic_write_text(
            directory / f"{request_id}.raw.md",
            f"# judge raw replies — {judge_type} {request_id}\n\n"
            + "\n".join(replies),
        )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"


def _rel(path: Path, state_dir: Path) -> str:
    try:
        return str(Path(path).relative_to(Path(state_dir)))
    except ValueError:
        return str(path)


def _safe(value: str) -> str:
    # Keep report/judge/conclusion paths injective for evaluation identities.
    # evaluation_run_id percent-encodes unsafe bytes; collapsing percent back
    # to hyphen here would recreate the a/b versus a-b collision.
    from zf.runtime.evaluation_packets import _safe_segment

    return _safe_segment(value)


__all__ = [
    "conclusion_result_path",
    "judge_result_path",
    "read_conclusion_result",
    "read_judge_result",
    "report_path",
    "versioned_report_path",
    "write_conclusion_result",
    "write_judge_result",
    "write_report",
]
