"""Versioned, secret-free project Agent Profile proposals."""

from __future__ import annotations

import difflib
import json
import uuid
from pathlib import Path, PurePath
from typing import Any, Iterable, Mapping

from zf.runtime.channel_contracts import CHANNEL_PERMISSION_PROFILES
from zf.web.agent_artifact_store import AgentArtifactStore
from zf.web.agent_runtime_db import connect, digest_json, dumps, loads, transaction, utc_now
from zf.web.agent_tool_policy import AgentToolCatalog


KNOWN_PROVIDERS = frozenset({"claude-headless", "codex-headless", "opencode", "pi"})
PROFILE_ROLES = frozenset({"operator", "channel", "ephemeral", "delivery"})
_SECRET_FIELD_NAMES = frozenset({
    "secret", "token", "password", "api_key", "apikey", "credential", "credentials",
})
_SECRET_FIELD_SUFFIXES = (
    "_secret", "_password", "_api_key", "_token",
)
_PERMISSION_RANK = {
    "read_only": 0,
    "operator": 1,
    "artifact_writer": 2,
    "project_writer": 3,
    "workspace_writer": 4,
    "isolated_writer": 4,
    "dangerous_full": 5,
}


class AgentProfileConflict(RuntimeError):
    pass


class AgentProfileNotFound(KeyError):
    pass


