"""真实 sidecar 文件与哈希组成的最小 PRD 测试数据。"""
from pathlib import Path

from zf.runtime.channel_contract_artifacts import (
    persist_channel_conclusion, persist_channel_prd, persist_channel_prd_readiness,
)


def persist_test_prd(state_dir: Path, channel_id: str, *, revision: int = 1,
                     verdict: str = "ready") -> dict:
    common = dict(channel_id=channel_id, thread_id="main", revision=revision,
                  created_by="test", source_event_id="evt-test-prd")
    readiness = persist_channel_prd_readiness(
        state_dir, **common, body={"verdict": verdict, "implementation_start": False},
    )
    prd = persist_channel_prd(
        state_dir, **common, previous_ref="", previous_digest="",
        body={"summary": "Review the documented scope.", "markdown": "# PRD\nKeep README.md."},
        readiness_descriptor=readiness,
    )
    conclusion = persist_channel_conclusion(
        state_dir, **common, prd_descriptor=prd, readiness_descriptor=readiness,
        summary="Review the documented scope.", source_refs=["event:requirement"],
    )
    return {
        "artifact_ref": prd["ref"], "artifact_digest": prd["sha256"],
        "prd_ref": prd["ref"], "prd_digest": prd["sha256"], "prd_revision": revision,
        "readiness_ref": readiness["ref"], "readiness_digest": readiness["sha256"],
        "readiness_verdict": verdict, "implementation_start": False,
        "conclusion_ref": conclusion["ref"], "conclusion_digest": conclusion["sha256"],
    }
