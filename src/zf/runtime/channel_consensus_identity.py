"""Canonical identity payloads for confirmed Channel PRDs."""

from __future__ import annotations

from typing import Any
from pathlib import Path
import re

from zf.runtime.sidecar_refs import SidecarRefError, hydrate_sidecar_ref

_REVISION_RE = re.compile(r"^[1-9][0-9]*$")


def _strict_revision(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and _REVISION_RE.fullmatch(value):
        return int(value)
    return None


def consensus_artifact_error(state_dir: Path, consensus: dict[str, Any]) -> str:
    """Verify the versioned PRD and its bound readiness/conclusion before freeze."""
    revision = _strict_revision(consensus.get("prd_revision"))
    if revision is None:
        if consensus.get("prd_revision") in (None, ""):
            # Legacy unversioned discussion artifacts.
            return ""
        return "prd:revision_invalid"
    try:
        bodies = {}
        for name, ref_key, digest_key in (
            ("prd", "artifact_ref", "artifact_digest"),
            ("readiness", "readiness_ref", "readiness_digest"),
            ("conclusion", "conclusion_ref", "conclusion_digest"),
        ):
            digest = str(consensus.get(digest_key) or "").removeprefix("sha256:")
            if not digest:
                return f"{name}:binding_missing"
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                return f"{name}:digest_invalid"
            body = hydrate_sidecar_ref(state_dir, {
                "ref": str(consensus.get(ref_key) or ""), "sha256": digest,
            }).payload
            body_revision = _strict_revision(body.get("revision")) if isinstance(body, dict) else None
            if body_revision != revision:
                return f"{name}:revision_mismatch"
            bodies[name] = body
        for name in ("prd", "conclusion"):
            for key in ("readiness_ref", "readiness_digest"):
                if bodies[name].get(key) != consensus.get(key):
                    return f"{name}:{key}_mismatch"
        for key in ("prd_ref", "prd_digest"):
            if bodies["conclusion"].get(key) != consensus.get(key):
                return f"conclusion:{key}_mismatch"
    except SidecarRefError as exc:
        return f"{name}:{exc.code}"
    return ""


def consensus_reached_payload(
    consensus: dict[str, Any],
    *,
    channel_id: str,
    thread_id: str,
    source: str,
    confirmed_by: str = "",
    risk_accepted: bool | None = None,
) -> dict[str, Any]:
    """Publish the complete immutable identity of a confirmed Channel PRD."""

    artifact_ref = str(consensus.get("artifact_ref") or "")
    artifact_digest = str(consensus.get("artifact_digest") or "")
    return {
        "channel_id": channel_id,
        "thread_id": thread_id,
        "artifact_ref": artifact_ref,
        "artifact_digest": artifact_digest,
        "prd_ref": str(consensus.get("prd_ref") or artifact_ref),
        "prd_digest": str(
            consensus.get("prd_digest") or artifact_digest
        ),
        "prd_revision": _strict_revision(consensus.get("prd_revision")) or 0,
        "readiness_ref": str(consensus.get("readiness_ref") or ""),
        "readiness_digest": str(
            consensus.get("readiness_digest") or ""
        ),
        "readiness_verdict": str(
            consensus.get("readiness_verdict") or "unassessed"
        ),
        "implementation_start": (
            consensus.get("implementation_start") is True
        ),
        "conclusion_ref": str(consensus.get("conclusion_ref") or ""),
        "conclusion_digest": str(
            consensus.get("conclusion_digest") or ""
        ),
        "confirmed_by": str(
            confirmed_by or consensus.get("human_confirmed_by") or ""
        ),
        "risk_accepted": (
            consensus.get("risk_accepted") is True
            if risk_accepted is None
            else risk_accepted
        ),
        "signed_by": sorted((consensus.get("signed") or {}).keys()),
        "product_mode": str(consensus.get("product_mode") or ""),
        "source_refs": [
            str(item)
            for item in consensus.get("source_refs") or []
            if str(item)
        ],
        "source": source,
    }


__all__ = ["consensus_reached_payload"]
