"""Strict, read-only evidence checks for isolated Channel E2E runs."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from zf.core.events.log import EventLog
from zf.core.events.model import ZfEvent
from zf.core.state.rotation import list_archives


class EvidenceError(ValueError):
    """Evidence is incomplete, corrupt, or contradicts a claimed result."""


class JsonlTail:
    """Buffer unfinished bytes; never skip a malformed complete record."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.pending = b""
        self.records: list[dict[str, Any]] = []
        self.inode: int | None = None
        self.lines = 0
        self.failure: EvidenceError | None = None

    def read(self, *, final: bool = False) -> list[dict[str, Any]]:
        if self.failure is not None:
            raise self.failure
        if not self.path.exists():
            if final or self.offset:
                raise EvidenceError(f"missing ledger: {self.path}")
            return []
        stat = self.path.stat()
        if self.inode is not None and stat.st_ino != self.inode:
            # EventLog rotation moves the old inode into its archive directory.
            self.offset, self.pending, self.records, self.lines = 0, b"", [], 0
        self.inode = stat.st_ino
        if stat.st_size < self.offset:
            self.failure = EvidenceError(f"truncated ledger: {self.path}")
            raise self.failure
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            raw = handle.read(stat.st_size - self.offset)
        self.offset += len(raw)
        complete = (self.pending + raw).split(b"\n")
        self.pending = complete.pop()
        for line in complete:
            self.lines += 1
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("record is not an object")
            except (ValueError, UnicodeError) as exc:
                self.failure = EvidenceError(f"malformed ledger: {self.path}:{self.lines}")
                raise self.failure from exc
            self.records.append(row)
        if final and self.pending:
            raise EvidenceError(f"unfinished ledger: {self.path}:{self.lines + 1}")
        return list(self.records)


class EvidenceEventLog(EventLog):
    """Use sanctioned writes and strict reads in an isolated test fixture.

    Production signing/envelope behavior stays owned by EventLog. This reader
    is deliberately limited to unsigned E2E fixtures and exported ledgers.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._evidence_tails: dict[Path, JsonlTail] = {}

    def read_all(self, *, final: bool = False) -> list[ZfEvent]:
        result = []
        paths = [*list_archives(self._archive_dir, suffix=".jsonl"), self.path]
        for path in paths:
            tail = self._evidence_tails.setdefault(path, JsonlTail(path))
            rows = tail.read(final=final if path == self.path else True)
            for row in rows:
                try:
                    result.append(ZfEvent.from_dict(row))
                except (TypeError, ValueError) as exc:
                    raise EvidenceError(f"invalid event: {path}") from exc
        return result


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise EvidenceError("evidence timestamps require a timezone")
    return result


def cancellation_verdict(
    *, cancel_at: str, stopped_at: str, tool_start_times: list[str], stop_budget_s: float,
) -> dict[str, Any]:
    """Judge tool starts from acceptance time, including the stop interval."""
    if not math.isfinite(stop_budget_s) or stop_budget_s <= 0:
        raise EvidenceError("stop budget must be finite and positive")
    cancelled = _time(cancel_at)
    elapsed = (_time(stopped_at) - cancelled).total_seconds()
    after = [stamp for stamp in tool_start_times if _time(stamp) >= cancelled]
    return {
        "status": "PASS" if 0 <= elapsed <= stop_budget_s and not after else "FAIL",
        "stop_budget_s": stop_budget_s,
        "stop_elapsed_s": elapsed,
        "post_cancel_tool_start_times": after,
    }


def validate_matrix(
    root: Path, rows: list[dict[str, Any]], *, required_ids: set[str],
) -> dict[str, Any]:
    """Validate all receipts, including failures; report closure separately."""
    ids = [row.get("id") for row in rows]
    if len(set(ids)) != len(ids) or set(ids) != required_ids:
        raise EvidenceError("matrix inventory mismatch or duplicate row")
    root = root.resolve()
    open_ids = []
    for row in rows:
        if row.get("status") not in {
            "PASS", "FAIL", "BLOCKED", "INDETERMINATE", "FAIL_TEST_INFRA", "N/A",
        }:
            raise EvidenceError(f"invalid status: {row['id']}")
        if not row.get("verification_level") or not row.get("source_refs"):
            raise EvidenceError(f"missing verification scope: {row['id']}")
        if row["status"] != "PASS" and not row.get("reason"):
            raise EvidenceError(f"missing reason: {row['id']}")
        refs = row.get("evidence_refs") or []
        if not refs:
            raise EvidenceError(f"missing evidence: {row['id']}")
        for ref in refs:
            path = (root / ref["path"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise EvidenceError(f"missing or escaped evidence: {row['id']}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
                raise EvidenceError(f"digest mismatch: {row['id']}")
        if row["status"] not in {"PASS", "N/A"}:
            open_ids.append(row["id"])
    return {"evidence_valid": True, "closure_passed": not open_ids, "open_ids": open_ids}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--require-closed", action="store_true")
    args = parser.parse_args()
    matrix = json.loads(args.matrix.read_text())
    result = validate_matrix(
        args.matrix.parent, matrix["rows"], required_ids=set(matrix["required_ids"]),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(args.require_closed and not result["closure_passed"])


if __name__ == "__main__":
    raise SystemExit(main())
