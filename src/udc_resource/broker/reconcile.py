"""Broker restart reconciliation."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from .. import store
from .docker_driver import ContainerInfo, DockerBackend, matches_managed_allocation

logger = logging.getLogger(__name__)

STALE_PENDING = timedelta(minutes=5)


def reconcile(
    connection: sqlite3.Connection,
    docker: DockerBackend,
    *,
    now: datetime | None = None,
    stale_pending: timedelta = STALE_PENDING,
) -> dict[str, int]:
    """Reconcile persisted allocations with the real Docker state.

    Unknown containers are never stopped or removed. Only rows that the broker
    owns are acted on.
    """
    current = now or store.utcnow()
    summary = {
        "pending_failed": 0,
        "expired": 0,
        "lost": 0,
        "kept": 0,
        "unmanaged": 0,
        "orphaned": 0,
        "list_failed": 0,
        "unsafe_skipped": 0,
        "cleanup_retried": 0,
        "cleanup_failed": 0,
    }
    try:
        containers = docker.list_containers()
    except Exception:
        logger.exception("reconcile could not list containers")
        summary["list_failed"] = 1
        return summary
    by_id = {container.id: container for container in containers}
    by_allocation: dict[str, list[ContainerInfo]] = {}
    for container in containers:
        allocation_id = container.labels.get("udc.allocation")
        if allocation_id:
            by_allocation.setdefault(allocation_id, []).append(container)

    pending = store.list_allocations(connection, statuses=("pending",), now=current)
    for allocation in pending:
        created_at = datetime.fromisoformat(str(allocation["created_at"]))
        if current - created_at < stale_pending:
            continue
        candidates = list(by_allocation.get(str(allocation["id"]), []))
        if allocation["container_id"]:
            direct = by_id.get(str(allocation["container_id"]))
            if direct is not None and direct not in candidates:
                candidates.append(direct)
        unsafe = [
            container
            for container in candidates
            if not matches_managed_allocation(
                container,
                str(allocation["id"]),
                str(allocation["container_id"]) or None,
            )
        ]
        if unsafe:
            summary["unsafe_skipped"] += 1
            logger.critical(
                "refusing to clean pending allocation %s: container identity mismatch: %s",
                allocation["id"],
                ", ".join(container.id for container in unsafe),
            )
            continue
        removed_ok = True
        for container in candidates:
            try:
                docker.remove_container(container.id)
            except Exception:
                removed_ok = False
                logger.exception("could not remove stale pending container %s", container.id)
        if not removed_ok:
            continue
        try:
            store.fail_allocation(
                connection,
                str(allocation["id"]),
                reason="pending timeout",
                now=current,
            )
            summary["pending_failed"] += 1
        except Exception:
            logger.exception("could not fail stale pending allocation %s", allocation["id"])

    # Allocations whose container cleanup failed earlier keep their resource
    # reservations; retry the removal and finalize only once the container is
    # confirmed gone.
    cleanup_pending = store.list_cleanup_pending(connection, now=current)
    for allocation in cleanup_pending:
        allocation_id = str(allocation["id"])
        container_id = str(allocation["container_id"] or "")
        if not container_id:
            # Nothing to clean up (should not happen); finalize directly.
            try:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason=str(allocation["note"] or "cleanup pending without container"),
                    now=current,
                )
                summary["cleanup_retried"] += 1
            except Exception:
                logger.exception("could not finalize cleanup for %s", allocation_id)
                summary["cleanup_failed"] += 1
            continue
        candidates = list(by_allocation.get(allocation_id, []))
        direct = by_id.get(container_id)
        if direct is not None and direct not in candidates:
            candidates.append(direct)
        unsafe = [
            container
            for container in candidates
            if not matches_managed_allocation(container, allocation_id, container_id)
        ]
        if unsafe:
            summary["cleanup_failed"] += 1
            logger.critical(
                "refusing to clean cleanup_pending allocation %s: container identity mismatch",
                allocation_id,
            )
            continue
        if not candidates:
            # Container is gone from Docker already; safe to finalize.
            try:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason="container removed after cleanup failure",
                    now=current,
                )
                summary["cleanup_retried"] += 1
            except Exception:
                logger.exception("could not finalize cleanup for %s", allocation_id)
                summary["cleanup_failed"] += 1
            continue
        removed_ok = True
        for container in candidates:
            try:
                docker.remove_container(container.id)
            except Exception:
                removed_ok = False
                logger.exception(
                    "cleanup retry could not remove container %s",
                    container.id,
                )
        if not removed_ok:
            summary["cleanup_failed"] += 1
            continue
        try:
            store.fail_allocation(
                connection,
                allocation_id,
                reason="container cleaned after activation failure",
                now=current,
            )
            summary["cleanup_retried"] += 1
        except Exception:
            logger.exception("could not finalize cleanup for %s", allocation_id)
            summary["cleanup_failed"] += 1

    active = store.list_allocations(connection, statuses=("active",), now=current)
    for allocation in active:
        allocation_id = str(allocation["id"])
        try:
            active_container = by_id.get(str(allocation["container_id"])) or docker.get_container(
                str(allocation["container_id"])
            )
        except Exception:
            logger.exception("could not inspect container for %s", allocation_id)
            continue
        if active_container is None:
            try:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason="container missing",
                    now=current,
                )
                summary["lost"] += 1
            except Exception:
                logger.exception("could not fail missing allocation %s", allocation_id)
            continue
        if not matches_managed_allocation(
            active_container,
            allocation_id,
            str(allocation["container_id"]),
        ):
            summary["unsafe_skipped"] += 1
            logger.critical(
                "refusing to touch allocation %s: container %s identity mismatch",
                allocation_id,
                active_container.id,
            )
            continue
        if active_container.status in {"running", "restarting", "paused"}:
            # Same liveness set as the GPU holder logic: a restarting or
            # paused container is a live allocation, not garbage to collect.
            pass
        else:
            removed_ok = True
            try:
                docker.remove_container(active_container.id)
            except Exception:
                removed_ok = False
                logger.exception("could not remove stopped container %s", active_container.id)
            if not removed_ok:
                # The orphan container may still exist: keep the GPU claims
                # and reservations (non-terminal) and let the next reconcile
                # pass retry, exactly like activation double failures.
                try:
                    store.mark_cleanup_pending(
                        connection,
                        allocation_id,
                        reason=(
                            f"container is not running ({active_container.status}) "
                            "and cleanup failed"
                        ),
                        now=current,
                    )
                    summary["cleanup_failed"] += 1
                except Exception:
                    logger.exception(
                        "could not mark cleanup pending for %s",
                        allocation_id,
                    )
                continue
            try:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason=f"container is not running ({active_container.status})",
                    now=current,
                )
                summary["lost"] += 1
            except Exception:
                logger.exception("could not fail stopped allocation %s", allocation_id)
            continue
        expires_at = datetime.fromisoformat(str(allocation["expires_at"]))
        if expires_at <= current:
            try:
                docker.remove_container(active_container.id)
            except Exception:
                logger.exception("could not remove expired container %s", active_container.id)
                continue
            try:
                store.expire_allocation(connection, allocation_id, now=current)
                summary["expired"] += 1
            except Exception:
                logger.exception("could not expire allocation %s", allocation_id)
        else:
            summary["kept"] += 1

    known_container_ids = {str(allocation["container_id"]) for allocation in pending + active}
    known_allocation_ids = {str(allocation["id"]) for allocation in pending + active}
    for container in containers:
        if container.labels.get("udc.managed") != "true":
            summary["unmanaged"] += 1
        elif (
            container.id not in known_container_ids
            and container.labels.get("udc.allocation") not in known_allocation_ids
        ):
            summary["orphaned"] += 1
    return summary
