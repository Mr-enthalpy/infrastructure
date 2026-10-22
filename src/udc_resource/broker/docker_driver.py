"""Docker and NVIDIA lifecycle backend."""

from __future__ import annotations

import csv
import io
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

import docker
from docker.errors import DockerException, NotFound

from ..config import LabConfig
from ..errors import DockerError
from ..errors import NotFound as UdcNotFound
from ..models import GPU, Allocation

logger = logging.getLogger(__name__)

LABEL_MANAGED = "udc.managed"
LABEL_OWNER = "udc.owner"
LABEL_ALLOCATION = "udc.allocation"
LABEL_GPUS = "udc.gpus"


@dataclass(frozen=True)
class ContainerRef:
    id: str
    name: str


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    image: str
    status: str
    labels: dict[str, str]


@dataclass(frozen=True)
class GPUProcess:
    gpu_uuid: str
    pid: int
    used_mib: int
    process_name: str


class ShellSession(Protocol):
    def read(self, size: int = 65536) -> bytes: ...

    def write(self, data: bytes) -> None: ...

    def resize(self, cols: int, rows: int) -> None: ...

    def close(self) -> None: ...


class DockerBackend(Protocol):
    def list_gpus(self) -> list[GPU]: ...

    def list_compute_processes(self) -> list[GPUProcess]: ...

    def list_containers(self) -> list[ContainerInfo]: ...

    def get_container(self, container_id: str) -> ContainerInfo | None: ...

    def create_container(self, allocation: Allocation) -> ContainerRef: ...

    def remove_container(self, container_id: str) -> None: ...

    def open_shell(self, container_id: str, cols: int, rows: int) -> ShellSession: ...

    def logs(self, container_id: str, tail: int) -> str: ...


