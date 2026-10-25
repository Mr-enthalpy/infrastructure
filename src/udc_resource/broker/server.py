"""Asyncio Unix-socket broker with SO_PEERCRED identity and SQLite transactions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from .. import store
from ..config import LabConfig, validate_active_mode_policy
from ..db import connect, init_db
from ..errors import (
    BadRequest,
    Conflict,
    DockerError,
    Forbidden,
    GpuBusy,
    InsufficientResources,
    InternalError,
    MonitorOnly,
    NotFound,
    UdcError,
)
from ..gpu_audit import GpuAuditResult, GpuAuditRunner, device_fingerprint
from ..identity import (
    Identity,
    IdentityResolver,
    IdentityResolverProtocol,
    peer_credentials,
)
from ..legacy_audit import LegacyAuditResult, LegacyAuditRunner
from ..members import load_members, public_member_data
from ..models import GPU, Allocation
from ..stream import (
    FRAME_DATA,
    FRAME_EXIT,
    FRAME_RESIZE,
    exit_payload,
    parse_resize,
    read_frame,
    write_frame,
)
from ..workspace_quota import (
    QuotaCheckResult,
    check_workspace_quota,
    provision_member_quota,
    verify_member_quota,
)
from . import workspace_io
from .docker_driver import (
    ContainerInfo,
    DockerBackend,
    DockerDriver,
    GPUProcess,
    ShellSession,
    matches_managed_allocation,
)
from .expiry import expire_once as expire_allocations
from .reconcile import reconcile as reconcile_allocations

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024
CONNECTION_TIMEOUT = 30.0
ACTIVE_GPU_HOLDER_STATUSES = {"running", "restarting", "paused"}
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


@dataclass(frozen=True)
class LiveResources:
    gpus: list[GPU]
    containers: list[ContainerInfo]
    processes: list[GPUProcess]
    error: str | None = None


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


class Broker:
    def __init__(
        self,
        config: LabConfig,
        *,
        db_path: str | Path | None = None,
        socket_path: str | Path | None = None,
        docker: DockerBackend | None = None,
        identity_resolver: IdentityResolverProtocol | None = None,
        legacy_audit: Callable[[], LegacyAuditResult] | None = None,
        gpu_audit: Callable[[], GpuAuditResult] | None = None,
        quota_check: Callable[[], QuotaCheckResult] | None = None,
        member_quota_check: Callable[[str, int, int], Any] | None = None,
        member_quota_provision: Callable[[str, int, int], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        expiry_interval: float = 30.0,
        reconcile_interval: float = 300.0,
    ) -> None:
        self.config = config
        self.db_path = Path(db_path or config.server.state_db)
        self.socket_path = Path(socket_path or config.server.broker_socket)
        self.docker = docker or DockerDriver(config)
        self.identity_resolver = identity_resolver or IdentityResolver(config.groups.admins)
        self.legacy_audit = legacy_audit or LegacyAuditRunner(
            config.policy.legacy_audit_script,
            timeout=config.policy.legacy_audit_timeout,
            allow_root_ssh_keys=config.policy.allow_root_ssh_keys,
        )
        self.gpu_audit = gpu_audit or GpuAuditRunner(
            config.policy.gpu_audit_script,
            timeout=config.policy.gpu_audit_timeout,
            expected_gpus=config.hardware.gpu_count,
        )
        self.quota_check = quota_check or (
            lambda: check_workspace_quota(
                config.server.workspace_root,
                config.policy.max_workspace_gb,
            )
        )
        self.member_quota_check = member_quota_check or verify_member_quota
        self.member_quota_provision = member_quota_provision or provision_member_quota
        self.clock = clock or store.utcnow
        self.expiry_interval = expiry_interval
        self.reconcile_interval = reconcile_interval
        self._mode = config.policy.mode
        self._server: asyncio.AbstractServer | None = None
        self._expiry_task: asyncio.Task[None] | None = None
        self._gpu_audit_failure: str | None = None
        self._quota_failure: str | None = None
        self._storage_failure: str | None = None
        self._gpu_fingerprint = device_fingerprint()
        self._tunnels: dict[str, set[asyncio.Task[None]]] = {}

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = connect(self.db_path)
        try:
            yield connection
        finally:
            connection.close()

    def _now(self) -> datetime:
        return self.clock()

    def _allocation_map(self, connection: Any, now: datetime) -> dict[str, dict[str, Any]]:
        rows = store.list_allocations(
            connection,
            statuses=("pending", "active"),
            now=now,
        )
        return {str(row["id"]): row for row in rows}

    def _legacy_audit_result(self) -> LegacyAuditResult:
        if not self.config.policy.legacy_audit_required:
            return LegacyAuditResult(True, "legacy audit disabled by configuration")
        return self.legacy_audit()

    def _run_gpu_audit(self) -> str | None:
        if not self.config.policy.gpu_audit_required:
            self._gpu_audit_failure = None
            return None
        try:
            result = self.gpu_audit()
        except Exception as exc:
            self._gpu_audit_failure = f"audit runner crashed: {exc}"
            logger.exception("GPU permission audit crashed")
            return self._gpu_audit_failure
        self._gpu_fingerprint = result.fingerprint or self._gpu_fingerprint
        if not result.passed:
            self._gpu_audit_failure = result.failure_message()
            logger.error("GPU permission audit failed: %s", self._gpu_audit_failure)
            return self._gpu_audit_failure
        self._gpu_audit_failure = None
        return None

    def _run_quota_check(self) -> str | None:
        required = self.config.policy.workspace_quota_required or (
            self.config.policy.max_workspace_gb > 0
        )
        if not required:
            self._quota_failure = None
            return None
        try:
            result = self.quota_check()
        except Exception as exc:
            self._quota_failure = f"quota checker crashed: {exc}"
            logger.exception("workspace quota gate crashed")
            return self._quota_failure
        if not result.passed:
            self._quota_failure = result.failure_message()
            logger.error("workspace quota gate failed: %s", self._quota_failure)
            return self._quota_failure
        self._quota_failure = None
        return None

    def _container_storage_params(self) -> dict[str, int]:
        """Global writable-layer accounting parameters for capacity checks.

        Only the development profile has a per-container writable-layer quota,
        so the budget applies there; in the restricted profile every value is
        zero and the check is skipped.
        """

        if self.config.policy.container_profile != "development":
            return {"writable_layer_gb": 0, "docker_storage_budget_gb": 0}
        return {
            "writable_layer_gb": self.config.policy.writable_layer_gb,
            "docker_storage_budget_gb": self.config.policy.docker_storage_budget_gb,
        }

    def _writable_layer_reservation_bytes(self, *, include_next_slot: bool) -> int:
        """Worst-case writable-layer growth promised to live allocations.

        Every pending/active/cleanup_pending allocation may still grow to its
        full quota, and a candidate allocation adds one more slot. Used to
        constrain admission by CURRENT free space, not filesystem capacity.
        A database failure fails closed with the full budget.
        """

        policy = self.config.policy
        if policy.container_profile != "development" or policy.writable_layer_gb <= 0:
            return 0
        try:
            with self._connection() as connection:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM allocations "
                        "WHERE status IN ('pending', 'active', 'cleanup_pending')"
                    ).fetchone()[0]
                )
        except Exception:
            logger.exception("could not count writable-layer reservations")
            return policy.docker_storage_budget_gb * 1024**3
        if include_next_slot:
            count += 1
        return count * policy.writable_layer_gb * 1024**3

    def _run_storage_check(self, *, include_next_slot: bool = False) -> str | None:
        try:
            passed, detail = self.docker.storage_preflight(
                writable_layer_entitlements_bytes=self._writable_layer_reservation_bytes(
                    include_next_slot=include_next_slot
                )
            )
        except Exception as exc:
            passed, detail = False, str(exc)
        if not passed:
            self._storage_failure = detail
            logger.error("Docker storage preflight failed: %s", detail)
            return detail
        self._storage_failure = None
        return None

    def _refresh_runtime_gates(self, *, include_storage: bool = True) -> str | None:
        failures: list[str] = []
        gpu_failure = self._run_gpu_audit()
        if gpu_failure:
            failures.append(f"GPU permission audit: {gpu_failure}")
        quota_failure = self._run_quota_check()
        if quota_failure:
            failures.append(f"workspace quota: {quota_failure}")
        if include_storage:
            storage_failure = self._run_storage_check()
            if storage_failure:
                failures.append(f"Docker storage: {storage_failure}")
        if failures:
            return "; ".join(failures)
        return None

    def _gate_failure(self) -> str | None:
        parts: list[str] = []
        if self._gpu_audit_failure:
            parts.append(f"GPU permission audit: {self._gpu_audit_failure}")
        if self._quota_failure:
            parts.append(f"workspace quota: {self._quota_failure}")
        if self._storage_failure:
            parts.append(f"Docker storage: {self._storage_failure}")
        return "; ".join(parts) if parts else None

    def _capabilities(self) -> dict[str, bool]:
        """Server-side feature flags; clients must defer to these instead of
        their own (possibly stale) local configuration."""

        return {
            "vscode": self.config.policy.vscode_enabled,
            "file_transfer": self.config.policy.file_transfer_enabled,
            "development_profile": self.config.policy.container_profile == "development",
        }

    def _live_resources(self) -> LiveResources:
        try:
            gpus = self.docker.list_gpus()
            processes = self.docker.list_compute_processes()
            containers = self.docker.list_containers()
        except Exception as exc:
            logger.warning("live resource inspection failed: %s", exc)
            return LiveResources([], [], [], str(exc))
        return LiveResources(gpus, containers, processes)

    @staticmethod
    def _resolve_gpu_refs(
        refs: tuple[str, ...],
        gpus: list[dict[str, Any]],
    ) -> tuple[set[str], bool]:
        by_uuid = {str(gpu["uuid"]): str(gpu["uuid"]) for gpu in gpus}
        by_index = {str(gpu["index"]): str(gpu["uuid"]) for gpu in gpus}
        resolved: set[str] = set()
        unknown = False
        for raw in refs:
            value = str(raw).strip()
            if value in {"", "none", "void"}:
                continue
            if value in {"all", "any"}:
                resolved.update(by_uuid.values())
                continue
            if value in by_uuid:
                resolved.add(by_uuid[value])
                continue
            if value in by_index:
                resolved.add(by_index[value])
                continue
            unknown = True
        return resolved, unknown

    def _managed_container_matches(
        self,
        container: ContainerInfo,
        allocations: dict[str, dict[str, Any]],
        gpus: list[dict[str, Any]],
    ) -> bool:
        if container.labels.get("udc.managed") != "true":
            return False
        allocation_id = container.labels.get("udc.allocation")
        if not allocation_id:
            return False
        allocation = allocations.get(allocation_id)
        if allocation is None:
            return False
        expected_container = str(allocation.get("container_id") or "")
        if not expected_container or container.id != expected_container:
            return False
        if container.labels.get("udc.owner") != allocation.get("owner"):
            return False
        resolved, unknown = self._resolve_gpu_refs(container.gpu_refs, gpus)
        if unknown:
            return False
        expected_gpus = {str(item) for item in allocation.get("gpu_uuids", [])}
        return resolved == expected_gpus

    def _container_gpu_holders(
        self,
        containers: list[ContainerInfo],
        gpus: list[dict[str, Any]],
        allocations: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, list[ContainerInfo]], bool]:
        holders: dict[str, list[ContainerInfo]] = {}
        unknown = False
        for container in containers:
            if container.status not in ACTIVE_GPU_HOLDER_STATUSES:
                continue
            if self._managed_container_matches(container, allocations, gpus):
                continue
            resolved, bad_ref = self._resolve_gpu_refs(container.gpu_refs, gpus)
            unknown = unknown or bad_ref
            for uuid in resolved:
                holders.setdefault(uuid, []).append(container)
        return holders, unknown

    def _merge_gpus(
        self,
        db_gpus: list[dict[str, Any]],
        live: LiveResources,
        allocations: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        live_by_uuid = {gpu.uuid: gpu for gpu in live.gpus}
        processes_by_uuid: dict[str, list[GPUProcess]] = {}
        for process in live.processes:
            processes_by_uuid.setdefault(process.gpu_uuid, []).append(process)
        holders, unknown_refs = self._container_gpu_holders(
            live.containers,
            db_gpus,
            allocations,
        )
        result: list[dict[str, Any]] = []
        known_uuids: set[str] = set()
        for gpu in db_gpus:
            uuid = str(gpu["uuid"])
            known_uuids.add(uuid)
            live_gpu = live_by_uuid.get(uuid)
            processes = processes_by_uuid.get(uuid, [])
            pids = set(live_gpu.compute_pids if live_gpu else ())
            pids.update(process.pid for process in processes)
            used_mib = live_gpu.used_mib if live_gpu else 0
            if processes:
                used_mib = max(used_mib, max(process.used_mib for process in processes))
            item = dict(gpu)
            item["live_used_mib"] = used_mib if live_gpu or processes else None
            item["utilization"] = live_gpu.utilization if live_gpu else None
            item["compute_pids"] = sorted(pids)
            holder_ids = [container.id for container in holders.get(uuid, [])]
            item["unmanaged_container_ids"] = holder_ids
            if item.get("allocation_id"):
                state = "allocated"
                reason = "database claim"
            elif live.error:
                state = "unknown"
                reason = f"live detection failed: {live.error}"
            elif live_gpu is None:
                state = "unknown"
                reason = "GPU is absent from live nvidia-smi inventory"
            elif pids or used_mib > 0:
                state = "unmanaged"
                reason = "live compute process or used memory"
            elif holder_ids:
                state = "unmanaged"
                reason = "unmanaged container holds GPU access"
            elif unknown_refs:
                state = "unknown"
                reason = "unresolved unmanaged container GPU reference"
            else:
                state = "free"
                reason = "no claim, no live compute process, no container holder"
            item["state"] = state
            item["state_reason"] = reason
            result.append(item)
        for live_gpu in live.gpus:
            if live_gpu.uuid in known_uuids:
                continue
            result.append(
                {
                    "index": live_gpu.index,
                    "uuid": live_gpu.uuid,
                    "name": live_gpu.name,
                    "memory_mib": live_gpu.memory_mib,
                    "state": "unknown",
                    "state_reason": "live GPU is not in the database inventory",
                    "allocation_id": None,
                    "owner": None,
                    "status": None,
                    "expires_at": None,
                    "remaining_seconds": None,
                    "container_id": None,
                    "live_used_mib": live_gpu.used_mib,
                    "utilization": live_gpu.utilization,
                    "compute_pids": list(live_gpu.compute_pids),
                    "unmanaged_container_ids": [],
                }
            )
        return sorted(result, key=lambda item: int(item["index"]))

    def initialize(self) -> None:
        connection = init_db(self.db_path)
        try:
            self._mode = store.get_mode(connection, self.config.policy.mode)
            leases = store.active_lease_count(connection)
            queued = store.queue_request_count(connection, now=self._now())
            target = self._mode
            audit_failure = ""
            runtime_failure = ""
            policy_failure = ""
            if self._mode == "active":
                try:
                    validate_active_mode_policy(self.config.policy)
                except ValueError as exc:
                    # A persisted active mode must never outlive the policy
                    # gates it was approved with.
                    policy_failure = str(exc)
            if self._mode == "active" and self.config.policy.legacy_audit_required:
                audit = self._legacy_audit_result()
                if not audit.passed:
                    audit_failure = audit.failure_message()
            if self._mode == "active":
                runtime_failure = self._refresh_runtime_gates() or ""
            if self._mode == "monitor" and (leases or queued):
                target = "maintenance"
            elif self._mode == "active" and not self.config.policy.deployment_allowed:
                target = "maintenance" if (leases or queued) else "monitor"
            elif self._mode == "active" and (policy_failure or audit_failure or runtime_failure):
                target = "maintenance" if (leases or queued) else "monitor"
            if target != self._mode:
                logger.warning(
                    "adjusting persisted mode %s -> %s "
                    "(leases=%d queued=%d deployment_allowed=%s policy=%s audit=%s runtime=%s)",
                    self._mode,
                    target,
                    leases,
                    queued,
                    self.config.policy.deployment_allowed,
                    policy_failure or "passed",
                    audit_failure or "passed",
                    runtime_failure or "passed",
                )
                store.set_mode(
                    connection,
                    target,
                    actor_uid=0,
                    now=self._now(),
                )
                self._mode = target
        finally:
            connection.close()
        try:
            gpus = self.docker.list_gpus()
        except Exception:
            logger.exception("Docker GPU discovery failed")
            gpus = []
        if gpus:
            with self._connection() as connection:
                store.upsert_gpus(connection, gpus)

    async def start(self) -> None:
        await asyncio.to_thread(self.initialize)
        try:
            summary = await asyncio.to_thread(self.reconcile_once)
            logger.info("startup reconcile: %s", summary)
        except Exception:
            logger.exception("startup reconcile failed")
        try:
            admitted = await self._schedule_queue()
            if admitted:
                logger.info("startup queue admission: %s", ", ".join(admitted))
        except Exception:
            logger.exception("startup queue scheduling failed")
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise RuntimeError(f"refusing to replace non-socket path: {path}")
            path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(path),
            limit=MAX_MESSAGE_BYTES,
        )
        self._secure_socket(path)
        self._expiry_task = asyncio.create_task(self._expiry_loop())
        logger.info("broker listening on %s in %s mode", path, self._mode)

    def _secure_socket(self, path: Path) -> None:
        os.chmod(path, 0o660)
        if os.name != "posix" or os.geteuid() != 0:
            return
        try:
            import grp

            gid = grp.getgrnam(self.config.groups.cli).gr_gid
            os.chown(path, 0, gid)
        except (ImportError, KeyError):
            logger.warning("could not chown %s to group %s", path, self.config.groups.cli)

    def reconcile_once(self) -> dict[str, int]:
        if self._mode not in {"active", "maintenance"}:
            logger.info("reconcile skipped in %s mode", self._mode)
            return {"skipped": 1}
        with self._connection() as connection:
            return reconcile_allocations(connection, self.docker, now=self._now())

    async def expire_once(self) -> list[str]:
        if self._mode not in {"active", "maintenance"}:
            return []
        return await asyncio.to_thread(self._expire_once)

    def _expire_once(self) -> list[str]:
        with self._connection() as connection:
            expired = expire_allocations(connection, self.docker, now=self._now())
            store.expire_queue_requests(connection, now=self._now())
        for allocation_id in expired:
            self._cancel_tunnels(allocation_id)
        return expired

    async def _expiry_loop(self) -> None:
        elapsed = 0.0
        audit_elapsed = 0.0
        while True:
            await asyncio.sleep(self.expiry_interval)
            elapsed += self.expiry_interval
            audit_elapsed += self.expiry_interval
            try:
                fingerprint = await asyncio.to_thread(device_fingerprint)
            except Exception:
                logger.exception("could not fingerprint GPU device nodes")
                fingerprint = self._gpu_fingerprint
            due = self.config.policy.gpu_audit_required and (
                audit_elapsed >= self.config.policy.gpu_audit_interval
                or fingerprint != self._gpu_fingerprint
            )
            if due:
                audit_elapsed = 0.0
                try:
                    failure = await asyncio.to_thread(self._run_gpu_audit)
                    if failure:
                        logger.error(
                            "continuous GPU audit failed; new allocations are blocked: %s",
                            failure,
                        )
                    else:
                        logger.info("continuous GPU audit passed")
                except Exception:
                    self._gpu_audit_failure = "continuous GPU audit crashed"
                    logger.exception("continuous GPU audit crashed")
            if elapsed >= self.reconcile_interval:
                elapsed = 0.0
                try:
                    summary = await asyncio.to_thread(self.reconcile_once)
                    self._cancel_inactive_tunnels()
                    logger.info("periodic reconcile: %s", summary)
                except Exception:
                    logger.exception("periodic reconcile failed")
            try:
                expired = await self.expire_once()
                if expired:
                    logger.info("expired allocations: %s", ", ".join(expired))
            except Exception:
                logger.exception("expiry tick failed")
            if self._mode == "active":
                try:
                    admitted = await self._schedule_queue()
                    if admitted:
                        logger.info("queue admissions: %s", ", ".join(admitted))
                except Exception:
                    logger.exception("queue scheduling failed")

    async def stop(self) -> None:
        for allocation_id in list(self._tunnels):
            self._cancel_tunnels(allocation_id)
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            self._expiry_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def run(self) -> None:
        await self.start()
        assert self._server is not None
        try:
            await self._server.serve_forever()
        finally:
            await self.stop()

    async def _send(self, writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
        payload = json.dumps(response, separators=(",", ":")) + "\n"
        writer.write(payload.encode("utf-8"))
        await writer.drain()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            sock = writer.get_extra_info("socket")
            pid, uid, gid = peer_credentials(sock)
        except Exception:
            logger.exception("could not read peer credentials")
            await self._send(writer, _error("forbidden", "peer identity is unavailable"))
            writer.close()
            await writer.wait_closed()
            return
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=CONNECTION_TIMEOUT)
                except TimeoutError:
                    break
                except ValueError:
                    await self._send(
                        writer,
                        _error("bad_request", "message is too large"),
                    )
                    break
                if not line:
                    break
                if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                    await self._send(
                        writer,
                        _error("bad_request", "message is too large or lacks a newline"),
                    )
                    break
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    await self._send(writer, _error("bad_request", "invalid JSON"))
                    continue
                if isinstance(payload, dict) and payload.get("cmd") == "shell":
                    await self._handle_shell(
                        payload,
                        reader=reader,
                        writer=writer,
                        uid=uid,
                        gid=gid,
                    )
                    break
                if isinstance(payload, dict) and payload.get("cmd") in {
                    "file_put",
                    "file_get",
                }:
                    await self._handle_file(
                        payload,
                        reader=reader,
                        writer=writer,
                        uid=uid,
                        gid=gid,
                    )
                    break
                if isinstance(payload, dict) and payload.get("cmd") == "tunnel":
                    await self._handle_tunnel(
                        payload,
                        reader=reader,
                        writer=writer,
                        uid=uid,
                        gid=gid,
                    )
                    break
                response = await self.handle_request(payload, uid=uid, gid=gid, pid=pid)
                await self._send(writer, response)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def _handle_file(
        self,
        payload: Any,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid: int,
        gid: int,
    ) -> None:
        upload: workspace_io.UploadHandle | None = None
        download_fd: int | None = None
        committed = False
        try:
            identity = self.identity_resolver.resolve(uid, gid)
            with self._connection() as connection:
                store.ensure_user(
                    connection,
                    uid=identity.uid,
                    username=identity.username,
                    role=identity.role,
                    now=self._now(),
                )
            command, args = self._parse_request(payload)
            if command not in {"file_put", "file_get"}:
                raise BadRequest("expected file transfer command")
            self._check_args(args, {"id", "path"})
            if not self.config.policy.file_transfer_enabled:
                raise Forbidden("file transfer is disabled by policy")
            allocation_id = args.get("id")
            if not isinstance(allocation_id, str) or not allocation_id:
                raise BadRequest("id must be a non-empty string")
            with self._connection() as connection:
                allocation = store.get_allocation(connection, allocation_id, now=self._now())
            if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may transfer files")
            if allocation["status"] != "active":
                raise Conflict(f"allocation is {allocation['status']}, not active")
            parts = workspace_io.relative_parts(args.get("path"))
            relative = PurePosixPath(*parts).as_posix()
            workspace = str(allocation["workspace"])
            if not workspace_io.supported():
                raise InternalError("descriptor-relative file access is unavailable")
            if command == "file_get":
                download_fd = await asyncio.to_thread(
                    workspace_io.open_read_file,
                    workspace,
                    parts,
                )
            else:
                owner_uid, owner_gid = self._allocation_owner_ids(allocation)
                upload = await asyncio.to_thread(
                    workspace_io.open_upload,
                    workspace,
                    parts,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
        except (KeyError, RuntimeError) as exc:
            if upload is not None:
                upload.abort()
            if download_fd is not None:
                os.close(download_fd)
            await self._send(writer, _error("forbidden", f"peer identity failed: {exc}"))
            return
        except UdcError as exc:
            if upload is not None:
                upload.abort()
            if download_fd is not None:
                os.close(download_fd)
            await self._send(writer, _error(exc.code, exc.message))
            return
        except Exception:
            if upload is not None:
                upload.abort()
            if download_fd is not None:
                os.close(download_fd)
            logger.exception("could not prepare file transfer")
            await self._send(writer, _error("internal", "could not prepare file transfer"))
            return

        try:
            await self._send(
                writer,
                _ok({"id": allocation_id, "path": relative}),
            )
        except Exception:
            if upload is not None:
                upload.abort()
            if download_fd is not None:
                os.close(download_fd)
            raise
        if command == "file_get":
            try:
                assert download_fd is not None
                with os.fdopen(download_fd, "rb") as handle:
                    download_fd = None
                    while chunk := handle.read(65536):
                        await write_frame(writer, FRAME_DATA, chunk)
                await write_frame(
                    writer,
                    FRAME_EXIT,
                    json.dumps(
                        {"ok": True, "data": {"path": relative}},
                        sort_keys=True,
                    ).encode("utf-8"),
                )
                with self._connection() as connection:
                    store.record_event(
                        connection,
                        actor_uid=identity.uid,
                        action="file_get",
                        allocation_id=allocation_id,
                        detail={"path": relative},
                        now=self._now(),
                    )
                    connection.commit()
            except Exception:
                logger.exception("file get failed")
                try:
                    await write_frame(
                        writer,
                        FRAME_EXIT,
                        json.dumps(
                            {
                                "ok": False,
                                "error": {
                                    "code": "internal",
                                    "message": "file download failed",
                                },
                            },
                            sort_keys=True,
                        ).encode("utf-8"),
                    )
                except Exception:
                    pass
            finally:
                if download_fd is not None:
                    os.close(download_fd)
            return

        written = 0
        assert upload is not None
        try:
            while True:
                frame = await read_frame(reader)
                if frame is None:
                    raise BadRequest("file upload ended without an exit frame")
                kind, data = frame
                if kind == FRAME_DATA:
                    if written + len(data) > self.config.policy.max_upload_bytes:
                        raise BadRequest(
                            "upload exceeds policy.max_upload_bytes "
                            f"({self.config.policy.max_upload_bytes})"
                        )
                    view = memoryview(data)
                    while view:
                        piece = view[:65536]
                        count = os.write(upload.fd, piece)
                        if count <= 0:
                            raise InternalError("short write while uploading")
                        written += count
                        view = view[count:]
                elif kind == FRAME_EXIT:
                    break
                else:
                    raise BadRequest("unexpected file transfer frame")
            upload.commit()
            committed = True
            with self._connection() as connection:
                store.record_event(
                    connection,
                    actor_uid=identity.uid,
                    action="file_put",
                    allocation_id=allocation_id,
                    detail={"path": relative, "bytes": written},
                    now=self._now(),
                )
                connection.commit()
            await write_frame(
                writer,
                FRAME_EXIT,
                json.dumps(
                    {
                        "ok": True,
                        "data": {"path": relative, "bytes": written},
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            )
        except Exception as exc:
            if not committed:
                upload.abort()
            logger.exception("file put failed")
            error = exc if isinstance(exc, UdcError) else InternalError(str(exc))
            try:
                await write_frame(
                    writer,
                    FRAME_EXIT,
                    json.dumps(
                        {
                            "ok": False,
                            "error": {"code": error.code, "message": error.message},
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
            except Exception:
                pass

    async def _handle_shell(
        self,
        payload: Any,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid: int,
        gid: int,
    ) -> None:
        try:
            identity = self.identity_resolver.resolve(uid, gid)
            with self._connection() as connection:
                store.ensure_user(
                    connection,
                    uid=identity.uid,
                    username=identity.username,
                    role=identity.role,
                    now=self._now(),
                )
            command, args = self._parse_request(payload)
            if command != "shell":
                raise BadRequest("expected shell command")
            self._check_args(args, {"id", "cols", "rows", "job"})
            allocation_id = args.get("id")
            if not isinstance(allocation_id, str) or not allocation_id:
                raise BadRequest("id must be a non-empty string")
            cols = self._parse_terminal_dimension(args.get("cols", 80), "cols")
            rows = self._parse_terminal_dimension(args.get("rows", 24), "rows")
            with self._connection() as connection:
                allocation = store.get_allocation(
                    connection,
                    allocation_id,
                    now=self._now(),
                )
            if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may open this shell")
            if allocation["status"] != "active":
                raise Conflict(f"allocation is {allocation['status']}, not active")
            container_id = str(allocation["container_id"])
            if not container_id:
                raise NotFound("allocation has no container")
            shell_command: list[str] | None = None
            job = args.get("job")
            if job is not None:
                job_name = self._job_name(job)
                jobs = await asyncio.to_thread(self.docker.list_jobs, container_id)
                if not any(item.get("name") == job_name for item in jobs):
                    raise NotFound(f"job not found: {job_name}")
                shell_command = [
                    "env",
                    "TERM=xterm-256color",
                    "tmux",
                    "attach-session",
                    "-t",
                    f"udc-{job_name}",
                ]
            session = await asyncio.to_thread(
                self.docker.open_shell,
                container_id,
                cols,
                rows,
                shell_command,
            )
        except (KeyError, RuntimeError) as exc:
            await self._send(writer, _error("forbidden", f"peer identity failed: {exc}"))
            return
        except UdcError as exc:
            await self._send(writer, _error(exc.code, exc.message))
            return
        except Exception:
            logger.exception("could not open shell")
            await self._send(writer, _error("internal", "could not open shell"))
            return
        await self._send(
            writer,
            _ok({"id": allocation_id, "cols": cols, "rows": rows}),
        )
        await self._relay_shell(reader, writer, session)

    async def _relay_shell(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: ShellSession,
    ) -> None:
        async def pump_output() -> None:
            try:
                while True:
                    data = await asyncio.to_thread(session.read, 65536)
                    if not data:
                        break
                    await write_frame(writer, FRAME_DATA, data)
                await write_frame(writer, FRAME_EXIT, exit_payload("shell exited"))
            except Exception:
                logger.exception("shell output pump failed")

        async def pump_input() -> None:
            while True:
                frame = await read_frame(reader)
                if frame is None:
                    return
                kind, payload = frame
                if kind == FRAME_DATA:
                    await asyncio.to_thread(session.write, payload)
                elif kind == FRAME_RESIZE:
                    cols, rows = parse_resize(payload)
                    await asyncio.to_thread(session.resize, cols, rows)
                elif kind == FRAME_EXIT:
                    return

        tasks = [
            asyncio.create_task(pump_output()),
            asyncio.create_task(pump_input()),
        ]
        try:
            _done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
        finally:
            try:
                await asyncio.to_thread(session.close)
            except Exception:
                logger.warning("could not close shell session", exc_info=True)
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _parse_terminal_dimension(raw: Any, name: str) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise BadRequest(f"{name} must be an integer")
        if not 1 <= raw <= 1000:
            raise BadRequest(f"{name} is out of range")
        return raw

    def _container_home(self, username: str, uid: int) -> str:
        configured = self.config.policy.container_home.strip()
        if configured:
            home = configured
        else:
            safe = re.sub(r"[^a-z0-9_-]", "-", username.lower())
            home = f"/home/{safe or uid}"
        if ".." in PurePosixPath(home).parts or not home.startswith("/home/") or home == "/home/":
            raise Conflict("development container HOME must be under /home")
        return home

    @staticmethod
    def _member_public_keys(username: str) -> list[str]:
        if os.name != "posix":
            return []
        try:
            import pwd

            record = pwd.getpwnam(username)
            home = record.pw_dir
        except (ImportError, KeyError):
            return []
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = os.O_RDONLY | os.O_DIRECTORY | nofollow
        home_fd = ssh_fd = key_fd = -1
        try:
            home_fd = os.open(home, directory)
            ssh_fd = os.open(".ssh", directory, dir_fd=home_fd)
            key_fd = os.open("authorized_keys", os.O_RDONLY | nofollow, dir_fd=ssh_fd)
            info = os.fstat(key_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
                return []
            if info.st_uid not in {record.pw_uid, 0}:
                return []
            with os.fdopen(key_fd, "r", encoding="utf-8", errors="replace") as handle:
                key_fd = -1
                text = handle.read()
        except OSError:
            return []
        finally:
            for fd in (key_fd, ssh_fd, home_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        keys: list[str] = []
        key_types = ("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-")
        for line in text.splitlines():
            parts = line.strip().split()
            if not parts or parts[0].startswith("#"):
                continue
            for index, token in enumerate(parts):
                if token.startswith(key_types) and index + 1 < len(parts):
                    key = f"{token} {parts[index + 1]}"
                    if key not in keys:
                        keys.append(key)
                    break
        return keys[:20]

    def _cancel_tunnels(self, allocation_id: str) -> None:
        for task in list(self._tunnels.pop(allocation_id, set())):
            task.cancel()

    def _cancel_inactive_tunnels(self) -> None:
        """Close tunnels whose allocation is no longer active after reconcile."""

        if not self._tunnels:
            return
        try:
            with self._connection() as connection:
                active = {
                    str(row["id"])
                    for row in store.list_allocations(
                        connection,
                        statuses=("active",),
                        now=self._now(),
                    )
                }
        except Exception:
            logger.exception("could not check active allocations for tunnel cleanup")
            return
        for allocation_id in list(self._tunnels):
            if allocation_id not in active:
                logger.info("closing tunnels for inactive allocation %s", allocation_id)
                self._cancel_tunnels(allocation_id)

    async def _handle_tunnel(
        self,
        payload: Any,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid: int,
        gid: int,
    ) -> None:
        task = asyncio.current_task()
        allocation_id = ""
        session: ShellSession | None = None
        try:
            try:
                if not self.config.policy.vscode_enabled:
                    raise Forbidden("VS Code tunnels are disabled by policy")
                if self.config.policy.container_profile != "development":
                    raise Conflict("VS Code tunnels require the development container profile")
                try:
                    identity = self.identity_resolver.resolve(uid, gid)
                except (KeyError, RuntimeError) as exc:
                    raise Forbidden(f"peer identity failed: {exc}") from exc
                with self._connection() as connection:
                    store.ensure_user(
                        connection,
                        uid=identity.uid,
                        username=identity.username,
                        role=identity.role,
                        now=self._now(),
                    )
                command, args = self._parse_request(payload)
                if command != "tunnel":
                    raise BadRequest("expected tunnel command")
                self._check_args(args, {"id"})
                raw_id = args.get("id")
                if not isinstance(raw_id, str) or not raw_id:
                    raise BadRequest("id must be a non-empty string")
                allocation_id = raw_id
                with self._connection() as connection:
                    allocation = store.get_allocation(
                        connection,
                        allocation_id,
                        now=self._now(),
                    )
                if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                    raise Forbidden("only the owner or an admin may open this tunnel")
                if allocation["status"] != "active":
                    raise Conflict(f"allocation is {allocation['status']}, not active")
                container_id = str(allocation["container_id"])
                if not container_id:
                    raise NotFound("allocation has no container")

                # Reserve the slot before the first blocking call. The check and
                # insert are synchronous in the event loop, so concurrent
                # connections cannot all observe the same free count.
                active = self._tunnels.setdefault(allocation_id, set())
                if task is not None:
                    active.discard(task)
                if len(active) >= self.config.policy.vscode_max_tunnels:
                    raise Conflict("too many open VS Code tunnels for this allocation")
                if task is not None:
                    active.add(task)

                owner_uid, owner_gid = self._allocation_owner_ids(allocation)
                # Use the allocation owner for the container passwd entry and
                # HOME. The CLI connects as this user; an administrator opening
                # the tunnel acts as the owner and only contributes a key.
                container_user = str(allocation["owner"])
                if not container_user:
                    raise NotFound("allocation owner is missing")
                if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", container_user):
                    raise Conflict(
                        "allocation owner name is not supported by the container SSH profile"
                    )
                # Authorize the allocation owner plus the current connecter:
                # an administrator opening a member's allocation must not
                # revoke the member's key material, and vice versa.
                connecter_keys = await asyncio.to_thread(
                    self._member_public_keys,
                    identity.username,
                )
                owner_keys = await asyncio.to_thread(
                    self._member_public_keys,
                    container_user,
                )
                keys: list[str] = []
                for key in [*owner_keys, *connecter_keys]:
                    key = key.strip()
                    if key and key not in keys:
                        keys.append(key)
                if not keys:
                    raise Conflict("no SSH public keys found for this allocation on the host")
                home = self._container_home(container_user, owner_uid)
                await asyncio.to_thread(
                    self.docker.prepare_container_ssh,
                    container_id,
                    owner_uid,
                    owner_gid,
                    container_user,
                    keys,
                    home,
                )
                session = await asyncio.to_thread(self.docker.open_tunnel, container_id)
            except UdcError as exc:
                await self._send(writer, _error(exc.code, exc.message))
                return
            except Exception:
                logger.exception("could not open VS Code tunnel")
                await self._send(writer, _error("internal", "could not open VS Code tunnel"))
                return

            active_session = session
            assert active_session is not None
            try:
                await self._send(writer, _ok({"id": allocation_id, "protocol": "ssh"}))
                await self._relay_tunnel(reader, writer, active_session)
            finally:
                try:
                    await asyncio.to_thread(active_session.close)
                except Exception:
                    logger.warning("could not close VS Code tunnel session", exc_info=True)
        finally:
            if task is not None:
                remaining = self._tunnels.get(allocation_id)
                if remaining is not None:
                    remaining.discard(task)
                    if not remaining:
                        self._tunnels.pop(allocation_id, None)

    async def _relay_tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: ShellSession,
    ) -> None:
        async def pump_input() -> None:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                await asyncio.to_thread(session.write, data)

        async def pump_output() -> None:
            while True:
                data = await asyncio.to_thread(session.read, 65536)
                if not data:
                    return
                writer.write(data)
                await writer.drain()

        tasks = [
            asyncio.create_task(pump_input()),
            asyncio.create_task(pump_output()),
        ]
        try:
            _done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in tasks:
                task.cancel()
            # A blocking recv() cannot be cancelled directly. Closing the
            # session socket unblocks the worker before we join it.
            try:
                await asyncio.to_thread(session.close)
            except Exception:
                logger.warning("could not close tunnel session", exc_info=True)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle_request(
        self,
        payload: Any,
        *,
        uid: int,
        gid: int,
        pid: int = 0,
    ) -> dict[str, Any]:
        try:
            identity = self.identity_resolver.resolve(uid, gid)
        except (KeyError, RuntimeError) as exc:
            return _error("forbidden", f"peer identity could not be resolved: {exc}")
        try:
            with self._connection() as connection:
                store.ensure_user(
                    connection,
                    uid=identity.uid,
                    username=identity.username,
                    role=identity.role,
                    now=self._now(),
                )
            command, args = self._parse_request(payload)
            if command == "ping":
                self._check_args(args)
                return _ok(
                    {
                        "uid": identity.uid,
                        "username": identity.username,
                        "role": identity.role,
                        "admin": identity.is_admin,
                        "pid": pid,
                        "mode": self._mode,
                    }
                )
            if command == "list":
                self._check_args(args)
                return _ok(await asyncio.to_thread(self._list))
            if command == "status":
                self._check_args(args, {"id"})
                return _ok(self._status(identity, args))
            if command == "apply":
                self._check_args(
                    args,
                    {"gpus", "cpus", "memory_gb", "hours", "image", "note", "queue"},
                )
                return _ok(await self._apply(identity, args))
            if command == "queue_list":
                self._check_args(args, {"all", "include_finished"})
                return _ok(self._queue_list(identity, args))
            if command == "queue_get":
                self._check_args(args, {"id"})
                return _ok(self._queue_get(identity, args))
            if command == "queue_cancel":
                self._check_args(args, {"id", "reason"})
                return _ok(self._queue_cancel(identity, args))
            if command == "release":
                self._check_args(args, {"id"})
                return _ok(await self._release(identity, args))
            if command == "logs":
                self._check_args(args, {"id", "tail"})
                return _ok(await self._logs(identity, args))
            if command == "job_start":
                self._check_args(args, {"id", "name", "command"})
                return _ok(await self._job_start(identity, args))
            if command == "job_list":
                self._check_args(args, {"id"})
                return _ok(await self._job_list(identity, args))
            if command == "job_files":
                self._check_args(args, {"id"})
                return _ok(await self._job_files(identity, args))
            if command == "job_logs":
                self._check_args(args, {"id", "name", "tail"})
                return _ok(await self._job_logs(identity, args))
            if command == "job_stop":
                self._check_args(args, {"id", "name"})
                return _ok(await self._job_stop(identity, args))
            if command == "admin_users":
                self._check_args(args)
                self._require_admin(identity)
                return _ok(self._admin_users())
            if command == "admin_resources":
                self._check_args(args)
                self._require_admin(identity)
                return _ok(await asyncio.to_thread(self._admin_resources))
            if command == "admin_audit":
                self._check_args(args, {"limit"})
                self._require_admin(identity)
                return _ok(self._admin_audit(args))
            if command == "admin_mode":
                self._check_args(args, {"mode"})
                self._require_admin(identity)
                return _ok(await self._admin_mode(identity, args))
            if command == "admin_release":
                self._check_args(args, {"id", "reason"})
                self._require_admin(identity)
                return _ok(await self._release(identity, args, admin=True))
            if command == "admin_reconcile":
                self._check_args(args)
                self._require_admin(identity)
                summary = await asyncio.to_thread(self.reconcile_once)
                self._cancel_inactive_tunnels()
                return _ok(summary)
            raise BadRequest(f"unknown command: {command}")
        except UdcError as exc:
            return _error(exc.code, exc.message)
        except Exception:
            logger.exception("unhandled broker request failure")
            return _error("internal", "internal broker error")

    def _parse_request(self, payload: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise BadRequest("request must be a JSON object")
        unknown = set(payload) - {"cmd", "args"}
        if unknown:
            raise BadRequest(f"unexpected request field(s): {', '.join(sorted(unknown))}")
        command = payload.get("cmd")
        if not isinstance(command, str) or not command:
            raise BadRequest("request cmd must be a non-empty string")
        args = payload.get("args", {})
        if not isinstance(args, dict):
            raise BadRequest("request args must be a JSON object")
        return command, args

    @staticmethod
    def _check_args(args: dict[str, Any], allowed: set[str] | None = None) -> None:
        extra = set(args) - (allowed or set())
        if extra:
            raise BadRequest(f"unexpected argument(s): {', '.join(sorted(extra))}")

    def _list(self) -> dict[str, Any]:
        now = self._now()
        live = self._live_resources()
        with self._connection() as connection:
            persisted = store.get_mode(connection, self._mode)
            if persisted == "monitor" and (
                store.active_lease_count(connection)
                or store.queue_request_count(connection, now=now)
            ):
                logger.warning(
                    "promoting monitor to maintenance while active/pending/queued work exists"
                )
                store.set_mode(connection, "maintenance", actor_uid=0, now=now)
                persisted = "maintenance"
            self._mode = persisted
            db_gpus = store.list_gpus(connection, now=now)
            allocations = store.list_allocations(connection, now=now)
            queue = store.list_queue_requests(connection, now=now)
            active_allocations = self._allocation_map(connection, now)
        data: dict[str, Any] = {
            "server": self.config.server.name,
            "mode": self._mode,
            "now": now.isoformat(),
            "detection_error": live.error,
            "gpus": self._merge_gpus(db_gpus, live, active_allocations),
            "allocations": allocations,
            "queue": queue,
            "unmanaged": self._unmanaged(db_gpus, active_allocations, live),
            "gpu_audit": {
                "required": self.config.policy.gpu_audit_required,
                "ok": self._gpu_audit_failure is None,
                "message": self._gpu_audit_failure or "",
            },
            "workspace_quota": {
                "required": self.config.policy.workspace_quota_required,
                "ok": self._quota_failure is None,
                "message": self._quota_failure or "",
            },
            "storage": {
                "required": (
                    self.config.policy.container_profile == "development"
                    or self.config.policy.min_free_docker_gb > 0
                ),
                "ok": self._storage_failure is None,
                "message": self._storage_failure or "",
            },
            "capabilities": self._capabilities(),
        }
        self._add_member_identity(data)
        return data

    def _load_member_directory(self) -> dict[str, Any]:
        try:
            return load_members(self.config.server.members_file)
        except (OSError, ValueError) as exc:
            logger.warning(
                "could not load member directory %s: %s",
                self.config.server.members_file,
                exc,
            )
            return {}

    def _add_member_identity(self, data: dict[str, Any]) -> None:
        members = self._load_member_directory()
        if not members:
            return
        public = {username: public_member_data(members, username) for username in members}
        data["members"] = public

        def enrich(
            item: dict[str, Any],
            username: Any,
            *,
            prefix: str = "owner",
        ) -> None:
            if not isinstance(username, str):
                return
            profile = public.get(username)
            if not profile:
                return
            if profile.get("display_name"):
                item[f"{prefix}_display"] = profile["display_name"]
            if profile.get("nickname"):
                item[f"{prefix}_nickname"] = profile["nickname"]
            if profile.get("public_contact"):
                item[f"{prefix}_public_contact"] = profile["public_contact"]

        for allocation in data.get("allocations", []):
            enrich(allocation, allocation.get("owner"))
        for request in data.get("queue", []):
            enrich(request, request.get("owner"))
        for gpu in data.get("gpus", []):
            enrich(gpu, gpu.get("owner"))
        unmanaged = data.get("unmanaged") or {}
        for container in unmanaged.get("containers", []):
            enrich(container, container.get("owner"))
        for process in unmanaged.get("processes", []):
            enrich(process, process.get("username"))
            enrich(process, process.get("container_owner"), prefix="container_owner")

    def _unmanaged(
        self,
        gpus: list[dict[str, Any]],
        allocations: dict[str, dict[str, Any]],
        live: LiveResources,
    ) -> dict[str, list[dict[str, Any]]]:
        container_by_uuid = {
            str(gpu["uuid"]): str(gpu["container_id"]) for gpu in gpus if gpu["container_id"]
        }
        index_by_uuid = {str(gpu["uuid"]): int(gpu["index"]) for gpu in gpus}
        unmanaged_containers: list[dict[str, Any]] = []
        for container in live.containers:
            allocation_id = container.labels.get("udc.allocation")
            if self._managed_container_matches(container, allocations, gpus):
                continue
            resolved, unknown_ref = self._resolve_gpu_refs(container.gpu_refs, gpus)
            indices = sorted({int(gpu["index"]) for gpu in gpus if str(gpu["uuid"]) in resolved})
            active_gpu_access = (
                bool(indices) or unknown_ref
            ) and container.status in ACTIVE_GPU_HOLDER_STATUSES
            unmanaged_containers.append(
                {
                    "id": container.id,
                    "name": container.name,
                    "image": container.image,
                    "status": container.status,
                    "allocation_id": allocation_id,
                    "owner": container.labels.get("udc.owner"),
                    "gpu_refs": list(container.gpu_refs),
                    "gpus": indices,
                    "gpu_unknown": unknown_ref,
                    "active_gpu_access": active_gpu_access,
                }
            )
        unmanaged_processes: list[dict[str, Any]] = []
        for process in live.processes:
            container_id = container_by_uuid.get(process.gpu_uuid)
            if container_id and self._pid_in_container(process.pid, container_id):
                continue
            process_container = self._pid_container(process.pid, live.containers)
            unmanaged_processes.append(
                {
                    "gpu_uuid": process.gpu_uuid,
                    "gpu_index": index_by_uuid.get(process.gpu_uuid),
                    "pid": process.pid,
                    "used_mib": process.used_mib,
                    "process_name": process.process_name,
                    "uid": process.uid,
                    "username": process.username,
                    "container_id": (process_container.id if process_container else None),
                    "container_name": (process_container.name if process_container else None),
                    "container_owner": (
                        process_container.labels.get("udc.owner") if process_container else None
                    ),
                }
            )
        return {
            "containers": unmanaged_containers,
            "processes": unmanaged_processes,
        }

    @staticmethod
    def _pid_container(pid: int, containers: list[ContainerInfo]) -> ContainerInfo | None:
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None
        for container in containers:
            if container.id and container.id in cgroup:
                return container
        return None

    @staticmethod
    def _pid_in_container(pid: int, container_id: str) -> bool:
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return False
        return container_id in cgroup

    def _queue_list(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        all_users = args.get("all", False)
        include_finished = args.get("include_finished", False)
        if not isinstance(all_users, bool) or not isinstance(include_finished, bool):
            raise BadRequest("all and include_finished must be booleans")
        if all_users:
            self._require_admin(identity)
        with self._connection() as connection:
            queue = store.list_queue_requests(
                connection,
                owner_uid=None if all_users else identity.uid,
                include_finished=include_finished,
                now=self._now(),
            )
        return {"queue": queue}

    def _queue_get(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        queue_id = args.get("id")
        if not isinstance(queue_id, str) or not queue_id:
            raise BadRequest("id must be a non-empty string")
        with self._connection() as connection:
            queue = store.get_queue_request(connection, queue_id, now=self._now())
            if queue["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may inspect this queue request")
            result: dict[str, Any] = {"queue": queue}
            allocation_id = queue.get("allocation_id")
            if isinstance(allocation_id, str) and allocation_id:
                try:
                    result["allocation"] = store.get_allocation(
                        connection,
                        allocation_id,
                        now=self._now(),
                    )
                    result["events"] = store.list_allocation_events(connection, allocation_id)
                except NotFound:
                    pass
        return result

    def _queue_cancel(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        queue_id = args.get("id")
        if not isinstance(queue_id, str) or not queue_id:
            raise BadRequest("id must be a non-empty string")
        reason = args.get("reason", "")
        if not isinstance(reason, str):
            raise BadRequest("reason must be a string")
        with self._connection() as connection:
            queue = store.get_queue_request(connection, queue_id, now=self._now())
            if queue["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may cancel this queue request")
            store.cancel_queue_request(
                connection,
                queue_id,
                actor_uid=identity.uid,
                reason=reason,
                now=self._now(),
            )
            return store.get_queue_request(connection, queue_id, now=self._now())

    def _status(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation_id = args.get("id")
        if allocation_id is not None and not isinstance(allocation_id, str):
            raise BadRequest("id must be a string")
        with self._connection() as connection:
            if allocation_id is None:
                return {
                    "allocations": store.list_allocations(
                        connection, owner_uid=identity.uid, now=self._now()
                    ),
                    "capabilities": self._capabilities(),
                }
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
            if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may inspect this allocation")
            return {
                "allocation": allocation,
                "events": store.list_allocation_events(connection, allocation_id),
                "capabilities": self._capabilities(),
            }

    async def _apply(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        self._require_active()
        if self.config.policy.gpu_audit_required:
            gpu_failure = await asyncio.to_thread(self._run_gpu_audit)
            if gpu_failure:
                raise Conflict(f"new allocations blocked by GPU permission audit: {gpu_failure}")
        if self.config.policy.workspace_quota_required or self.config.policy.max_workspace_gb > 0:
            quota_failure = await asyncio.to_thread(self._run_quota_check)
            if quota_failure:
                raise Conflict(f"new allocations blocked by workspace quota: {quota_failure}")
        storage_failure = await asyncio.to_thread(
            self._run_storage_check,
            include_next_slot=True,
        )
        if storage_failure:
            raise Conflict(f"new allocations blocked by Docker storage: {storage_failure}")
        gate_failure = self._gate_failure()
        if gate_failure:
            raise Conflict(f"new allocations blocked by host safety gate: {gate_failure}")
        gpus = self._parse_gpus(args.get("gpus", []))
        cpus = self._parse_positive_float(args.get("cpus"), "cpus")
        memory_gb = self._parse_positive_int(args.get("memory_gb"), "memory_gb")
        hours = self._parse_positive_int(
            args.get("hours", self.config.policy.default_hours), "hours"
        )
        if not 1 <= hours <= self.config.policy.max_hours:
            raise BadRequest(f"hours must be between 1 and {self.config.policy.max_hours}")
        if cpus > self.config.policy.max_cpus_per_allocation:
            raise InsufficientResources(
                "requested cpus exceed policy.max_cpus_per_allocation "
                f"({self.config.policy.max_cpus_per_allocation:g})"
            )
        if memory_gb > self.config.policy.max_memory_gb_per_allocation:
            raise InsufficientResources(
                "requested memory exceeds policy.max_memory_gb_per_allocation "
                f"({self.config.policy.max_memory_gb_per_allocation})"
            )
        queue_requested = args.get("queue", self.config.policy.queue_enabled)
        if not isinstance(queue_requested, bool):
            raise BadRequest("queue must be a boolean")
        image = args.get("image", self.config.image.base)
        if not isinstance(image, str) or not image:
            raise BadRequest("image must be a non-empty string")
        if not identity.is_admin and not self.config.image.image_allowed(image):
            raise Forbidden("members may only use images on the configured allowlist")
        note = args.get("note", "")
        if not isinstance(note, str):
            raise BadRequest("note must be a string")
        workspace = self._workspace(identity.username)
        starts_at = self._now()
        expires_at = starts_at + timedelta(hours=hours)
        busy_reason: str | None = None
        if gpus:
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                gpu_rows = store.gpus_for_indices(connection, gpus)
                active_allocations = self._allocation_map(connection, now)
            live = await asyncio.to_thread(self._live_resources)
            merged = self._merge_gpus(db_gpus, live, active_allocations)
            by_index = {int(gpu["index"]): gpu for gpu in merged}
            for index in gpus:
                gpu = by_index.get(index)
                if gpu is None:
                    raise NotFound(f"unknown GPU index: {index}")
                state = str(gpu["state"])
                if state == "unknown":
                    raise DockerError(f"cannot determine GPU {index} state: {gpu['state_reason']}")
                if state == "unmanaged":
                    busy_reason = f"GPU {index} is unmanaged: {gpu['state_reason']}"
                elif state not in {"free", "allocated"}:
                    busy_reason = f"GPU {index} is {state}: {gpu['state_reason']}"
            gpu_uuids = [str(row["uuid"]) for row in gpu_rows]
        else:
            gpu_uuids = []
        if self.config.policy.min_free_gb:
            disk_path = Path(self.config.server.workspace_root)
            while not disk_path.exists() and disk_path != disk_path.parent:
                disk_path = disk_path.parent
            try:
                usage = shutil.disk_usage(disk_path)
            except OSError as exc:
                raise InternalError(f"could not inspect workspace disk: {exc}") from exc
            free_gb = usage.free // (1024**3)
            if free_gb < self.config.policy.min_free_gb:
                raise InsufficientResources(
                    f"workspace disk has only {free_gb} GiB free; "
                    f"policy.min_free_gb is {self.config.policy.min_free_gb}"
                )
        with self._connection() as connection:
            pending_before = store.queue_request_count(connection, now=self._now())
        if pending_before:
            if not self.config.policy.queue_enabled:
                raise Conflict(
                    "queued requests are pending while queue_enabled is false; "
                    "an administrator must resolve the queue"
                )
            await self._schedule_queue_safely("direct_apply")
            with self._connection() as connection:
                pending_after = store.queue_request_count(connection, now=self._now())
            if pending_after:
                reason = busy_reason or "queued requests are pending"
                if not queue_requested:
                    raise GpuBusy(f"{reason}; --no-queue cannot bypass queued requests")
                return await self._enqueue_allocation(
                    identity,
                    gpus=gpus,
                    cpus=cpus,
                    memory_gb=memory_gb,
                    hours=hours,
                    image=image,
                    workspace=workspace,
                    note=note,
                    reason=reason,
                )
        if busy_reason is not None:
            if not (self.config.policy.queue_enabled and queue_requested):
                raise GpuBusy(busy_reason)
            return await self._enqueue_allocation(
                identity,
                gpus=gpus,
                cpus=cpus,
                memory_gb=memory_gb,
                hours=hours,
                image=image,
                workspace=workspace,
                note=note,
                reason=busy_reason,
            )
        try:
            with self._connection() as connection:
                allocation_id = store.create_allocation(
                    connection,
                    owner_uid=identity.uid,
                    owner_name=identity.username,
                    gpu_uuids=gpu_uuids,
                    cpus=cpus,
                    memory_gb=memory_gb,
                    image=image,
                    workspace=workspace,
                    note=note,
                    starts_at=starts_at,
                    expires_at=expires_at,
                    max_cpus=self.config.policy.global_max_cpus,
                    max_memory_gb=self.config.policy.global_max_memory_gb,
                    max_cpus_per_user=self.config.policy.max_cpus_per_user,
                    max_memory_gb_per_user=self.config.policy.max_memory_gb_per_user,
                    max_gpus_per_user=self.config.policy.max_gpus_per_user,
                    max_active_allocations_per_user=(
                        self.config.policy.max_active_allocations_per_user
                    ),
                    now=self._now(),
                    **self._container_storage_params(),
                )
        except (GpuBusy, InsufficientResources) as exc:
            if not (self.config.policy.queue_enabled and queue_requested):
                raise
            return await self._enqueue_allocation(
                identity,
                gpus=gpus,
                cpus=cpus,
                memory_gb=memory_gb,
                hours=hours,
                image=image,
                workspace=workspace,
                note=note,
                reason=str(exc),
            )
        return await self._materialize_allocation(allocation_id)

    async def _enqueue_allocation(
        self,
        identity: Identity,
        *,
        gpus: list[int],
        cpus: float,
        memory_gb: int,
        hours: int,
        image: str,
        workspace: str,
        note: str,
        reason: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            queue_id = store.create_queue_request(
                connection,
                owner_uid=identity.uid,
                owner_name=identity.username,
                requested_gpus=gpus,
                cpus=cpus,
                memory_gb=memory_gb,
                hours=hours,
                image=image,
                workspace=workspace,
                note=note,
                max_queue_length=self.config.policy.max_queue_length,
                max_queued_allocations_per_user=(
                    self.config.policy.max_queued_allocations_per_user
                ),
                max_queue_hours=self.config.policy.max_queue_hours,
                now=self._now(),
            )
        logger.info(
            "queued allocation request %s for %s after: %s",
            queue_id,
            identity.username,
            reason,
        )
        await self._schedule_queue_safely("apply")
        with self._connection() as connection:
            return store.get_queue_request(connection, queue_id, now=self._now())

    def _verify_member_workspace_quota(self, workspace: str, owner_uid: int) -> None:
        """Per-allocation gate: the member directory must really be bounded.

        ``check_workspace_quota`` only proves the filesystem *supports* project
        quotas. This verifies the member's own project ID, project inheritance,
        and hard limit, provisioning them on first use so a workspace created
        after ``configure_quotas.sh`` ran can never slip through unbounded.
        """

        if (
            not self.config.policy.workspace_quota_required
            or self.config.policy.max_workspace_gb <= 0
        ):
            return
        if owner_uid <= 0:
            logger.warning("skipping member workspace quota verification for UID %d", owner_uid)
            return
        limit_gb = self.config.policy.max_workspace_gb
        result = self.member_quota_check(workspace, owner_uid, limit_gb)
        if result.passed:
            return
        logger.warning(
            "member workspace %s is not quota-bounded (%s); reprovisioning",
            workspace,
            result.detail,
        )
        provisioned = self.member_quota_provision(workspace, owner_uid, limit_gb)
        if not provisioned.passed:
            raise Conflict(f"workspace quota could not be enforced: {provisioned.detail}")

    async def _materialize_allocation(self, allocation_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            allocation_data = store.get_allocation(
                connection,
                allocation_id,
                now=self._now(),
            )
            starts_at = datetime.fromisoformat(str(allocation_data["starts_at"]))
            expires_at = datetime.fromisoformat(str(allocation_data["expires_at"]))
            owner_uid = int(allocation_data["owner_uid"])
            owner_gid = owner_uid
        if os.name == "posix":
            try:
                import pwd

                owner_gid = pwd.getpwuid(owner_uid).pw_gid
            except (ImportError, KeyError):
                pass
        try:
            await asyncio.to_thread(
                workspace_io.ensure_workspace,
                str(allocation_data["workspace"]),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            await asyncio.to_thread(
                self._verify_member_workspace_quota,
                str(allocation_data["workspace"]),
                owner_uid,
            )
        except Exception as exc:
            with self._connection() as connection:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason=f"workspace preparation failed: {exc}",
                    now=self._now(),
                )
            if isinstance(exc, UdcError):
                raise
            raise InternalError(f"workspace preparation failed: {exc}") from exc
        allocation = self._allocation_model(allocation_data, starts_at, expires_at)
        try:
            container = await asyncio.to_thread(self.docker.create_container, allocation)
        except Exception as exc:
            logger.exception("container creation failed for %s", allocation_id)
            with self._connection() as connection:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason=f"docker create failed: {exc}",
                    now=self._now(),
                )
            raise DockerError(f"container creation failed: {exc}") from exc
        # Record the container reference before any later step can fail, so a
        # restarted broker can still find and clean up the orphan.
        try:
            with self._connection() as connection:
                store.record_container(
                    connection,
                    allocation_id,
                    container_id=container.id,
                    container_name=container.name,
                    now=self._now(),
                )
        except Exception:
            logger.exception("could not record container %s", container.id)

        async def _fail_activated_container(step: str, exc: Exception) -> NoReturn:
            logger.exception("%s failed for %s", step, allocation_id)
            cleanup_failed = False
            try:
                await asyncio.to_thread(self.docker.remove_container, container.id)
            except Exception:
                cleanup_failed = True
                logger.exception(
                    "could not remove container %s after %s failure",
                    container.id,
                    step,
                )
            with self._connection() as connection:
                if cleanup_failed:
                    # The orphan container may still be running: keep the GPU
                    # claims and CPU/memory/storage reservations (non-terminal
                    # state) until reconciliation confirms the removal.
                    store.mark_cleanup_pending(
                        connection,
                        allocation_id,
                        reason=f"{step} failed: {exc}",
                        now=self._now(),
                    )
                else:
                    store.fail_allocation(
                        connection,
                        allocation_id,
                        reason=f"{step} failed: {exc}",
                        now=self._now(),
                    )
            if cleanup_failed:
                raise InternalError(
                    f"{step} failed and container cleanup is pending; resources remain reserved"
                ) from exc
            raise InternalError("allocation could not be activated") from exc

        # The container only becomes usable once its member identity exists:
        # without this gate, `sudo` inside the container fails with "you do
        # not exist in the passwd database" on first use for base images that
        # lack the member UID.
        try:
            await asyncio.to_thread(
                self.docker.initialize_container_identity,
                container.id,
                allocation,
            )
        except Exception as exc:
            await _fail_activated_container("container identity initialization", exc)
        try:
            with self._connection() as connection:
                store.activate_allocation(
                    connection,
                    allocation_id,
                    container_id=container.id,
                    container_name=container.name,
                    now=self._now(),
                )
                return store.get_allocation(connection, allocation_id, now=self._now())
        except Exception as exc:
            await _fail_activated_container("allocation activation", exc)

    async def _schedule_queue_safely(self, trigger: str) -> None:
        try:
            admitted = await self._schedule_queue()
        except Exception:
            logger.exception("queue scheduling failed during %s", trigger)
            return
        if admitted:
            logger.info("%s queue admissions: %s", trigger, ", ".join(admitted))

    async def _schedule_queue(self) -> list[str]:
        if self._mode != "active" or not self.config.policy.queue_enabled:
            return []
        with self._connection() as connection:
            if store.queue_request_count(connection, now=self._now()) == 0:
                return []
        # Admission must be as fresh as a direct apply: re-run the audit gates
        # here instead of trusting results cached by an earlier check, so a
        # host permission or quota change between periodic audits pauses
        # automatic admission too.
        gpu_failure = await asyncio.to_thread(self._run_gpu_audit)
        if gpu_failure:
            logger.warning("queue scheduling paused by GPU audit gate: %s", gpu_failure)
            return []
        quota_failure = await asyncio.to_thread(self._run_quota_check)
        if quota_failure:
            logger.warning("queue scheduling paused by workspace quota gate: %s", quota_failure)
            return []
        # Re-check the nosuid/quota storage gate at admission time instead of
        # relying only on the value cached at startup or the last direct apply.
        storage_failure = await asyncio.to_thread(
            self._run_storage_check,
            include_next_slot=True,
        )
        if storage_failure:
            logger.warning("queue scheduling paused by Docker storage gate: %s", storage_failure)
            return []
        gate_failure = self._gate_failure()
        if gate_failure:
            logger.warning("queue scheduling paused by host safety gate: %s", gate_failure)
            return []
        admitted: list[str] = []
        while True:
            live = await asyncio.to_thread(self._live_resources)
            if live.error:
                logger.warning("queue scheduling paused: live GPU detection failed: %s", live.error)
                break
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                active_allocations = self._allocation_map(connection, now)
                merged = self._merge_gpus(db_gpus, live, active_allocations)
                available_gpu_uuids = {str(gpu["uuid"]) for gpu in merged if gpu["state"] == "free"}
                allocation_id = store.admit_next_queued(
                    connection,
                    max_cpus=self.config.policy.global_max_cpus,
                    max_memory_gb=self.config.policy.global_max_memory_gb,
                    max_cpus_per_user=self.config.policy.max_cpus_per_user,
                    max_memory_gb_per_user=self.config.policy.max_memory_gb_per_user,
                    max_gpus_per_user=self.config.policy.max_gpus_per_user,
                    max_active_allocations_per_user=(
                        self.config.policy.max_active_allocations_per_user
                    ),
                    available_gpu_uuids=available_gpu_uuids,
                    backfill_enabled=self.config.policy.queue_backfill_enabled,
                    backfill_reserve_minutes=self.config.policy.queue_backfill_reserve_minutes,
                    now=now,
                    **self._container_storage_params(),
                )
            if allocation_id is None:
                break
            logger.info("admitting queued request as allocation %s", allocation_id)
            try:
                await self._materialize_allocation(allocation_id)
            except Exception:
                logger.exception("queued allocation %s could not be materialized", allocation_id)
            admitted.append(allocation_id)
        return admitted

    async def _release(
        self,
        identity: Identity,
        args: dict[str, Any],
        *,
        admin: bool = False,
    ) -> dict[str, Any]:
        allocation_id = args.get("id")
        if not isinstance(allocation_id, str) or not allocation_id:
            raise BadRequest("id must be a non-empty string")
        reason = args.get("reason", "")
        if not isinstance(reason, str):
            raise BadRequest("reason must be a string")
        with self._connection() as connection:
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
        if not admin and allocation["owner_uid"] != identity.uid:
            raise Forbidden("only the owner or an admin may release this allocation")
        try:
            container_id = str(allocation["container_id"])
            if container_id:
                container = await asyncio.to_thread(self.docker.get_container, container_id)
                if container is not None and not matches_managed_allocation(
                    container,
                    allocation_id,
                    container_id,
                ):
                    raise Conflict(
                        "refusing to remove a container that does not match this allocation"
                    )
                await asyncio.to_thread(self.docker.remove_container, container_id)
            else:
                containers = await asyncio.to_thread(self.docker.list_containers)
                for container in containers:
                    if matches_managed_allocation(container, allocation_id):
                        await asyncio.to_thread(self.docker.remove_container, container.id)
        except Conflict:
            raise
        except Exception as exc:
            raise DockerError(f"cannot remove allocation container: {exc}") from exc
        with self._connection() as connection:
            store.release_allocation(
                connection,
                allocation_id,
                actor_uid=identity.uid,
                reason=reason,
                now=self._now(),
            )
            released = store.get_allocation(connection, allocation_id, now=self._now())
        self._cancel_tunnels(allocation_id)
        await self._schedule_queue_safely("release")
        return released

    @staticmethod
    def _job_name(raw: Any) -> str:
        if not isinstance(raw, str) or not JOB_NAME_RE.fullmatch(raw):
            raise BadRequest("job name must match [A-Za-z0-9_.-]{1,32}")
        if raw in {".", ".."}:
            raise BadRequest("invalid job name")
        return raw

    def _job_allocation(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation_id = args.get("id")
        if not isinstance(allocation_id, str) or not allocation_id:
            raise BadRequest("id must be a non-empty string")
        with self._connection() as connection:
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
        if allocation["owner_uid"] != identity.uid and not identity.is_admin:
            raise Forbidden("only the owner or an admin may manage jobs")
        if allocation["status"] != "active":
            raise Conflict(f"allocation is {allocation['status']}, not active")
        if not allocation["container_id"]:
            raise NotFound("allocation has no container")
        return allocation

    @staticmethod
    def _job_log_path(allocation: dict[str, Any], name: str) -> Path:
        # Display path only.  Reads and writes use workspace_io so this path
        # cannot become a check/use race.
        return Path(str(allocation["workspace"])) / ".udc-logs" / f"{name}.log"

    @staticmethod
    def _allocation_owner_ids(allocation: dict[str, Any]) -> tuple[int, int]:
        owner_uid = int(allocation["owner_uid"])
        owner_gid = owner_uid
        if os.name == "posix":
            try:
                import pwd

                owner_gid = pwd.getpwuid(owner_uid).pw_gid
            except (ImportError, KeyError):
                pass
        return owner_uid, owner_gid

    @staticmethod
    def _ensure_job_log(allocation: dict[str, Any], name: str) -> None:
        if not workspace_io.supported():
            raise InternalError("descriptor-relative job-log access is unavailable")
        owner_uid, owner_gid = Broker._allocation_owner_ids(allocation)
        workspace_io.ensure_job_log(
            str(allocation["workspace"]),
            name,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

    async def _job_start(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._job_allocation(identity, args)
        name = self._job_name(args.get("name"))
        command = args.get("command")
        if isinstance(command, str):
            # Copy-pasteable shell command. It runs inside the member's own
            # container as `bash -lc` with a fixed argv; the broker never
            # interprets it and it grants nothing the member does not already
            # have through `shell`.
            shell_text = command.strip()
            if not shell_text:
                raise BadRequest("shell command must not be empty")
            if "\x00" in shell_text:
                raise BadRequest("shell command must not contain NUL bytes")
            if len(shell_text) > 4096:
                raise BadRequest("shell command must not exceed 4096 characters")
            command_argv = ["bash", "-lc", shell_text]
        elif (
            isinstance(command, list)
            and command
            and len(command) <= 64
            and all(isinstance(item, str) and item and len(item) <= 512 for item in command)
        ):
            command_argv = command
        else:
            raise BadRequest(
                "command must be a non-empty shell string or a list of argument strings"
            )
        container_id = str(allocation["container_id"])
        log_path = self._job_log_path(allocation, name)
        await asyncio.to_thread(self._ensure_job_log, allocation, name)
        await asyncio.to_thread(self.docker.start_job, container_id, name, command_argv)
        with self._connection() as connection:
            store.record_event(
                connection,
                actor_uid=identity.uid,
                action="job_started",
                allocation_id=str(allocation["id"]),
                detail={"name": name, "command": command_argv},
                now=self._now(),
            )
            connection.commit()
        return {
            "id": allocation["id"],
            "name": name,
            "session": f"udc-{name}",
            "log": str(log_path),
        }

    async def _job_list(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._job_allocation(identity, args)
        container_id = str(allocation["container_id"])
        jobs = await asyncio.to_thread(self.docker.list_jobs, container_id)
        enriched: list[dict[str, Any]] = []
        for job in jobs:
            name = str(job["name"])
            path = self._job_log_path(allocation, name)
            item: dict[str, Any] = dict(job)
            item["log"] = str(path)
            if not workspace_io.supported():
                raise InternalError("descriptor-relative job-log access is unavailable")
            size = await asyncio.to_thread(
                workspace_io.regular_file_size,
                str(allocation["workspace"]),
                (".udc-logs", f"{name}.log"),
            )
            item["log_bytes"] = size or 0
            enriched.append(item)
        return {"id": allocation["id"], "jobs": enriched}

    def _log_allocation(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        """Fetch an allocation for log access: owner or admin, any status.

        Persistent job logs outlive the container (the workspace survives
        release and expiry), so unlike container operations this may target
        released or expired allocations.
        """

        allocation_id = args.get("id")
        if not isinstance(allocation_id, str) or not allocation_id:
            raise BadRequest("id must be a non-empty string")
        with self._connection() as connection:
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
        if allocation["owner_uid"] != identity.uid and not identity.is_admin:
            raise Forbidden("only the owner or an admin may read these logs")
        return allocation

    async def _job_files(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._log_allocation(identity, args)
        files = await asyncio.to_thread(
            workspace_io.list_workspace_logs,
            str(allocation["workspace"]),
        )
        return {"id": allocation["id"], "files": files}

    async def _job_logs(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._log_allocation(identity, args)
        name = self._job_name(args.get("name"))
        tail = self._parse_positive_int(args.get("tail", 200), "tail")
        if tail > 10000:
            raise BadRequest("tail must not exceed 10000")
        path = self._job_log_path(allocation, name)
        if not workspace_io.supported():
            raise InternalError("descriptor-relative job-log access is unavailable")
        content = await asyncio.to_thread(
            workspace_io.tail_file,
            str(allocation["workspace"]),
            (".udc-logs", f"{name}.log"),
            tail,
        )
        return {
            "id": allocation["id"],
            "name": name,
            "log": str(path),
            "logs": content,
        }

    async def _job_stop(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._job_allocation(identity, args)
        name = self._job_name(args.get("name"))
        container_id = str(allocation["container_id"])
        await asyncio.to_thread(self.docker.stop_job, container_id, name)
        with self._connection() as connection:
            store.record_event(
                connection,
                actor_uid=identity.uid,
                action="job_stopped",
                allocation_id=str(allocation["id"]),
                detail={"name": name},
                now=self._now(),
            )
            connection.commit()
        return {"id": allocation["id"], "name": name, "stopped": True}

    async def _logs(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation_id = args.get("id")
        if not isinstance(allocation_id, str) or not allocation_id:
            raise BadRequest("id must be a non-empty string")
        tail = self._parse_positive_int(args.get("tail", 200), "tail")
        if tail > 1000:
            raise BadRequest("tail must not exceed 1000")
        with self._connection() as connection:
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
        if allocation["owner_uid"] != identity.uid and not identity.is_admin:
            raise Forbidden("only the owner or an admin may read these logs")
        container_id = str(allocation["container_id"])
        if not container_id:
            raise NotFound("allocation has no container")
        content = await asyncio.to_thread(self.docker.logs, container_id, tail)
        return {
            "id": allocation_id,
            "container_id": container_id,
            "logs": content,
        }

    def _admin_users(self) -> dict[str, Any]:
        with self._connection() as connection:
            return {"users": store.list_users(connection)}

    def _admin_resources(self) -> dict[str, Any]:
        data = self._list()
        with self._connection() as connection:
            data["users"] = store.list_users(connection)
        return data

    def _admin_audit(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = self._parse_positive_int(args.get("limit", 100), "limit")
        if limit > 1000:
            raise BadRequest("limit must not exceed 1000")
        with self._connection() as connection:
            return {"events": store.list_events(connection, limit=limit)}

    async def _admin_mode(
        self,
        identity: Identity,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        mode = args.get("mode")
        if mode not in {"monitor", "maintenance", "active"}:
            raise BadRequest("mode must be monitor, maintenance, or active")
        if mode == "monitor":
            with self._connection() as connection:
                leases = store.active_lease_count(connection)
                queued = store.queue_request_count(connection, now=self._now())
            if leases or queued:
                raise Conflict(
                    "cannot switch to monitor while active/pending leases or queued "
                    "requests exist; use maintenance mode"
                )
            try:
                managed = [
                    container
                    for container in self.docker.list_containers()
                    if container.labels.get("udc.managed") == "true"
                ]
            except Exception as exc:
                raise Conflict(f"cannot verify managed containers: {exc}") from exc
            if managed:
                raise Conflict(
                    "cannot switch to monitor while managed containers exist; use maintenance mode"
                )
        if mode == "active":
            try:
                validate_active_mode_policy(self.config.policy)
            except ValueError as exc:
                raise Forbidden(f"active mode blocked by policy configuration: {exc}") from exc
        if mode == "active":
            audit = await asyncio.to_thread(self._legacy_audit_result)
            if not audit.passed:
                raise Conflict(
                    "active mode blocked by legacy access audit: " + audit.failure_message()
                )
            runtime_failure = await asyncio.to_thread(self._refresh_runtime_gates)
            if runtime_failure:
                raise Conflict(f"active mode blocked by host safety gate: {runtime_failure}")
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                active_allocations = self._allocation_map(connection, now)
            live = await asyncio.to_thread(self._live_resources)
            if live.error:
                raise Conflict(f"active mode blocked: live GPU detection failed: {live.error}")
            merged = self._merge_gpus(db_gpus, live, active_allocations)
            unmanaged = self._unmanaged(db_gpus, active_allocations, live)
            blocking_gpus = [
                f"GPU {gpu['index']} ({gpu['state']}: {gpu['state_reason']})"
                for gpu in merged
                if gpu["state"] in {"unmanaged", "unknown"}
            ]
            blocking_containers = [
                str(container["name"])
                for container in unmanaged["containers"]
                if container.get("active_gpu_access")
            ]
            blocking_processes = [
                f"pid {process['pid']} on GPU {process.get('gpu_index')}"
                for process in unmanaged["processes"]
            ]
            blockers = blocking_gpus + blocking_containers + blocking_processes
            if blockers:
                raise Conflict(
                    "active mode blocked by unmanaged GPU access: " + "; ".join(blockers[:10])
                )
        with self._connection() as connection:
            store.set_mode(connection, str(mode), actor_uid=identity.uid, now=self._now())
        self._mode = str(mode)
        if self._mode == "active":
            await self._schedule_queue_safely("mode_activation")
        return {"mode": self._mode}

    @staticmethod
    def _require_admin(identity: Identity) -> None:
        if not identity.is_admin:
            raise Forbidden("admin permission required")

    def _require_active(self) -> None:
        if self._mode == "maintenance":
            raise MonitorOnly("broker is in maintenance mode; new allocations are disabled")
        if self._mode != "active":
            raise MonitorOnly("broker is in monitor-only mode")

    @staticmethod
    def _parse_gpus(raw: Any) -> list[int]:
        if not isinstance(raw, list):
            raise BadRequest("gpus must be a list of integer indices")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in raw):
            raise BadRequest("gpus must be a list of integer indices")
        if any(item < 0 for item in raw):
            raise BadRequest("gpu indices must not be negative")
        if len(set(raw)) != len(raw):
            raise BadRequest("gpus must not contain duplicates")
        return raw

    @staticmethod
    def _parse_positive_float(raw: Any, name: str) -> float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BadRequest(f"{name} must be a number")
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise BadRequest(f"{name} must be a positive finite number")
        return value

    @staticmethod
    def _parse_positive_int(raw: Any, name: str) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise BadRequest(f"{name} must be an integer")
        if raw <= 0:
            raise BadRequest(f"{name} must be positive")
        return raw

    def _workspace(self, username: str) -> str:
        if not username or username in {".", ".."} or "/" in username or "\\" in username:
            raise Forbidden("invalid username for workspace path")
        return f"{self.config.server.workspace_root.rstrip('/')}/{username}"

    @staticmethod
    def _allocation_model(
        data: dict[str, Any],
        starts_at: datetime,
        expires_at: datetime,
    ) -> Allocation:
        return Allocation(
            id=str(data["id"]),
            owner_uid=int(data["owner_uid"]),
            owner_name=str(data["owner"]),
            gpus=tuple(str(gpu) for gpu in data["gpu_uuids"]),
            cpus=float(data["cpus"]),
            memory_gb=int(data["memory_gb"]),
            image=str(data["image"]),
            workspace=str(data["workspace"]),
            note=str(data["note"]),
            starts_at=starts_at,
            expires_at=expires_at,
            status="pending",
        )
