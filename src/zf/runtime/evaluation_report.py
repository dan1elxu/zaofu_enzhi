"""Compact report and audit projection for Channel PRD evaluation.

This module owns the human-facing ``report.md`` projection and verbose audit
sidecars.  Packet discovery, queueing, and provider execution remain in the
observer/runner modules so the report cannot change evaluation decisions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zf.core.state.atomic_io import atomic_write_text
from zf.runtime.evaluation_types import necessity_label
from zf.runtime.evaluation_runner import (
    conclusion_result_path,
    judge_result_path,
    report_path,
    write_report,
)

def render_report(
    *,
    workflow_run_id: str,
    trial_id: str,
    channel_id: str,
    freeze: dict[str, Any],
    judges: dict[str, dict[str, Any]],
    conclusion: dict[str, Any] | None = None,
    agent_profiles: list[dict[str, Any]] | None = None,
) -> str:
    """Fixed-order Markdown report per PRD §9 (short, links to raw JSON)."""

    discussion = (judges.get("discussion") or {}).get("result") or {}
    prd_fidelity = (judges.get("prd_fidelity") or {}).get("result") or {}
    contribution = (judges.get("contribution") or {}).get("result") or {}
    classification = (judges.get("task_type") or {}).get("result") or {}
    task_type = str(classification.get("task_type") or "")
    task_confidence = classification.get("confidence")
    # Role-driven contribution is reported per Agent; averaging would hide
    # necessity and cross-agent differences.
    agent_names = {
        str(item.get("agent_id") or ""): str(item.get("agent_name") or item.get("agent_id") or "")
        for item in (agent_profiles or []) if isinstance(item, dict)
    }
    contribution_summary = (
        "见逐 Agent 分析（不汇总贡献均值）"
        if contribution.get("agent_findings")
        else "incomplete"
    )

    lines = [
        f"# PRD Evaluation Report — {workflow_run_id}",
        "",
        f"- run: `{workflow_run_id}`  trial: `{trial_id}`",
        f"- channel: `{channel_id}`  prd: `{freeze.get('prd_ref') or ''}`",
        f"- task_type: `{task_type or 'unknown'}`"
        + (f" (confidence {task_confidence})" if task_confidence is not None else ""),
        f"- status: {(_judge_line_status(judges, conclusion))}",
        "",
        "## 1. Run / Task / Channel / PRD",
        "",
        f"- task_id: `{freeze.get('task_id') or ''}`",
        f"- prd_revision: {freeze.get('prd_revision') or 0}",
        f"- freeze_event: `{freeze.get('event_type') or 'channel.prd.frozen'}`",
        "",
        "## Scores",
        "",
        f"- PRD fidelity (`prd_fidelity`): **{judges.get('prd_fidelity', {}).get('status', 'pending')}**",
        f"- Discussion effectiveness (`discussion`): **{judges.get('discussion', {}).get('status', 'pending')}**",
        f"- Agent contribution (`contribution`): **{contribution_summary}**",
        f"- Evaluation status: **{_evaluation_status(judges, conclusion)}**",
        "",
        "## 2. 逐 Agent 作用分析（Contribution Judge）",
        "",
    ]
    findings = contribution.get("agent_findings") or []
    if findings:
        for item in findings:
            lines.append(
                f"- **{agent_names.get(str(item.get('agent_id') or ''), item.get('agent_id'))}** "
                f"(`{item.get('agent_id')}`) — score {item.get('score')}, "
                f"necessity `{_canonical_necessity(item.get('necessity'))}`（{necessity_label(item.get('necessity'))}）, "
                f"necessity_status `{item.get('necessity_status') or 'complete'}`, "
                f"status `{item.get('status')}`: {item.get('role_summary')}"
                + (f"（{item.get('rationale')}）" if item.get('rationale') else "")
            )
    else:
        lines.append(f"- 无可用结论（status: {judges.get('contribution', {}).get('status', 'pending')}）")
    lines += ["", "## 3. Discussion Score（五维度）", ""]
    dims = discussion.get("dimensions") or []
    if dims:
        for item in dims:
            lines.append(
                f"- `{item.get('dimension_id')}`: {item.get('score')} "
                f"（{item.get('rationale')}）"
            )
    else:
        lines.append(f"- 无可用分数（status: {judges.get('discussion', {}).get('status', 'pending')}）")
    lines += ["", "## 4. PRD Fidelity Score（两项对齐）", ""]
    prd_dims = prd_fidelity.get("dimensions") or []
    if prd_dims:
        for item in prd_dims:
            lines.append(
                f"- `{item.get('dimension_id')}`: {item.get('score')} "
                f"verdict={item.get('verdict')}（{item.get('rationale')}）"
            )
        for diff in prd_fidelity.get("differences") or []:
            lines.append(f"- difference: {diff}")
    else:
        lines.append(f"- 无可用分数（status: {judges.get('prd_fidelity', {}).get('status', 'pending')}）")
    lines += ["", "## 5. Conclusion / 缺失证据 / 下一步", ""]
    if conclusion:
        result = conclusion.get("result") or {}
        team = result.get("team_findings") or {}
        lines.append(f"- status: `{conclusion.get('status')}`")
        lines.append(f"- 结论：{result.get('conclusion_zh') or '（无）'}")
        lines.append(
            "- PRD 可进入下一阶段："
            f"`{team.get('prd_ready_for_next_stage', result.get('prd_ready_for_next_stage'))}`"
        )
        lines.append(
            "- 讨论有效："
            f"`{team.get('discussion_meaningful', result.get('discussion_meaningful'))}`；"
            "讨论有推进："
            f"`{team.get('discussion_progressed', result.get('discussion_progressed'))}`"
        )
        for label, key in (
            ("problems", "problems"),
            ("underperformed roles", "underperformed_roles"),
            ("unnecessary roles", "unnecessary_roles"),
        ):
            value = team.get(key, result.get(key))
            if value:
                lines.append(f"- {label}: {value}")
        for action in result.get("next_actions") or []:
            lines.append(f"- next: {action}")
    else:
        lines.append("- conclusion：等待三个 Judge 终态")
    missing = _collect_missing(judges)
    if missing:
        lines += ["", "### missing", ""]
        for item in missing:
            lines.append(f"- required: {item['required']}")
            lines.append(f"  reason: {item['reason']}")
    # Derive the display paths through the same safe path builders used for
    # persisted sidecars.  A workflow id may contain ``/`` or other path
    # separators; interpolating it directly would produce a link to a path
    # that does not exist (and could escape the intended run directory).
    judge_dir = judge_result_path(
        Path("."), workflow_run_id=workflow_run_id,
        judge_type="contribution", request_id="placeholder",
    ).parent.parent
    conclusion_dir = conclusion_result_path(
        Path("."), workflow_run_id=workflow_run_id, request_id="placeholder",
    ).parent
    lines += [
        "",
        "## 6. 原始 Judge JSON",
        "",
        f"- judges dir: `{judge_dir.as_posix()}/`",
        f"- conclusions dir: `{conclusion_dir.as_posix()}/`",
        "",
    ]
    return "\n".join(lines)


def _canonical_necessity(value: Any) -> str:
    aliases = {"necessary": "must", "unnecessary": "not_needed", "misfit": "not_needed"}
    normalized = str(value or "").strip()
    return aliases.get(normalized, normalized or "undetermined")


def _evaluation_status(
    judges: dict[str, dict[str, Any]], conclusion: dict[str, Any] | None,
) -> str:
    conclusion_status = str(conclusion.get("status") or "") if conclusion else ""
    if conclusion:
        if conclusion_status in {"inconclusive", "invalid", "incomplete",
                                 "evidence_missing", "abstain", "conflict"}:
            return conclusion_status
    required = ("contribution", "discussion", "prd_fidelity")
    statuses = [str((judges.get(key) or {}).get("status") or "pending") for key in required]
    if any(status in {"inconclusive", "judge_timeout", "invalid", "evidence_missing",
                      "abstain", "conflict", "incomplete"} for status in statuses):
        return "incomplete"
    if any(status not in {"scored"} for status in statuses):
        return "incomplete"
    # Three subagent scores without the main Judge's terminal conclusion are
    # still an unfinished evaluation.  This prevents an intermediate report
    # from claiming completion during a rescan/race.
    return "complete" if conclusion_status == "complete" else "incomplete"


def _refresh_report(
    state_dir: Path,
    *,
    workflow_run_id: str,
    trial_id: str,
    channel_id: str,
    freeze: dict[str, Any],
    judges: dict[str, dict[str, Any]],
    conclusion: dict[str, Any] | None = None,
    source_snapshot_digest: str = "",
    role_catalog: dict[str, Any] | None = None,
    agent_profiles: list[dict[str, Any]] | None = None,
) -> None:
    from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION

    write_report(
        state_dir, workflow_run_id,
        render_report(
            workflow_run_id=workflow_run_id, trial_id=trial_id,
            channel_id=channel_id, freeze=freeze,
            judges=judges, conclusion=conclusion, agent_profiles=agent_profiles,
        ),
        prompt_version=EVALUATION_PROMPT_VERSION,
        trial_id=trial_id, source_snapshot_digest=source_snapshot_digest,
        final=conclusion is not None,
    )
    _write_audit_sidecars(
        state_dir,
        workflow_run_id=workflow_run_id,
        trial_id=trial_id,
        source_snapshot_digest=source_snapshot_digest,
        channel_id=channel_id,
        freeze=freeze,
        judges=judges,
        conclusion=conclusion,
        agent_profiles=agent_profiles,
        role_catalog=role_catalog,
    )


def _write_audit_sidecars(
    state_dir: Path,
    *,
    workflow_run_id: str,
    trial_id: str,
    source_snapshot_digest: str,
    channel_id: str,
    freeze: dict[str, Any],
    judges: dict[str, dict[str, Any]],
    conclusion: dict[str, Any] | None,
    agent_profiles: list[dict[str, Any]] | None = None,
    role_catalog: dict[str, Any] | None = None,
) -> None:
    """Persist verbose provenance beside the short human report."""

    from zf.runtime.evaluation_prompts import EVALUATION_PROMPT_VERSION

    # Keep verbose provenance out of the short ``report.md`` projection and
    # make each immutable evaluation identity its own directory.  ``report_path``
    # applies the runner's safe run-id normalization; never concatenate an
    # untrusted workflow id into a filesystem path here.
    from zf.runtime.evaluation_packets import _safe_segment
    from zf.runtime.evaluation_runner import report_path

    prompt_component = _safe_segment(EVALUATION_PROMPT_VERSION)
    trial_component = _safe_segment(trial_id)
    source_component = _safe_segment(source_snapshot_digest)
    root = (
        report_path(Path(state_dir), workflow_run_id).parent
        / "audit"
        / f"{prompt_component}-{trial_component}-{source_component}"
    )
    root.mkdir(parents=True, exist_ok=True)
    classification = (judges.get("task_type") or {}).get("result") or {}
    classified_type = str(classification.get("task_type") or "")
    role_catalog = role_catalog or ((judges.get("task_type") or {}).get("role_catalog")
                                    or (judges.get("contribution") or {}).get("role_catalog"))
    role_data = {}
    if isinstance(role_catalog, dict):
        roles = role_catalog.get("roles")
        if isinstance(roles, dict) and isinstance(roles.get(classified_type), dict):
            role_data = roles[classified_type]
    # If the classifier result does not echo role metadata, recover it from
    # the frozen packet catalog (the role bytes remain immutable there).
    if not role_data:
        for value in judges.values():
            result = value.get("result") or {}
            if str(result.get("task_type") or "") == classified_type:
                role_data = {
                    key: result.get(key)
                    for key in ("role_ref", "role_version", "role_digest")
                    if result.get(key)
                }
                if role_data:
                    break
    metadata = {
        "schema_version": "channel-prd-evaluation.audit.v1",
        "workflow_run_id": workflow_run_id,
        "trial_id": trial_id,
        "channel_id": channel_id,
        "freeze_event_id": freeze.get("event_id") or "",
        "source_snapshot_digest": source_snapshot_digest,
        "freeze": freeze,
        "task_type": classified_type,
        "task_type_confidence": classification.get("confidence"),
        "role_ref": classification.get("role_ref") or role_data.get("role_ref") or "",
        "role_version": classification.get("role_version") or role_data.get("role_version") or "",
        "role_digest": classification.get("role_digest") or role_data.get("role_digest") or "",
        "agent_profiles": agent_profiles or [],
        "prompt_version": EVALUATION_PROMPT_VERSION,
        "judge_statuses": {key: value.get("status") for key, value in judges.items()},
        "conclusion_status": conclusion.get("status") if conclusion else "pending",
    }
    atomic_write_text(root / "evaluation.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    packet_manifest = {
        "schema_version": "channel-prd-evaluation-packet-manifest.v1",
        "workflow_run_id": workflow_run_id,
        "trial_id": trial_id,
        "source_snapshot_digest": source_snapshot_digest,
        "freeze_event_id": freeze.get("event_id") or "",
        "freeze_event_type": freeze.get("event_type") or "",
        "prd_ref": freeze.get("prd_ref") or "",
        "prd_digest": freeze.get("prd_digest") or "",
        "stages": {
            key: {
                "request_id": value.get("request_id") or "",
                "source_snapshot_digest": value.get("source_snapshot_digest") or source_snapshot_digest,
                "status": value.get("status") or "",
            }
            for key, value in judges.items()
        },
    }
    atomic_write_text(
        root / "packet.manifest.json",
        json.dumps(packet_manifest, ensure_ascii=False, indent=2) + "\n",
    )
    classification_payload = {
        "judge": judges.get("task_type") or {},
        "result": classification,
        "selected_role": role_data,
    }
    atomic_write_text(root / "judge.json", json.dumps(classification_payload, ensure_ascii=False, indent=2) + "\n")
    subagents = root / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    for stage in ("contribution", "discussion", "prd_fidelity"):
        record = judges.get(stage)
        if record is None:
            continue
        atomic_write_text(
            subagents / f"{stage}.json",
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        )
































def _judge_line_status(
    judges: dict[str, dict[str, Any]],
    conclusion: dict[str, Any] | None,
) -> str:
    parts = [
        f"{judge_type}={str(record.get('status') or 'pending')}"
        for judge_type, record in sorted(judges.items())
    ]
    if conclusion:
        parts.append(f"conclusion={conclusion.get('status')}")
    return ", ".join(parts) if parts else "pending"


def _collect_missing(judges: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for judge_type, record in judges.items():
        result = record.get("result") or {}
        for ref in result.get("missing_evidence") or []:
            missing.append({
                "required": str(ref),
                "reason": f"{judge_type} judge reported missing evidence",
            })
        if str(record.get("status") or "") in {
            "judge_timeout", "inconclusive", "evidence_missing", "invalid",
            "abstain", "conflict", "incomplete",
        }:
            missing.append({
                "required": judge_type,
                "reason": str(record.get("error") or "judge timed out"),
            })
    return missing
