"""Asyncio Unix-socket broker with SO_PEERCRED identity and SQLite transactions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import store
from ..config import LabConfig
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
from ..identity import (
    Identity,
    IdentityResolver,
    IdentityResolverProtocol,
    peer_credentials,
)
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
        clock: Callable[[], datetime] | None = None,
        expiry_interval: float = 30.0,
        reconcile_interval: float = 300.0,
    ) -> None:
        self.config = config
        self.db_path = Path(db_path or config.server.state_db)
        self.socket_path = Path(socket_path or config.server.broker_socket)
        self.docker = docker or DockerDriver(config)
        self.identity_resolver = identity_resolver or IdentityResolver(config.groups.admins)
        self.clock = clock or store.utcnow
        self.expiry_interval = expiry_interval
        self.reconcile_interval = reconcile_interval
        self._mode = config.policy.mode
        self._server: asyncio.AbstractServer | None = None
        self._expiry_task: asyncio.Task[None] | None = None

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = connect(self.db_path)
        try:
            yield connection
        finally:
            connection.close()

    def _now(self) -> datetime:
        return self.clock()

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

    def _container_gpu_holders(
        self,
        containers: list[ContainerInfo],
        gpus: list[dict[str, Any]],
        active_allocation_ids: set[str],
    ) -> tuple[dict[str, list[ContainerInfo]], bool]:
        holders: dict[str, list[ContainerInfo]] = {}
        unknown = False
        for container in containers:
            managed_active = (
                container.labels.get("udc.managed") == "true"
                and container.labels.get("udc.allocation") in active_allocation_ids
            )
            if managed_active:
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
        active_allocation_ids: set[str],
    ) -> list[dict[str, Any]]:
        live_by_uuid = {gpu.uuid: gpu for gpu in live.gpus}
        processes_by_uuid: dict[str, list[GPUProcess]] = {}
        for process in live.processes:
            processes_by_uuid.setdefault(process.gpu_uuid, []).append(process)
        holders, unknown_refs = self._container_gpu_holders(
            live.containers,
            db_gpus,
            active_allocation_ids,
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
            if self._mode == "active" and not self.config.policy.deployment_allowed:
                logger.warning(
                    "active mode was persisted but deployment_allowed=false; forcing monitor"
                )
                store.set_mode(
                    connection,
                    "monitor",
                    actor_uid=0,
                    now=self._now(),
                )
                self._mode = "monitor"
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
        if self._mode != "active":
            logger.info("reconcile skipped in %s mode", self._mode)
            return {"skipped": 1}
        with self._connection() as connection:
            return reconcile_allocations(connection, self.docker, now=self._now())

    async def expire_once(self) -> list[str]:
        if self._mode != "active":
            return []
        return await asyncio.to_thread(self._expire_once)

    def _expire_once(self) -> list[str]:
        with self._connection() as connection:
            return expire_allocations(connection, self.docker, now=self._now())

    async def _expiry_loop(self) -> None:
        elapsed = 0.0
        while True:
            await asyncio.sleep(self.expiry_interval)
            elapsed += self.expiry_interval
            if elapsed >= self.reconcile_interval:
                elapsed = 0.0
                try:
                    summary = await asyncio.to_thread(self.reconcile_once)
                    logger.info("periodic reconcile: %s", summary)
                except Exception:
                    logger.exception("periodic reconcile failed")
            try:
                expired = await self.expire_once()
                if expired:
                    logger.info("expired allocations: %s", ", ".join(expired))
            except Exception:
                logger.exception("expiry tick failed")

    async def stop(self) -> None:
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
            self._check_args(args, {"id", "cols", "rows"})
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
            session = await asyncio.to_thread(
                self.docker.open_shell,
                container_id,
                cols,
                rows,
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
            await asyncio.to_thread(session.close)
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _parse_terminal_dimension(raw: Any, name: str) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise BadRequest(f"{name} must be an integer")
        if not 1 <= raw <= 1000:
            raise BadRequest(f"{name} is out of range")
        return raw

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
                self._check_args(args, {"gpus", "cpus", "memory_gb", "hours", "image", "note"})
                return _ok(await self._apply(identity, args))
            if command == "release":
                self._check_args(args, {"id"})
                return _ok(await self._release(identity, args))
            if command == "logs":
                self._check_args(args, {"id", "tail"})
                return _ok(await self._logs(identity, args))
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
                return _ok(await asyncio.to_thread(self.reconcile_once))
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
            self._mode = store.get_mode(connection, self._mode)
            db_gpus = store.list_gpus(connection, now=now)
            allocations = store.list_allocations(connection, now=now)
            active_ids = {
                str(allocation["id"])
                for allocation in store.list_allocations(
                    connection,
                    statuses=("pending", "active"),
                    now=now,
                )
            }
        return {
            "server": self.config.server.name,
            "mode": self._mode,
            "now": now.isoformat(),
            "detection_error": live.error,
            "gpus": self._merge_gpus(db_gpus, live, active_ids),
            "allocations": allocations,
            "unmanaged": self._unmanaged(db_gpus, active_ids, live),
        }

    def _unmanaged(
        self,
        gpus: list[dict[str, Any]],
        active_allocation_ids: set[str],
        live: LiveResources,
    ) -> dict[str, list[dict[str, Any]]]:
        container_by_uuid = {
            str(gpu["uuid"]): str(gpu["container_id"]) for gpu in gpus if gpu["container_id"]
        }
        index_by_uuid = {str(gpu["uuid"]): int(gpu["index"]) for gpu in gpus}
        unmanaged_containers: list[dict[str, Any]] = []
        for container in live.containers:
            allocation_id = container.labels.get("udc.allocation")
            if (
                container.labels.get("udc.managed") == "true"
                and allocation_id in active_allocation_ids
            ):
                continue
            resolved, unknown_ref = self._resolve_gpu_refs(container.gpu_refs, gpus)
            indices = sorted({int(gpu["index"]) for gpu in gpus if str(gpu["uuid"]) in resolved})
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
                }
            )
        unmanaged_processes: list[dict[str, Any]] = []
        for process in live.processes:
            container_id = container_by_uuid.get(process.gpu_uuid)
            if container_id and self._pid_in_container(process.pid, container_id):
                continue
            unmanaged_processes.append(
                {
                    "gpu_uuid": process.gpu_uuid,
                    "gpu_index": index_by_uuid.get(process.gpu_uuid),
                    "pid": process.pid,
                    "used_mib": process.used_mib,
                    "process_name": process.process_name,
                }
            )
        return {
            "containers": unmanaged_containers,
            "processes": unmanaged_processes,
        }

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

    def _status(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation_id = args.get("id")
        if allocation_id is not None and not isinstance(allocation_id, str):
            raise BadRequest("id must be a string")
        with self._connection() as connection:
            if allocation_id is None:
                return {
                    "allocations": store.list_allocations(
                        connection, owner_uid=identity.uid, now=self._now()
                    )
                }
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
            if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may inspect this allocation")
            return {
                "allocation": allocation,
                "events": store.list_allocation_events(connection, allocation_id),
            }

    async def _apply(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        self._require_active()
        gpus = self._parse_gpus(args.get("gpus", []))
        cpus = self._parse_positive_float(args.get("cpus"), "cpus")
        memory_gb = self._parse_positive_int(args.get("memory_gb"), "memory_gb")
        hours = self._parse_positive_int(
            args.get("hours", self.config.policy.default_hours), "hours"
        )
        if not 1 <= hours <= self.config.policy.max_hours:
            raise BadRequest(f"hours must be between 1 and {self.config.policy.max_hours}")
        if cpus > self.config.policy.max_cpus:
            raise InsufficientResources(
                f"requested cpus exceed policy.max_cpus ({self.config.policy.max_cpus:g})"
            )
        if memory_gb > self.config.policy.max_memory_gb:
            raise InsufficientResources(
                f"requested memory exceeds policy.max_memory_gb "
                f"({self.config.policy.max_memory_gb})"
            )
        image = args.get("image", self.config.image.base)
        if not isinstance(image, str) or not image:
            raise BadRequest("image must be a non-empty string")
        if not identity.is_admin and image != self.config.image.base:
            raise Forbidden("members may only use the configured base image")
        note = args.get("note", "")
        if not isinstance(note, str):
            raise BadRequest("note must be a string")
        workspace = self._workspace(identity.username)
        starts_at = self._now()
        expires_at = starts_at + timedelta(hours=hours)
        if gpus:
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                gpu_rows = store.gpus_for_indices(connection, gpus)
                active_ids = {
                    str(allocation["id"])
                    for allocation in store.list_allocations(
                        connection,
                        statuses=("pending", "active"),
                        now=now,
                    )
                }
            live = await asyncio.to_thread(self._live_resources)
            merged = self._merge_gpus(db_gpus, live, active_ids)
            by_index = {int(gpu["index"]): gpu for gpu in merged}
            for index in gpus:
                gpu = by_index.get(index)
                if gpu is None:
                    raise NotFound(f"unknown GPU index: {index}")
                state = str(gpu["state"])
                if state == "unknown":
                    raise DockerError(f"cannot determine GPU {index} state: {gpu['state_reason']}")
                if state != "free":
                    raise GpuBusy(f"GPU {index} is {state}: {gpu['state_reason']}")
            gpu_uuids = [str(row["uuid"]) for row in gpu_rows]
        else:
            gpu_uuids = []
        try:
            await asyncio.to_thread(self._ensure_workspace, workspace)
        except UdcError:
            raise
        except Exception as exc:
            raise InternalError(f"workspace creation failed: {exc}") from exc
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
                max_cpus=self.config.policy.max_cpus,
                max_memory_gb=self.config.policy.max_memory_gb,
                now=self._now(),
            )
            allocation_data = store.get_allocation(connection, allocation_id, now=self._now())
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
            logger.exception("allocation activation failed for %s", allocation_id)
            try:
                await asyncio.to_thread(self.docker.remove_container, container.id)
            except Exception:
                logger.exception(
                    "could not remove container %s after activation failure",
                    container.id,
                )
                raise InternalError(
                    "allocation activation failed and container cleanup failed"
                ) from exc
            with self._connection() as connection:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason=f"activation failed: {exc}",
                    now=self._now(),
                )
            raise InternalError("allocation could not be activated") from exc

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
            return store.get_allocation(connection, allocation_id, now=self._now())

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

    def _ensure_workspace(self, path: str) -> None:
        workspace = Path(path)
        if workspace.is_symlink():
            raise Forbidden(f"workspace path is a symlink: {workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
        if not workspace.is_dir():
            raise Forbidden(f"workspace path is not a directory: {workspace}")
        workspace.chmod(0o700)

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
        if mode not in {"monitor", "active"}:
            raise BadRequest("mode must be monitor or active")
        if mode == "active" and not self.config.policy.deployment_allowed:
            raise Forbidden("deployment_allowed=false; active mode is not approved")
        if mode == "active":
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                active_ids = {
                    str(allocation["id"])
                    for allocation in store.list_allocations(
                        connection,
                        statuses=("pending", "active"),
                        now=now,
                    )
                }
            live = await asyncio.to_thread(self._live_resources)
            if live.error:
                raise Conflict(f"active mode blocked: live GPU detection failed: {live.error}")
            merged = self._merge_gpus(db_gpus, live, active_ids)
            unmanaged = self._unmanaged(db_gpus, active_ids, live)
            blocking_gpus = [
                f"GPU {gpu['index']} ({gpu['state']}: {gpu['state_reason']})"
                for gpu in merged
                if gpu["state"] in {"unmanaged", "unknown"}
            ]
            blocking_containers = [
                str(container["name"])
                for container in unmanaged["containers"]
                if container.get("gpus")
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
        return {"mode": self._mode}

    @staticmethod
    def _require_admin(identity: Identity) -> None:
        if not identity.is_admin:
            raise Forbidden("admin permission required")

    def _require_active(self) -> None:
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
