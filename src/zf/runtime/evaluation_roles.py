"""Versioned, read-only evaluator role catalog.

The Channel/PRD evaluator uses a small, closed set of task roles.  This
module is deliberately boring: it loads the three checked-in role documents,
binds their exact bytes to SHA-256 digests, and exposes only the three fixed
evaluation scopes.  It does not classify a task and it does not let a caller
provide an arbitrary role path or rubric.

The judge is responsible for returning a task type.  Runtime code should
validate that value and then call :func:`select_profile` against a frozen
catalog snapshot.  A missing or malformed role fails closed instead of using a
fallback prompt or silently inventing a rubric.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


ROLE_CATALOG_VERSION = "evaluation-role-catalog.v1"
ROLE_REF_PREFIX = "evaluation_roles/"
TASK_TYPES: tuple[str, ...] = ("bugfix", "refactor", "greenfield")
PROFILE_SCOPES: tuple[str, ...] = ("prd_fidelity", "discussion", "contribution")

_ROLE_FILES = MappingProxyType({task_type: f"{task_type}.md" for task_type in TASK_TYPES})
_META_RE = re.compile(r"^<!--\s*([a-z_]+)\s*:\s*(.*?)\s*-->\s*$")
_PROFILE_HEADING = re.compile(r"^###\s+`([^`]+)`\s*$")
_META_KEYS = frozenset({"role_id", "task_type", "role_version", "catalog_version"})


class EvaluationRoleError(ValueError):
    """Raised when the built-in role catalog cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class EvaluationRole:
    """One immutable role document and its content digest."""

    task_type: str
    role_ref: str
    role_version: str
    role_digest: str
    body: str
    profiles: tuple[str, ...] = PROFILE_SCOPES
    catalog_version: str = ROLE_CATALOG_VERSION

    @property
    def version(self) -> str:
        """Compatibility alias for callers that use ``version``."""

        return self.role_version

    @property
    def digest(self) -> str:
        """Compatibility alias for callers that use ``digest``."""

        return self.role_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "role_ref": self.role_ref,
            "role_version": self.role_version,
            "role_digest": self.role_digest,
            "version": self.role_version,
            "digest": self.role_digest,
            "body": self.body,
            "profiles": list(self.profiles),
            "catalog_version": self.catalog_version,
        }


class EvaluationRoleCatalog:
    """Read-only loader for the allowlisted evaluator roles.

    ``role_dir`` exists to make isolated tests possible.  Even with a custom
    directory, only the three fixed filenames are accepted and every document
    must contain the expected version metadata and all three profile blocks.
    """

    def __init__(self, role_dir: Path | None = None) -> None:
        self._role_dir = (
            Path(role_dir).resolve()
            if role_dir is not None
            else Path(__file__).resolve().parent / "evaluation_roles"
        )

    @property
    def role_dir(self) -> Path:
        return self._role_dir

    @property
    def task_types(self) -> tuple[str, ...]:
        return TASK_TYPES

    def load(self, task_type: str) -> EvaluationRole:
        """Load one allowlisted role from its exact checked-in path."""

        normalized = _normalize_task_type(task_type)
        filename = _ROLE_FILES.get(normalized)
        if filename is None:
            raise EvaluationRoleError(f"unknown evaluation task type: {task_type!r}")
        path = (self._role_dir / filename).resolve()
        try:
            path.relative_to(self._role_dir)
        except ValueError as exc:
            raise EvaluationRoleError("evaluation role escaped catalog directory") from exc
        if not path.is_file():
            raise EvaluationRoleError(f"evaluation role is missing: {ROLE_REF_PREFIX}{filename}")
        try:
            raw = path.read_bytes()
            body = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise EvaluationRoleError(
                f"evaluation role is unreadable: {ROLE_REF_PREFIX}{filename}"
            ) from exc

        metadata = _parse_metadata(body)
        missing = _META_KEYS.difference(metadata)
        if missing:
            raise EvaluationRoleError(
                f"evaluation role {ROLE_REF_PREFIX}{filename} missing metadata: "
                + ", ".join(sorted(missing))
            )
        if any(not metadata[key].strip() for key in _META_KEYS):
            raise EvaluationRoleError(
                f"evaluation role {ROLE_REF_PREFIX}{filename} has empty metadata"
            )
        if metadata["role_id"] != normalized or metadata["task_type"] != normalized:
            raise EvaluationRoleError(
                f"evaluation role {ROLE_REF_PREFIX}{filename} has mismatched task_type"
            )
        if metadata["catalog_version"] != ROLE_CATALOG_VERSION:
            raise EvaluationRoleError(
                f"evaluation role {ROLE_REF_PREFIX}{filename} has unsupported catalog_version"
            )
        profiles = _profile_scopes(body)
        if profiles != PROFILE_SCOPES:
            raise EvaluationRoleError(
                f"evaluation role {ROLE_REF_PREFIX}{filename} must define scopes "
                + ", ".join(PROFILE_SCOPES)
            )
        return EvaluationRole(
            task_type=normalized,
            role_ref=f"{ROLE_REF_PREFIX}{filename}",
            role_version=metadata["role_version"],
            role_digest=hashlib.sha256(raw).hexdigest(),
            body=body,
            profiles=profiles,
            catalog_version=metadata["catalog_version"],
        )

    def load_all(self) -> dict[str, EvaluationRole]:
        """Load every role; any missing or invalid role aborts the load."""

        return {task_type: self.load(task_type) for task_type in TASK_TYPES}

    def snapshot(self) -> dict[str, Any]:
        """Return a self-contained, digest-bound catalog snapshot.

        The returned mapping contains fresh lists/dicts and can be serialized
        into a frozen packet.  The loader never writes or mutates role files.
        """

        roles = self.load_all()
        role_payload = {task_type: roles[task_type].to_dict() for task_type in TASK_TYPES}
        digest_payload = {
            "catalog_version": ROLE_CATALOG_VERSION,
            "roles": role_payload,
        }
        catalog_digest = _sha256_json(digest_payload)
        return {
            "schema_version": ROLE_CATALOG_VERSION,
            "catalog_version": ROLE_CATALOG_VERSION,
            "catalog_digest": catalog_digest,
            "roles": role_payload,
        }