class DockerShellSession:
    def __init__(self, client: Any, exec_id: str, sock: Any) -> None:
        self.client = client
        self.exec_id = exec_id
        self.sock = sock

    def read(self, size: int = 65536) -> bytes:
        try:
            return bytes(self.sock.recv(size))
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def resize(self, cols: int, rows: int) -> None:
        try:
            self.client.api.exec_resize(self.exec_id, height=rows, width=cols)
        except DockerException:
            logger.warning("could not resize shell %s", self.exec_id, exc_info=True)

    def close(self) -> None:
        try:
            self.sock.shutdown(2)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class DockerDriver:
    """Real Docker/NVIDIA backend used by the root broker."""

    def __init__(self, config: LabConfig) -> None:
        self.config = config
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                self._client = docker.from_env()
            except DockerException as exc:
                raise DockerError(f"Docker is unavailable: {exc}") from exc
        return self._client

    def _nvidia_smi(self, query: str) -> list[list[str]]:
        command = [
            "nvidia-smi",
            f"--query-{query}",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise DockerError("nvidia-smi is not installed or not in PATH") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or "").strip() or str(exc)
            raise DockerError(f"nvidia-smi failed: {message}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError("nvidia-smi timed out") from exc
        reader = csv.reader(io.StringIO(result.stdout))
        return [
            [cell.strip() for cell in row]
            for row in reader
            if row and any(cell.strip() for cell in row)
        ]

    @staticmethod
    def _as_int(value: str) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def list_gpus(self) -> list[GPU]:
        rows = self._nvidia_smi("gpu=index,uuid,name,memory.total,memory.used,utilization.gpu")
        pids_by_uuid: dict[str, list[int]] = {}
        for process in self.list_compute_processes():
            pids_by_uuid.setdefault(process.gpu_uuid, []).append(process.pid)
        gpus: list[GPU] = []
        for row in rows:
            if len(row) < 6:
                continue
            try:
                index = int(row[0])
            except ValueError:
                continue
            uuid = row[1]
            gpus.append(
                GPU(
                    index=index,
                    uuid=uuid,
                    name=row[2],
                    memory_mib=self._as_int(row[3]),
                    used_mib=self._as_int(row[4]),
                    utilization=self._as_int(row[5]),
                    compute_pids=tuple(pids_by_uuid.get(uuid, ())),
                )
            )
        return gpus

    def list_compute_processes(self) -> list[GPUProcess]:
        rows = self._nvidia_smi("compute-apps=gpu_uuid,pid,used_memory,process_name")
        processes: list[GPUProcess] = []
        for row in rows:
            if len(row) < 4:
                continue
            try:
                pid = int(row[1])
            except ValueError:
                continue
            processes.append(
                GPUProcess(
                    gpu_uuid=row[0],
                    pid=pid,
                    used_mib=self._as_int(row[2]),
                    process_name=row[3],
                )
            )
        return processes

    @staticmethod
    def _container_info(container: Any) -> ContainerInfo:
        attrs = getattr(container, "attrs", {}) or {}
        config = attrs.get("Config", {}) or {}
        raw_labels = config.get("Labels") or {}
        labels = {str(key): str(value) for key, value in raw_labels.items()}
        state = attrs.get("State", {}) or {}
        return ContainerInfo(
            id=str(container.id),
            name=str(container.name),
            image=str(config.get("Image", "")),
            status=str(getattr(container, "status", "") or state.get("Status", "")),
            labels=labels,
        )

    def list_containers(self) -> list[ContainerInfo]:
        try:
            containers = self.client.containers.list(all=True)
        except DockerException as exc:
            raise DockerError(f"cannot list containers: {exc}") from exc
        return [self._container_info(container) for container in containers]

    def get_container(self, container_id: str) -> ContainerInfo | None:
        if not container_id:
            return None
        try:
            return self._container_info(self.client.containers.get(container_id))
        except NotFound:
            return None
        except DockerException as exc:
            raise DockerError(f"cannot inspect container {container_id}: {exc}") from exc

    def create_container(self, allocation: Allocation) -> ContainerRef:
        labels = {
            LABEL_MANAGED: "true",
            LABEL_OWNER: allocation.owner_name,
            LABEL_ALLOCATION: allocation.id,
            LABEL_GPUS: ",".join(allocation.gpus),
        }
        name = f"udc-{allocation.owner_name}-{allocation.id[:12]}"
        kwargs: dict[str, Any] = {
            "command": ["sleep", "infinity"],
            "detach": True,
            "name": name,
            "labels": labels,
            "nano_cpus": int(allocation.cpus * 1_000_000_000),
            "mem_limit": f"{allocation.memory_gb}g",
            "memswap_limit": f"{allocation.memory_gb}g",
            "volumes": {
                allocation.workspace: {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            },
            "working_dir": "/workspace",
            "environment": {"NVIDIA_DRIVER_CAPABILITIES": "compute,utility"},
            "security_opt": ["no-new-privileges"],
            "init": True,
            "privileged": False,
            "network_mode": "bridge",
            "user": "0:0",
        }
        if allocation.gpus:
            kwargs["device_requests"] = [
                docker.types.DeviceRequest(
                    device_ids=list(allocation.gpus),
                    capabilities=[["gpu"]],
                )
            ]
        try:
            container = self.client.containers.run(allocation.image, **kwargs)
        except DockerException as exc:
            raise DockerError(f"docker run failed: {exc}") from exc
        return ContainerRef(id=str(container.id), name=str(container.name))

    def remove_container(self, container_id: str) -> None:
        if not container_id:
            return
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return
        except DockerException as exc:
            raise DockerError(f"cannot inspect container {container_id}: {exc}") from exc
        try:
            container.stop(timeout=10)
        except NotFound:
            return
        except DockerException:
            logger.warning("could not stop container %s cleanly", container_id, exc_info=True)
        try:
            container.remove(force=True)
        except NotFound:
            return
        except DockerException as exc:
            raise DockerError(f"cannot remove container {container_id}: {exc}") from exc

    def open_shell(self, container_id: str, cols: int, rows: int) -> ShellSession:
        try:
            exec_id = self.client.api.exec_create(
                container_id,
                cmd=["/bin/bash", "-l"],
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
                workdir="/workspace",
                user="0",
            )["Id"]
            sock = self.client.api.exec_start(exec_id, tty=True, socket=True)
            self.client.api.exec_resize(exec_id, height=rows, width=cols)
            sock.settimeout(None)
        except DockerException as exc:
            raise DockerError(f"cannot open shell in {container_id}: {exc}") from exc
        return DockerShellSession(self.client, str(exec_id), sock)

    def logs(self, container_id: str, tail: int) -> str:
        try:
            container = self.client.containers.get(container_id)
            data = container.logs(tail=tail, timestamps=False)
        except NotFound as exc:
            raise UdcNotFound(f"container not found: {container_id}") from exc
        except DockerException as exc:
            raise DockerError(f"cannot read container logs: {exc}") from exc
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)
