"""Frozen workflow lineage and artifact identity validation for evaluation inputs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

WORKFLOW_BIND_EVENTS = frozenset({
    "workflow.invoke.requested",
    "workflow.invoke.accepted",
})
_REVISION_RE = re.compile(r"^[1-9][0-9]*$")


def _bound_workflows(events: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Accepted Workflow bindings, preserving their exact Channel PRD lineage."""

    bound: dict[str, list[dict[str, Any]]] = {}
    requests: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type not in WORKFLOW_BIND_EVENTS or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if event.type == "workflow.invoke.requested":
            requests[event.id] = payload
            continue
        requested = requests.get(str(payload.get("source_event_id") or ""))
        if requested is None:
            continue
        requested_refs = requested.get("source_refs") if isinstance(requested.get("source_refs"), dict) else {}
        accepted_refs = payload.get("source_refs") if isinstance(payload.get("source_refs"), dict) else {}
        if _workflow_lineage_conflict(requested, payload):
            # Accepted metadata may be omitted or repeated, but it cannot
            # redefine the Channel PRD or workflow-bound artifact admitted by
            # the request.  Otherwise a forged accepted event could swap in
            # another revision/readiness while retaining the original request id.
            continue
        refs = {
            **requested_refs,
            **accepted_refs,
        }
        payload = {**requested, **payload}
        payload_channel_id = str(payload.get("channel_id") or "").strip()
        refs_channel_id = str(refs.get("channel_id") or "").strip()
        if payload_channel_id and refs_channel_id and payload_channel_id != refs_channel_id:
            continue
        channel_id = payload_channel_id or refs_channel_id or str(payload.get("correlation_id") or "")
        if not channel_id:
            continue
        payload_thread_id = str(payload.get("thread_id") or "").strip()
        refs_thread_id = str(
            refs.get("channel_thread_id") or refs.get("thread_id") or ""
        ).strip()
        if payload_thread_id and refs_thread_id and payload_thread_id != refs_thread_id:
            continue
        # A workflow-bound copy is distinct from the canonical Channel PRD.
        # Only fall back to the Channel pair when *neither* bound alias is
        # present.  Pairing a copy ref with the Channel digest (or vice versa)
        # would make the later artifact check read an unbound/mismatched file.
        bound_ref = str(
            payload.get("prd_ref")
            or payload.get("source_ref")
            or refs.get("prd_ref")
            or refs.get("source_ref")
            or refs.get("task_source_ref")
            or ""
        )
        bound_digest = str(
            payload.get("prd_digest")
            or payload.get("source_digest")
            or refs.get("prd_digest")
            or refs.get("source_digest")
            or refs.get("task_source_digest")
            or ""
        )
        if not bound_ref and not bound_digest:
            bound_ref = str(refs.get("channel_prd_ref") or "")
            bound_digest = str(refs.get("channel_prd_digest") or "")
        bound.setdefault(channel_id, []).append({
            "event_id": event.id,
            "workflow_run_id": str(payload.get("workflow_run_id") or payload.get("run_id") or ""),
            "task_id": str(payload.get("task_id") or ""),
            "channel_thread_id": payload_thread_id or refs_thread_id,
            "channel_prd_ref": str(
                refs.get("channel_prd_ref") or payload.get("channel_prd_ref") or ""
            ),
            "channel_prd_revision": str(
                refs.get("channel_prd_revision")
                or payload.get("channel_prd_revision")
                or ""
            ),
            "channel_prd_digest": str(
                refs.get("channel_prd_digest") or payload.get("channel_prd_digest") or ""
            ),
            "channel_prd_readiness_ref": str(
                refs.get("channel_prd_readiness_ref")
                or refs.get("readiness_ref")
                or payload.get("channel_prd_readiness_ref")
                or payload.get("readiness_ref")
                or ""
            ),
            "channel_prd_readiness_digest": str(
                refs.get("channel_prd_readiness_digest")
                or refs.get("readiness_digest")
                or payload.get("channel_prd_readiness_digest")
                or payload.get("readiness_digest")
                or ""
            ),
            "prd_ref": bound_ref,
            "prd_digest": bound_digest,
        })
    return bound


