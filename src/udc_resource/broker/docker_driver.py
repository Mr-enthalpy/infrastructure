"""Docker and NVIDIA lifecycle backend."""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import shlex
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import docker
from docker.errors import DockerException, NotFound

from ..config import LabConfig
from ..errors import Conflict, DockerError
from ..errors import NotFound as UdcNotFound
from ..models import GPU, Allocation

logger = logging.getLogger(__name__)

LABEL_MANAGED = "udc.managed"
LABEL_OWNER = "udc.owner"
LABEL_ALLOCATION = "udc.allocation"
LABEL_GPUS = "udc.gpus"
LABEL_PROFILE = "udc.profile"

# sudo inside the disposable development container needs a small set of
# capabilities. Nothing here grants host privileges or host namespaces.
DEV_CAPABILITIES = (
    "AUDIT_WRITE",
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
)


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
    gpu_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class GPUProcess:
    gpu_uuid: str
    pid: int
    used_mib: int
    process_name: str
    uid: int | None = None
    username: str = ""


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

    def initialize_container_identity(self, container_id: str, allocation: Allocation) -> None: ...

    def remove_container(self, container_id: str) -> None: ...

    def storage_preflight(self) -> tuple[bool, str]: ...

    def prepare_container_ssh(
        self,
        container_id: str,
        uid: int,
        gid: int,
        username: str,
        public_keys: list[str],
        home: str,
    ) -> None: ...

    def open_tunnel(self, container_id: str) -> ShellSession: ...

    def open_shell(
        self,
        container_id: str,
        cols: int,
        rows: int,
        command: list[str] | None = None,
    ) -> ShellSession: ...

    def logs(self, container_id: str, tail: int) -> str: ...

    def start_job(self, container_id: str, name: str, command: list[str]) -> None: ...

    def list_jobs(self, container_id: str) -> list[dict[str, str]]: ...

    def stop_job(self, container_id: str, name: str) -> None: ...


def matches_managed_allocation(
    container: ContainerInfo,
    allocation_id: str,
    expected_container_id: str | None = None,
) -> bool:
    if container.labels.get("udc.managed") != "true":
        return False
    if container.labels.get("udc.allocation") != allocation_id:
        return False
    if expected_container_id is not None and container.id != expected_container_id:
        return False
    return True


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
        except (AttributeError, OSError):
            pass
        try:
            self.sock.close()
        except (AttributeError, OSError):
            pass


