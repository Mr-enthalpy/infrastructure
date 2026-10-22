"""Expiry worker for timed allocations."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from .. import store
from .docker_driver import DockerBackend

logger = logging.getLogger(__name__)


def expire_once(
    connection: sqlite3.Connection,
    docker: DockerBackend,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Expire active allocations whose lease has ended.

    The container is removed before the claim is released. If removal fails the
    allocation stays active so the next tick can retry safely.
    """
    expired_ids: list[str] = []
    for allocation in store.list_expired_active(connection, now=now):
        allocation_id = str(allocation["id"])
        try:
            docker.remove_container(str(allocation["container_id"]))
        except Exception:
            logger.exception("could not remove expired container for %s", allocation_id)
            continue
        try:
            store.expire_allocation(connection, allocation_id, now=now)
        except Exception:
            logger.exception("could not mark allocation %s expired", allocation_id)
            continue
        expired_ids.append(allocation_id)
        logger.info("allocation %s expired", allocation_id)
    return expired_ids
