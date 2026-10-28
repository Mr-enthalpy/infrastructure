from __future__ import annotations

from typing import Any

from citespec_resource.broker.docker_driver import (
    ContainerInfo,
    ContainerRef,
    GPUProcess,
)
from citespec_resource.errors import DockerError
from citespec_resource.identity import Identity
from citespec_resource.models import GPU, Allocation


class FakeShellSession:
    def __init__(self, output: bytes = b"") -> None:
        self.chunks = [output] if output else []
        self.written = bytearray()
        self.sizes: list[tuple[int, int]] = []
        self.closed = False

    def read(self, size: int = 65536) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    def close(self) -> None:
        self.closed = True


class FakeDocker:
    def __init__(
        self,
        gpu_count: int = 8,
        fail_create: bool = False,
        fail_remove: bool = False,
        fail_identity_init: bool = False,
    ) -> None:
        self.gpus = [
            GPU(
                index=index,
                uuid=f"GPU-{index}",
                name="NVIDIA GeForce RTX 4090",
                memory_mib=49140,
            )
            for index in range(gpu_count)
        ]
        self.created: list[Allocation] = []
        self.removed: list[str] = []
        self.containers: dict[str, ContainerInfo] = {}
        self.processes: list[GPUProcess] = []
        self.container_logs: dict[str, str] = {}
        self.shell_output = b"fake shell output\n"
        self.shells: list[FakeShellSession] = []
        self.jobs: dict[str, dict[str, dict[str, str]]] = {}
        self.started_job_commands: list[tuple[str, str, list[str]]] = []
        self.fail_create = fail_create
        self.fail_remove = fail_remove
        self.storage_ok = True
        self.storage_detail = "storage preflight passed"
        self.storage_entitlement_calls: list[int] = []
        self.storage_entitlement_limit_bytes = 0
        self.prepared_ssh: list[dict[str, object]] = []
        self.initialized_identity: list[dict[str, object]] = []
        self.fail_identity_init = fail_identity_init
        self.tunnels: list[str] = []
        self.images: dict[str, dict[str, object]] = {}
        self.exec_calls: list[tuple[str, list[str], str | None]] = []
        self.exec_output: tuple[int, str, str] = (0, "", "")
        self._next_id = 1

    def list_gpus(self) -> list[GPU]:
        return list(self.gpus)

    def list_compute_processes(self) -> list[GPUProcess]:
        return list(self.processes)

    def list_containers(self) -> list[ContainerInfo]:
        return list(self.containers.values())

    def get_container(self, container_id: str) -> ContainerInfo | None:
        return self.containers.get(container_id)

    def image_details(self, image: str) -> dict[str, Any]:
        details = self.images.get(image)
        if details is None:
            raise DockerError(f"image not found locally: {image}")
        return dict(details)

    def exec_capture(
        self,
        container_id: str,
        argv: list[str],
        *,
        user: str | None = None,
    ) -> tuple[int, str, str]:
        self.exec_calls.append((container_id, list(argv), user))
        return self.exec_output

    def add_container(self, container: ContainerInfo) -> None:
        self.containers[container.id] = container

    def add_process(self, process: GPUProcess) -> None:
        self.processes.append(process)

    def create_container(self, allocation: Allocation) -> ContainerRef:
        if self.fail_create:
            raise RuntimeError("simulated docker failure")
        container_id = f"container-{self._next_id}"
        name = f"citespec-{allocation.owner_name}-{self._next_id}"
        self._next_id += 1
        self.created.append(allocation)
        self.containers[container_id] = ContainerInfo(
            id=container_id,
            name=name,
            image=allocation.image,
            status="running",
            labels={
                "citespec.managed": "true",
                "citespec.owner": allocation.owner_name,
                "citespec.allocation": allocation.id,
                "citespec.gpus": ",".join(allocation.gpus),
                "citespec.profile": "restricted",
            },
        )
        return ContainerRef(id=container_id, name=name)

    def initialize_container_identity(self, container_id: str, allocation: Allocation) -> None:
        if self.fail_identity_init:
            raise RuntimeError("simulated identity initialization failure")
        self.initialized_identity.append(
            {
                "container_id": container_id,
                "allocation_id": allocation.id,
                "owner_uid": allocation.owner_uid,
                "owner_name": allocation.owner_name,
            }
        )

    def remove_container(self, container_id: str) -> None:
        if self.fail_remove:
            raise RuntimeError("simulated docker remove failure")
        self.removed.append(container_id)
        self.containers.pop(container_id, None)

    def storage_preflight(
        self,
        *,
        writable_layer_entitlements_bytes: int = 0,
    ) -> tuple[bool, str]:
        self.storage_entitlement_calls.append(writable_layer_entitlements_bytes)
        if (
            self.storage_entitlement_limit_bytes > 0
            and writable_layer_entitlements_bytes > self.storage_entitlement_limit_bytes
        ):
            return (
                False,
                "simulated: writable-layer entitlements exceed the free headroom",
            )
        return self.storage_ok, self.storage_detail

    def prepare_container_ssh(
        self,
        container_id: str,
        uid: int,
        gid: int,
        username: str,
        public_keys: list[str],
        home: str,
    ) -> None:
        self.prepared_ssh.append(
            {
                "container_id": container_id,
                "uid": uid,
                "gid": gid,
                "username": username,
                "public_keys": list(public_keys),
                "home": home,
            }
        )

    def open_tunnel(self, container_id: str) -> FakeShellSession:
        self.tunnels.append(container_id)
        return FakeShellSession()

    def open_shell(
        self,
        container_id: str,
        cols: int,
        rows: int,
        command: list[str] | None = None,
    ) -> FakeShellSession:
        session = FakeShellSession(self.shell_output)
        session.sizes.append((cols, rows))
        session.command = command
        self.shells.append(session)
        return session

    def logs(self, container_id: str, tail: int) -> str:
        return self.container_logs.get(container_id, "fake log line\n")

    def start_job(self, container_id: str, name: str, command: list[str]) -> None:
        container_jobs = self.jobs.setdefault(container_id, {})
        if name in container_jobs:
            raise RuntimeError(f"job already exists: {name}")
        container_jobs[name] = {"created": "1", "attached": "0"}
        self.started_job_commands.append((container_id, name, list(command)))

    def list_jobs(self, container_id: str) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "session": f"citespec-{name}",
                "created": info["created"],
                "attached": info["attached"],
            }
            for name, info in self.jobs.get(container_id, {}).items()
        ]

    def stop_job(self, container_id: str, name: str) -> None:
        self.jobs.get(container_id, {}).pop(name, None)


class FakeIdentityResolver:
    def __init__(self, identities: dict[int, Identity] | None = None) -> None:
        self.identities = identities or {}

    def resolve(self, uid: int, gid: int) -> Identity:
        if uid in self.identities:
            return self.identities[uid]
        return Identity(
            uid=uid,
            gid=gid,
            username=f"user{uid}",
            role="member",
            is_admin=False,
        )