def _workflow_lineage_conflict(
    requested: dict[str, Any], accepted: dict[str, Any],
) -> bool:
    """Return whether acceptance rewrites immutable Channel PRD lineage.

    Workflow producers have historically put Channel identity in either the
    event envelope or ``source_refs``.  Compare both locations together.  A
    check limited to nested refs lets an accepted event override the
    top-level ``prd_ref``/``prd_digest`` and make the evaluator read an
    artifact that was never part of the frozen request.
    """

    # Workflow producers put the same identity in either the event envelope or
    # ``source_refs``.  Collect both locations before comparing so moving a
    # value between the two is harmless, while introducing a second value is
    # fail-closed.
    fields = {
        "channel_id": {
            "top": ("channel_id",),
            "refs": ("channel_id",),
            "digest": False,
            "bound": False,
        },
        "channel_thread_id": {
            "top": ("thread_id",),
            "refs": ("channel_thread_id", "thread_id"),
            "digest": False,
            "bound": False,
        },
        "channel_prd_ref": {
            "top": ("channel_prd_ref",),
            "refs": ("channel_prd_ref",),
            "digest": False,
            "bound": False,
        },
        "channel_prd_digest": {
            "top": ("channel_prd_digest",),
            "refs": ("channel_prd_digest",),
            "digest": True,
            "bound": False,
        },
        "channel_prd_revision": {
            "top": ("channel_prd_revision",),
            "refs": ("channel_prd_revision",),
            "digest": False,
            "bound": False,
        },
        "channel_prd_readiness_ref": {
            "top": ("channel_prd_readiness_ref",),
            "refs": ("channel_prd_readiness_ref", "readiness_ref"),
            "digest": False,
            "bound": False,
        },
        "channel_prd_readiness_digest": {
            "top": ("channel_prd_readiness_digest",),
            "refs": ("channel_prd_readiness_digest", "readiness_digest"),
            "digest": True,
            "bound": False,
        },
        "workflow_run_id": {
            "top": ("workflow_run_id", "run_id"),
            "refs": ("workflow_run_id", "run_id"),
            "digest": False,
            "bound": False,
        },
        # These identify the workflow-bound artifact, which may intentionally
        # be a copied/generated PRD at a different ref than channel_prd_ref.
        "bound_prd_ref": {
            "top": ("prd_ref", "source_ref", "task_source_ref"),
            "refs": ("prd_ref", "source_ref", "task_source_ref"),
            "digest": False,
            "bound": True,
        },
        "bound_prd_digest": {
            "top": ("prd_digest", "source_digest", "task_source_digest"),
            "refs": ("prd_digest", "source_digest", "task_source_digest"),
            "digest": True,
            "bound": True,
        },
    }

    def values(
        payload: dict[str, Any], spec: dict[str, Any],
    ) -> tuple[set[str], bool, bool]:
        refs = payload.get("source_refs")
        refs = refs if isinstance(refs, dict) else {}
        raw: list[tuple[bool, Any]] = [
            *((key in payload, payload.get(key)) for key in spec["top"]),
            *((key in refs, refs.get(key)) for key in spec["refs"]),
        ]
        present = any(is_present for is_present, _value in raw)
        empty_present = any(
            is_present and not str(value or "").strip()
            for is_present, value in raw
        )
        result = {
            str(value).strip()
            for is_present, value in raw
            if is_present and str(value or "").strip()
        }
        if spec["digest"]:
            result = {_bare_digest(value) for value in result}
        return result, present, empty_present

    for spec in fields.values():
        requested_values, _requested_present, requested_empty = values(requested, spec)
        accepted_values, accepted_present, accepted_empty = values(accepted, spec)
        # A contradiction within either event is unsafe even if the other
        # event omits the field.  Accepted may omit requested metadata, but it
        # may not rewrite it.  Bound refs/digests are stricter: acceptance may
        # not inject a new artifact identity that was absent from the request.
        if (
            len(requested_values) > 1
            or len(accepted_values) > 1
            or (requested_empty and requested_values)
            or (accepted_empty and accepted_values)
        ):
            return True
        if spec["bound"] and not requested_values and accepted_values:
            return True
        if requested_values and accepted_present and not accepted_values:
            # An explicit empty alias is a rewrite, rather than an omitted
            # metadata field.  This matters for a bound digest/ref pair:
            # allowing it would turn a complete request into a partial pair
            # after the accepted refs are merged.
            return True
        if requested_values and accepted_values and requested_values != accepted_values:
            return True
    return False


