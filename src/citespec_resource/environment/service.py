"""Read-side views for environment memory: refs, summaries, diffs."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import models
from . import store as env_store

REF_RE = re.compile(r"^(?P<project>[A-Za-z0-9][A-Za-z0-9._-]{0,63})@v(?P<version>[0-9]{1,6})$")


def parse_ref(ref: Any) -> tuple[str | None, int | None, str | None]:
    """Split ``project@v3`` or a raw ``ver_...`` id."""

    if not isinstance(ref, str) or not ref:
        raise models.EnvironmentError("环境引用不能为空")
    if ref.startswith("ver_"):
        return None, None, ref
    match = REF_RE.match(ref)
    if match is None:
        raise models.EnvironmentError("环境引用格式应为 <项目>@v<版本>，例如 reconstruction@v1")
    return match.group("project"), int(match.group("version")), None


def resolve_ref(
    connection: sqlite3.Connection,
    *,
    ref: Any,
    owner_member_id: str | None,
) -> dict[str, Any]:
    """Resolve a user-facing ref to a joined version context.

    Members are scoped to their own projects, so the same project name in two
    accounts is never ambiguous. Admins may address any project by name only
    when that name is unique.
    """

    project_name, version, version_id = parse_ref(ref)
    if version_id is not None:
        return env_store.version_context(connection, version_id)
    assert project_name is not None and version is not None
    if owner_member_id is not None:
        project = env_store.get_project_by_name(
            connection, owner_member_id=owner_member_id, name=project_name
        )
    else:
        matches = env_store.projects_named(connection, project_name)
        if not matches:
            raise models.EnvironmentNotFound(f"项目 {project_name} 不存在")
        if len(matches) > 1:
            raise models.EnvironmentConflict("存在多个同名项目，请改用版本 ID")
        project = matches[0]
    profile = env_store.default_profile(connection, str(project["project_id"]))
    row = env_store.get_version_by_number(
        connection, profile_id=str(profile["profile_id"]), version=version
    )
    return env_store.version_context(connection, str(row["version_id"]))


def summary(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "version_id": context["version_id"],
        "ref": f"{context['project_name']}@v{context['version']}",
        "project": context["project_name"],
        "project_id": context["project_id"],
        "profile": context["profile_name"],
        "version": int(context["version"]),
        "origin": context["origin"],
        "capture_fidelity": context["capture_fidelity"],
        "verification": context["verification"],
        "visibility": context["visibility"],
        "sha256": context["sha256"],
        "created_at": context["created_at"],
        "created_by": context["created_by_member_id"],
        "archived": context["project_status"] == models.PROJECT_ARCHIVED,
    }


def detail(connection: sqlite3.Connection, context: dict[str, Any]) -> dict[str, Any]:
    try:
        manifest = json.loads(str(context["manifest"]))
    except ValueError:
        manifest = {}
    return {
        "version": summary(context),
        "manifest": manifest,
        "artifacts": env_store.list_artifacts(connection, str(context["version_id"])),
        "builds": env_store.list_builds(connection, str(context["version_id"])),
    }


def project_summary(connection: sqlite3.Connection, project: dict[str, Any]) -> dict[str, Any]:
    profile = env_store.default_profile(connection, str(project["project_id"]))
    latest = env_store.latest_version(connection, str(profile["profile_id"]))
    return {
        "project_id": project["project_id"],
        "name": project["name"],
        "status": project["status"],
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
        "versions": env_store.count_versions(connection, str(profile["profile_id"])),
        "latest": (None if latest is None else f"{project['name']}@v{int(latest['version'])}"),
    }


def diff_manifests(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_manifest = _manifest_of(left)
    right_manifest = _manifest_of(right)
    base = _field_diff(
        left_manifest.get("base", {}),
        right_manifest.get("base", {}),
        ("image", "digest"),
    )
    python_left = left_manifest.get("python", {})
    python_right = right_manifest.get("python", {})
    python = {
        "version": _value_diff(python_left.get("version"), python_right.get("version")),
        "manager": _value_diff(python_left.get("manager"), python_right.get("manager")),
        "packages": _package_diff(python_left.get("packages"), python_right.get("packages")),
        "lockfiles": _package_diff(
            python_left.get("lockfiles"), python_right.get("lockfiles"), key="name"
        ),
    }
    system = {
        "package_manager": _value_diff(
            left_manifest.get("system", {}).get("package_manager"),
            right_manifest.get("system", {}).get("package_manager"),
        ),
        "packages": _package_diff(
            left_manifest.get("system", {}).get("packages"),
            right_manifest.get("system", {}).get("packages"),
        ),
    }
    changed = any(
        [
            base,
            python["version"],
            python["manager"],
            python["packages"]["added"],
            python["packages"]["removed"],
            python["packages"]["changed"],
            python["lockfiles"]["added"],
            python["lockfiles"]["removed"],
            python["lockfiles"]["changed"],
            system["package_manager"],
            system["packages"]["added"],
            system["packages"]["removed"],
            system["packages"]["changed"],
            _field_diff(
                {"platform": left_manifest.get("platform")},
                {"platform": right_manifest.get("platform")},
                ("platform",),
            ),
        ]
    )
    return {
        "changed": changed,
        "base": base,
        "platform": left_manifest.get("platform"),
        "python": python,
        "system": system,
    }


def _manifest_of(context: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(context["manifest"]))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _value_diff(left: Any, right: Any) -> dict[str, Any]:
    if left == right:
        return {}
    return {"from": left, "to": right}


def _field_diff(
    left: dict[str, Any], right: dict[str, Any], keys: tuple[str, ...]
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in keys:
        if left.get(key) != right.get(key):
            changes[key] = {"from": left.get(key), "to": right.get(key)}
    return changes


def _package_diff(left: Any, right: Any, *, key: str = "name") -> dict[str, Any]:
    left_map = _package_map(left, key=key)
    right_map = _package_map(right, key=key)
    added = [right_map[name] for name in sorted(set(right_map) - set(left_map))]
    removed = [left_map[name] for name in sorted(set(left_map) - set(right_map))]
    changed = [
        {"from": left_map[name], "to": right_map[name]}
        for name in sorted(set(left_map) & set(right_map))
        if left_map[name] != right_map[name]
    ]
    return {"added": added, "removed": removed, "changed": changed}


def _package_map(value: Any, *, key: str) -> dict[str, Any]:
    if not isinstance(value, list):
        return {}
    items: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get(key)
        if isinstance(name, str) and name:
            items[name] = item
    return items
