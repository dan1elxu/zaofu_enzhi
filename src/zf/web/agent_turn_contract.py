"""Shared contracts and validation for durable WebChat turns."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from zf.web.agent_runtime_db import loads


TURN_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "abandoned"})
TURN_ACTIVE_STATES = frozenset({"claimed", "running", "waiting_input", "retry_wait"})
TURN_CLAIMABLE_STATES = frozenset({"submitted", "queued", "resume_queued", "retry_wait"})
INTERACTION_KINDS = frozenset({
    "ask_user", "tool_approval", "action_approval", "artifact_adoption",
})
CONTROL_OPERATIONS = frozenset({"queue", "steer", "interrupt", "cancel"})
MAX_INTERACTION_REQUEST_BYTES = 64 * 1024
MAX_INTERACTION_QUESTION_CHARS = 2_000
_SECRET_FIELD_NAMES = frozenset({
    "api_key", "apikey", "authorization", "cookie", "credential",
    "oauth_token", "password", "refresh_token", "secret", "token",
})


class AgentTurnConflict(RuntimeError):
    pass


class AgentTurnNotFound(KeyError):
    pass


class AgentInteractionValidationError(ValueError):
    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if str(error)))
        super().__init__("; ".join(self.errors) or "invalid interaction request")


@dataclass(frozen=True)
class TurnClaim:
    turn_id: str
    attempt_id: str
    attempt_no: int
    fence_token: str
    lease_expires_at: str
    resumed_from_attempt_id: str = ""


def new_runtime_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def lease_expiry(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(1.0, seconds))
    ).isoformat().replace("+00:00", "Z")


def decode_runtime_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    for key in ("request_json", "response_json", "currentness_json", "refs_json", "payload_json"):
        if key in value:
            value[key.removesuffix("_json")] = loads(
                value.pop(key),
                {} if key != "refs_json" else {},
            )
    return value


def decode_turn_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    value = decode_runtime_row(row)
    if value is None:
        return None
    value.setdefault("schema_version", "webchat-turn.v1")
    return value


def _secret_paths(value: Any, *, prefix: str = "request") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            path = f"{prefix}.{raw_key}"
            if key in _SECRET_FIELD_NAMES or key.endswith("_password"):
                found.append(path)
            found.extend(_secret_paths(item, prefix=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_secret_paths(item, prefix=f"{prefix}[{index}]"))
    return found


def validate_interaction_request(kind: str, request: dict[str, Any]) -> None:
    """Validate provider-neutral interaction payloads before they become actionable."""

    errors: list[str] = []
    encoded = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    if len(encoded) > MAX_INTERACTION_REQUEST_BYTES:
        errors.append(
            f"interaction request exceeds {MAX_INTERACTION_REQUEST_BYTES} byte limit"
        )
    secret_paths = _secret_paths(request)
    if secret_paths:
        errors.append("interaction request contains secret fields: " + ", ".join(secret_paths[:5]))
    if request.get("valid") is False:
        errors.append(str(request.get("validation_error") or "interaction request is invalid"))

    if kind == "ask_user":
        raw_questions = request.get("questions")
        if isinstance(raw_questions, list):
            questions = [item for item in raw_questions if isinstance(item, dict)]
            if len(questions) != len(raw_questions) or not 1 <= len(questions) <= 3:
                errors.append("ask_user requires one to three structured questions")
        else:
            questions = [request]
        for index, question in enumerate(questions, start=1):
            text = str(question.get("question") or question.get("text") or "").strip()
            if not text:
                errors.append(f"question {index} text is required")
            elif len(text) > MAX_INTERACTION_QUESTION_CHARS:
                errors.append(
                    f"question {index} exceeds {MAX_INTERACTION_QUESTION_CHARS} characters"
                )
            if bool(question.get("isSecret") or question.get("is_secret")):
                errors.append(f"question {index} cannot request a secret")
            options = question.get("options")
            if options is None and question is request:
                options = request.get("choices")
            if options is not None:
                if not isinstance(options, list) or not 2 <= len(options) <= 20:
                    errors.append(f"question {index} options must contain 2 to 20 choices")
                    continue
                identities: list[str] = []
                for option in options:
                    if isinstance(option, dict):
                        identity = str(
                            option.get("id") or option.get("value") or option.get("label") or ""
                        ).strip().casefold()
                    else:
                        identity = str(option).strip().casefold()
                    if identity:
                        identities.append(identity)
                if len(identities) != len(options) or len(set(identities)) != len(identities):
                    errors.append(f"question {index} options must be non-empty and mutually distinct")
    elif kind == "tool_approval":
        if not str(request.get("tool") or "").strip():
            errors.append("tool approval requires tool identity")
        if not str(request.get("args_digest") or "").strip():
            errors.append("tool approval requires exact args_digest")
    elif kind == "action_approval":
        if not str(request.get("action") or "").strip():
            errors.append("action approval requires action identity")
    elif kind == "artifact_adoption":
        if not str(request.get("version_id") or request.get("artifact_ref") or "").strip():
            errors.append("artifact adoption requires an exact artifact version")

    if errors:
        raise AgentInteractionValidationError(errors)


__all__ = [
    "AgentInteractionValidationError",
    "AgentTurnConflict",
    "AgentTurnNotFound",
    "CONTROL_OPERATIONS",
    "INTERACTION_KINDS",
    "TURN_ACTIVE_STATES",
    "TURN_CLAIMABLE_STATES",
    "TURN_TERMINAL_STATES",
    "TurnClaim",
    "decode_runtime_row",
    "decode_turn_row",
    "lease_expiry",
    "new_runtime_id",
    "validate_interaction_request",
]
