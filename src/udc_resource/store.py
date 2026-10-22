"""Transactional persistence for allocations, GPU claims, and audit events."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from .db import get_setting
from .errors import Conflict, GpuBusy, InsufficientResources, NotFound
from .models import GPU


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_user(
    connection: sqlite3.Connection,
    *,
    uid: int,
    username: str,
    role: str,
    now: datetime | None = None,
) -> None:
    timestamp = (now or utcnow()).isoformat()
    connection.execute(
        """
        INSERT INTO users(uid, username, role, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
            username = excluded.username,
            role = excluded.role
        """,
        (uid, username, role, timestamp),
    )
    connection.commit()


def upsert_gpus(connection: sqlite3.Connection, gpus: Iterable[GPU]) -> None:
    for gpu in gpus:
        connection.execute(
            """
            INSERT INTO gpus(uuid, idx, name, memory_mib, state) VALUES (?, ?, ?, ?, 'idle')
            ON CONFLICT(uuid) DO UPDATE SET
                idx = excluded.idx,
                name = excluded.name,
                memory_mib = excluded.memory_mib
            """,
            (gpu.uuid, gpu.index, gpu.name, gpu.memory_mib),
        )
    connection.commit()


def list_gpus(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT g.uuid, g.idx, g.name, g.memory_mib,
               c.allocation_id, a.owner_name, a.status, a.expires_at,
               a.container_id
        FROM gpus AS g
        LEFT JOIN active_gpu_claims AS c ON c.gpu_uuid = g.uuid
        LEFT JOIN allocations AS a ON a.id = c.allocation_id
        ORDER BY g.idx
        """
    ).fetchall()
    current = now or utcnow()
    result: list[dict[str, Any]] = []
    for row in rows:
        expires_at = row["expires_at"]
        remaining = None
        if expires_at:
            remaining = max(
                0,
                int((datetime.fromisoformat(str(expires_at)) - current).total_seconds()),
            )
        db_state = "allocated" if row["allocation_id"] else "free"
        result.append(
            {
                "index": int(row["idx"]),
                "uuid": str(row["uuid"]),
                "name": str(row["name"]),
                "memory_mib": int(row["memory_mib"]),
                "state": "unknown",
                "db_state": db_state,
                "allocation_id": row["allocation_id"],
                "owner": row["owner_name"],
                "status": row["status"],
                "expires_at": expires_at,
                "remaining_seconds": remaining,
                "container_id": row["container_id"],
            }
        )
    return result


def gpus_for_indices(
    connection: sqlite3.Connection,
    indices: Sequence[int],
) -> list[dict[str, Any]]:
    unique_indices = list(dict.fromkeys(indices))
    if not unique_indices:
        raise NotFound("no GPU indices were requested")
    placeholders = ",".join("?" for _ in unique_indices)
    rows = connection.execute(
        f"SELECT uuid, idx, name, memory_mib FROM gpus WHERE idx IN ({placeholders})",
        unique_indices,
    ).fetchall()
    found = {int(row["idx"]): dict(row) for row in rows}
    missing = [index for index in unique_indices if index not in found]
    if missing:
        raise NotFound(f"unknown GPU index(es): {', '.join(map(str, missing))}")
    return [found[index] for index in unique_indices]