def _contains_secret(value: Any, *, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SECRET_FIELD_NAMES or normalized.endswith(_SECRET_FIELD_SUFFIXES):
                found.append(child_path)
            else:
                found.extend(_contains_secret(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_contains_secret(child, path=f"{path}[{index}]"))
    return found


class AgentProfileBuilder:
    def __init__(self, state_dir: Path, *, project_root: Path) -> None:
        self.state_dir = Path(state_dir)
        self.project_root = Path(project_root).resolve()
        self.artifacts = AgentArtifactStore(self.state_dir, project_root=self.project_root)
        self.tools = AgentToolCatalog()
        with connect(self.state_dir):
            pass

    def catalog(self) -> dict[str, Any]:
        skills_root = self.project_root / "skills"
        skills = sorted(
            path.parent.name
            for path in skills_root.glob("*/SKILL.md")
            if path.is_file()
        ) if skills_root.exists() else []
        return {
            "schema_version": "agent-profile-catalog.v1",
            "providers": sorted(KNOWN_PROVIDERS),
            "roles": sorted(PROFILE_ROLES),
            "permission_profiles": sorted(CHANNEL_PERMISSION_PROFILES),
            "skills": skills,
            "tools": [item["name"] for item in self.tools.projection(surface="kanban_agent")["items"]],
            "contains_secrets": False,
        }

    def validate(
        self,
        profile: Mapping[str, Any],
        *,
        permission_ceiling: str = "workspace_writer",
        profile_id: str = "",
    ) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        provider = str(profile.get("provider") or "")
        if provider not in KNOWN_PROVIDERS:
            errors.append({"field": "provider", "reason": f"unknown provider {provider!r}"})
        elif profile_id == "kanban-agent" and provider not in {"claude-headless", "codex-headless"}:
            errors.append({
                "field": "provider",
                "reason": "kanban-agent profile requires a supported headless chat provider",
            })
        role = str(profile.get("role") or "operator")
        if role not in PROFILE_ROLES:
            errors.append({"field": "role", "reason": f"unknown role {role!r}"})
        if profile_id == "kanban-agent" and role != "operator":
            errors.append({"field": "role", "reason": "kanban-agent profile must use operator role"})
        permission = str(profile.get("permission_profile") or "read_only")
        if permission not in CHANNEL_PERMISSION_PROFILES:
            errors.append({"field": "permission_profile", "reason": "unknown permission profile"})
        elif _PERMISSION_RANK[permission] > _PERMISSION_RANK.get(permission_ceiling, -1):
            errors.append({"field": "permission_profile", "reason": "permission ceiling exceeded"})
        catalog = self.catalog()
        known_skills = set(catalog["skills"])
        for index, skill in enumerate(profile.get("skills") or []):
            if str(skill) not in known_skills:
                errors.append({"field": f"skills[{index}]", "reason": f"unknown skill {skill!r}"})
        known_tools = set(catalog["tools"])
        for index, tool in enumerate(profile.get("tools") or []):
            try:
                canonical = self.tools.resolve(str(tool)).name
            except KeyError:
                canonical = ""
            if not canonical or canonical not in known_tools:
                errors.append({"field": f"tools[{index}]", "reason": f"unknown tool {tool!r}"})
        for field in _contains_secret(profile):
            errors.append({"field": field, "reason": "secret fields are not allowed"})
        eligibility = profile.get("eligibility") if isinstance(profile.get("eligibility"), Mapping) else {}
        if role == "delivery" and not bool(eligibility.get("delivery")):
            errors.append({"field": "eligibility.delivery", "reason": "delivery role must declare delivery eligibility"})
        if role == "channel" and not bool(eligibility.get("channel")):
            errors.append({"field": "eligibility.channel", "reason": "channel role must declare channel eligibility"})
        if role == "ephemeral" and not bool(eligibility.get("ephemeral")):
            errors.append({"field": "eligibility.ephemeral", "reason": "ephemeral role must declare ephemeral eligibility"})
        path_policy = profile.get("path_policy") if isinstance(profile.get("path_policy"), Mapping) else {}
        for field in ("allow", "deny"):
            raw_paths = path_policy.get(field) or []
            if not isinstance(raw_paths, list):
                errors.append({"field": f"path_policy.{field}", "reason": "must be a list"})
                continue
            for index, raw_path in enumerate(raw_paths):
                path = str(raw_path or "").strip().replace("\\", "/")
                if not path or path.startswith("/") or ".." in PurePath(path).parts:
                    errors.append({
                        "field": f"path_policy.{field}[{index}]",
                        "reason": "path must be project-relative and cannot traverse parents",
                    })
        budget = profile.get("budget") if isinstance(profile.get("budget"), Mapping) else {}
        for field, value in budget.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                errors.append({"field": f"budget.{field}", "reason": "budget must be a non-negative number"})
        return {
            "schema_version": "agent-profile-validation.v1",
            "valid": not errors,
            "errors": errors,
            "profile_digest": digest_json(profile),
        }

    def current(self, profile_id: str) -> dict[str, Any] | None:
        with connect(self.state_dir) as conn:
            row = conn.execute(
                """
                SELECT * FROM webchat_profile_revision WHERE profile_id = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["profile"] = loads(item.pop("profile_json"), {})
        item["schema_version"] = "agent-profile-revision.v1"
        return item

    def propose(
        self,
        *,
        project_id: str,
        profile_id: str,
        base_profile: Mapping[str, Any],
        proposed_profile: Mapping[str, Any],
        permission_ceiling: str = "workspace_writer",
    ) -> tuple[dict[str, Any], bool]:
        current = self.current(profile_id)
        actual_base = current["profile"] if current else dict(base_profile)
        secret_fields = [
            *(_contains_secret(actual_base)),
            *(_contains_secret(proposed_profile)),
        ]
        if secret_fields:
            raise AgentProfileConflict(
                "profile proposal contains secret fields: " + ", ".join(secret_fields[:5])
            )
        base_digest = digest_json(actual_base)
        validation = self.validate(
            proposed_profile,
            permission_ceiling=permission_ceiling,
            profile_id=profile_id,
        )
        profile_digest = digest_json(proposed_profile)
        proposal_digest = digest_json({
            "profile_id": profile_id,
            "base_digest": base_digest,
            "profile_digest": profile_digest,
        })
        proposal_body = {
            "schema_version": "agent-profile-proposal.v1",
            "profile_id": profile_id,
            "base_digest": base_digest,
            "base_profile": actual_base,
            "proposed_profile": dict(proposed_profile),
            "profile_digest": profile_digest,
            "proposal_digest": proposal_digest,
            "validation": validation,
            "created_at": utc_now(),
        }
        with connect(self.state_dir) as conn:
            existing = conn.execute(
                "SELECT * FROM webchat_profile_proposal WHERE profile_id = ? AND proposal_digest = ?",
                (profile_id, proposal_digest),
            ).fetchone()
        if existing is not None:
            return self.get_proposal(str(existing["proposal_id"])), False
        body = json.dumps(proposal_body, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        artifact, _ = self.artifacts.capture_bytes(
            project_id=project_id,
            logical_name=f"agent-profiles/{profile_id}.proposal.json",
            data=body,
            media_type="application/json",
            producer={"agent_id": "agent-profile-builder"},
            lineage={"profile_id": profile_id},
        )
        proposal_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"{profile_id}:{proposal_digest}")
        proposal_id = f"profile-proposal-{proposal_uuid.hex}"
        now = utc_now()
        with transaction(self.state_dir) as conn:
            conn.execute(
                """
                INSERT INTO webchat_profile_proposal(
                  proposal_id, profile_id, base_digest, proposal_digest,
                  proposal_ref, status, validation_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?, ?)
                """,
                (
                    proposal_id, profile_id, base_digest,
                    proposal_body["proposal_digest"], artifact["storage_uri"],
                    dumps(validation), now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM webchat_profile_proposal WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            assert row is not None
        result = self._proposal(row)
        result["proposal"] = proposal_body
        result["diff"] = self.diff(actual_base, proposed_profile)
        return result, True

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        with connect(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM webchat_profile_proposal WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise AgentProfileNotFound(proposal_id)
        result = self._proposal(row)
        _, body = self.artifacts.read(
            str(result["proposal_ref"]).removeprefix("artifact://"),
            principal="agent-profile-builder",
        )
        result["proposal"] = json.loads(body.decode("utf-8"))
        result["diff"] = self.diff(
            result["proposal"]["base_profile"],
            result["proposal"]["proposed_profile"],
        )
        return result

    def apply(
        self,
        proposal_id: str,
        *,
        expected_base_digest: str,
        expected_proposal_digest: str,
    ) -> dict[str, Any]:
        proposal = self.get_proposal(proposal_id)
        if proposal["status"] == "applied":
            current = self.current(str(proposal["profile_id"]))
            assert current is not None
            return current
        if proposal["status"] != "proposed":
            raise AgentProfileConflict(f"proposal is {proposal['status']}")
        if proposal["base_digest"] != expected_base_digest:
            raise AgentProfileConflict("profile base digest mismatch")
        if proposal["proposal_digest"] != expected_proposal_digest:
            raise AgentProfileConflict("profile proposal digest mismatch")
        if not bool(proposal["validation"].get("valid")):
            raise AgentProfileConflict("invalid profile proposal cannot be applied")
        current = self.current(str(proposal["profile_id"]))
        current_digest = str(current["profile_digest"]) if current else expected_base_digest
        if current_digest != expected_base_digest:
            raise AgentProfileConflict("profile changed after proposal")
        profile = proposal["proposal"]["proposed_profile"]
        profile_digest = str(proposal["proposal"]["profile_digest"])
        now = utc_now()
        with transaction(self.state_dir) as conn:
            latest = conn.execute(
                """
                SELECT revision, profile_digest FROM webchat_profile_revision
                WHERE profile_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (proposal["profile_id"],),
            ).fetchone()
            latest_digest = str(latest["profile_digest"]) if latest is not None else expected_base_digest
            if latest_digest != expected_base_digest:
                raise AgentProfileConflict("profile changed after proposal")
            if latest is None or latest_digest != profile_digest:
                revision = int(latest["revision"] if latest is not None else 0) + 1
                conn.execute(
                    """
                    INSERT INTO webchat_profile_revision(
                      profile_id, revision, profile_digest, profile_json,
                      proposal_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal["profile_id"], revision, profile_digest,
                        dumps(profile), proposal_id, now,
                    ),
                )
            conn.execute(
                """
                UPDATE webchat_profile_proposal SET status = 'applied', updated_at = ?
                WHERE proposal_id = ? AND status = 'proposed'
                """,
                (now, proposal_id),
            )
        result = self.current(str(proposal["profile_id"]))
        assert result is not None
        return result

    @staticmethod
    def diff(base: Mapping[str, Any], proposed: Mapping[str, Any]) -> str:
        left = json.dumps(base, ensure_ascii=False, indent=2, sort_keys=True).splitlines(keepends=True)
        right = json.dumps(proposed, ensure_ascii=False, indent=2, sort_keys=True).splitlines(keepends=True)
        return "".join(difflib.unified_diff(left, right, fromfile="current", tofile="proposed"))

    @staticmethod
    def _proposal(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["validation"] = loads(item.pop("validation_json"), {})
        item["schema_version"] = "agent-profile-proposal-record.v1"
        return item


__all__ = [
    "AgentProfileBuilder",
    "AgentProfileConflict",
    "AgentProfileNotFound",
    "KNOWN_PROVIDERS",
    "PROFILE_ROLES",
]
