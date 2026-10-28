"""SQLite persistence for environment memory.

All writes go through this module so the broker cannot accidentally leave a
half-created project or a rewritten version behind. Content columns of a
version are additionally protected by database triggers (migration 7); this
module only ever changes the recorded verification result and visibility.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from . import models
from .normalize import artifact_name, canonical_json, digest_text

PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_PROFILE = "default"
ARTIFACT_KINDS = frozenset(
    {"lockfile", "requirements", "conda", "pyproject", "package_list", "probe"}
)


def _timestamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@contextmanager
def _write(connection: sqlite3.Connection) -> Iterator[None]:
    """Own a transaction only when the caller has not already opened one."""

    own = not connection.in_transaction
    if own:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise


def validate_project_name(name: Any) -> str:
    if not isinstance(name, str) or not PROJECT_NAME.match(name):
        raise models.EnvironmentError(
            "项目名只能包含字母、数字、点、下划线和连字符，且以字母或数字开头（最多 64 个字符）"
        )
    return name


def create_project(
    connection: sqlite3.Connection,
    *,
    owner_member_id: str,
    name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a project and its default environment profile."""

    validate_project_name(name)
    timestamp = _timestamp(now)
    project_id = _new_id("prj")
    with _write(connection):
        connection.execute(
            """
            INSERT INTO research_projects(
                project_id, owner_member_id, name, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (project_id, owner_member_id, name, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO environment_profiles(profile_id, project_id, name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (_new_id("env"), project_id, DEFAULT_PROFILE, timestamp),
        )
    return get_project(connection, project_id)


def get_project(connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM research_projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound("项目不存在")
    return dict(row)


def get_project_by_name(
    connection: sqlite3.Connection,
    *,
    owner_member_id: str,
    name: str,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM research_projects WHERE owner_member_id = ? AND name = ?",
        (owner_member_id, name),
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound(f"项目 {name} 不存在")
    return dict(row)


def projects_named(connection: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM research_projects WHERE name = ? ORDER BY created_at",
        (name,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_projects(
    connection: sqlite3.Connection,
    *,
    owner_member_id: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if owner_member_id is not None:
        clauses.append("owner_member_id = ?")
        params.append(owner_member_id)
    if not include_archived:
        clauses.append("status = 'active'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM research_projects {where} ORDER BY name",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def archive_project(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    with _write(connection):
        cursor = connection.execute(
            "UPDATE research_projects SET status = 'archived', updated_at = ? "
            "WHERE project_id = ? AND status = 'active'",
            (_timestamp(now), project_id),
        )
        if cursor.rowcount == 0:
            project = get_project(connection, project_id)
            if project["status"] == "archived":
                return project
            raise models.EnvironmentConflict("项目状态不允许归档")
    return get_project(connection, project_id)


def default_profile(connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM environment_profiles WHERE project_id = ? AND name = ?",
        (project_id, DEFAULT_PROFILE),
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound("项目没有默认环境档案")
    return dict(row)


def list_profiles(connection: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM environment_profiles WHERE project_id = ? ORDER BY name",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def create_version(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    manifest_text: str,
    sha256: str,
    origin: str,
    capture_fidelity: str,
    created_by_member_id: str,
    visibility: str = models.VISIBILITY_PRIVATE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append the next immutable version to a profile.

    The version number is computed inside the same statement, and the write is
    serialized with ``BEGIN IMMEDIATE``, so two concurrent captures can never
    claim the same number.
    """

    if origin not in models.ORIGINS:
        raise models.EnvironmentError(f"未知的环境来源: {origin}")
    if capture_fidelity not in models.CAPTURE_FIDELITIES:
        raise models.EnvironmentError(f"未知的采集完整度: {capture_fidelity}")
    if visibility not in models.VISIBILITIES:
        raise models.EnvironmentError(f"未知的可见范围: {visibility}")
    if digest_text(manifest_text) != sha256:
        raise models.EnvironmentError("manifest 摘要与内容不一致")
    version_id = _new_id("ver")
    timestamp = _timestamp(now)
    with _write(connection):
        connection.execute(
            """
            INSERT INTO environment_versions(
                version_id, profile_id, version, manifest, sha256, origin,
                capture_fidelity, verification, visibility, created_by_member_id,
                created_at
            )
            SELECT ?, ?, COALESCE(MAX(version), 0) + 1, ?, ?, ?, ?, 'unverified', ?, ?, ?
            FROM environment_versions WHERE profile_id = ?
            """,
            (
                version_id,
                profile_id,
                manifest_text,
                sha256,
                origin,
                capture_fidelity,
                visibility,
                created_by_member_id,
                timestamp,
                profile_id,
            ),
        )
    return get_version(connection, version_id)


def get_version(connection: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM environment_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound("环境版本不存在")
    return dict(row)


def version_context(connection: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    """A version joined with its profile and owning project."""

    row = connection.execute(
        """
        SELECT v.version_id, v.version, v.manifest, v.sha256, v.origin,
               v.capture_fidelity, v.verification, v.visibility,
               v.created_by_member_id, v.created_at,
               p.profile_id, p.name AS profile_name, p.project_id,
               r.name AS project_name, r.owner_member_id, r.status AS project_status
        FROM environment_versions v
        JOIN environment_profiles p ON p.profile_id = v.profile_id
        JOIN research_projects r ON r.project_id = p.project_id
        WHERE v.version_id = ?
        """,
        (version_id,),
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound("环境版本不存在")
    return dict(row)


def get_version_by_number(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    version: int,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM environment_versions WHERE profile_id = ? AND version = ?",
        (profile_id, version),
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound(f"环境版本 v{version} 不存在")
    return dict(row)


def list_versions(
    connection: sqlite3.Connection,
    profile_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM environment_versions WHERE profile_id = ? ORDER BY version DESC LIMIT ?",
        (profile_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_version(connection: sqlite3.Connection, profile_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM environment_versions WHERE profile_id = ? ORDER BY version DESC LIMIT 1",
        (profile_id,),
    ).fetchone()
    return None if row is None else dict(row)


def count_versions(connection: sqlite3.Connection, profile_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM environment_versions WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()
    return int(row["total"])


def set_verification(
    connection: sqlite3.Connection,
    version_id: str,
    state: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = get_version(connection, version_id)
    if not models.verification_allows(str(current["verification"]), state):
        raise models.EnvironmentConflict(f"验证状态不能从 {current['verification']} 变为 {state}")
    with _write(connection):
        cursor = connection.execute(
            "UPDATE environment_versions SET verification = ? "
            "WHERE version_id = ? AND verification = ?",
            (state, version_id, current["verification"]),
        )
        if cursor.rowcount == 0:
            raise models.EnvironmentConflict("验证状态已被其他操作更新")
    return get_version(connection, version_id)


def set_visibility(
    connection: sqlite3.Connection,
    version_id: str,
    visibility: str,
) -> dict[str, Any]:
    if visibility not in models.VISIBILITIES:
        raise models.EnvironmentError(f"未知的可见范围: {visibility}")
    with _write(connection):
        cursor = connection.execute(
            "UPDATE environment_versions SET visibility = ? WHERE version_id = ?",
            (visibility, version_id),
        )
        if cursor.rowcount == 0:
            raise models.EnvironmentNotFound("环境版本不存在")
    return get_version(connection, version_id)


def add_artifact(
    connection: sqlite3.Connection,
    *,
    version_id: str,
    kind: str,
    name: str,
    content: str,
    max_bytes: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Store one small text artifact (lockfile content, probe output)."""

    if kind not in ARTIFACT_KINDS:
        raise models.EnvironmentError(f"未知的制品类型: {kind}")
    if not isinstance(content, str):
        raise models.EnvironmentError("制品内容必须是文本")
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise models.EnvironmentError(
            f"制品 {name} 超过 {max_bytes} 字节上限（实际 {len(encoded)}）"
        )
    artifact_id = _new_id("art")
    timestamp = _timestamp(now)
    with _write(connection):
        connection.execute(
            """
            INSERT INTO environment_artifacts(
                artifact_id, version_id, kind, name, sha256, size_bytes, content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                version_id,
                kind,
                artifact_name(name),
                digest_text(content),
                len(encoded),
                content,
                timestamp,
            ),
        )
    row = connection.execute(
        "SELECT * FROM environment_artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    assert row is not None
    return dict(row)


def list_artifacts(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    include_content: bool = False,
) -> list[dict[str, Any]]:
    columns = (
        "*"
        if include_content
        else ("artifact_id, version_id, kind, name, sha256, size_bytes, created_at")
    )
    rows = connection.execute(
        f"SELECT {columns} FROM environment_artifacts WHERE version_id = ? ORDER BY kind, name",
        (version_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def artifacts_total_bytes(connection: sqlite3.Connection, version_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM environment_artifacts "
        "WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    return int(row["total"])


def create_build(
    connection: sqlite3.Connection,
    *,
    version_id: str,
    server_id: str,
    base_image: str,
    image_tag: str,
    build_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved = build_id or _new_id("bld")
    timestamp = _timestamp(now)
    with _write(connection):
        connection.execute(
            """
            INSERT INTO environment_builds(
                build_id, version_id, server_id, base_image, image_tag,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (resolved, version_id, server_id, base_image, image_tag, timestamp),
        )
    return get_build(connection, resolved)


def get_build(connection: sqlite3.Connection, build_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM environment_builds WHERE build_id = ?", (build_id,)
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound("构建记录不存在")
    return dict(row)


def latest_build(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    server_id: str | None = None,
    successful_only: bool = False,
) -> dict[str, Any] | None:
    clauses = ["version_id = ?"]
    params: list[Any] = [version_id]
    if server_id is not None:
        clauses.append("server_id = ?")
        params.append(server_id)
    if successful_only:
        clauses.append("status IN ('built', 'verified')")
    row = connection.execute(
        "SELECT * FROM environment_builds "
        f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, rowid DESC LIMIT 1",
        params,
    ).fetchone()
    return None if row is None else dict(row)


def list_builds(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM environment_builds WHERE version_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (version_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_build_running(
    connection: sqlite3.Connection,
    build_id: str,
    *,
    base_image_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    with _write(connection):
        cursor = connection.execute(
            "UPDATE environment_builds SET status = 'running', base_image_id = ?, "
            "started_at = ? WHERE build_id = ? AND status = 'pending'",
            (base_image_id, _timestamp(now), build_id),
        )
        if cursor.rowcount == 0:
            raise models.EnvironmentConflict("构建记录不处于待执行状态")
    return get_build(connection, build_id)


def finish_build(
    connection: sqlite3.Connection,
    build_id: str,
    *,
    status: str,
    image_id: str = "",
    detail: str = "",
    evidence: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if status not in {models.BUILD_BUILT, models.BUILD_VERIFIED, models.BUILD_FAILED}:
        raise models.EnvironmentError(f"未知的构建结果: {status}")
    with _write(connection):
        cursor = connection.execute(
            "UPDATE environment_builds SET status = ?, image_id = ?, detail = ?, "
            "evidence = ?, finished_at = ? WHERE build_id = ? AND status IN ('pending', 'running')",
            (
                status,
                image_id,
                detail,
                canonical_json(evidence or {}),
                _timestamp(now),
                build_id,
            ),
        )
        if cursor.rowcount == 0:
            raise models.EnvironmentConflict("构建记录已结束，不能重复写入结果")
    return get_build(connection, build_id)


def mark_build_verified(
    connection: sqlite3.Connection,
    build_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record that the built image was used for a successful functional run."""

    with _write(connection):
        cursor = connection.execute(
            "UPDATE environment_builds SET status = 'verified', finished_at = ? "
            "WHERE build_id = ? AND status IN ('built', 'verified')",
            (_timestamp(now), build_id),
        )
        if cursor.rowcount == 0:
            raise models.EnvironmentConflict("只有构建成功的镜像才能标记为已验证")
    return get_build(connection, build_id)


def successful_builds(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """All built or verified images, newest first, across every version."""

    rows = connection.execute(
        "SELECT * FROM environment_builds WHERE status IN ('built', 'verified') "
        "ORDER BY created_at DESC, rowid DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def record_legacy_source(
    connection: sqlite3.Connection,
    *,
    server_id: str,
    container_id: str,
    image_ref: str = "",
    image_id: str = "",
    container_name: str = "",
    state_at_scan: str = "",
    container_created_at: str = "",
    sanitized_metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Insert or refresh one scanned source without changing a claim decision."""

    timestamp = _timestamp(now)
    with _write(connection):
        connection.execute(
            """
            INSERT INTO legacy_sources(
                source_id, server_id, container_id, image_ref, image_id,
                container_name, state_at_scan, container_created_at,
                sanitized_metadata, status, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?)
            ON CONFLICT(server_id, container_id) DO UPDATE SET
                image_ref = excluded.image_ref,
                image_id = excluded.image_id,
                container_name = excluded.container_name,
                state_at_scan = excluded.state_at_scan,
                container_created_at = excluded.container_created_at,
                sanitized_metadata = excluded.sanitized_metadata,
                scanned_at = excluded.scanned_at
            """,
            (
                _new_id("src"),
                server_id,
                container_id,
                image_ref,
                image_id,
                container_name,
                state_at_scan,
                container_created_at,
                canonical_json(sanitized_metadata or {}),
                timestamp,
            ),
        )
    row = connection.execute(
        "SELECT * FROM legacy_sources WHERE server_id = ? AND container_id = ?",
        (server_id, container_id),
    ).fetchone()
    assert row is not None
    return dict(row)


def get_legacy_source(connection: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM legacy_sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound("旧容器来源不存在")
    return dict(row)


def list_legacy_sources(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    if status is None:
        rows = connection.execute(
            "SELECT * FROM legacy_sources ORDER BY scanned_at DESC"
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM legacy_sources WHERE status = ? ORDER BY scanned_at DESC",
            (status,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_legacy_source_status(
    connection: sqlite3.Connection,
    source_id: str,
    status: str,
) -> dict[str, Any]:
    if status not in models.SOURCE_STATUSES:
        raise models.EnvironmentError(f"未知的来源状态: {status}")
    with _write(connection):
        cursor = connection.execute(
            "UPDATE legacy_sources SET status = ? WHERE source_id = ?",
            (status, source_id),
        )
        if cursor.rowcount == 0:
            raise models.EnvironmentNotFound("旧容器来源不存在")
    return get_legacy_source(connection, source_id)


def create_claim(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    member_id: str,
    project_id: str,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    source = get_legacy_source(connection, source_id)
    if source["status"] != models.SOURCE_AVAILABLE:
        raise models.EnvironmentConflict("该旧容器已不可认领")
    timestamp = _timestamp(now)
    claim_id = _new_id("clm")
    try:
        with _write(connection):
            connection.execute(
                """
                INSERT INTO legacy_claims(
                    claim_id, source_id, member_id, project_id, reason, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (claim_id, source_id, member_id, project_id, reason, timestamp, timestamp),
            )
    except sqlite3.IntegrityError as exc:
        raise models.EnvironmentConflict("你已经就该来源提交过认领申请") from exc
    return get_claim(connection, claim_id)


def get_claim(connection: sqlite3.Connection, claim_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM legacy_claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if row is None:
        raise models.EnvironmentNotFound("认领申请不存在")
    return dict(row)


def list_claims(
    connection: sqlite3.Connection,
    *,
    member_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if member_id is not None:
        clauses.append("member_id = ?")
        params.append(member_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM legacy_claims {where} ORDER BY created_at DESC",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def pending_claims_for_source(
    connection: sqlite3.Connection,
    source_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM legacy_claims WHERE source_id = ? AND status = 'pending' "
        "ORDER BY created_at",
        (source_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def decide_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    *,
    approve: bool,
    reviewer: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Approve or reject a pending claim; never auto-resolve a contest."""

    claim = get_claim(connection, claim_id)
    if claim["status"] != models.CLAIM_PENDING:
        raise models.EnvironmentConflict("认领申请已经处理过")
    if approve:
        others = [
            other
            for other in pending_claims_for_source(connection, str(claim["source_id"]))
            if other["claim_id"] != claim_id
        ]
        if others:
            raise models.EnvironmentConflict(
                "该旧容器存在其他待处理认领，请先驳回或标记冲突后再批准"
            )
    timestamp = _timestamp(now)
    new_status = models.CLAIM_APPROVED if approve else models.CLAIM_REJECTED
    with _write(connection):
        cursor = connection.execute(
            "UPDATE legacy_claims SET status = ?, reviewer = ?, review_note = ?, "
            "reviewed_at = ?, updated_at = ? WHERE claim_id = ? AND status = 'pending'",
            (new_status, reviewer, note, timestamp, timestamp, claim_id),
        )
        if cursor.rowcount == 0:
            raise models.EnvironmentConflict("认领申请已被其他操作更新")
        if approve:
            connection.execute(
                "UPDATE legacy_sources SET status = 'claimed' WHERE source_id = ?",
                (claim["source_id"],),
            )
    return get_claim(connection, claim_id)


def mark_claim_conflict(
    connection: sqlite3.Connection,
    claim_id: str,
    *,
    reviewer: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _timestamp(now)
    with _write(connection):
        cursor = connection.execute(
            "UPDATE legacy_claims SET status = 'conflict', reviewer = ?, review_note = ?, "
            "updated_at = ? WHERE claim_id = ? AND status = 'pending'",
            (reviewer, note, timestamp, claim_id),
        )
        if cursor.rowcount == 0:
            raise models.EnvironmentConflict("只有待处理的申请可以标记冲突")
    return get_claim(connection, claim_id)


def link_claim_version(
    connection: sqlite3.Connection,
    claim_id: str,
    version_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    claim = get_claim(connection, claim_id)
    if claim["status"] != models.CLAIM_APPROVED:
        raise models.EnvironmentConflict("只有已批准的认领才能生成环境版本")
    with _write(connection):
        connection.execute(
            "UPDATE legacy_claims SET version_id = ?, updated_at = ? WHERE claim_id = ?",
            (version_id, _timestamp(now), claim_id),
        )
    return get_claim(connection, claim_id)
