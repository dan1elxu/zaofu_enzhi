"""Bounded readiness checks for real Channel provider dispatch."""

from __future__ import annotations

import shutil
from typing import Any, Callable

from zf.core.config.backend_identity import canonical_backend_id
from zf.runtime.preflight import probe_provider_auth


CHANNEL_DISPATCH_POLICIES = frozenset({"auto", "trigger_only"})
_FAKE_BACKENDS = frozenset({"fake", "mock", "deterministic", "python"})
_PROVIDER_COMMANDS = {
    "codex": "codex",
    "codex-headless": "codex",
    "claude-code": "claude",
    "claude-headless": "claude",
}


def normalize_channel_dispatch_policy(value: object) -> str:
    policy = str(value or "auto").strip().lower().replace("-", "_")
    if policy not in CHANNEL_DISPATCH_POLICIES:
        allowed = ", ".join(sorted(CHANNEL_DISPATCH_POLICIES))
        raise ValueError(f"dispatch_policy must be one of: {allowed}")
    return policy


def channel_provider_preflight(
    members: list[dict[str, Any]],
    *,
    dispatch_policy: str = "auto",
    auth_probe: Callable[[str], tuple[bool, str]] = probe_provider_auth,
    command_lookup: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    """Check every real backend before a Channel can auto-dispatch.

    The probe is intentionally command/auth-only; it never starts a model turn.
    ``trigger_only`` defers the provider check because it creates durable
    requests without launching them.
    """

    policy = normalize_channel_dispatch_policy(dispatch_policy)
    backends = sorted({
        canonical_backend_id(member.get("backend") or member.get("provider"))
        for member in members
        if isinstance(member, dict)
    } - {""})
    if policy == "trigger_only":
        return {
            "schema_version": "channel.provider-preflight.v1",
            "status": "deferred",
            "dispatch_policy": policy,
            "backends": backends,
            "checks": [],
            "reason": "provider dispatch is explicitly paused",
        }

    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    for backend in backends:
        if backend in _FAKE_BACKENDS:
            checks.append({
                "backend": backend,
                "status": "passed",
                "detail": "deterministic backend does not require external auth",
            })
            continue
        command = _PROVIDER_COMMANDS.get(backend)
        if command is None:
            detail = f"no bounded readiness probe is registered for {backend!r}"
            checks.append({"backend": backend, "status": "blocked", "detail": detail})
            blockers.append(f"{backend}: {detail}")
            continue
        if command_lookup(command) is None:
            detail = f"command missing: {command!r}"
            checks.append({"backend": backend, "status": "blocked", "detail": detail})
            blockers.append(f"{backend}: {detail}")
            continue
        probe_backend = "codex" if backend.startswith("codex") else "claude-code"
        ok, detail = auth_probe(probe_backend)
        checks.append({
            "backend": backend,
            "status": "passed" if ok else "blocked",
            "detail": detail,
        })
        if not ok:
            blockers.append(f"{backend}: {detail}")

    return {
        "schema_version": "channel.provider-preflight.v1",
        "status": "blocked" if blockers else "passed",
        "dispatch_policy": policy,
        "backends": backends,
        "checks": checks,
        "blockers": blockers,
        "reason": "; ".join(blockers),
    }


__all__ = [
    "CHANNEL_DISPATCH_POLICIES",
    "channel_provider_preflight",
    "normalize_channel_dispatch_policy",
]
