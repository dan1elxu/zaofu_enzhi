from __future__ import annotations

from zf.runtime.channel_provider_preflight import (
    channel_provider_preflight,
    normalize_channel_dispatch_policy,
)


def test_trigger_only_defers_provider_probe() -> None:
    calls: list[str] = []

    result = channel_provider_preflight(
        [{"backend": "codex", "member_id": "arch"}],
        dispatch_policy="trigger_only",
        auth_probe=lambda backend: calls.append(backend) or (False, "should not run"),
    )

    assert result["status"] == "deferred"
    assert result["dispatch_policy"] == "trigger_only"
    assert calls == []


def test_codex_preflight_reports_auth_failure_without_turn() -> None:
    result = channel_provider_preflight(
        [{"backend": "codex-headless", "member_id": "arch"}],
        auth_probe=lambda backend: (False, "authenticate with `codex login`"),
        command_lookup=lambda command: "/usr/bin/" + command,
    )

    assert result["status"] == "blocked"
    assert result["checks"] == [{
        "backend": "codex-headless",
        "status": "blocked",
        "detail": "authenticate with `codex login`",
    }]
    assert "codex-headless" in result["reason"]


def test_fake_backend_preflight_does_not_require_external_command() -> None:
    result = channel_provider_preflight(
        [{"backend": "fake", "member_id": "critic"}],
        command_lookup=lambda command: None,
    )

    assert result["status"] == "passed"
    assert result["checks"][0]["detail"].startswith("deterministic backend")


def test_dispatch_policy_is_bounded() -> None:
    assert normalize_channel_dispatch_policy(None) == "auto"
    assert normalize_channel_dispatch_policy("trigger-only") == "trigger_only"
    try:
        normalize_channel_dispatch_policy("background")
    except ValueError as exc:
        assert "auto" in str(exc)
        assert "trigger_only" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("invalid policy was accepted")
