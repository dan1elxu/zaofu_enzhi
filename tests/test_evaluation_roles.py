from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zf.runtime.evaluation_roles import (
    EvaluationRoleCatalog,
    EvaluationRoleError,
    PROFILE_SCOPES,
    ROLE_CATALOG_VERSION,
    TASK_TYPES,
    select_profile,
    snapshot_catalog,
)


def test_builtin_catalog_is_complete_and_digest_bound() -> None:
    catalog = snapshot_catalog()

    assert catalog["schema_version"] == ROLE_CATALOG_VERSION
    assert catalog["catalog_version"] == ROLE_CATALOG_VERSION
    assert set(catalog["roles"]) == set(TASK_TYPES)
    for task_type in TASK_TYPES:
        role = catalog["roles"][task_type]
        assert role["task_type"] == task_type
        assert role["role_ref"] == f"evaluation_roles/{task_type}.md"
        assert role["role_version"]
        assert role["version"] == role["role_version"]
        assert role["role_digest"] == hashlib.sha256(
            role["body"].encode("utf-8")
        ).hexdigest()
        assert role["digest"] == role["role_digest"]
        for scope in PROFILE_SCOPES:
            assert f"### `{scope}`" in role["body"]


def test_select_profile_returns_only_requested_scope() -> None:
    catalog = snapshot_catalog()
    profile = select_profile(catalog, "bugfix", "contribution")

    assert profile["task_type"] == "bugfix"
    assert profile["scope"] == "contribution"
    assert profile["profile_id"] == "bugfix.contribution"
    assert profile["role_ref"] == "evaluation_roles/bugfix.md"
    assert profile["role_version"] == "evaluation-role-bugfix.v1"
    assert profile["role_digest"] == profile["digest"]
    assert "## 共同评估契约" in profile["body"]
    assert "## 统一评分锚点" in profile["body"]
    assert "### `contribution`" in profile["body"]
    assert "### `prd_fidelity`" not in profile["body"]
    assert "### `discussion`" not in profile["body"]


def test_catalog_rejects_unknown_task_or_scope() -> None:
    catalog = snapshot_catalog()

    with pytest.raises(EvaluationRoleError):
        select_profile(catalog, "hotfix", "contribution")
    with pytest.raises(EvaluationRoleError):
        select_profile(catalog, "bugfix", "review")


def test_snapshot_tampering_fails_closed() -> None:
    catalog = snapshot_catalog()
    catalog["roles"]["bugfix"]["body"] += "\nchanged"

    with pytest.raises(EvaluationRoleError, match="digest mismatch"):
        select_profile(catalog, "bugfix", "prd_fidelity")


def test_missing_role_fails_closed(tmp_path: Path) -> None:
    role_dir = tmp_path / "evaluation_roles"
    role_dir.mkdir()
    for task_type in TASK_TYPES:
        source = Path("src/zf/runtime/evaluation_roles") / f"{task_type}.md"
        (role_dir / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (role_dir / "refactor.md").unlink()

    with pytest.raises(EvaluationRoleError, match="missing"):
        EvaluationRoleCatalog(role_dir).snapshot()


def test_role_metadata_tampering_fails_closed(tmp_path: Path) -> None:
    role_dir = tmp_path / "evaluation_roles"
    role_dir.mkdir()
    for task_type in TASK_TYPES:
        source = Path("src/zf/runtime/evaluation_roles") / f"{task_type}.md"
        body = source.read_text(encoding="utf-8")
        if task_type == "bugfix":
            body = body.replace(
                "<!-- role_id: bugfix -->",
                "<!-- role_id: bugfix -->\n<!-- role_id: refactor -->",
                1,
            )
        (role_dir / source.name).write_text(body, encoding="utf-8")

    with pytest.raises(EvaluationRoleError, match="duplicate"):
        EvaluationRoleCatalog(role_dir).snapshot()


def test_extra_or_duplicate_profile_fails_closed(tmp_path: Path) -> None:
    role_dir = tmp_path / "evaluation_roles"
    role_dir.mkdir()
    for task_type in TASK_TYPES:
        source = Path("src/zf/runtime/evaluation_roles") / f"{task_type}.md"
        body = source.read_text(encoding="utf-8")
        if task_type == "bugfix":
            body += "\n### `unregistered`\nDo not use this profile.\n"
        (role_dir / source.name).write_text(body, encoding="utf-8")

    with pytest.raises(EvaluationRoleError, match="scopes"):
        EvaluationRoleCatalog(role_dir).snapshot()


def test_snapshot_is_json_serializable() -> None:
    json.dumps(snapshot_catalog(), ensure_ascii=False, sort_keys=True)
