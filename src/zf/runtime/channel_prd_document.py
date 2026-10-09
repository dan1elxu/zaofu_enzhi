"""Mechanical validation and rendering of agent-authored PRD evidence."""
from __future__ import annotations

import json
from html import escape
from typing import Any


def validate_prd_document(body: dict[str, Any]) -> str:
    # Existing persisted contracts remain readable. New provider prompts ask
    # for the full document; the presence of this field opts into its schema.
    if "prd_markdown" not in body:
        return ""
    if not isinstance(body["prd_markdown"], str) or not body["prd_markdown"].strip():
        return "prd_markdown must be a non-empty string"
    rows = body.get("requirement_coverage")
    if not isinstance(rows, list) or not rows:
        return "requirement_coverage must be a non-empty list"
    seen = set()
    acceptance_ids = {str(item.get("id") or "") for item in body.get("acceptance_criteria") or []
                      if isinstance(item, dict)}
    for row in rows:
        if not isinstance(row, dict):
            return "requirement_coverage entries must be objects"
        identity = row.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            return "requirement_coverage id must be non-empty and unique"
        seen.add(identity)
        if row.get("status") not in {"covered", "deferred", "gap"}:
            return "requirement_coverage status must be covered, deferred, or gap"
        for field in ("source_refs", "acceptance_ids"):
            values = row.get(field)
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                return f"requirement_coverage {field} must contain string references"
        if not row["source_refs"]:
            return "requirement_coverage requires source_refs"
        if row["status"] == "covered":
            if not isinstance(row.get("section"), str) or not row["section"].strip():
                return "covered requirement requires a PRD section"
            if not row["acceptance_ids"] or not set(row["acceptance_ids"]) <= acceptance_ids:
                return "covered requirement must reference declared acceptance ids"
        elif not isinstance(row.get("reason"), str) or not row["reason"].strip():
            return "deferred or gap requirement requires a reason"
    if not isinstance(body.get("decision_log"), list):
        return "decision_log must be a list"
    return ""


def render_prd_document(synthesis: dict[str, Any], *, channel_id: str, thread_id: str, source_refs: list[str]) -> str:
    """Preserve the full author body and carry review evidence in the export."""
    review_catalogs = []
    for title, field in (("假设", "assumptions"), ("风险", "risks"), ("未消除的分歧", "dissent")):
        if synthesis.get(field):
            review_catalogs.extend(_catalog_table(title, synthesis[field]))
    return synthesis["prd_markdown"].rstrip() + "\n\n" + "\n".join([
        "## 需求追溯与决策记录",
        "以下为作者声明的映射，需审阅者对照原文核实；不代表自动质量通过。",
        *_catalog_table("来源覆盖", synthesis.get("requirement_coverage", [])),
        *_catalog_table("新增决定", synthesis.get("decision_log", [])),
        *(["## 假设、风险与分歧", *review_catalogs] if review_catalogs else []),
        "## 实施交接合同",
        "以下为同版 PRD 的完整交付合同。channel_prd_scope=full_product 时据此编译 Task；"
        "channel_prd_scope=approved_task 时使用另行批准的局部目标、验收、验证命令与范围，"
        "仍须绑定并校验本版 PRD。局部 Task 不自动继承下列完整交付义务；测试命令中的文件可由后续实施创建。",
        *_catalog_table("验收目录", synthesis.get("acceptance_criteria", [])),
        *_catalog_table("验证命令", synthesis.get("verification_commands", [])),
        *_catalog_table("非目标", synthesis.get("out_of_scope", [])),
        "## Provenance",
        f"- Channel: `{channel_id}`", f"- Thread: `{thread_id}`",
        *[f"- Source: `{ref}`" for ref in source_refs], "",
    ])


def _catalog_table(title: str, values: list[Any]) -> list[str]:
    """Render every declared field without copying a second JSON envelope."""
    lines = ["", f"### {title}", ""]
    if not values:
        return [*lines, "无。", ""]
    rows = [item if isinstance(item, dict) else {"value": item} for item in values]
    fields = list(dict.fromkeys(key for row in rows for key in row))
    if not fields:
        return [*lines, "```json", json.dumps(values, ensure_ascii=False), "```", ""]

    def cell(value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return escape(text, quote=False).replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`").replace("\r\n", "\n").replace("\n", "<br>")

    lines.extend([
        "| " + " | ".join(cell(field) for field in fields) + " |",
        "| " + " | ".join("---" for _ in fields) + " |",
    ])
    lines.extend("| " + " | ".join(cell(row[field]) if field in row else "" for field in fields) + " |" for row in rows)
    return [*lines, ""]
