"""Full author Markdown reaches the canonical PRD and same-version export."""
import hashlib
import json

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime.channel_projection import project_channel
from zf.runtime.channel_reply_contract import emit_structured_reply_events
from zf.runtime.channel_prd_document import render_prd_document, validate_prd_document


def document():
    return {
        "summary": "Short overview, intentionally different from the full document.",
        "prd_markdown": "# Inventory PRD\n\n## Atomic reservation\n" + "Detailed failure behavior. " * 1000 + "END-OF-FULL-PRD\n",
        "requirement_coverage": [{"id": "REQ-1", "source_refs": ["source:PRD#reservation"],
                                  "section": "Atomic reservation", "acceptance_ids": ["AC-1"], "status": "covered"}],
        "acceptance_criteria": [{"id": "AC-1", "criterion": "Concurrent reservations cannot oversell."}],
        "decision_log": [{"decision": "Atomic reservation", "source_refs": ["source:PRD#reservation"], "rationale": "Source requirement"}],
        "assumptions": [{"id": "A-01", "statement": "Reservations expire after 15 minutes.", "risk": "Time source drift", "source_refs": ["source:PRD#expiry"]}],
        "risks": [{"id": "R-01", "risk": "Concurrent expiration", "mitigation": "Serialize expiration with reservation", "source_refs": ["source:PRD#reservation"]}],
        "dissent": [{"id": "D-01", "statement": "Expiry duration remains disputed.", "custom_evidence": {"minutes": [10, 15]}}],
        "open_questions": [],
    }


@pytest.mark.parametrize("missing", [False, True])
def test_full_document_reaches_canonical_artifact_or_requires_repair(tmp_path, missing):
    state = tmp_path / "state"
    writer = EventWriter(EventLog(state / "events.jsonl"))
    writer.emit("channel.created", actor="test", payload={"channel_id": "ch-document"})
    body = document()
    if missing:
        del body["prd_markdown"]
    emit_structured_reply_events(
        state_dir=state, writer=writer, channel=project_channel(state, "ch-document"),
        request={"thread_id": "main", "target_member_id": "synthesizer"},
        message={"refs": {"synthesis_request_id": "synth-1", "prd_document_contract": "full.v1"}},
        reply=json.dumps({"channel_synthesis": body}), reply_event_id="reply-1", actor="test", source="test",
    )
    events = writer.event_log.read_all()
    syntheses = [e.payload for e in events if e.type == "channel.synthesis.proposed"]
    if missing:
        assert not syntheses
        assert any(e.type == "channel.synthesis.repair.requested" and "prd_markdown" in e.payload.get("contract_error", "") for e in events)
        return
    assert len(syntheses) == 1
    synthesis = syntheses[0]
    canonical = (state / synthesis["artifact_ref"]).read_bytes()
    assert hashlib.sha256(canonical).hexdigest() == synthesis["artifact_digest"]
    prd = json.loads(canonical)["body"]
    markdown = (state / synthesis["spec_path"]).read_text()
    assert markdown == prd["markdown"]
    assert markdown.startswith(body["prd_markdown"].rstrip())
    assert "END-OF-FULL-PRD" in markdown
    assert "REQ-1" in markdown and "AC-1" in markdown
    assert "Reservations expire after 15 minutes." in markdown
    assert "Serialize expiration with reservation" in markdown
    assert "Expiry duration remains disputed." in markdown
    assert "source:PRD#expiry" in markdown and "custom_evidence" in markdown
    assert prd["synthesis"]["assumptions"] == body["assumptions"]
    assert prd["summary"] == body["summary"]
    assert hashlib.sha256(markdown.encode()).hexdigest() == synthesis["spec_digest"]
    consensus = next(e for e in events if e.type == "channel.consensus.proposed")
    assert consensus.payload["artifact_digest"] == synthesis["artifact_digest"]
    assert consensus.payload["prd_revision"] == synthesis["prd_revision"]


@pytest.mark.parametrize("fault", ["unknown_acceptance", "no_source", "empty_body", "duplicate_id", "unexplained_gap"])
def test_full_document_reference_contract_rejects_incomplete_mapping(fault):
    body = document()
    row = body["requirement_coverage"][0]
    if fault == "unknown_acceptance": row["acceptance_ids"] = ["invented"]
    if fault == "no_source": row["source_refs"] = []
    if fault == "empty_body": body["prd_markdown"] = " "
    if fault == "duplicate_id": body["requirement_coverage"].append(dict(row))
    if fault == "unexplained_gap": row["status"] = "gap"
    assert validate_prd_document(body)


def test_export_catalog_preserves_commands_unknown_fields_and_author_body():
    body = document()
    body["acceptance_criteria"][0]["risk_note"] = "Keep this uncommon acceptance evidence."
    body["verification_commands"] = [{
        "id": "VC-1", "command": "python -m pytest tests/test_inventory.py -q",
        "acceptance_ids": ["AC-1"], "owner": "verify", "tier": "runtime",
        "deterministic": True, "reusable": False, "timeout_seconds": 60,
        "producer_paths": ["evidence/inventory.json"], "custom_proof": "exit + ledger",
    }]
    original = json.loads(json.dumps(body))
    markdown = render_prd_document(body, channel_id="ch", thread_id="main", source_refs=["source:PRD"])
    assert markdown.startswith(body["prd_markdown"].rstrip())
    assert markdown.count("Concurrent reservations cannot oversell.") == 1
    assert markdown.count("python -m pytest tests/test_inventory.py -q") == 1
    assert "Keep this uncommon acceptance evidence." in markdown
    for field in body["verification_commands"][0]:
        assert field in markdown
    assert "exit + ledger" in markdown and "evidence/inventory.json" in markdown
    assert "REQ-1" in markdown and "source:PRD#reservation" in markdown
    assert "Atomic reservation" in markdown and "Source requirement" in markdown
    assert body == original
    assert "```json" not in markdown
    for field in ("assumptions", "risks", "dissent"):
        for key, value in body[field][0].items():
            assert key in markdown
            if isinstance(value, str):
                assert value in markdown


def test_export_catalog_keeps_multiline_and_pipe_values_in_one_table_row():
    body = document()
    body["verification_commands"] = [{"id": "VC-1", "command": "printf '<ok>' | cat\ntrue"}]
    markdown = render_prd_document(body, channel_id="ch", thread_id="main", source_refs=[])
    line = next(line for line in markdown.splitlines() if "printf" in line)
    assert "&lt;ok&gt;" in line and "\\| cat<br>true" in line
    assert "channel_prd_scope=approved_task" in markdown


def test_export_omits_empty_review_catalogs():
    body = document()
    for field in ("assumptions", "risks", "dissent"):
        body[field] = []
    markdown = render_prd_document(body, channel_id="ch", thread_id="main", source_refs=[])
    assert "## 假设、风险与分歧" not in markdown