def _allocation_row(connection: sqlite3.Connection, allocation_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM allocations WHERE id = ?", (allocation_id,)).fetchone()
    if row is None:
        raise NotFound(f"allocation not found: {allocation_id}")
    return row


def _gpu_refs(connection: sqlite3.Connection, allocation_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT g.idx, g.uuid, g.name
        FROM allocation_gpus AS ag
        JOIN gpus AS g ON g.uuid = ag.gpu_uuid
        WHERE ag.allocation_id = ?
        ORDER BY g.idx
        """,
        (allocation_id,),
    ).fetchall()
    return [
        {"index": int(row["idx"]), "uuid": str(row["uuid"]), "name": str(row["name"])}
        for row in rows
    ]


def allocation_to_dict(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    gpus = _gpu_refs(connection, str(row["id"]))
    expires_at = datetime.fromisoformat(str(row["expires_at"]))
    current = now or utcnow()
    remaining = max(0, int((expires_at - current).total_seconds()))
    return {
        "id": str(row["id"]),
        "owner_uid": int(row["owner_uid"]),
        "owner": str(row["owner_name"]),
        "status": str(row["status"]),
        "image": str(row["image"]),
        "cpus": float(row["cpus"]),
        "memory_gb": int(row["memory_gb"]),
        "workspace": str(row["workspace"]),
        "note": str(row["note"]),
        "created_at": str(row["created_at"]),
        "starts_at": str(row["starts_at"]),
        "expires_at": str(row["expires_at"]),
        "remaining_seconds": remaining,
        "container_id": str(row["container_id"]),
        "container_name": str(row["container_name"]),
        "gpus": [gpu["index"] for gpu in gpus],
        "gpu_uuids": [gpu["uuid"] for gpu in gpus],
    }


def get_allocation(
    connection: sqlite3.Connection,
    allocation_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    return allocation_to_dict(connection, _allocation_row(connection, allocation_id), now=now)


def list_allocations(
    connection: sqlite3.Connection,
    *,
    owner_uid: int | None = None,
    statuses: Sequence[str] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if owner_uid is not None:
        clauses.append("owner_uid = ?")
        params.append(owner_uid)
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM allocations{where} ORDER BY created_at DESC", params
    ).fetchall()
    return [allocation_to_dict(connection, row, now=now) for row in rows]


def record_event(
    connection: sqlite3.Connection,
    *,
    actor_uid: int | None,
    action: str,
    allocation_id: str | None = None,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    connection.execute(
        "INSERT INTO events(ts, actor_uid, action, allocation_id, detail) VALUES (?, ?, ?, ?, ?)",
        (
            (now or utcnow()).isoformat(),
            actor_uid,
            action,
            allocation_id,
            json.dumps(detail or {}, sort_keys=True),
        ),
    )


def create_allocation(
    connection: sqlite3.Connection,
    *,
    owner_uid: int,
    owner_name: str,
    gpu_uuids: Sequence[str],
    cpus: float,
    memory_gb: int,
    image: str,
    workspace: str,
    note: str,
    starts_at: datetime,
    expires_at: datetime,
    max_cpus: float,
    max_memory_gb: int,
    now: datetime | None = None,
) -> str:
    if len(set(gpu_uuids)) != len(gpu_uuids):
        raise GpuBusy("duplicate GPUs in request")
    allocation_id = uuid.uuid4().hex
    timestamp = (now or utcnow()).isoformat()
    try:
        connection.execute("BEGIN IMMEDIATE")
        busy: list[sqlite3.Row] = []
        if gpu_uuids:
            placeholders = ",".join("?" for _ in gpu_uuids)
            busy = connection.execute(
                f"SELECT gpu_uuid FROM active_gpu_claims WHERE gpu_uuid IN ({placeholders})",
                list(gpu_uuids),
            ).fetchall()
        if busy:
            names = ", ".join(str(row["gpu_uuid"]) for row in busy)
            raise GpuBusy(f"GPU already allocated: {names}")
        used = connection.execute(
            """
            SELECT COALESCE(SUM(cpus), 0) AS cpus,
                   COALESCE(SUM(memory_gb), 0) AS memory_gb
            FROM allocations
            WHERE status IN ('pending', 'active')
            """
        ).fetchone()
        if float(used["cpus"]) + cpus > max_cpus:
            raise InsufficientResources(
                f"CPU budget exceeded: {float(used['cpus']) + cpus:g} > {max_cpus:g}"
            )
        if int(used["memory_gb"]) + memory_gb > max_memory_gb:
            raise InsufficientResources(
                f"memory budget exceeded: {int(used['memory_gb']) + memory_gb} > {max_memory_gb}"
            )
        connection.execute(
            """
            INSERT INTO allocations(
                id, owner_uid, owner_name, status, image, cpus, memory_gb,
                workspace, note, created_at, starts_at, expires_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                allocation_id,
                owner_uid,
                owner_name,
                image,
                cpus,
                memory_gb,
                workspace,
                note,
                timestamp,
                starts_at.isoformat(),
                expires_at.isoformat(),
            ),
        )
        connection.executemany(
            "INSERT INTO allocation_gpus(allocation_id, gpu_uuid) VALUES (?, ?)",
            [(allocation_id, gpu_uuid) for gpu_uuid in gpu_uuids],
        )
        connection.executemany(
            "INSERT INTO active_gpu_claims(gpu_uuid, allocation_id) VALUES (?, ?)",
            [(gpu_uuid, allocation_id) for gpu_uuid in gpu_uuids],
        )
        record_event(
            connection,
            actor_uid=owner_uid,
            action="allocation_pending",
            allocation_id=allocation_id,
            detail={"gpus": list(gpu_uuids), "cpus": cpus, "memory_gb": memory_gb},
            now=now,
        )
        connection.commit()
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise GpuBusy("GPU is already allocated") from exc
    except Exception:
        connection.rollback()
        raise
    return allocation_id


def activate_allocation(
    connection: sqlite3.Connection,
    allocation_id: str,
    *,
    container_id: str,
    container_name: str,
    now: datetime | None = None,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _allocation_row(connection, allocation_id)
        if row["status"] != "pending":
            raise Conflict(f"allocation {allocation_id} is not pending")
        connection.execute(
            """
            UPDATE allocations
            SET status = 'active', container_id = ?, container_name = ?
            WHERE id = ?
            """,
            (container_id, container_name, allocation_id),
        )
        record_event(
            connection,
            actor_uid=int(row["owner_uid"]),
            action="allocation_active",
            allocation_id=allocation_id,
            detail={"container_id": container_id, "container_name": container_name},
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def fail_allocation(
    connection: sqlite3.Connection,
    allocation_id: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _allocation_row(connection, allocation_id)
        connection.execute(
            "UPDATE allocations SET status = 'failed' WHERE id = ?", (allocation_id,)
        )
        connection.execute(
            "DELETE FROM active_gpu_claims WHERE allocation_id = ?", (allocation_id,)
        )
        record_event(
            connection,
            actor_uid=int(row["owner_uid"]),
            action="allocation_failed",
            allocation_id=allocation_id,
            detail={"reason": reason},
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _finish_allocation(
    connection: sqlite3.Connection,
    allocation_id: str,
    *,
    status: str,
    action: str,
    actor_uid: int | None,
    reason: str,
    now: datetime | None = None,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _allocation_row(connection, allocation_id)
        if row["status"] not in {"pending", "active"}:
            raise Conflict(f"allocation {allocation_id} is already {row['status']}")
        connection.execute(
            "UPDATE allocations SET status = ? WHERE id = ?",
            (status, allocation_id),
        )
        connection.execute(
            "DELETE FROM active_gpu_claims WHERE allocation_id = ?", (allocation_id,)
        )
        record_event(
            connection,
            actor_uid=actor_uid,
            action=action,
            allocation_id=allocation_id,
            detail={"reason": reason} if reason else {},
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def expire_allocation(
    connection: sqlite3.Connection,
    allocation_id: str,
    *,
    now: datetime | None = None,
) -> None:
    _finish_allocation(
        connection,
        allocation_id,
        status="expired",
        action="allocation_expired",
        actor_uid=None,
        reason="lease expired",
        now=now,
    )


def release_allocation(
    connection: sqlite3.Connection,
    allocation_id: str,
    *,
    actor_uid: int,
    reason: str = "",
    now: datetime | None = None,
) -> None:
    _finish_allocation(
        connection,
        allocation_id,
        status="released",
        action="allocation_released",
        actor_uid=actor_uid,
        reason=reason,
        now=now,
    )


def list_expired_active(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = now or utcnow()
    rows = connection.execute(
        "SELECT * FROM allocations WHERE status = 'active' AND expires_at <= ? ORDER BY expires_at",
        (current.isoformat(),),
    ).fetchall()
    return [allocation_to_dict(connection, row, now=current) for row in rows]


def list_users(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT uid, username, role, created_at FROM users ORDER BY username"
    ).fetchall()
    return [dict(row) for row in rows]


def _event_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        detail = json.loads(str(row["detail"]))
    except json.JSONDecodeError:
        detail = {"raw": str(row["detail"])}
    return {
        "id": int(row["id"]),
        "ts": str(row["ts"]),
        "actor_uid": row["actor_uid"],
        "action": str(row["action"]),
        "allocation_id": row["allocation_id"],
        "detail": detail,
    }


def list_events(connection: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, ts, actor_uid, action, allocation_id, detail FROM events "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_event_to_dict(row) for row in rows]


def list_allocation_events(
    connection: sqlite3.Connection,
    allocation_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, ts, actor_uid, action, allocation_id, detail FROM events "
        "WHERE allocation_id = ? ORDER BY id DESC LIMIT ?",
        (allocation_id, limit),
    ).fetchall()
    return [_event_to_dict(row) for row in rows]


def get_mode(connection: sqlite3.Connection, default: str = "monitor") -> str:
    mode = get_setting(connection, "mode", default)
    if mode in {"monitor", "active"}:
        return str(mode)
    return default


def set_mode(
    connection: sqlite3.Connection,
    mode: str,
    *,
    actor_uid: int,
    now: datetime | None = None,
) -> None:
    if mode not in {"monitor", "active"}:
        raise ValueError("mode must be monitor or active")
    timestamp = (now or utcnow()).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES ('mode', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (mode, timestamp),
        )
        record_event(
            connection,
            actor_uid=actor_uid,
            action="mode_changed",
            detail={"mode": mode},
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
