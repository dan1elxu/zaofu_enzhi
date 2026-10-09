"""Regression checks for truthful Channel E2E receipts."""

import hashlib
import json

import pytest

from tests.e2e.channel.evidence import (
    EvidenceError, EvidenceEventLog, JsonlTail, cancellation_verdict, validate_matrix,
)
from zf.core.events import EventWriter, ZfEvent


def test_tail_buffers_partial_utf8_and_does_not_double_count(tmp_path):
    path = tmp_path / "events.jsonl"
    raw = json.dumps({"text": "真实"}, ensure_ascii=False).encode()
    path.write_bytes(raw[:12])
    tail = JsonlTail(path)
    assert tail.read() == []
    with path.open("ab") as handle:
        handle.write(raw[12:] + b"\n")
    assert tail.read() == [{"text": "真实"}]
    assert tail.read(final=True) == [{"text": "真实"}]


@pytest.mark.parametrize("raw", [b"{broken}\n", b"\n", b"[]\n"])
def test_complete_malformed_records_fail_instead_of_disappearing(tmp_path, raw):
    path = tmp_path / "events.jsonl"
    path.write_bytes(raw)
    tail = JsonlTail(path)
    with pytest.raises(EvidenceError, match="malformed ledger"):
        tail.read()
    with pytest.raises(EvidenceError, match="malformed ledger"):
        tail.read()


def test_quiescent_unfinished_record_is_not_a_pass(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"valid_json_but_not_committed":true}')
    tail = JsonlTail(path)
    assert tail.read() == []
    with pytest.raises(EvidenceError, match="unfinished ledger"):
        tail.read(final=True)


def test_drill_log_uses_event_writer_and_rejects_corruption(tmp_path):
    log = EvidenceEventLog(tmp_path / "events.jsonl")
    event = EventWriter(log).append(ZfEvent(type="simulation.done", actor="operator"))
    assert log.read_all(final=True)[0].id == event.id
    with log.path.open("ab") as handle:
        handle.write(b"{corrupted complete row}\n")
    with pytest.raises(EvidenceError, match="malformed ledger"):
        log.read_all()


def test_tool_started_during_stop_interval_keeps_cancellation_failed():
    result = cancellation_verdict(
        cancel_at="2026-10-08T05:00:00Z", stopped_at="2026-10-08T05:00:06Z",
        tool_start_times=["2026-10-08T05:00:03Z"], stop_budget_s=10,
    )
    assert result["status"] == "FAIL"
    assert len(result["post_cancel_tool_start_times"]) == 1


def test_late_stop_cannot_pass_even_without_new_tools():
    assert cancellation_verdict(
        cancel_at="2026-10-08T05:00:00Z", stopped_at="2026-10-08T05:00:11Z",
        tool_start_times=[], stop_budget_s=10,
    )["status"] == "FAIL"


def _row(tmp_path, *, status="PASS"):
    path = tmp_path / "receipt.json"
    path.write_text('{"ok":true}\n')
    return {"id": "native", "status": status, "reason": "actual outcome",
            "verification_level": "real-provider", "source_refs": ["source.py"],
            "evidence_refs": [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}


def test_valid_failure_receipt_is_valid_evidence_but_not_closure(tmp_path):
    result = validate_matrix(tmp_path, [_row(tmp_path, status="BLOCKED")], required_ids={"native"})
    assert result == {"evidence_valid": True, "closure_passed": False, "open_ids": ["native"]}


def test_missing_inventory_row_and_tampered_receipt_fail(tmp_path):
    row = _row(tmp_path)
    with pytest.raises(EvidenceError, match="inventory mismatch"):
        validate_matrix(tmp_path, [row], required_ids={"native", "callback"})
    (tmp_path / "receipt.json").write_text('{"ok":false}\n')
    with pytest.raises(EvidenceError, match="digest mismatch"):
        validate_matrix(tmp_path, [row], required_ids={"native"})
