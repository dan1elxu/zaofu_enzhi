"""Read-only Channel evaluation progress, folded from the event ledger."""
from __future__ import annotations

from typing import Any

EVALUATION_EVENTS = {
    "benchmark.evaluation.progress",
    "benchmark.evaluation.judge.completed",
    "benchmark.evaluation.conclusion.completed",
}
STAGES = ("milestone", "communication", "prd_result", "conclusion")


def fold_evaluation(channel: dict[str, Any], payload: dict[str, Any], *,
                    event_type: str, ts: str) -> None:
    run_id, stage = str(payload.get("workflow_run_id") or ""), str(payload.get("judge_type") or "")
    if not run_id or stage not in STAGES:
        return
    jobs = channel.setdefault("_evaluation_jobs", {})
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        request_id = str(payload.get("request_id") or "")
        # One active stage per channel. Conclusion request identity includes
        # completed judge bodies, while its queue identity binds frozen input.
        job_id = next((key for key, job in jobs.items()
                       if request_id and job.get("request_id") == request_id), "") or next((key for key, job in jobs.items()
                       if job["workflow_run_id"] == run_id and job["judge_type"] == stage
                       and job.get("status") in {"running", "retryable_failure"}),
                      request_id)
    if not job_id:
        return
    if job_id not in jobs:
        jobs[job_id] = {"job_id": job_id, "workflow_run_id": run_id, "judge_type": stage}
        channel["_evaluation_latest_run"] = run_id
    job = jobs[job_id]
    for key in ("status", "attempt", "max_attempts", "slot_id", "phase", "error",
                "last_activity_at", "tool_calls", "transcript_bytes", "request_id"):
        if key in payload:
            job[key] = payload[key]
    if event_type.endswith(".completed"):
        job["verdict"] = payload.get("status")
        job["status"] = "inconclusive" if job["verdict"] == "inconclusive" else "completed"
    job["updated_at"] = ts


def public_evaluation(channel: dict[str, Any]) -> dict[str, Any]:
    run_id = channel.get("_evaluation_latest_run")
    stages = {}
    for job in (channel.get("_evaluation_jobs") or {}).values():
        if job["workflow_run_id"] == run_id:
            stages[job["judge_type"]] = dict(job)
    if not stages:
        return {}
    return {"workflow_run_id": run_id, "stages": [stages[key] for key in STAGES if key in stages]}