class DockerTunnelSession:
    """Duplex byte stream over a Docker exec without allocating a TTY.

    Docker multiplexes stdout and stderr with an 8-byte header when ``tty`` is
    false. The tunnel only exposes stdout (the SSH protocol); stderr is kept for
    diagnostics.
    """

    def __init__(self, sock: Any) -> None:
        self.sock = sock
        self._buffer = bytearray()
        self._stderr = bytearray()

    def _recv_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                return b""
            data.extend(chunk)
        return bytes(data)

    def _fill(self) -> bool:
        header = self._recv_exact(8)
        if len(header) != 8:
            return False
        stream = header[0]
        length = struct.unpack(">I", header[4:])[0]
        if length == 0:
            return True
        payload = self._recv_exact(length)
        if len(payload) != length:
            return False
        if stream == 2:
            if len(self._stderr) < 64 * 1024:
                self._stderr.extend(payload[: 64 * 1024 - len(self._stderr)])
        else:
            self._buffer.extend(payload)
        return True

    def read(self, size: int = 65536) -> bytes:
        # Return after one stdout frame (or whatever is already buffered). SSH
        # is interactive: waiting for `size` bytes would deadlock until the peer
        # replies, because the banner and key-exchange packets are much smaller.
        while not self._buffer:
            try:
                if not self._fill():
                    return b""
            except OSError:
                return b""
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def resize(self, cols: int, rows: int) -> None:
        del cols, rows

    def close(self) -> None:
        try:
            self.sock.shutdown(2)
        except (AttributeError, OSError):
            pass
        try:
            self.sock.close()
        except (AttributeError, OSError):
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

    @staticmethod
    def _process_owner(pid: int) -> tuple[int | None, str]:
        try:
            status = Path(f"/proc/{pid}/status").read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None, ""
        uid = None
        for line in status.splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        uid = int(parts[1])
                    except ValueError:
                        uid = None
                break
        if uid is None:
            return None, ""
        try:
            import pwd
        except ImportError:
            return uid, ""
        try:
            return uid, pwd.getpwuid(uid).pw_name
        except KeyError:
            return uid, ""

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
            uid, username = self._process_owner(pid)
            processes.append(
                GPUProcess(
                    gpu_uuid=row[0],
                    pid=pid,
                    used_mib=self._as_int(row[2]),
                    process_name=row[3],
                    uid=uid,
                    username=username,
                )
            )
        return processes

    @staticmethod
    def _container_info(container: Any) -> ContainerInfo:
        attrs = getattr(container, "attrs", {}) or {}
        config = attrs.get("Config", {}) or {}
        host_config = attrs.get("HostConfig", {}) or {}
        raw_labels = config.get("Labels") or {}
        labels = {str(key): str(value) for key, value in raw_labels.items()}
        state = attrs.get("State", {}) or {}
        refs: list[str] = []
        for request in host_config.get("DeviceRequests") or []:
            if not isinstance(request, dict):
                continue
            device_ids = request.get("DeviceIDs") or request.get("DeviceIds") or []
            if device_ids:
                refs.extend(str(item) for item in device_ids)
                continue
            count = request.get("Count")
            if count == -1 or (isinstance(count, int) and count < 0):
                refs.append("all")
            elif isinstance(count, int) and count > 0:
                refs.append("any")
        if not refs:
            for device in host_config.get("Devices") or []:
                if not isinstance(device, dict):
                    continue
                path = str(device.get("PathOnHost", ""))
                match = re.search(r"/dev/nvidia(\d+)$", path)
                if match:
                    refs.append(match.group(1))
        if not refs:
            # NVIDIA_VISIBLE_DEVICES is inherited from base images (CUDA
            # images default to "all"), so it is only consulted when Docker
            # reports no authoritative device assignment of its own. Otherwise
            # a container granted one GPU via DeviceRequests plus an inherited
            # image env would falsely appear to hold every GPU.
            env_value = ""
            for item in config.get("Env") or []:
                text = str(item)
                if text.startswith("NVIDIA_VISIBLE_DEVICES="):
                    env_value = text.split("=", 1)[1]
                    break
            if env_value.strip().lower() not in {"", "void", "none"}:
                refs.extend(part.strip() for part in env_value.split(",") if part.strip())
        return ContainerInfo(
            id=str(container.id),
            name=str(container.name),
            image=str(config.get("Image", "")),
            status=str(getattr(container, "status", "") or state.get("Status", "")),
            labels=labels,
            gpu_refs=tuple(dict.fromkeys(refs)),
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

    def _container_user(self, allocation: Allocation) -> str:
        if self.config.policy.container_user == "root":
            return "0:0"
        if os.name != "posix":
            return "0:0"
        try:
            import pwd

            gid = pwd.getpwuid(allocation.owner_uid).pw_gid
        except (ImportError, KeyError):
            gid = allocation.owner_uid
        return f"{allocation.owner_uid}:{gid}"

    @staticmethod
    def _split_user(user: str) -> tuple[int, int]:
        try:
            uid_text, gid_text = user.split(":", 1)
            return int(uid_text), int(gid_text)
        except (ValueError, AttributeError):
            return 0, 0

    def _development_home(self, allocation: Allocation) -> str:
        configured = self.config.policy.container_home.strip()
        if configured:
            home = configured
        else:
            safe_name = re.sub(r"[^a-z0-9_-]", "-", allocation.owner_name.lower())
            home = f"/home/{safe_name or allocation.owner_uid}"
        normalized = PurePosixPath(home)
        if (
            ".." in normalized.parts
            or not home.startswith("/home/")
            or home == "/home/"
            or home == "/"
            or home == "/workspace"
            or home.startswith("/workspace/")
        ):
            raise DockerError("development container HOME must be under /home")
        return home

    def _gpu_group_id(self) -> int | None:
        if self.config.policy.container_user != "member" or os.name != "posix":
            return None
        try:
            import grp

            return grp.getgrnam(self.config.groups.gpu).gr_gid
        except (ImportError, KeyError):
            return None

    def create_container(self, allocation: Allocation) -> ContainerRef:
        dev_profile = self.config.policy.container_profile == "development"
        labels = {
            LABEL_MANAGED: "true",
            LABEL_OWNER: allocation.owner_name,
            LABEL_ALLOCATION: allocation.id,
            LABEL_GPUS: ",".join(allocation.gpus),
            LABEL_PROFILE: self.config.policy.container_profile,
        }
        name = f"udc-{allocation.owner_name}-{allocation.id[:12]}"
        container_user = self._container_user(allocation)
        uid, gid = self._split_user(container_user)
        home = self._development_home(allocation) if dev_profile else "/workspace"
        tmpfs = {
            "/tmp": "rw,nosuid,nodev,size=1g",
            "/run": "rw,nosuid,nodev,size=64m",
            "/var/tmp": "rw,nosuid,nodev,size=256m",
        }
        if dev_profile and self.config.policy.container_home_tmpfs_gb > 0:
            # Opt-in tmpfs HOME. By default HOME lives in the container's
            # quota-bounded writable layer (created by
            # initialize_container_identity) so VS Code Server and tool caches
            # are bounded by the same storage_opt limit and vanish with the
            # container instead of consuming host memory.
            tmpfs[home] = (
                f"rw,nosuid,nodev,size={self.config.policy.container_home_tmpfs_gb}g,"
                f"uid={uid},gid={gid},mode=0700"
            )
        # CUDA base images ship NVIDIA_VISIBLE_DEVICES=all. Without an explicit
        # override the NVIDIA runtime injects every GPU into a container that
        # was granted one, and CPU-only containers inherit image defaults.
        if allocation.gpus:
            nvidia_env = {
                "NVIDIA_VISIBLE_DEVICES": ",".join(allocation.gpus),
                "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
            }
        else:
            nvidia_env = {
                "NVIDIA_VISIBLE_DEVICES": "void",
                "NVIDIA_DRIVER_CAPABILITIES": "void",
            }
        kwargs: dict[str, Any] = {
            "command": ["sleep", "infinity"],
            "detach": True,
            "name": name,
            "labels": labels,
            "nano_cpus": int(allocation.cpus * 1_000_000_000),
            "mem_limit": f"{allocation.memory_gb}g",
            "memswap_limit": f"{allocation.memory_gb}g",
            "pids_limit": self.config.policy.pids_limit,
            "volumes": {
                allocation.workspace: {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            },
            "working_dir": "/workspace",
            "environment": {
                "HOME": home,
                "PYTHONDONTWRITEBYTECODE": "1",
                **nvidia_env,
            },
            "cap_drop": ["ALL"],
            "read_only": False if dev_profile else self.config.policy.container_read_only,
            "tmpfs": tmpfs,
            "init": True,
            "privileged": False,
            "network_mode": "bridge",
            "user": container_user,
        }
        if dev_profile:
            kwargs["cap_add"] = list(DEV_CAPABILITIES)
            if self.config.policy.writable_layer_gb > 0:
                kwargs["storage_opt"] = {"size": f"{self.config.policy.writable_layer_gb}G"}
        else:
            kwargs["security_opt"] = ["no-new-privileges"]
        if self.config.policy.userns_mode:
            kwargs["userns_mode"] = self.config.policy.userns_mode
        if allocation.gpus:
            kwargs["device_requests"] = [
                docker.types.DeviceRequest(
                    device_ids=list(allocation.gpus),
                    capabilities=[["gpu"]],
                )
            ]
            gpu_group = self._gpu_group_id()
            if container_user != "0:0" and gpu_group is not None:
                kwargs["group_add"] = [str(gpu_group)]
        try:
            container = self.client.containers.run(allocation.image, **kwargs)
        except DockerException as exc:
            raise DockerError(f"docker run failed: {exc}") from exc
        return ContainerRef(id=str(container.id), name=str(container.name))

    def storage_preflight(self) -> tuple[bool, str]:
        """Check Docker storage capacity and writable-layer quota enforcement.

        ``storage-opt size`` works only with the overlay2 driver on XFS mounted
        with project quotas. Anything else must fail closed. Independently of
        the profile, the Docker root must keep ``policy.min_free_docker_gb``
        free: per-container writable-layer quotas do not stop many containers
        from together filling the disk that also holds OS and Docker metadata.
        """

        policy = self.config.policy
        info: dict[str, Any] | None = None
        if (
            policy.min_free_docker_gb > 0
            or policy.docker_storage_budget_gb > 0
            or (policy.container_profile == "development" and policy.writable_layer_gb > 0)
        ):
            try:
                info = self.client.info()
            except DockerException as exc:
                return False, f"cannot inspect Docker storage: {exc}"
        if policy.min_free_docker_gb > 0 or policy.docker_storage_budget_gb > 0:
            docker_root = str((info or {}).get("DockerRootDir", ""))
            if not docker_root:
                return False, "DockerRootDir is unknown; cannot verify free capacity"
            try:
                usage = shutil.disk_usage(docker_root)
            except OSError as exc:
                return False, f"cannot inspect Docker storage directory: {exc}"
            if policy.min_free_docker_gb > 0:
                free_gb = usage.free // 1024**3
                if free_gb < policy.min_free_docker_gb:
                    return False, (
                        f"Docker storage {docker_root} has only {free_gb} GiB free; "
                        f"policy.min_free_docker_gb is {policy.min_free_docker_gb}"
                    )
            if policy.docker_storage_budget_gb > 0:
                # The budget promises writable-layer space to containers; it
                # must fit the real filesystem together with the reserve, or
                # the aggregate protection is fiction.
                required_bytes = (
                    policy.docker_storage_budget_gb + policy.min_free_docker_gb
                ) * 1024**3
                total_gib = usage.total // 1024**3
                if usage.total < required_bytes:
                    return False, (
                        f"Docker storage budget {policy.docker_storage_budget_gb} GiB plus "
                        f"reserve {policy.min_free_docker_gb} GiB exceeds the {total_gib} GiB "
                        f"filesystem at {docker_root}; lower docker_storage_budget_gb"
                    )
        if policy.container_profile != "development" or policy.writable_layer_gb <= 0:
            return True, "writable-layer quota is not required"
        if os.name != "posix":
            return False, "Docker storage preflight is only supported on Linux"
        driver = str((info or {}).get("Driver", ""))
        docker_root = str((info or {}).get("DockerRootDir", ""))
        if driver != "overlay2":
            return False, f"Docker storage driver is {driver or 'unknown'}, expected overlay2"
        findmnt = shutil.which("findmnt")
        if findmnt is None:
            return False, "findmnt is not installed"
        try:
            completed = subprocess.run(
                [findmnt, "-no", "FSTYPE,OPTIONS", "-T", docker_root],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"cannot inspect Docker root filesystem: {exc}"
        if completed.returncode != 0 or not completed.stdout.strip():
            return False, f"cannot inspect Docker root filesystem: {completed.stderr.strip()}"
        parts = completed.stdout.strip().split(None, 1)
        fstype = parts[0] if parts else ""
        options = set(parts[1].split(",")) if len(parts) > 1 else set()
        if fstype != "xfs":
            return False, f"Docker root filesystem is {fstype or 'unknown'}, expected XFS"
        if not {"prjquota", "pquota"} & options:
            return False, "Docker root XFS filesystem is not mounted with prjquota"
        # Container root is host root without a user namespace. The workspace is
        # the only writable host bind mount, so it must never honor host setuid
        # bits or file capabilities written by the container's sudo.
        workspace_root = self.config.server.workspace_root.strip()
        if not workspace_root or not os.path.isabs(workspace_root):
            return False, "server.workspace_root must be an absolute path; cannot verify nosuid"
        if workspace_root.rstrip("/") == "":
            return False, "server.workspace_root must not be the filesystem root"
        if not Path(workspace_root).is_dir():
            return False, f"workspace root does not exist: {workspace_root}"
        try:
            workspace_mount = subprocess.run(
                [findmnt, "-no", "OPTIONS", "-T", workspace_root],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"cannot inspect workspace filesystem: {exc}"
        if workspace_mount.returncode != 0 or not workspace_mount.stdout.strip():
            return False, ("cannot inspect workspace filesystem: " + workspace_mount.stderr.strip())
        workspace_options = {item.strip() for item in workspace_mount.stdout.strip().split(",")}
        if "nosuid" not in workspace_options:
            return False, (
                f"workspace filesystem {workspace_root} must be mounted nosuid "
                "so container root cannot create host setuid files"
            )
        return True, "overlay2 on XFS with project quotas and a nosuid workspace are available"

    def _exec_capture(
        self,
        container_id: str,
        argv: list[str],
        *,
        user: str | None = None,
    ) -> tuple[int, str, str]:
        try:
            exec_id = self.client.api.exec_create(
                container_id,
                cmd=argv,
                stdin=False,
                stdout=True,
                stderr=True,
                tty=False,
                user=user,
            )["Id"]
            output = self.client.api.exec_start(exec_id, tty=False, stream=False)
            info = self.client.api.exec_inspect(exec_id)
        except DockerException as exc:
            raise DockerError(f"docker exec failed: {exc}") from exc
        text = (
            output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        )
        return int(info.get("ExitCode", -1)), text, ""

    @staticmethod
    def _identity_script(
        uid: int,
        gid: int,
        username: str,
        home: str,
        gpu_gid: int | None = None,
    ) -> str:
        """Build the idempotent passwd/group/home bootstrap for a member UID.

        Docker happily runs a numeric UID that has no /etc/passwd entry, but
        sudo and login shells need a full identity record. This runs at
        allocation creation (not only when VS Code is opened) so the very
        first `sudo apt install` inside the container works.

        When ``gpu_gid`` is set, a container group with the host's udc-gpu
        numeric GID is ensured and the member is added to it: host NVIDIA
        device nodes are root:<gpu-gid> 0660, and an SSH login rebuilds
        supplementary groups from the container's /etc/group, so docker's
        group_add alone only covers the docker-exec path.
        """

        safe_name = re.sub(r"[^a-z0-9_-]", "-", username.lower()) or f"udc{uid}"
        gpu_group_block = ""
        if gpu_gid is not None:
            gpu_group_block = f"""# GPU device nodes are host root:{gpu_gid} 0660.
# Reuse an existing container group with that GID if present, otherwise
# create one, and make the member a supplementary member so sshd login
# sessions get the same device access as docker exec (HostConfig group_add
# does not apply to sshd sessions).
if grep -q "^[^:]*:[^:]*:{gpu_gid}:" /etc/group; then
    awk -F: -v OFS=: -v gid="{gpu_gid}" -v name="{safe_name}" '
        $3 == gid {{
            if ($4 == "") {{
                $4 = name
            }} else {{
                split($4, members, ",")
                found = 0
                for (i in members) if (members[i] == name) found = 1
                if (!found) $4 = $4 "," name
            }}
            done = 1
        }}
        {{ print }}
        END {{
            if (!done) {{
                print "no container group has GID {gpu_gid}" > "/dev/stderr"
                exit 1
            }}
        }}
    ' /etc/group > /etc/group.udc
    chmod 0644 /etc/group.udc
    mv /etc/group.udc /etc/group
else
    printf 'udc-gpu:x:{gpu_gid}:{safe_name}\\n' >> /etc/group
fi
"""
        return f"""
set -eu
home={shlex.quote(home)}
group_entry="udc-{safe_name}:x:{gid}:"
# Serialize concurrent preparation when util-linux is available.
if command -v flock >/dev/null 2>&1; then
    exec 9>/run/udc-ssh.lock
    flock 9
fi
# Base images commonly already use UID 1000 for an "ubuntu" account. Reuse the
# entry for the member UID instead of failing login, and drop a conflicting
# image account with the same name when it is a regular user account.
awk -F: -v OFS=: -v uid="{uid}" -v gid="{gid}" -v name="{safe_name}" -v home="$home" '
    $1 == name && $3 != uid {{
        if ($3 + 0 < 1000) {{
            printf "conflicting system user %s has UID %s\\n", $1, $3 > "/dev/stderr"
            failed = 1
            exit 1
        }}
        next
    }}
    $3 == uid {{
        if (done) next
        $1 = name
        $4 = gid
        $5 = "UDC member"
        $6 = home
        $7 = "/bin/bash"
        print
        done = 1
        next
    }}
    {{ print }}
    END {{
        if (failed) exit 1
        if (!done) print name ":x:" uid ":" gid ":UDC member:" home ":/bin/bash"
    }}
' /etc/passwd > /etc/passwd.udc
chmod 0644 /etc/passwd.udc
mv /etc/passwd.udc /etc/passwd
grep -q "^[^:]*:[^:]*:{gid}:" /etc/group || echo "$group_entry" >> /etc/group
{gpu_group_block}mkdir -p "$home"
chown {uid}:{gid} "$home"
chmod 700 "$home"
"""

    def initialize_container_identity(self, container_id: str, allocation: Allocation) -> None:
        """Create the member identity inside a dev container after creation.

        Called by the broker before an allocation can become active, so the
        container is never usable without a passwd entry. No-op for the
        restricted profile, which grants no sudo and keeps a read-only rootfs.
        """

        if self.config.policy.container_profile != "development":
            return
        container_user = self._container_user(allocation)
        uid, gid = self._split_user(container_user)
        if uid <= 0:
            raise DockerError("development containers require a non-root member UID")
        home = self._development_home(allocation)
        code, output, _err = self._exec_capture(
            container_id,
            [
                "/bin/sh",
                "-c",
                self._identity_script(
                    uid,
                    gid,
                    allocation.owner_name,
                    home,
                    gpu_gid=self._gpu_group_id(),
                ),
            ],
            user="0:0",
        )
        if code != 0:
            raise DockerError(f"cannot initialize container identity: {output.strip()}")

    def prepare_container_ssh(
        self,
        container_id: str,
        uid: int,
        gid: int,
        username: str,
        public_keys: list[str],
        home: str,
    ) -> None:
        """Install a per-allocation SSH identity inside a dev container.

        The passwd entry is only re-ensured here (idempotently); the
        authoritative creation point is :meth:`initialize_container_identity`
        at allocation creation.
        """

        if self.config.policy.container_profile != "development":
            raise DockerError("VS Code tunnels require the development container profile")
        if not public_keys:
            raise DockerError("member has no SSH public keys to authorize")
        if uid <= 0:
            raise DockerError("VS Code tunnels require a non-root member UID")
        if ".." in PurePosixPath(home).parts or not home.startswith("/home/") or home == "/home/":
            raise DockerError("invalid container home for SSH")
        keys = "\n".join(key.strip() for key in public_keys if key.strip())
        script = f"""
set -eu
{self._identity_script(uid, gid, username, home, gpu_gid=self._gpu_group_id())}
mkdir -p "$home/.ssh"
chmod 700 "$home/.ssh"
# Merge with existing authorized keys instead of overwriting: a concurrent
# connection (e.g. an administrator opening the member's allocation) must not
# revoke keys that other authorized users installed earlier. Dedup, prefer the
# newest additions, and cap the file so it cannot grow without bound.
{{
    if [ -f "$home/.ssh/authorized_keys" ]; then
        cat "$home/.ssh/authorized_keys"
    fi
    printf '%s\\n' {shlex.quote(keys)}
}} | awk '!seen[$0]++' | tail -n 40 > "$home/.ssh/authorized_keys.udc"
chmod 600 "$home/.ssh/authorized_keys.udc"
chown {uid}:{gid} "$home/.ssh/authorized_keys.udc"
mv "$home/.ssh/authorized_keys.udc" "$home/.ssh/authorized_keys"
chown {uid}:{gid} "$home/.ssh"
# /run is a fresh tmpfs at container start, so the image's /run/sshd is gone.
mkdir -p /run/sshd
chmod 0755 /run/sshd
# Create any missing host keys without replacing keys that already exist.
ssh-keygen -A >/dev/null
"""
        code, output, _err = self._exec_capture(
            container_id,
            ["/bin/sh", "-c", script],
            user="0:0",
        )
        if code != 0:
            raise DockerError(f"cannot prepare container SSH: {output.strip()}")

    def open_tunnel(self, container_id: str) -> ShellSession:
        """Open a byte tunnel to the container's inetd-mode sshd."""

        if self.config.policy.container_profile != "development":
            raise DockerError("VS Code tunnels require the development container profile")
        try:
            exec_id = self.client.api.exec_create(
                container_id,
                cmd=["/usr/sbin/sshd", "-i", "-e", "-f", "/etc/ssh/sshd_config"],
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                user="0:0",
            )["Id"]
            sock = self.client.api.exec_start(exec_id, tty=False, socket=True)
            sock.settimeout(None)
        except DockerException as exc:
            raise DockerError(f"cannot open container SSH tunnel: {exc}") from exc
        return DockerTunnelSession(sock)

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

    def open_shell(
        self,
        container_id: str,
        cols: int,
        rows: int,
        command: list[str] | None = None,
    ) -> ShellSession:
        cmd = command or ["/bin/bash", "-l"]
        try:
            exec_id = self.client.api.exec_create(
                container_id,
                cmd=cmd,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
                workdir="/workspace",
            )["Id"]
            sock = self.client.api.exec_start(exec_id, tty=True, socket=True)
            self.client.api.exec_resize(exec_id, height=rows, width=cols)
            sock.settimeout(None)
        except DockerException as exc:
            raise DockerError(f"cannot open shell in {container_id}: {exc}") from exc
        return DockerShellSession(self.client, str(exec_id), sock)

    def _exec_detached(self, container_id: str, argv: list[str]) -> None:
        try:
            exec_id = self.client.api.exec_create(
                container_id,
                cmd=argv,
                stdin=False,
                stdout=True,
                stderr=True,
                tty=False,
            )["Id"]
            self.client.api.exec_start(exec_id, detach=True)
        except DockerException as exc:
            raise DockerError(f"docker exec failed: {exc}") from exc

    @staticmethod
    def _job_session(name: str) -> str:
        return f"udc-{name}"

    def start_job(self, container_id: str, name: str, command: list[str]) -> None:
        if not command:
            raise DockerError("job command is empty")
        session = self._job_session(name)
        logfile = f"/workspace/.udc-logs/{name}.log"
        code, _out, _err = self._exec_capture(container_id, ["tmux", "-V"])
        if code != 0:
            raise DockerError("tmux is not available in the container image")
        code, _out, _err = self._exec_capture(
            container_id,
            ["tmux", "has-session", "-t", session],
        )
        if code == 0:
            raise Conflict(f"job already exists: {name}")
        inner = (
            "mkdir -p /workspace/.udc-logs && "
            f"exec {shlex.join(command)} >> {shlex.quote(logfile)} 2>&1"
        )
        self._exec_detached(
            container_id,
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session,
                "-c",
                "/workspace",
                "/bin/bash",
                "-lc",
                inner,
            ],
        )

    def list_jobs(self, container_id: str) -> list[dict[str, str]]:
        code, output, _err = self._exec_capture(
            container_id,
            [
                "tmux",
                "list-sessions",
                "-F",
                "#{session_name}|#{session_created}|#{session_attached}",
            ],
        )
        if code != 0:
            return []
        jobs: list[dict[str, str]] = []
        for line in output.splitlines():
            parts = line.split("|")
            if len(parts) != 3:
                continue
            if not parts[0].startswith("udc-"):
                continue
            jobs.append(
                {
                    "name": parts[0][4:],
                    "session": parts[0],
                    "created": parts[1],
                    "attached": parts[2],
                }
            )
        return jobs

    def stop_job(self, container_id: str, name: str) -> None:
        code, _out, _err = self._exec_capture(
            container_id,
            ["tmux", "kill-session", "-t", self._job_session(name)],
        )
        if code != 0:
            raise UdcNotFound(f"job not found: {name}")

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
