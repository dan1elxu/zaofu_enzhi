"""Versioned built-in Agent Channel templates."""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from typing import Any

from zf.runtime.channel_contracts import (
    CHANNEL_PERMISSION_PROFILES,
    CHANNEL_ROLES,
    normalize_permission_profile,
    normalize_provider,
)


TEMPLATE_VERSION = "2026-07-31.1"
WRITER_PROFILES = {
    "artifact_writer",
    "project_writer",
    "workspace_writer",
    "isolated_writer",
    "dangerous_full",
}
ALLOWED_OVERRIDE_KEYS = {
    "backend",
    "model",
    "role_overrides",
    "writer_role",
    "writer_scope",
    "budget",
    "discussion_mode",
}
# ``member_selection`` is an auto-template-only extension (see
# ``_resolve_member_selection``). It is popped from the overrides BEFORE the
# ALLOWED_OVERRIDE_KEYS check so this whitelist and every other template's
# behavior stay untouched; non-auto templates that receive it are rejected
# explicitly inside materialization.
AUTO_MEMBER_SELECTION_MAX = 10
AUTO_MEMBER_SELECTION_FORBIDDEN = frozenset({"owner_delegate"})
AUTO_MEMBER_WRITER_ROLES = frozenset({"product_pm"})
_AUTO_MEMBER_EXTRA_SKILLS: dict[str, tuple[str, ...]] = {
    "synthesizer": ("zf-channel-discussion-synthesizer",),
    "security_reviewer": ("zf-channel-security-review",),
    "researcher": ("zf-channel-research-participant",),
}
_BASE_MEMBER_SKILL = "zf-channel-discussion-participant"
ALLOWED_ROLE_OVERRIDE_KEYS = {"backend", "model", "enabled", "profile_id"}
ALLOWED_BUDGET_KEYS = {"max_rounds", "max_parallel_replies", "phase_deadline_seconds"}
ALLOWED_DEADLINE_PHASES = {
    "phase1_blind",
    "phase2_relay",
    "phase3_synthesis",
}
PROFILE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def _member(
    role: str,
    *,
    permission_profile: str = "read_only",
    skills: tuple[str, ...] = (),
    optional: bool = False,
) -> dict[str, Any]:
    context_name = role.replace("_", "-")
    skill_refs = list(
        dict.fromkeys(f"skills/{skill}/SKILL.md" for skill in skills if skill.strip())
    )
    return {
        "member_id": role,
        "channel_role": role,
        "permission_profile": permission_profile,
        "role_context_ref": f"channel_roles/{context_name}.md",
        "skill_refs": skill_refs,
        "optional": optional,
    }


