import json

from zf.core.events import EventLog, EventWriter
from zf.runtime import evaluation_report, evaluation_runner
from zf.runtime.evaluation_config import observed_evaluator_config
from zf.runtime.evaluation_turns import JudgeTurnObservation
from tests.test_evaluation_fail_closed import _packet


def test_config_digest_is_stable_and_excludes_secrets(monkeypatch):
    monkeypatch.setenv("ZF_EVALUATION_JUDGE_TIMEOUT_S", "42")
    first = observed_evaluator_config(
        backend="codex", provider="tmux-codex", observed_model="gpt-test",
        observed_model_source="transcript.turn_context", prompt_version="v1",
        prompt_digest="p", system_prompt_digest="s", role_ref="roles/x",
        role_digest="r",
    )
    second = observed_evaluator_config(
        role_digest="r", role_ref="roles/x", system_prompt_digest="s",
        prompt_digest="p", prompt_version="v1", observed_model_source="transcript.turn_context",
        observed_model="gpt-test", provider="tmux-codex", backend="codex",
    )
    assert first == second
    assert first["timeouts"]["turn_seconds"] == 42
    assert first["model"] == "gpt-test"
    assert "api_key" not in str(first).lower()
    assert "secret" not in str(first).lower()


def test_unknown_model_is_explicit_and_sampling_is_not_invented():
    value = observed_evaluator_config(backend="injected", provider="injected")
    assert value["model"] == "unknown"
    assert value["model_source"] == "unknown"
    assert value["sampling"] == "unknown"
    assert value["retry_budget"]["transport_max_attempts"] == 2
    assert value["sample_count"] == 1
    assert value["token_budget"] is None and value["cost_budget"] is None


def test_runner_persists_bound_observation_metadata_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("ZF_EVALUATION_JUDGE_TIMEOUT_S", "42")
    packet = _packet("discussion")
    packet["allowed_evidence_refs"] = ["channel/message/x"]
    reply = {
        "schema_version": "discussion_result.v2", "judge_type": "discussion",
        "dimensions": [{"dimension_id": key, "score": 4, "confidence": 0.9,
                         "evidence_refs": ["channel/message/x"], "rationale": "evidence"}
                       for key in ("information_exchange", "clarity", "task_assistance", "efficiency", "necessity")],
        "discussion_meaningful": True, "discussion_progressed": True, "missing_evidence": [],
    }

    def backend(**kwargs):
        # This simulates a mutable process environment after the turn began.
        monkeypatch.setenv("ZF_EVALUATION_JUDGE_TIMEOUT_S", "999")
        return JudgeTurnObservation(status="completed", reply=json.dumps(reply), turn_id="turn-1",
                                    observed_model="observed-test-model",
                                    observed_model_source="transcript.turn_context")

    result = evaluation_runner.run_judge(
        state_dir=tmp_path, project_root=None, config=None,
        writer=EventWriter(EventLog(tmp_path / "events.jsonl")), workflow_run_id="run-1",
        trial_id="trial", judge_type="discussion", packet=packet, backend=backend,
    )
    config = result["evaluator_config"]
    assert result["model"] == "observed-test-model"
    assert result["attempt"] == 1
    assert config["timeouts"]["turn_seconds"] == 42
    assert config["sample_count"] == 1
    evaluation_report._refresh_report(
        tmp_path, workflow_run_id="run-1", trial_id="trial", channel_id="channel",
        freeze={}, judges={"discussion": result}, source_snapshot_digest=packet["source_snapshot_digest"],
        conclusion={"status": "inconclusive", "provider": "deterministic", "model": "unknown",
                    "model_source": "not_applicable", "config_digest": "deterministic-config",
                    "evaluator_config": {"provider": "deterministic"}, "result": {}},
    )
    audit = next((tmp_path / "artifacts/benchmark-evaluations/reports").rglob("evaluation.json"))
    metadata = json.loads(audit.read_text(encoding="utf-8"))
    assert metadata["stages"]["discussion"]["model"] == "observed-test-model"
    assert metadata["stages"]["conclusion"]["model_source"] == "not_applicable"
    report = (tmp_path / "artifacts/benchmark-evaluations/reports/run-1/report.md").read_text(encoding="utf-8")
    assert "observed-test-model" not in report