_DEFAULT_CATALOG: EvaluationRoleCatalog | None = None


def default_catalog() -> EvaluationRoleCatalog:
    """Return the process-local built-in catalog loader."""

    global _DEFAULT_CATALOG
    if _DEFAULT_CATALOG is None:
        _DEFAULT_CATALOG = EvaluationRoleCatalog()
    return _DEFAULT_CATALOG


def snapshot_catalog() -> dict[str, Any]:
    """Snapshot all built-in roles, including body, version and digests."""

    return default_catalog().snapshot()


def select_profile(
    catalog: Mapping[str, Any] | EvaluationRoleCatalog,
    task_type: str,
    scope: str,
) -> dict[str, Any]:
    """Select one fixed evaluator profile from a frozen role catalog.

    ``catalog`` may be the dictionary returned by :func:`snapshot_catalog` or
    an :class:`EvaluationRoleCatalog` (useful before packet freeze).  A
    snapshot's role digest is verified against its body before it is returned;
    tampering or an unknown task/scope raises :class:`EvaluationRoleError`.
    """

    normalized_task = _normalize_task_type(task_type)
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in PROFILE_SCOPES:
        raise EvaluationRoleError(f"unknown evaluation profile scope: {scope!r}")

    if isinstance(catalog, EvaluationRoleCatalog):
        role = catalog.load(normalized_task)
        role_data = role.to_dict()
    else:
        _validate_snapshot(catalog)
        roles = catalog.get("roles") if isinstance(catalog, Mapping) else None
        if not isinstance(roles, Mapping):
            raise EvaluationRoleError("evaluation catalog snapshot has no roles mapping")
        raw_role = roles.get(normalized_task)
        if not isinstance(raw_role, Mapping):
            raise EvaluationRoleError(f"evaluation role is not registered: {normalized_task}")
        role_data = _validate_snapshot_role(raw_role, normalized_task)

    body = str(role_data["body"])
    profile_body = _profile_body(body, normalized_scope)
    return {
        "task_type": normalized_task,
        "scope": normalized_scope,
        "profile_id": f"{normalized_task}.{normalized_scope}",
        "role_ref": str(role_data["role_ref"]),
        "role_version": str(role_data["role_version"]),
        "role_digest": str(role_data["role_digest"]),
        "version": str(role_data["role_version"]),
        "digest": str(role_data["role_digest"]),
        "body": profile_body,
    }