def _bound_prd_payload(
    state_dir: Path,
    bound_workflows: list[dict[str, Any]],
    frozen_prd: dict[str, Any],
    *, channel_id: str = "", thread_id: str = "main", prd_revision: int = 0,
) -> dict[str, Any] | None:
    """v1 workflow-bound PRD view; None until a workflow actually binds."""

    expected_readiness_ref = str(frozen_prd.get("readiness_ref") or "").strip()
    expected_readiness_digest = _bare_digest(frozen_prd.get("readiness_digest"))

    def _matches_frozen_readiness(binding: dict[str, Any]) -> bool:
        """Keep a workflow bind on the same readiness reviewed at freeze."""

        if expected_readiness_ref and str(
            binding.get("channel_prd_readiness_ref") or ""
        ).strip() != expected_readiness_ref:
            return False
        if expected_readiness_digest and _bare_digest(
            binding.get("channel_prd_readiness_digest")
        ) != expected_readiness_digest:
            return False
        return True

    latest = next((binding for binding in reversed(bound_workflows) if (
        str(binding.get("workflow_run_id") or "").strip()
        and
        binding.get("channel_thread_id") == thread_id
        and binding.get("channel_prd_revision") == str(prd_revision)
        and binding.get("channel_prd_ref") == frozen_prd.get("prd_ref")
        and str(binding.get("channel_prd_digest") or "").removeprefix("sha256:")
            == str(frozen_prd.get("prd_digest") or "").removeprefix("sha256:")
        and _matches_frozen_readiness(binding)
    )), None)
    if latest is None:
        return None
    ref = str(latest.get("prd_ref") or "")
    digest = str(latest.get("prd_digest") or "")
    if bool(ref) != bool(digest):
        raise ValueError("workflow-bound PRD ref and digest must be supplied together")
    if not ref:
        raise ValueError("workflow-bound PRD ref is missing")
    body = _read_json_artifact(state_dir, ref, expected_digest=digest)
    if body is None:
        raise ValueError("workflow-bound PRD artifact is unreadable")
    frozen_body = frozen_prd.get("body") if isinstance(frozen_prd.get("body"), dict) else {}
    if body.get("schema_version") == "channel-prd.v1":
        expected_channel = str(channel_id or frozen_body.get("channel_id") or "")
        if (
            (expected_channel and body.get("channel_id") != expected_channel)
            or
            body.get("thread_id") != thread_id
            or body.get("revision") != prd_revision
        ):
            raise ValueError("workflow-bound PRD identity does not match freeze")
    return {
        "workflow_run_id": latest.get("workflow_run_id") or "",
        "task_id": latest.get("task_id") or "",
        "prd_ref": ref,
        "prd_digest": digest,
        "bound_from_channel_prd_ref": latest["channel_prd_ref"],
        "binding_event_id": latest.get("event_id") or "",
        "body": body,
    }


def _read_json_artifact(
    state_dir: Path, ref: str, *, expected_digest: str | None = None,
) -> dict[str, Any] | None:
    ref = str(ref or "").strip()
    if not ref:
        return None
    root = Path(state_dir).resolve()
    path = (root / ref).resolve()
    if Path(ref).is_absolute() or not path.is_relative_to(root):
        raise ValueError("PRD artifact ref escapes state_dir")
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if expected_digest is not None:
        digest = expected_digest.strip().removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("canonical PRD digest missing, invalid or mismatched")
    text = raw.decode("utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Frozen PRD bodies are Markdown artifacts; keep them body-wrapped
        # so judges still receive the full text inline.
        return {"body": text} if text.strip() else None
    return payload if isinstance(payload, dict) else {"body": payload}


def _bare_digest(value: object) -> str:
    return str(value or "").strip().removeprefix("sha256:")


def _revision_value(value: object, *, fallback: object = None) -> int:
    """Coerce a revision identity; malformed or absent values fail closed."""

    candidate = value if value not in (None, "") else fallback
    return _strict_revision(candidate)


def _strict_revision(value: object) -> int:
    """Accept only a positive integer or its canonical decimal string form."""

    if isinstance(value, bool) or value is None:
        return -1
    if isinstance(value, int):
        return value if value > 0 else -1
    if isinstance(value, str) and _REVISION_RE.fullmatch(value):
        try:
            return int(value)
        except ValueError:
            return -1
    return -1


def _require_artifact_identity(
    payload: dict[str, Any],
    *,
    channel_id: str,
    thread_id: str,
    prd_revision: int,
    source_event_id: str = "",
    prd_ref: str = "",
    prd_digest: str = "",
    readiness_ref: str = "",
    readiness_digest: str = "",
    label: str,
) -> None:
    """Reject a sidecar whose body is not the exact frozen identity."""

    actual_channel = str(payload.get("channel_id") or "").strip()
    actual_thread = str(payload.get("thread_id") or "").strip()
    actual_revision = _strict_revision(
        payload.get("revision", payload.get("prd_revision"))
    )
    if actual_revision is None:
        raise ValueError(f"{label} artifact identity is missing or invalid") from None
    if (
        actual_channel != str(channel_id)
        or actual_thread != str(thread_id)
        or actual_revision != int(prd_revision)
    ):
        raise ValueError(f"{label} artifact identity does not match freeze")
    if source_event_id and str(payload.get("source_event_id") or "") != str(source_event_id):
        raise ValueError(f"{label} source event does not match freeze")
    if prd_ref and str(payload.get("prd_ref") or "") != str(prd_ref):
        raise ValueError(f"{label} PRD ref does not match freeze")
    if prd_digest and _bare_digest(payload.get("prd_digest")) != _bare_digest(prd_digest):
        raise ValueError(f"{label} PRD digest does not match freeze")
    if readiness_ref and str(payload.get("readiness_ref") or "") != str(readiness_ref):
        raise ValueError(f"{label} readiness ref does not match freeze")
    if readiness_digest and _bare_digest(payload.get("readiness_digest")) != _bare_digest(readiness_digest):
        raise ValueError(f"{label} readiness digest does not match freeze")


__all__ = [
    "WORKFLOW_BIND_EVENTS",
    "_bound_workflows",
    "_workflow_lineage_conflict",
    "_bound_prd_payload",
    "_read_json_artifact",
    "_bare_digest",
    "_revision_value",
    "_strict_revision",
    "_require_artifact_identity",
]