CHANNEL_TEMPLATES: dict[str, dict[str, Any]] = {
    "prd-clarification": {
        "name": "PRD Clarification",
        "members": [
            _member(
                "product_pm",
                permission_profile="project_writer",
                skills=("zf-channel-discussion-participant",),
            ),
            _member("arch", skills=("zf-channel-discussion-participant",)),
            _member(
                "critic",
                skills=("zf-channel-discussion-participant",),
            ),
            _member(
                "synthesizer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-discussion-synthesizer",
                ),
            ),
            _member(
                "security_reviewer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-security-review",
                ),
                optional=True,
            ),
        ],
        "writer_roles": ["product_pm"],
        "writer_scope": ["docs/design/**", "docs/impl/**"],
        "discussion": {
            "mode": "conversation",
            "synthesizer": "synthesizer",
        },
    },
    "research-review": {
        "name": "Research Review",
        "members": [
            _member(
                "researcher",
                permission_profile="artifact_writer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-research-participant",
                    "zf-research-preflight-law",
                    "source-verification",
                ),
            ),
            _member(
                "arch",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-research-participant",
                    "zf-research-preflight-law",
                    "source-verification",
                ),
            ),
            _member(
                "critic",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-research-participant",
                    "zf-research-preflight-law",
                ),
            ),
            _member(
                "synthesizer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-research-synthesizer",
                    "zf-channel-discussion-synthesizer",
                    "zf-research-preflight-law",
                ),
            ),
        ],
        "writer_roles": ["researcher"],
        "writer_scope": [".zf/research/**", "/tmp/zf-research/**"],
        "discussion": {
            "mode": "conversation",
            "synthesizer": "synthesizer",
        },
    },
    "architecture-review": {
        "name": "Architecture Review",
        "members": [
            _member(
                "arch",
                permission_profile="project_writer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-discussion-synthesizer",
                    "zf-cr",
                    "zf-harness-design-impl-game-review",
                ),
            ),
            _member(
                "security_reviewer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-security-review",
                    "zf-harness-evidence-collection",
                ),
            ),
            _member(
                "dev_reviewer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-harness-design-impl-game-review",
                ),
            ),
            _member(
                "critic",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-harness-gate-evaluator",
                ),
            ),
        ],
        "writer_roles": ["arch"],
        "writer_scope": ["docs/design/**", "docs/impl/**"],
        "discussion": {
            "mode": "multi_lens",
            "synthesizer": "arch",
        },
    },
    "quick-change": {
        "name": "Quick Change",
        "members": [
            _member(
                "tech_leader",
                permission_profile="workspace_writer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-quick-change",
                    "zf-channel-discussion-synthesizer",
                ),
            ),
            _member(
                "dev_reviewer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-quick-change",
                ),
            ),
            _member(
                "qa_analyst",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-quick-change",
                    "zf-harness-verification-checklist",
                ),
            ),
        ],
        "writer_roles": ["tech_leader"],
        "writer_scope": ["**"],
        "discussion": {
            "mode": "conversation",
            "synthesizer": "tech_leader",
        },
    },
    "incident-triage": {
        "name": "Incident Triage",
        "members": [
            _member(
                "tech_leader",
                permission_profile="workspace_writer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-incident-triage",
                    "zf-channel-discussion-synthesizer",
                ),
            ),
            _member(
                "qa_analyst",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-incident-triage",
                    "zf-harness-evidence-collection",
                ),
            ),
            _member(
                "security_reviewer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-incident-triage",
                    "zf-channel-security-review",
                ),
                optional=True,
            ),
        ],
        "writer_roles": ["tech_leader"],
        "writer_scope": ["**"],
        "discussion": {
            "mode": "clarification",
            "synthesizer": "tech_leader",
        },
    },
    "auto": {
        "name": "Auto Deliberation",
        "members": [
            _member(
                "product_pm",
                permission_profile="project_writer",
                skills=("zf-channel-discussion-participant",),
            ),
            _member("arch", skills=("zf-channel-discussion-participant",)),
            _member(
                "critic",
                skills=("zf-channel-discussion-participant",),
            ),
            _member(
                "synthesizer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-discussion-synthesizer",
                ),
            ),
            _member(
                "security_reviewer",
                skills=(
                    "zf-channel-discussion-participant",
                    "zf-channel-security-review",
                ),
                optional=True,
            ),
        ],
        "writer_roles": ["product_pm"],
        "writer_scope": ["docs/design/**", "docs/impl/**"],
        "discussion": {
            "mode": "multi_lens",
            "synthesizer": "synthesizer",
            "auto": True,
            "moderator": True,
        },
    },
}


