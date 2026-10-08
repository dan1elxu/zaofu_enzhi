"""机械格式修复保留判断，不能放宽评分、证据或重试门禁。"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_observer as observer, evaluation_runner as runner
from zf.runtime.evaluation_output import normalize_provider_output
from zf.runtime.evaluation_packets import stable_digest
from zf.runtime.evaluation_scheduler import EvaluationScheduler
from zf.runtime.evaluation_schemas import normalize_judge_output
from zf.runtime.evaluation_turns import JudgeTurnObservation
from zf.web.channel_evaluation_route import build_channel_evaluation
from tests.evaluation_fixtures import _judge_reply, _packet, _seed_channel_state

REF = "artifact/prd/channel-frozen.json"


def _raw():
    raw = json.loads(_judge_reply("prd_fidelity"))
    raw["differences"] = [{
        "dimension_id": "original_to_channel_prd", "description": "后续事项：接入服务。",
        "evidence_refs": [REF],
    }]
    return raw


@pytest.mark.parametrize("bound", [False, True])
def test_known_shape_is_lossless_and_canonical_schema_stays_strict(bound):
    raw = _raw()
    if bound:
        raw["dimensions"].append({**raw["dimensions"][0], "dimension_id": "channel_to_workflow_prd"})
        raw["differences"][0]["dimension_id"] = "channel_to_workflow_prd"
    before = copy.deepcopy(raw)
    options = dict(judge_type="prd_fidelity", allowed_evidence_refs=[REF], workflow_bound=bound)
    assert normalize_judge_output(raw=raw, **options)[0] is None
    result, error, audit = normalize_provider_output(raw=raw, **options)
    assert result is not None and not error and result.status == "scored"
    difference = raw["differences"][0]
    assert result.differences == [
        f"{difference['description']}（维度：{difference['dimension_id']}；证据：{REF}）"
    ]
    expected = {**raw, "differences": result.differences}
    assert result == normalize_judge_output(raw=expected, **options)[0]
    assert raw == before
    assert audit["original_output_digest"] == stable_digest(raw)
    assert audit["canonical_output_digest"] == stable_digest(expected)


@pytest.mark.parametrize("change", [
    {"description": ""}, {"description": {}}, {"description": " "},
    {"evidence_refs": ["invented"]}, {"evidence_refs": []}, {"evidence_refs": [None]},
    {"dimension_id": "channel_to_workflow_prd"}, {"dimension_id": []},
    {"score": 4}, {"extra": "must not silently discard this finding"},
])
def test_unknown_shapes_and_unbound_evidence_are_never_repaired(change):
    raw = _raw()
    raw["differences"][0].update(change)
    result, error, audit = normalize_provider_output(
        judge_type="prd_fidelity", raw=raw, allowed_evidence_refs=[REF], workflow_bound=False,
    )
    assert result is None and error and audit == {}


@pytest.mark.parametrize("field,value", [
    ("score", 5), ("score", True), ("evidence_refs", ["invented"]),
    ("rationale", ""), ("confidence", float("inf")),
])
def test_format_repair_cannot_bypass_quality_gates(field, value):
    raw = _raw()
    raw["dimensions"][0][field] = value
    result, error, audit = normalize_provider_output(
        judge_type="prd_fidelity", raw=raw, allowed_evidence_refs=[REF],
    )
    assert result is None or result.status == "invalid"
    assert not audit


def test_real_entrypoint_repairs_once_and_preserves_audited_report(tmp_path):
    _seed_channel_state(tmp_path)
    calls = []
    raw_fidelity = _raw()

    def backend(**kwargs):
        stage = kwargs["judge_type"]
        calls.append(stage)
        rendered = Path(kwargs["packet_path"]).read_text()
        ref = re.search(r"channel/message/[A-Za-z0-9._:-]+", rendered).group(0)
        if stage == "prd_fidelity":
            assert "不要返回对象数组" in rendered
            return json.dumps(raw_fidelity, ensure_ascii=False), ""
        return _judge_reply(stage, ref), ""

    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        result = observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=backend)
        assert result["errors"] == []
        assert calls.count("prd_fidelity") == 1
        page = build_channel_evaluation(tmp_path, "ch-1")
        assert page["status"] == "complete" and page["report"]["available"]
        assert page["scores"]["prd_fidelity"]["dimensions"][0]["score"] == 3
        assert raw_fidelity["differences"][0]["description"] in page["report"]["markdown"]
        record_path = next((tmp_path / "artifacts/benchmark-evaluations/judges").glob("*/prd_fidelity/*.json"))
        record = json.loads(record_path.read_text())
        assert record["output_repair"]["schema_version"] == "channel-prd-evaluation.output-repair.v1"
        turn = json.loads(next((record_path.parent / "attempts").glob("*-schema-1.json")).read_text())
        assert json.loads(turn["reply"]) == raw_fidelity
        assert turn["output_repair"] == record["output_repair"]
        assert observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler,
            backend=lambda **_: pytest.fail("repaired verdict must remain idempotent"),
        )["judges_run"] == 0
    finally:
        scheduler.close()


def test_provider_overload_never_becomes_a_score_or_unbounded_retry(tmp_path):
    calls = []

    def overloaded(**kwargs):
        calls.append((kwargs["attempt"], kwargs["schema_attempt"]))
        return JudgeTurnObservation(
            status="provider_error", error="server_overloaded: Selected model is at capacity",
            reply=json.dumps(_raw()),
        )

    result = runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")), workflow_run_id="wf-x",
        trial_id="t-1", judge_type="prd_fidelity", packet=_packet("prd_fidelity"), backend=overloaded,
    )
    assert calls == [(1, 1), (2, 1)]
    assert result["status"] == "inconclusive" and result["result"] == {}
    assert "output_repair" not in result
