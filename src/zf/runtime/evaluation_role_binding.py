"""Mechanical binding checks for role-driven evaluation results."""
from __future__ import annotations

from typing import Any


def role_binding_error(result: dict[str, Any], packet: Any) -> str:
    """Reject a quality result that claims a different frozen evaluator role."""
    expected_task = str(packet.get("task_type") or "").strip()
    expected_ref = str(packet.get("role_ref") or "").strip()
    details = packet.get("task_details") if hasattr(packet, "get") else {}
    metadata = details.get("evaluation_metadata") if isinstance(details, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    expected_version = str(metadata.get("role_version") or "").strip()
    if not expected_task or not expected_ref:
        return ""
    result_task = str(result.get("task_type") or "").strip()
    result_id = str(result.get("role_id") or "").strip()
    result_version = str(result.get("role_version") or "").strip()
    if result_task != expected_task:
        return f"judge role task_type mismatch: expected {expected_task!r}, got {result_task!r}"
    if result_id != expected_task:
        return f"judge role_id mismatch: expected {expected_task!r}, got {result_id!r}"
    if expected_version and result_version != expected_version:
        return f"judge role_version mismatch: expected {expected_version!r}, got {result_version!r}"
    role_metadata = result.get("role_metadata")
    role_metadata = role_metadata if isinstance(role_metadata, dict) else {}
    returned_ref = str(role_metadata.get("role_ref") or "").strip()
    returned_digest = str(role_metadata.get("role_digest") or "").strip()
    expected_digest = str(packet.get("role_digest") or "").strip()
    if returned_ref and returned_ref != expected_ref:
        return f"judge role_ref mismatch: expected {expected_ref!r}, got {returned_ref!r}"
    if returned_digest and expected_digest and returned_digest != expected_digest:
        return "judge role_digest mismatch"
    return ""


__all__ = ["role_binding_error"]
