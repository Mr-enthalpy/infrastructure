"""Transactional persistence for allocations, GPU claims, and audit events."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
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


def _check_capacity(
    connection: sqlite3.Connection,
    *,
    owner_uid: int,
    gpu_uuids: Sequence[str],
    cpus: float,
    memory_gb: int,
    max_cpus: float,
    max_memory_gb: int,
    max_cpus_per_user: float,
    max_memory_gb_per_user: int,
    max_gpus_per_user: int,
    max_active_allocations_per_user: int,
    available_gpu_uuids: set[str] | None = None,
) -> None:
    if len(set(gpu_uuids)) != len(gpu_uuids):
        raise GpuBusy("duplicate GPUs in request")
    if available_gpu_uuids is not None:
        unavailable = sorted(set(gpu_uuids) - available_gpu_uuids)
        if unavailable:
            raise GpuBusy("GPU is not currently free: " + ", ".join(unavailable))
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
    user = connection.execute(
        """
        SELECT COUNT(*) AS allocations,
               COALESCE(SUM(cpus), 0) AS cpus,
               COALESCE(SUM(memory_gb), 0) AS memory_gb
        FROM allocations
        WHERE owner_uid = ? AND status IN ('pending', 'active')
        """,
        (owner_uid,),
    ).fetchone()
    user_allocations = int(user["allocations"])
    if user_allocations + 1 > max_active_allocations_per_user:
        raise InsufficientResources(
            f"per-user allocation limit exceeded: "
            f"{user_allocations + 1} > {max_active_allocations_per_user}"
        )
    if float(user["cpus"]) + cpus > max_cpus_per_user:
        raise InsufficientResources(
            f"per-user CPU limit exceeded: {float(user['cpus']) + cpus:g} > {max_cpus_per_user:g}"
        )
    if int(user["memory_gb"]) + memory_gb > max_memory_gb_per_user:
        raise InsufficientResources(
            f"per-user memory limit exceeded: "
            f"{int(user['memory_gb']) + memory_gb} > {max_memory_gb_per_user}"
        )
    user_gpus = int(
        connection.execute(
            "SELECT COUNT(*) FROM allocation_gpus AS ag "
            "JOIN allocations AS a ON a.id = ag.allocation_id "
            "WHERE a.owner_uid = ? AND a.status IN ('pending', 'active')",
            (owner_uid,),
        ).fetchone()[0]
    )
    if user_gpus + len(gpu_uuids) > max_gpus_per_user:
        raise InsufficientResources(
            f"per-user GPU limit exceeded: {user_gpus + len(gpu_uuids)} > {max_gpus_per_user}"
        )


def _insert_allocation(
    connection: sqlite3.Connection,
    *,
    allocation_id: str,
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
    now: datetime | None,
    action: str = "allocation_pending",
) -> None:
    timestamp = (now or utcnow()).isoformat()
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
        action=action,
        allocation_id=allocation_id,
        detail={"gpus": list(gpu_uuids), "cpus": cpus, "memory_gb": memory_gb},
        now=now,
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
    max_cpus_per_user: float | None = None,
    max_memory_gb_per_user: int | None = None,
    max_gpus_per_user: int = 4,
    max_active_allocations_per_user: int = 4,
    now: datetime | None = None,
) -> str:
    allocation_id = uuid.uuid4().hex
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Fairness backstop: the queue-empty decision must be part of the same
        # transaction that inserts the allocation. An application-layer check
        # alone can race with a concurrent enqueue between check and insert.
        if queue_request_count(connection, now=now or utcnow()):
            raise GpuBusy("queued requests are pending; new requests must not bypass the queue")
        _check_capacity(
            connection,
            owner_uid=owner_uid,
            gpu_uuids=gpu_uuids,
            cpus=cpus,
            memory_gb=memory_gb,
            max_cpus=max_cpus,
            max_memory_gb=max_memory_gb,
            max_cpus_per_user=max_cpus if max_cpus_per_user is None else max_cpus_per_user,
            max_memory_gb_per_user=(
                max_memory_gb if max_memory_gb_per_user is None else max_memory_gb_per_user
            ),
            max_gpus_per_user=max_gpus_per_user,
            max_active_allocations_per_user=max_active_allocations_per_user,
        )
        _insert_allocation(
            connection,
            allocation_id=allocation_id,
            owner_uid=owner_uid,
            owner_name=owner_name,
            gpu_uuids=gpu_uuids,
            cpus=cpus,
            memory_gb=memory_gb,
            image=image,
            workspace=workspace,
            note=note,
            starts_at=starts_at,
            expires_at=expires_at,
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


def _queue_row(connection: sqlite3.Connection, queue_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT rowid AS seq, * FROM queue_requests WHERE id = ?",
        (queue_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"queue request not found: {queue_id}")
    return row


def _queue_position(connection: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    if str(row["state"]) != "queued":
        return None
    position = connection.execute(
        """
        SELECT COUNT(*) FROM queue_requests
        WHERE state = 'queued'
          AND (
              priority > ?
              OR (
                  priority = ?
                  AND (
                      created_at < ?
                      OR (created_at = ? AND rowid < ?)
                  )
              )
          )
        """,
        (
            int(row["priority"]),
            int(row["priority"]),
            str(row["created_at"]),
            str(row["created_at"]),
            int(row["seq"]),
        ),
    ).fetchone()
    return int(position[0]) + 1 if position else 1


def queue_to_dict(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        decoded_gpus = json.loads(str(row["requested_gpus"]))
        requested_gpus = (
            [int(index) for index in decoded_gpus] if isinstance(decoded_gpus, list) else []
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        requested_gpus = []
    expires_at = datetime.fromisoformat(str(row["expires_at"]))
    current = now or utcnow()
    remaining = max(0, int((expires_at - current).total_seconds()))
    return {
        "queue_id": str(row["id"]),
        "owner_uid": int(row["owner_uid"]),
        "owner": str(row["owner_name"]),
        "status": str(row["state"]),
        "cpus": float(row["cpus"]),
        "memory_gb": int(row["memory_gb"]),
        "hours": int(row["hours"]),
        "image": str(row["image"]),
        "workspace": str(row["workspace"]),
        "note": str(row["note"]),
        "gpus": [int(index) for index in requested_gpus],
        "created_at": str(row["created_at"]),
        "expires_at": str(row["expires_at"]),
        "remaining_seconds": remaining,
        "position": _queue_position(connection, row),
        "allocation_id": row["allocation_id"],
        "admitted_at": str(row["admitted_at"]),
        "reason": str(row["reason"]),
    }


def create_queue_request(
    connection: sqlite3.Connection,
    *,
    owner_uid: int,
    owner_name: str,
    requested_gpus: Sequence[int],
    cpus: float,
    memory_gb: int,
    hours: int,
    image: str,
    workspace: str,
    note: str,
    max_queue_length: int,
    max_queued_allocations_per_user: int,
    max_queue_hours: int,
    priority: int = 0,
    now: datetime | None = None,
) -> str:
    if len(set(requested_gpus)) != len(requested_gpus):
        raise GpuBusy("duplicate GPUs in request")
    current = now or utcnow()
    queue_id = uuid.uuid4().hex
    try:
        connection.execute("BEGIN IMMEDIATE")
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM queue_requests WHERE state = 'queued'"
            ).fetchone()[0]
        )
        if total >= max_queue_length:
            raise InsufficientResources(f"queue is full ({max_queue_length} requests)")
        user_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM queue_requests WHERE state = 'queued' AND owner_uid = ?",
                (owner_uid,),
            ).fetchone()[0]
        )
        if user_total >= max_queued_allocations_per_user:
            raise InsufficientResources(
                "per-user queue limit exceeded: "
                f"{user_total} queued requests (max {max_queued_allocations_per_user})"
            )
        connection.execute(
            """
            INSERT INTO queue_requests(
                id, owner_uid, owner_name, cpus, memory_gb, hours, image,
                workspace, note, requested_gpus, priority, state, created_at,
                expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                queue_id,
                owner_uid,
                owner_name,
                cpus,
                memory_gb,
                hours,
                image,
                workspace,
                note,
                json.dumps(sorted(requested_gpus)),
                priority,
                current.isoformat(),
                (current + timedelta(hours=max_queue_hours)).isoformat(),
            ),
        )
        record_event(
            connection,
            actor_uid=owner_uid,
            action="queue_enqueued",
            detail={
                "queue_id": queue_id,
                "gpus": sorted(requested_gpus),
                "cpus": cpus,
                "memory_gb": memory_gb,
                "hours": hours,
            },
            now=current,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return queue_id


def list_queue_requests(
    connection: sqlite3.Connection,
    *,
    owner_uid: int | None = None,
    include_finished: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if owner_uid is not None:
        clauses.append("owner_uid = ?")
        params.append(owner_uid)
    if not include_finished:
        clauses.append("state = 'queued'")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    # rowid is the insertion order tiebreaker. The random hex id must never
    # decide ordering: with identical created_at (frozen clock, same-second
    # commits) it would let a later request overtake an older one.
    rows = connection.execute(
        f"""
        SELECT rowid AS seq, * FROM queue_requests{where}
        ORDER BY priority DESC, created_at ASC, rowid
        """,
        params,
    ).fetchall()
    return [queue_to_dict(connection, row, now=now) for row in rows]


def get_queue_request(
    connection: sqlite3.Connection,
    queue_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    return queue_to_dict(connection, _queue_row(connection, queue_id), now=now)


def cancel_queue_request(
    connection: sqlite3.Connection,
    queue_id: str,
    *,
    actor_uid: int,
    reason: str = "",
    now: datetime | None = None,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _queue_row(connection, queue_id)
        if row["state"] != "queued":
            raise Conflict(f"queue request {queue_id} is {row['state']}")
        connection.execute(
            "UPDATE queue_requests SET state = 'cancelled', reason = ? WHERE id = ?",
            (reason, queue_id),
        )
        record_event(
            connection,
            actor_uid=actor_uid,
            action="queue_cancelled",
            detail={"queue_id": queue_id, "reason": reason},
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def expire_queue_requests(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[str]:
    current = now or utcnow()
    expired: list[str] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT id FROM queue_requests "
            "WHERE state = 'queued' AND expires_at <= ? ORDER BY created_at",
            (current.isoformat(),),
        ).fetchall()
        for row in rows:
            queue_id = str(row["id"])
            connection.execute(
                "UPDATE queue_requests SET state = 'expired', reason = ? WHERE id = ?",
                ("queue wait expired", queue_id),
            )
            record_event(
                connection,
                actor_uid=None,
                action="queue_expired",
                detail={"queue_id": queue_id},
                now=current,
            )
            expired.append(queue_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return expired


def queue_request_count(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> int:
    current = now or utcnow()
    row = connection.execute(
        "SELECT COUNT(*) FROM queue_requests WHERE state = 'queued' AND expires_at > ?",
        (current.isoformat(),),
    ).fetchone()
    return int(row[0]) if row else 0


def admit_next_queued(
    connection: sqlite3.Connection,
    *,
    max_cpus: float,
    max_memory_gb: int,
    max_cpus_per_user: float,
    max_memory_gb_per_user: int,
    max_gpus_per_user: int,
    max_active_allocations_per_user: int,
    available_gpu_uuids: set[str] | None = None,
    now: datetime | None = None,
) -> str | None:
    current = now or utcnow()
    try:
        connection.execute("BEGIN IMMEDIATE")
        # rowid keeps strict insertion order when created_at ties (see
        # list_queue_requests); the random id must never break the tie.
        rows = connection.execute(
            "SELECT rowid AS seq, * FROM queue_requests "
            "WHERE state = 'queued' AND expires_at > ? "
            "ORDER BY priority DESC, created_at ASC, rowid",
            (current.isoformat(),),
        ).fetchall()
        for row in rows:
            queue_id = str(row["id"])
            try:
                requested = [int(index) for index in json.loads(str(row["requested_gpus"]))]
            except (TypeError, ValueError, json.JSONDecodeError):
                connection.execute(
                    "UPDATE queue_requests SET state = 'failed', reason = ? WHERE id = ?",
                    ("invalid GPU request", queue_id),
                )
                record_event(
                    connection,
                    actor_uid=None,
                    action="queue_failed",
                    detail={"queue_id": queue_id, "reason": "invalid GPU request"},
                    now=current,
                )
                continue
            try:
                gpu_uuids = (
                    [str(gpu["uuid"]) for gpu in gpus_for_indices(connection, requested)]
                    if requested
                    else []
                )
            except NotFound:
                connection.execute(
                    "UPDATE queue_requests SET state = 'failed', reason = ? WHERE id = ?",
                    ("requested GPU no longer exists", queue_id),
                )
                record_event(
                    connection,
                    actor_uid=None,
                    action="queue_failed",
                    detail={"queue_id": queue_id, "reason": "requested GPU no longer exists"},
                    now=current,
                )
                continue
            try:
                _check_capacity(
                    connection,
                    owner_uid=int(row["owner_uid"]),
                    gpu_uuids=gpu_uuids,
                    cpus=float(row["cpus"]),
                    memory_gb=int(row["memory_gb"]),
                    max_cpus=max_cpus,
                    max_memory_gb=max_memory_gb,
                    max_cpus_per_user=max_cpus_per_user,
                    max_memory_gb_per_user=max_memory_gb_per_user,
                    max_gpus_per_user=max_gpus_per_user,
                    max_active_allocations_per_user=max_active_allocations_per_user,
                    available_gpu_uuids=available_gpu_uuids,
                )
            except (GpuBusy, InsufficientResources):
                # Strict queue order: a request that cannot run yet blocks
                # later requests instead of letting them jump ahead.
                connection.commit()
                return None
            allocation_id = uuid.uuid4().hex
            starts_at = current
            expires_at = current + timedelta(hours=int(row["hours"]))
            _insert_allocation(
                connection,
                allocation_id=allocation_id,
                owner_uid=int(row["owner_uid"]),
                owner_name=str(row["owner_name"]),
                gpu_uuids=gpu_uuids,
                cpus=float(row["cpus"]),
                memory_gb=int(row["memory_gb"]),
                image=str(row["image"]),
                workspace=str(row["workspace"]),
                note=str(row["note"]),
                starts_at=starts_at,
                expires_at=expires_at,
                now=current,
            )
            connection.execute(
                """
                UPDATE queue_requests
                SET state = 'admitted', admitted_at = ?, allocation_id = ?
                WHERE id = ?
                """,
                (current.isoformat(), allocation_id, queue_id),
            )
            record_event(
                connection,
                actor_uid=int(row["owner_uid"]),
                action="queue_admitted",
                allocation_id=allocation_id,
                detail={"queue_id": queue_id},
                now=current,
            )
            connection.commit()
            return allocation_id
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return None


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


def active_lease_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM allocations WHERE status IN ('pending', 'active')"
    ).fetchone()
    return int(row[0]) if row else 0


def get_mode(connection: sqlite3.Connection, default: str = "monitor") -> str:
    mode = get_setting(connection, "mode", default)
    if mode in {"monitor", "maintenance", "active"}:
        return str(mode)
    return default


def set_mode(
    connection: sqlite3.Connection,
    mode: str,
    *,
    actor_uid: int,
    now: datetime | None = None,
) -> None:
    if mode not in {"monitor", "maintenance", "active"}:
        raise ValueError("mode must be monitor, maintenance, or active")
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