def _normalize_task_type(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in TASK_TYPES:
        raise EvaluationRoleError(f"unknown evaluation task type: {value!r}")
    return normalized


def _parse_metadata(body: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in body.splitlines():
        match = _META_RE.match(line)
        if match:
            if match.group(1) in metadata:
                raise EvaluationRoleError(f"duplicate evaluation role metadata: {match.group(1)}")
            metadata[match.group(1)] = match.group(2).strip()
    return metadata


def _profile_scopes(body: str) -> tuple[str, ...]:
    found: list[str] = []
    for line in body.splitlines():
        match = _PROFILE_HEADING.match(line.strip())
        if match:
            found.append(match.group(1))
    return tuple(found)


def _profile_body(body: str, scope: str) -> str:
    lines = body.splitlines(keepends=True)
    first_profile: int | None = None
    start: int | None = None
    end: int | None = None
    for index, line in enumerate(lines):
        match = _PROFILE_HEADING.match(line.strip())
        if not match:
            continue
        if first_profile is None:
            first_profile = index
        if match.group(1) == scope:
            start = index
        elif start is not None:
            end = index
            break
    if start is None:
        raise EvaluationRoleError(f"evaluation profile is missing: {scope}")
    # Keep the role's task scope, common evidence contract, and score anchors
    # with the selected block.  Omit every other profile block so subagents
    # remain independent.  Metadata/title are harmless and keep the prompt
    # self-describing when this body is persisted as a sidecar.
    prefix = lines[:first_profile] if first_profile is not None else []
    selected = "".join(prefix + lines[start:end]).strip()
    if not selected:
        raise EvaluationRoleError(f"evaluation profile is empty: {scope}")
    return selected


def _validate_snapshot_role(raw_role: Mapping[str, Any], task_type: str) -> dict[str, Any]:
    required = ("task_type", "role_ref", "role_version", "role_digest", "body")
    if any(not str(raw_role.get(key) or "").strip() for key in required):
        raise EvaluationRoleError(f"evaluation role snapshot is incomplete: {task_type}")
    if str(raw_role["task_type"]) != task_type:
        raise EvaluationRoleError(f"evaluation role snapshot task_type mismatch: {task_type}")
    expected_ref = f"{ROLE_REF_PREFIX}{task_type}.md"
    if str(raw_role["role_ref"]) != expected_ref:
        raise EvaluationRoleError(f"evaluation role snapshot role_ref mismatch: {task_type}")
    if str(raw_role.get("catalog_version") or "") != ROLE_CATALOG_VERSION:
        raise EvaluationRoleError(f"evaluation role snapshot catalog_version mismatch: {task_type}")
    if "version" in raw_role and str(raw_role["version"]) != str(raw_role["role_version"]):
        raise EvaluationRoleError(f"evaluation role snapshot version mismatch: {task_type}")
    if "digest" in raw_role and str(raw_role["digest"]) != str(raw_role["role_digest"]):
        raise EvaluationRoleError(f"evaluation role snapshot digest alias mismatch: {task_type}")
    if "profiles" in raw_role and tuple(raw_role["profiles"] or ()) != PROFILE_SCOPES:
        raise EvaluationRoleError(f"evaluation role snapshot profiles mismatch: {task_type}")
    body = str(raw_role["body"])
    actual_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if actual_digest != str(raw_role["role_digest"]):
        raise EvaluationRoleError(f"evaluation role snapshot digest mismatch: {task_type}")
    if _profile_scopes(body) != PROFILE_SCOPES:
        raise EvaluationRoleError(f"evaluation role snapshot scopes mismatch: {task_type}")
    return {key: raw_role[key] for key in required}


def _validate_snapshot(catalog: Mapping[str, Any]) -> None:
    if str(catalog.get("schema_version") or "") != ROLE_CATALOG_VERSION:
        raise EvaluationRoleError("evaluation catalog snapshot schema_version mismatch")
    if str(catalog.get("catalog_version") or "") != ROLE_CATALOG_VERSION:
        raise EvaluationRoleError("evaluation catalog snapshot catalog_version mismatch")
    roles = catalog.get("roles")
    if not isinstance(roles, Mapping) or set(roles) != set(TASK_TYPES):
        raise EvaluationRoleError("evaluation catalog snapshot must contain the fixed role allowlist")
    role_payload: dict[str, Any] = {}
    for task_type in TASK_TYPES:
        raw_role = roles.get(task_type)
        if not isinstance(raw_role, Mapping):
            raise EvaluationRoleError(f"evaluation catalog snapshot role is invalid: {task_type}")
        _validate_snapshot_role(raw_role, task_type)
        role_payload[task_type] = dict(raw_role)
    expected = _sha256_json({"catalog_version": ROLE_CATALOG_VERSION, "roles": role_payload})
    if expected != str(catalog.get("catalog_digest") or ""):
        raise EvaluationRoleError("evaluation catalog snapshot digest mismatch")


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "EvaluationRole",
    "EvaluationRoleCatalog",
    "EvaluationRoleError",
    "PROFILE_SCOPES",
    "ROLE_CATALOG_VERSION",
    "ROLE_REF_PREFIX",
    "TASK_TYPES",
    "default_catalog",
    "select_profile",
    "snapshot_catalog",
]
