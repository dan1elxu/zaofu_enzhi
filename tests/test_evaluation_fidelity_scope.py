"""PRD 内容对齐与后续实施事项经过实际评测入口独立保存。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from zf.runtime import evaluation_observer as observer
from zf.runtime.evaluation_roles import TASK_TYPES
from zf.runtime.evaluation_scheduler import EvaluationScheduler
from zf.web.channel_evaluation_route import build_channel_evaluation
from tests.evaluation_fixtures import _judge_reply, _seed_channel_state


@pytest.mark.parametrize("task_type", TASK_TYPES)
def test_fidelity_role_retries_contradiction_and_reports_followups(tmp_path, task_type):
    _seed_channel_state(tmp_path)
    followup = "后续事项：开发阶段接入真实身份及附件服务（artifact/prd/channel-frozen.json）。"
    attempts = []

    def backend(**kwargs):
        stage = kwargs["judge_type"]
        prompt = Path(kwargs["packet_path"]).read_text(encoding="utf-8")
        ref = re.search(r"channel/message/[A-Za-z0-9._:-]+", prompt).group(0)
        reply = json.loads(_judge_reply(stage, ref))
        if stage == "task_type":
            reply["task_type"] = task_type
        elif stage in {"contribution", "discussion", "prd_fidelity"}:
            details = json.loads(next(
                line.split(": ", 1)[1] for line in prompt.splitlines()
                if line.startswith("Task details and selected role metadata: ")
            ))
            metadata = details["evaluation_metadata"]
            reply.update(task_type=task_type, role_id=task_type,
                         role_version=metadata["role_version"])
            if stage == "prd_fidelity":
                # Verify these rules reach the provider's actual prompt file.
                assert metadata["role_version"] == f"evaluation-role-{task_type}.v3"
                assert "PRD 内容对齐边界" in metadata["role_body"]
                assert "missing_evidence" in metadata["role_body"]
                assert "### `discussion`" not in metadata["role_body"]
                attempts.append(kwargs["schema_attempt"])
                reply["dimensions"][0]["score"] = 4
                # Readiness remains a separate role decision, without deleting
                # a valid fidelity score or blocking the summary's execution.
                reply["prd_ready_for_next_stage"] = False
                if kwargs["schema_attempt"] == 1:
                    reply["missing_evidence"] = ["G-VERIFY-01：服务尚未接入"]
                else:
                    assert "evidence_missing dimension" in kwargs["prompt"]
                    reply["differences"] = [followup]
        return json.dumps(reply, ensure_ascii=False), ""

    scheduler = EvaluationScheduler(tmp_path, inline=True)
    try:
        result = observer.observe_once(state_dir=tmp_path, scheduler=scheduler, backend=backend)
        assert result["errors"] == []
        assert attempts == [1, 2]
        payload = build_channel_evaluation(tmp_path, "ch-1")
        assert payload["status"] == "complete"
        fidelity = payload["scores"]["prd_fidelity"]
        assert fidelity["status"] == "scored"
        assert fidelity["dimensions"][0]["score"] == 4
        assert fidelity["missing_evidence"] == []
        assert fidelity["differences"] == [followup]
        assert payload["conclusion"]["team_findings"]["prd_ready_for_next_stage"] is False
        assert followup in payload["report"]["markdown"]
        assert "G-VERIFY-01" not in payload["report"]["markdown"]

        # Rescans use the admitted result, without another scoring request.
        again = observer.observe_once(
            state_dir=tmp_path, scheduler=scheduler,
            backend=lambda **_: pytest.fail("completed freeze was rescored"),
        )
        assert again["queued"] == 0 and again["errors"] == []
    finally:
        scheduler.close()
