"""Public, non-sensitive execution metadata for Channel/PRD evaluation."""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any


def _number(name: str, default: float) -> int | float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    value = max(0.0, value)
    return int(value) if value.is_integer() else value


def capture_evaluator_budgets() -> dict[str, Any]:
    """Capture the same environment-backed budgets used by turn execution."""
    return {
        "timeouts": {
            "start_seconds": _number("ZF_EVALUATION_JUDGE_START_TIMEOUT_S", 120),
            "turn_seconds": _number("ZF_EVALUATION_JUDGE_TIMEOUT_S", 1800),
            "first_progress_seconds": _number("ZF_EVALUATION_JUDGE_FIRST_PROGRESS_TIMEOUT_S", 300),
            "idle_seconds": _number("ZF_EVALUATION_JUDGE_IDLE_TIMEOUT_S", 300),
        },
        "tool_budgets": {
            "max_tool_calls": _number("ZF_EVALUATION_JUDGE_MAX_TOOL_CALLS", 0),
            "max_same_tool_fingerprint": _number("ZF_EVALUATION_JUDGE_MAX_SAME_TOOL_FINGERPRINT", 0),
            "max_tool_wall_time_seconds": _number("ZF_EVALUATION_JUDGE_MAX_TOOL_WALL_TIME_S", 0),
        },
    }


def observed_evaluator_config(*, backend: str, provider: str,
                              observed_model: str = "unknown",
                              observed_model_source: str = "unknown",
                              prompt_version: str = "",
                              prompt_digest: str = "",
                              system_prompt_digest: str = "",
                              role_ref: str = "", role_digest: str = "",
                              max_attempts: int = 2,
                              schema_max_attempts: int = 2,
                              budgets: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return stable audit metadata without copying provider secrets/config."""
    model = observed_model.strip() if isinstance(observed_model, str) and observed_model.strip() else "unknown"
    source = observed_model_source.strip() if isinstance(observed_model_source, str) and observed_model_source.strip() else "unknown"
    budgets = budgets or capture_evaluator_budgets()
    result: dict[str, Any] = {
        "schema_version": "channel-prd-evaluation.config.v1",
        "provider": str(provider or "unknown"),
        "backend": str(backend or "unknown"),
        "model": model,
        "model_source": source,
        "sampling": "unknown",
        "sample_count": 1,
        "sampling_source": "not_observed",
        "timeouts": dict(budgets.get("timeouts") or {}),
        "tool_budgets": dict(budgets.get("tool_budgets") or {}),
        "token_budget": None,
        "cost_budget": None,
        "retry_budget": {
            "transport_max_attempts": int(max_attempts),
            "schema_max_attempts_per_transport_attempt": int(schema_max_attempts),
        },
        "prompt_version": str(prompt_version or ""),
        "prompt_digest": str(prompt_digest or ""),
        "system_prompt_digest": str(system_prompt_digest or ""),
        "role_ref": str(role_ref or ""),
        "role_digest": str(role_digest or ""),
    }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    result["config_digest"] = hashlib.sha256(encoded).hexdigest()
    return result


__all__ = ["capture_evaluator_budgets", "observed_evaluator_config"]
