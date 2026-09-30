"""Read-only Channel evaluation progress, folded from the event ledger."""
from __future__ import annotations

import re
from typing import Any

from zf.runtime.evaluation_packets import evaluation_request_id
from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION

EVALUATION_CONTRACT = "channel-prd-eval.v1"

EVALUATION_EVENTS = {
    "benchmark.evaluation.progress",
    "benchmark.evaluation.judge.completed",
    "benchmark.evaluation.conclusion.completed",
}
STAGES = ("task_type", "contribution", "discussion", "prd_fidelity", "conclusion")
STAGE_PROFILES = {
    "contribution": "contribution",
    "discussion": "discussion",
    "prd_fidelity": "prd_fidelity",
}

_IDENTITY_FIELDS = (
    "evaluation_contract",
    "prompt_version",
    "freeze_event_id",
    "trial_id",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[1-9][0-9]*$")


def _valid_digest(value: object) -> bool:
    """Accept only the unprefixed SHA-256 form used by evaluation packets."""

    return bool(_SHA256_RE.fullmatch(str(value or "").strip().lower()))


def _strict_revision(value: object) -> int | None:
    """Accept only a positive integer or its canonical decimal string form."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and _REVISION_RE.fullmatch(value):
        return int(value)
    return None


def _legacy_event_payload(channel: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Recover omitted v7 fields only from a bound queue/request identity."""

    if "evaluation_contract" in payload:
        return payload
    run_id = str(payload.get("workflow_run_id") or "")
    stage = str(payload.get("judge_type") or "")
    trial_id = str(payload.get("trial_id") or "")
    job_id = str(payload.get("job_id") or "")
    request_id = str(payload.get("request_id") or "")
    source_digest = str(payload.get("source_snapshot_digest") or "")
    # Old scheduler rows already carry the freeze and source. Their hashed
    # job id proves the prompt version; never infer it from a run name alone.
    if (payload.get("freeze_event_id") and trial_id and _valid_digest(source_digest)
            and job_id == evaluation_request_id(
                workflow_run_id=run_id, trial_id=trial_id, judge_type=stage,
                source_snapshot_digest=source_digest,
            )):
        return {
            "evaluation_contract": EVALUATION_CONTRACT,
            "prompt_version": EVALUATION_PROMPT_VERSION,
            **payload,
        }
    candidates = [
        job for job in (channel.get("_evaluation_jobs") or {}).values()
        if job["workflow_run_id"] == run_id and job["judge_type"] == stage
        and trial_id and job.get("trial_id") == trial_id
        and (
            (job_id or request_id) in (job["job_id"], job.get("request_id"))
            or (stage == "conclusion" and not job_id and request_id)
        )
    ]
    if len(candidates) != 1:
        return payload
    job = candidates[0]
    # Conclusion requests hash the Judge results, so their request id differs
    # from the queue id. Only a unique active row can establish that binding.
    if ((job_id or request_id) not in (job["job_id"], job.get("request_id"))
            and job.get("status") not in {"running", "retryable_failure"}):
        return payload
    return {
        **{field: job[field] for field in (*_IDENTITY_FIELDS, "source_snapshot_digest")},
        **payload,
    }


def _event_identity(channel: dict[str, Any], payload: dict[str, Any]) -> dict[str, str] | None:
    """Return the canonical evaluation identity or reject the event.

    Evaluation events must carry the contract and immutable freeze lineage,
    either explicitly or recovered from a matching legacy queue record.
    A stage/run name alone is insufficient for retired evaluator events.
    """

    if str(payload.get("evaluation_contract") or "") != EVALUATION_CONTRACT:
        return None
    identity = {
        field: str(payload.get(field) or "").strip()
        for field in _IDENTITY_FIELDS
    }
    source_digest = str(payload.get("source_snapshot_digest") or "").strip().lower()
    # A non-empty prompt version is not enough: accepting a future/retired
    # prompt would make the read projection compare scores produced under
    # different rubrics.  The runner and scheduler bind this exact value.
    if identity["prompt_version"] != EVALUATION_PROMPT_VERSION or not identity["freeze_event_id"]:
        return None
    if not identity["trial_id"] or not _valid_digest(source_digest):
        return None
    freezes = channel.get("_evaluation_freeze_identities") or {}
    if identity["freeze_event_id"] not in freezes:
        return None
    freeze = freezes[identity["freeze_event_id"]]
    event_thread = str(payload.get("thread_id") or "").strip()
    if event_thread and event_thread != str(freeze.get("thread_id") or ""):
        return None
    revision_raw = payload.get("prd_revision", payload.get("revision"))
    if revision_raw is not None:
        revision = _strict_revision(revision_raw)
        if revision is None or revision != int(freeze.get("prd_revision") or 0):
            return None
    # ``workflow_run_id`` is bound by the first accepted event for this freeze
    # rather than reconstructed from channel/revision.  This keeps the
    # projection compatible with future occurrence-specific run ids while
    # still rejecting a late event from a different run.
    event_run = str(payload.get("workflow_run_id") or "").strip()
    bound_run = str(freeze.get("workflow_run_id") or "").strip()
    if not event_run:
        return None
    if bound_run and event_run != bound_run:
        return None
    if not bound_run:
        freeze["workflow_run_id"] = event_run
    identity["workflow_run_id"] = event_run
    identity["source_snapshot_digest"] = source_digest
    return identity


def fold_evaluation(channel: dict[str, Any], payload: dict[str, Any], *,
                    event_type: str, ts: str, seq: int = 0) -> None:
    run_id, stage = str(payload.get("workflow_run_id") or ""), str(payload.get("judge_type") or "")
    if not run_id or stage not in STAGES:
        return
    payload = _legacy_event_payload(channel, payload)
    identity = _event_identity(channel, payload)
    if identity is None:
        return
    jobs = channel.setdefault("_evaluation_jobs", {})
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        request_id = str(payload.get("request_id") or "")
        # Quality stage request ids are also the queue job ids.  Conclusion
        # request ids include the completed Judge bodies, so its completion
        # event may arrive with a different id while the queue row is active.
        job_id = request_id if request_id in jobs else next(
            (key for key, job in jobs.items()
             if request_id and job.get("request_id") == request_id),
            "",
        )
        if not job_id:
            candidates = [
                (key, job) for key, job in jobs.items()
                if job["workflow_run_id"] == run_id
                and job["judge_type"] == stage
                and job.get("freeze_event_id") == identity["freeze_event_id"]
                and job.get("trial_id") == identity["trial_id"]
            ]
            if len(candidates) == 1:
                # A conclusion result has no queue id.  It is attributable
                # only while this immutable run/trial has one queue row.
                job_id = candidates[0][0]
            elif stage == "conclusion" and candidates:
                # An unbound request cannot be attributed across same-trial
                # queues, even if only one is running: it may be a delayed
                # result from an older queue.
                return
        if not job_id:
            job_id = request_id
    if not job_id:
        return
    if job_id not in jobs:
        jobs[job_id] = {
            "job_id": job_id,
            "workflow_run_id": run_id,
            "judge_type": stage,
            **identity,
            "_queue_seq": seq,
            "_event_seq": seq,
        }
    else:
        job = jobs[job_id]
        # A request id is immutable.  A late or forged event with a different
        # freeze/prompt/trial identity must not mutate its projection row.
        if any(str(job.get(field) or "") != value
               for field, value in identity.items()
               if field != "source_snapshot_digest"):
            return
        if str(job.get("source_snapshot_digest") or "") != identity["source_snapshot_digest"]:
            # Conclusion source digests include the upstream Judge records and
            # legitimately differ from the queue row's frozen-input digest.
            if stage != "conclusion":
                return
            job["result_source_snapshot_digest"] = identity["source_snapshot_digest"]
        job["_event_seq"] = max(int(job.get("_event_seq") or 0), int(seq or 0))
    job = jobs[job_id]
    job["evaluator_profile"] = STAGE_PROFILES.get(stage, stage)
    for key in ("status", "attempt", "max_attempts", "slot_id", "phase", "error",
                "last_activity_at", "tool_calls", "transcript_bytes", "request_id",
                "task_type", "role_ref", "role_digest", "evaluator_profile",
                "source_snapshot_digest", "prompt_version", "freeze_event_id", "trial_id"):
        if key in payload:
            # The conclusion queue is bound to the PRD-fidelity digest.
            # Its result request is instead bound to the digest of four Judge
            # records; retain that only as audit metadata.
            if (key == "source_snapshot_digest" and stage == "conclusion"
                    and str(job.get("source_snapshot_digest") or "")
                    != str(payload.get(key) or "")):
                job["result_source_snapshot_digest"] = str(payload.get(key) or "")
                continue
            job[key] = payload[key]
    if event_type.endswith(".completed"):
        verdict = str(payload.get("status") or "incomplete")
        job["verdict"] = verdict
        job["status"] = verdict
        job["terminal"] = True
    elif job.get("terminal"):
        # Scheduler's final progress transition is emitted after the runner's
        # completed event. Preserve the semantic verdict in the projection.
        job["queue_status"] = payload.get("status")
        job["status"] = job.get("verdict") or job.get("status")
    job["updated_at"] = ts

    latest_rank = int(channel.get("_evaluation_latest_rank") or -1)
    rank = int((channel.get("_evaluation_freeze_identities") or {})
               .get(identity["freeze_event_id"], {}).get("seq") or 0)
    # A freeze occurrence establishes one immutable run.  Do not let an old
    # event with the same/lower rank (or a different run at the same rank)
    # roll the public projection back to stale conclusions.
    if rank > latest_rank or (
        rank == latest_rank and channel.get("_evaluation_latest_run") in (None, "")
    ):
        channel["_evaluation_latest_run"] = run_id
        channel["_evaluation_latest_rank"] = rank
        channel["_evaluation_latest_freeze_event_id"] = identity["freeze_event_id"]


def public_evaluation(channel: dict[str, Any]) -> dict[str, Any]:
    run_id = channel.get("_evaluation_latest_run")
    freeze_event_id = str(channel.get("_evaluation_latest_freeze_event_id") or "")
    jobs = [
        job for job in (channel.get("_evaluation_jobs") or {}).values()
        if (job["workflow_run_id"] == run_id
            and (not freeze_event_id or job.get("freeze_event_id") == freeze_event_id))
    ]
    fidelity_jobs = [job for job in jobs if job.get("judge_type") == "prd_fidelity"]
    current_fidelity = max(
        fidelity_jobs,
        key=lambda job: (
            int(job.get("_queue_seq") or job.get("_event_seq") or 0),
            int(job.get("_event_seq") or 0),
        ),
        default=None,
    )
    current_fidelity_digest = str(
        (current_fidelity or {}).get("source_snapshot_digest") or ""
    )
    stages: dict[str, dict[str, Any]] = {}
    for job in jobs:
        stage = str(job.get("judge_type") or "")
        # A workflow bind requeues only PRD fidelity and its conclusion.  The
        # queue's frozen-input digest is the local generation boundary; do
        # not combine a newly queued fidelity review with an old conclusion.
        if stage == "prd_fidelity" and job is not current_fidelity:
            continue
        if (stage == "conclusion" and current_fidelity_digest
                and str(job.get("source_snapshot_digest") or "") != current_fidelity_digest):
            continue
        prior = stages.get(stage)
        if prior is None or int(job.get("_event_seq") or 0) >= int(prior.get("_event_seq") or 0):
            stages[stage] = dict(job)
    if not stages:
        return {}
    result = {"workflow_run_id": run_id, "stages": []}
    for key in STAGES:
        if key not in stages:
            continue
        row = dict(stages[key])
        row.pop("_event_seq", None)
        row.pop("_queue_seq", None)
        row.pop("result_source_snapshot_digest", None)
        result["stages"].append(row)
    classification = stages.get("task_type") or {}
    if classification.get("task_type"):
        result["task_type"] = classification["task_type"]
    return result