def template_digest(template_id: str) -> str:
    template = CHANNEL_TEMPLATES[template_id]
    canonical = json.dumps(
        {"version": TEMPLATE_VERSION, "template": template},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def channel_template_default_backend(config: object | None = None) -> str:
    """Resolve the default member backend for template materialization.

    Mirrors the web/Kanban agent backend selection order: explicit env
    override first, then the configured orchestrator backend, then the
    historical codex default.
    """
    raw = os.environ.get("ZF_KANBAN_AGENT_BACKEND", "")
    if not raw.strip() and config is not None:
        raw = str(
            getattr(getattr(config, "orchestrator", None), "backend", "")
            or ""
        )
    return normalize_provider(raw) or "codex"


def _resolve_member_selection(
    selection: object,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Validate an auto-template ``member_selection`` override and build members.

    ``selection`` is ``{"roles": [<channel_role>, ...]}``. Every role must be
    a registered CHANNEL_ROLES entry; ``synthesizer`` is mandatory because
    the auto discussion mode converges through it; ``owner_delegate`` is
    excluded (owner authority in auto mode comes from the pre-approved
    Plan, not from a channel member). Members keep the template permission
    ceiling: ``read_only`` by default, ``project_writer`` only for
    ``product_pm`` — the kanban agent chooses WHO discusses, never their
    write reach.
    """

    if not isinstance(selection, dict):
        return None, "member_selection must be a mapping"
    roles = selection.get("roles")
    if not isinstance(roles, list) or not roles:
        return None, "member_selection.roles must be a non-empty role list"
    normalized: list[str] = []
    for raw in roles:
        role = str(raw or "").strip()
        if not role:
            return None, "member_selection.roles entries must be non-empty strings"
        if role not in CHANNEL_ROLES:
            return None, f"unknown channel role in member_selection: {role}"
        if role in AUTO_MEMBER_SELECTION_FORBIDDEN:
            return None, f"role is not allowed in member_selection: {role}"
        if role not in normalized:
            normalized.append(role)
    if len(normalized) > AUTO_MEMBER_SELECTION_MAX:
        return None, (
            f"member_selection supports at most "
            f"{AUTO_MEMBER_SELECTION_MAX} roles"
        )
    if "synthesizer" not in normalized:
        return None, (
            "member_selection must include synthesizer "
            "(required for PRD convergence)"
        )
    members: list[dict[str, Any]] = []
    for role in normalized:
        skills = [_BASE_MEMBER_SKILL, *_AUTO_MEMBER_EXTRA_SKILLS.get(role, ())]
        members.append(
            _member(
                role,
                permission_profile=(
                    "project_writer"
                    if role in AUTO_MEMBER_WRITER_ROLES
                    else "read_only"
                ),
                skills=tuple(skills),
            )
        )
    return members, ""


def materialize_channel_template(
    template_id: str,
    *,
    overrides: object = None,
    default_backend: object = "codex",
) -> tuple[dict[str, Any] | None, str]:
    if template_id not in CHANNEL_TEMPLATES:
        return None, f"unknown channel template: {template_id}"
    override_map = dict(overrides) if isinstance(overrides, dict) else {}
    member_selection = override_map.pop("member_selection", None)
    unknown = sorted(set(override_map) - ALLOWED_OVERRIDE_KEYS)
    if unknown:
        return None, f"unsupported channel template override: {', '.join(unknown)}"

    result = deepcopy(CHANNEL_TEMPLATES[template_id])
    discussion_mode = str(override_map.get("discussion_mode") or "").strip()
    if discussion_mode:
        if discussion_mode == "auto":
            result.setdefault("discussion", {})["mode"] = "multi_lens"
            result["discussion"]["auto"] = True
            result["discussion"]["moderator"] = True
        elif discussion_mode not in {"conversation", "clarification", "multi_lens"}:
            return None, (
                "discussion_mode must be conversation, clarification, "
                "multi_lens, or auto"
            )
        else:
            result.setdefault("discussion", {})["mode"] = discussion_mode

    if member_selection is not None:
        # Auto-only: the kanban agent picks the roster itself. The picked
        # member list replaces the template roster BEFORE the standard
        # materialization pass, so backend filling, role overrides, leader
        # wiring, budgets, and the materialization digest all apply to the
        # picked roster unchanged.
        if template_id != "auto":
            return None, (
                "member_selection is only supported by the auto template"
            )
        selected_members, selection_error = _resolve_member_selection(
            member_selection
        )
        if selection_error:
            return None, selection_error
        result["members"] = selected_members
        selected_roles = {
            str(member["channel_role"]) for member in selected_members
        }
        # Keep writer_roles consistent with the picked roster: with no
        # product_pm the leader wiring falls back to the synthesizer.
        result["writer_roles"] = [
            role
            for role in result.get("writer_roles", [])
            if role in selected_roles
        ]

    members = result["members"]
    by_role = {str(member["channel_role"]): member for member in members}
    backend = str(override_map.get("backend") or default_backend or "codex").strip()
    provider = normalize_provider(backend)
    if not provider:
        return None, f"unsupported channel backend: {backend}"
    model = str(override_map.get("model") or "").strip()
    role_overrides = override_map.get("role_overrides") or {}
    if not isinstance(role_overrides, dict):
        return None, "role_overrides must be a mapping"
    for role, raw in role_overrides.items():
        if role not in by_role or role not in CHANNEL_ROLES:
            return None, f"unknown template role override: {role}"
        if not isinstance(raw, dict):
            return None, f"role_overrides.{role} must be a mapping"
        unknown_role_keys = sorted(set(raw) - ALLOWED_ROLE_OVERRIDE_KEYS)
        if unknown_role_keys:
            return None, (
                f"unsupported role override for {role}: "
                + ", ".join(unknown_role_keys)
            )
        if "enabled" in raw and not bool(raw.get("enabled")):
            if not bool(by_role[role].get("optional")):
                return None, f"required template role cannot be disabled: {role}"
            by_role[role]["enabled"] = False
        if "profile_id" in raw:
            profile_id = str(raw.get("profile_id") or "").strip()
            if not PROFILE_ID_RE.fullmatch(profile_id):
                return None, (
                    f"role_overrides.{role}.profile_id must be a valid "
                    "Channel profile id"
                )
            by_role[role]["profile_id"] = profile_id
            by_role[role]["profile_selection_explicit"] = True
        role_backend = str(raw.get("backend") or backend).strip()
        if not normalize_provider(role_backend):
            return None, f"unsupported channel backend for {role}: {role_backend}"
        by_role[role]["backend"] = role_backend
        by_role[role]["provider"] = normalize_provider(role_backend)
        by_role[role]["model"] = str(raw.get("model") or model).strip()

    enabled_members: list[dict[str, Any]] = []
    for member in members:
        if member.get("enabled") is False:
            continue
        member.setdefault("backend", backend)
        member.setdefault("provider", provider)
        member.setdefault("model", model)
        member["member_type"] = "provider_agent"
        member.setdefault("profile_id", str(member["member_id"]))
        member["profile_revision"] = 1
        member["profile_provenance"] = "template_inline"
        member["permission_profile"] = "read_only"
        member["permission_ceiling"] = "read_only"
        member["permissions"] = ["read", "message", "summarize"]
        enabled_members.append(member)
    result["members"] = enabled_members

    writer_role = str(
        override_map.get("writer_role")
        or next(iter(result.get("writer_roles") or []), "")
    ).strip()
    if writer_role and writer_role not in result.get("writer_roles", []):
        return None, f"writer_role is not allowed by template: {writer_role}"
    writer_scope = override_map.get("writer_scope", result.get("writer_scope", []))
    if (
        not isinstance(writer_scope, list)
        or not writer_scope
        or not all(
            isinstance(item, str) and item.strip() for item in writer_scope
        )
    ):
        return None, "writer_scope must be a non-empty string list"
    leader_member_id = writer_role or str(
        result.get("discussion", {}).get("synthesizer") or ""
    )
    for member in enabled_members:
        role = str(member["channel_role"])
        profile = normalize_permission_profile(member.get("permission_profile"))
        member["writer_scope"] = []
        if str(member["member_id"]) == leader_member_id:
            member["permissions"] = [
                "read",
                "message",
                "summarize",
                "propose_workflow",
            ]
        if normalize_permission_profile(member["permission_profile"]) not in (
            CHANNEL_PERMISSION_PROFILES
        ):
            return None, f"unknown permission profile for role: {role}"
    budget = override_map.get("budget") or {}
    if not isinstance(budget, dict):
        return None, "budget must be a mapping"
    unknown_budget = sorted(set(budget) - ALLOWED_BUDGET_KEYS)
    if unknown_budget:
        return None, f"unsupported channel budget override: {', '.join(unknown_budget)}"
    max_rounds_explicit = budget.get("max_rounds") not in (None, "")
    max_rounds = 0
    if max_rounds_explicit:
        max_rounds, error = _bounded_budget_int(
            budget.get("max_rounds"),
            default=0,
            minimum=1,
            maximum=256,
            field="max_rounds",
        )
        if error:
            return None, error
    max_parallel_replies, error = _bounded_budget_int(
        budget.get("max_parallel_replies"),
        default=len(enabled_members),
        minimum=1,
        maximum=max(1, len(enabled_members)),
        field="max_parallel_replies",
    )
    if error:
        return None, error
    deadlines, error = _phase_deadlines(
        budget.get("phase_deadline_seconds")
    )
    if error:
        return None, error
    discussion = result["discussion"]
    discussion["participants"] = [
        str(member["member_id"]) for member in enabled_members
    ]
    discussion["default_responder_id"] = str(
        discussion.get("default_responder_id")
        or discussion.get("synthesizer")
        or writer_role
        or ""
    )
    discussion["max_rounds"] = max_rounds
    discussion["max_rounds_explicit"] = max_rounds_explicit
    discussion["max_parallel_replies"] = max_parallel_replies
    if deadlines:
        discussion["phase_deadline_seconds"] = deadlines
    materialization_digest = _template_materialization_digest(
        template_id,
        result,
    )
    result.update({
        "template_id": template_id,
        "template_version": TEMPLATE_VERSION,
        "template_digest": template_digest(template_id),
        "materialization_digest": materialization_digest,
        "writer_role": writer_role,
        "writer_scope": list(writer_scope),
        "leader_member_id": leader_member_id,
    })
    return result, ""


def _bounded_budget_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
    field: str,
) -> tuple[int, str]:
    if value in (None, ""):
        return default, ""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0, f"budget.{field} must be an integer"
    if parsed < minimum or parsed > maximum:
        return 0, (
            f"budget.{field} must be between {minimum} and {maximum}"
        )
    return parsed, ""


def _template_materialization_digest(
    template_id: str,
    materialized: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "template_id": template_id,
            "version": TEMPLATE_VERSION,
            "materialized": materialized,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _phase_deadlines(value: object) -> tuple[dict[str, int], str]:
    if value in (None, ""):
        return {}, ""
    if not isinstance(value, dict):
        return {}, "budget.phase_deadline_seconds must be a mapping"
    unknown = sorted(set(value) - ALLOWED_DEADLINE_PHASES)
    if unknown:
        return {}, (
            "unsupported phase deadline: " + ", ".join(unknown)
        )
    deadlines: dict[str, int] = {}
    for phase, raw in value.items():
        parsed, error = _bounded_budget_int(
            raw,
            default=0,
            minimum=1,
            maximum=86400,
            field=f"phase_deadline_seconds.{phase}",
        )
        if error:
            return {}, error
        deadlines[str(phase)] = parsed
    return deadlines, ""


__all__ = [
    "CHANNEL_TEMPLATES",
    "TEMPLATE_VERSION",
    "channel_template_default_backend",
    "materialize_channel_template",
    "template_digest",
]
