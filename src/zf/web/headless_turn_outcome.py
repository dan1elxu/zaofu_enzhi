"""Stable outcome classification for Kanban Agent headless turns."""

from __future__ import annotations

from typing import Any


def classify_headless_turn(
    *,
    status: object,
    error: object = "",
    incomplete: bool = False,
    completion_reason: object = "",
) -> dict[str, Any]:
    """Map provider/backend details to one observable terminal contract."""

    normalized = str(status or "failed").strip().lower().replace("-", "_")
    reason = str(error or completion_reason or status or "failed").strip()
    lower_reason = reason.lower()
    if incomplete or normalized in {"incomplete", "truncated", "length"}:
        return _outcome(
            "incomplete",
            "provider_incomplete",
            retryable=True,
            next_action="continue_or_retry",
            reason=reason,
        )
    if normalized in {"completed", "complete", "success", "succeeded"}:
        return _outcome("completed", "none", retryable=False, next_action="none", reason=reason)
    if normalized in {"waiting_input", "needs_input", "needs_owner"}:
        return _outcome("waiting_input", "none", retryable=False, next_action="owner_input", reason=reason)
    if normalized in {"cancelled", "canceled"}:
        return _outcome("cancelled", "cancelled", retryable=False, next_action="none", reason=reason)
    if normalized in {"timeout", "timed_out", "provider_timeout"} or "timeout" in lower_reason:
        return _outcome("timeout", "timeout", retryable=True, next_action="retry", reason=reason)
    if normalized in {"blocked", "forbidden", "permission_denied", "validation_error", "stale"}:
        failure_class = {
            "forbidden": "permission",
            "permission_denied": "permission",
            "validation_error": "validation",
            "stale": "stale",
        }.get(normalized, "blocked")
        return _outcome(normalized if normalized == "blocked" else "blocked", failure_class, retryable=False, next_action="owner_action", reason=reason)
    provider_markers = ("provider", "codex", "app-server", "authentication", "login", "model")
    if any(marker in lower_reason for marker in provider_markers):
        return _outcome("failed", "provider_error", retryable=True, next_action="retry_or_fix_provider", reason=reason)
    return _outcome("failed", "ordinary", retryable=True, next_action="retry", reason=reason)


def _outcome(
    outcome: str,
    failure_class: str,
    *,
    retryable: bool,
    next_action: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "failure_class": failure_class,
        "retryable": retryable,
        "next_action": next_action,
        "reason": reason,
    }


__all__ = ["classify_headless_turn"]
