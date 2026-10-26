import os
import shutil
import struct
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from docker.errors import NotFound

from citespec_resource.broker import docker_driver
from citespec_resource.broker.docker_driver import DockerDriver, DockerTunnelSession
from citespec_resource.errors import DockerError
from citespec_resource.models import Allocation


class FakeContainer:
    def __init__(
        self,
        container_id: str = "container-1",
        name: str = "citespec-alice-test",
        status: str = "running",
        labels: dict[str, str] | None = None,
    ) -> None:
        self.id = container_id
        self.name = name
        self.status = status
        self.attrs = {
            "Config": {"Labels": labels or {}, "Image": "citespec-base:2026.10"},
            "State": {"Status": status},
        }
        self.stopped = False
        self.removed = False

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True

    def remove(self, force: bool = False) -> None:
        self.removed = True


class FakeContainers:
    def __init__(self) -> None:
        self.items: dict[str, FakeContainer] = {}
        self.run_kwargs: dict | None = None

    def run(self, image: str, **kwargs):
        self.run_kwargs = kwargs
        container = FakeContainer(
            container_id="new-container",
            name=kwargs.get("name", "citespec-test"),
            labels=kwargs.get("labels"),
        )
        self.items[container.id] = container
        return container

    def get(self, container_id: str) -> FakeContainer:
        if container_id not in self.items:
            raise NotFound("missing")
        return self.items[container_id]

    def list(self, all: bool = True) -> list[FakeContainer]:
        return list(self.items.values())


class FakeClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.info_data: dict = {}
        self.api = FakeAPI()

    def info(self) -> dict:
        return self.info_data


class FakeSocket:
    def __init__(self, data: bytes) -> None:
        self.data = bytearray(data)
        self.sent = bytearray()
        self.closed = False
        self.shutdown_called = False

    def recv(self, size: int) -> bytes:
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def shutdown(self, how: int) -> None:
        self.shutdown_called = True

    def close(self) -> None:
        self.closed = True


class StrictSocket(FakeSocket):
    """Fail if a read waits for a frame that has not arrived yet."""

    def recv(self, size: int) -> bytes:
        chunk = super().recv(size)
        if not chunk:
            raise AssertionError("tunnel read waited for more data")
        return chunk


class FakeAPI:
    def __init__(self) -> None:
        self.exec_create_kwargs: dict | None = None
        self.exec_start_kwargs: dict | None = None
        self.exit_code = 0

    def exec_create(self, container_id: str, **kwargs) -> dict:
        self.exec_create_kwargs = {"container_id": container_id, **kwargs}
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, **kwargs) -> bytes:
        self.exec_start_kwargs = {"exec_id": exec_id, **kwargs}
        return b""

    def exec_inspect(self, exec_id: str) -> dict:
        return {"ExitCode": self.exit_code}


def _allocation(tmp_path: Path) -> Allocation:
    return Allocation(
        id="abcdef1234567890",
        owner_uid=1000,
        owner_name="alice",
        gpus=("GPU-3", "GPU-5"),
        cpus=8.0,
        memory_gb=32,
        image="citespec-base:2026.10",
        workspace=str(tmp_path / "alice"),
        note="",
        starts_at=datetime(2026, 10, 22, tzinfo=UTC),
        expires_at=datetime(2026, 10, 23, tzinfo=UTC),
    )


def test_list_gpus_parses_nvidia_smi(monkeypatch, active_config):
    def fake_run(command, **kwargs):
        if command[1].startswith("--query-gpu="):
            stdout = (
                "0, GPU-0, NVIDIA GeForce RTX 4090, 49140, 1024, 5\n"
                "1, GPU-1, NVIDIA GeForce RTX 4090, 49140, 0, 0\n"
            )
        else:
            stdout = "GPU-0, 1234, 1024, python\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(docker_driver.subprocess, "run", fake_run)
    driver = DockerDriver(active_config)
    gpus = driver.list_gpus()
    assert [gpu.index for gpu in gpus] == [0, 1]
    assert gpus[0].used_mib == 1024
    assert gpus[0].compute_pids == (1234,)
    assert gpus[1].compute_pids == ()


def test_create_container_uses_expected_isolation(monkeypatch, active_config, tmp_path):
    driver = DockerDriver(active_config)
    fake_client = FakeClient()
    driver._client = fake_client
    allocation = _allocation(tmp_path)
    ref = driver.create_container(allocation)
    assert ref.id == "new-container"
    kwargs = fake_client.containers.run_kwargs
    assert kwargs is not None
    assert kwargs["command"] == ["sleep", "infinity"]
    assert kwargs["labels"] == {
        "citespec.managed": "true",
        "citespec.owner": "alice",
        "citespec.allocation": allocation.id,
        "citespec.gpus": "GPU-3,GPU-5",
        "citespec.profile": "restricted",
    }
    assert kwargs["nano_cpus"] == 8_000_000_000
    assert kwargs["mem_limit"] == "32g"
    assert kwargs["memswap_limit"] == "32g"
    assert kwargs["volumes"] == {allocation.workspace: {"bind": "/workspace", "mode": "rw"}}
    assert kwargs["working_dir"] == "/workspace"
    assert kwargs["privileged"] is False
    assert kwargs["network_mode"] == "bridge"
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["pids_limit"] == active_config.policy.pids_limit
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["read_only"] is True
    assert kwargs["tmpfs"]["/tmp"].startswith("rw,nosuid,nodev")
    assert kwargs["environment"]["HOME"] == "/workspace"
    assert kwargs["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert kwargs["environment"]["NVIDIA_VISIBLE_DEVICES"] == "GPU-3,GPU-5"
    assert kwargs["environment"]["NVIDIA_DRIVER_CAPABILITIES"] == "compute,utility"
    assert kwargs["user"]
    device_request = kwargs["device_requests"][0]
    assert device_request.device_ids == ["GPU-3", "GPU-5"]
    assert device_request.capabilities == [["gpu"]]


def test_development_profile_creates_disposable_admin_container(active_config, tmp_path):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
            container_home="",
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    driver._client = fake_client
    allocation = _allocation(tmp_path)
    driver.create_container(allocation)
    kwargs = fake_client.containers.run_kwargs
    assert kwargs is not None
    assert kwargs["read_only"] is False
    assert "security_opt" not in kwargs
    assert kwargs["cap_drop"] == ["ALL"]
    assert "SETUID" in kwargs["cap_add"]
    assert "SETGID" in kwargs["cap_add"]
    assert kwargs["storage_opt"] == {"size": "20G"}
    assert kwargs["labels"]["citespec.profile"] == "development"
    assert kwargs["environment"]["HOME"] == "/home/alice"
    # Default HOME lives in the quota-bounded writable layer, not a tmpfs.
    assert "/home/alice" not in kwargs["tmpfs"]
    assert kwargs["environment"]["NVIDIA_VISIBLE_DEVICES"] == "GPU-3,GPU-5"
    assert kwargs["volumes"] == {allocation.workspace: {"bind": "/workspace", "mode": "rw"}}


def test_development_profile_tmpfs_home_is_opt_in(active_config, tmp_path):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
            container_home="",
            container_home_tmpfs_gb=2,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    driver._client = fake_client
    driver.create_container(_allocation(tmp_path))
    kwargs = fake_client.containers.run_kwargs
    assert kwargs is not None
    assert "size=2g" in kwargs["tmpfs"]["/home/alice"]
    assert "mode=0700" in kwargs["tmpfs"]["/home/alice"]


def test_cpu_only_container_disables_nvidia_injection(active_config, tmp_path):
    driver = DockerDriver(active_config)
    fake_client = FakeClient()
    driver._client = fake_client
    allocation = replace(_allocation(tmp_path), gpus=())
    driver.create_container(allocation)
    kwargs = fake_client.containers.run_kwargs
    assert kwargs is not None
    assert kwargs["environment"]["NVIDIA_VISIBLE_DEVICES"] == "void"
    assert kwargs["environment"]["NVIDIA_DRIVER_CAPABILITIES"] == "void"
    assert "device_requests" not in kwargs


def test_storage_preflight_rejects_non_overlay_driver(monkeypatch, active_config):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    fake_client.info_data = {"Driver": "vfs", "DockerRootDir": "/var/lib/docker"}
    driver._client = fake_client
    monkeypatch.setattr(docker_driver, "os", SimpleNamespace(name="posix", path=os.path))
    passed, detail = driver.storage_preflight()
    assert passed is False
    assert "overlay2" in detail


def test_storage_preflight_accepts_overlay_on_xfs_prjquota(monkeypatch, active_config):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    fake_client.info_data = {"Driver": "overlay2", "DockerRootDir": "/var/lib/docker"}
    driver._client = fake_client
    Path(config.server.workspace_root).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(docker_driver, "os", SimpleNamespace(name="posix", path=os.path))
    monkeypatch.setattr(docker_driver.shutil, "which", lambda name: "/usr/bin/findmnt")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="xfs rw,relatime,prjquota,nosuid\n",
            stderr="",
        )

    monkeypatch.setattr(docker_driver.subprocess, "run", fake_run)
    passed, detail = driver.storage_preflight()
    assert passed is True
    assert "nosuid workspace" in detail


def test_storage_preflight_blocks_low_docker_disk(active_config, tmp_path, monkeypatch):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            min_free_docker_gb=10,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    fake_client.info_data = {"Driver": "overlay2", "DockerRootDir": str(tmp_path)}
    driver._client = fake_client
    usage = SimpleNamespace(free=5 * 1024**3, total=1024 * 1024**3)
    monkeypatch.setattr(docker_driver.shutil, "disk_usage", lambda path: usage)
    passed, detail = driver.storage_preflight()
    assert passed is False
    assert "5 GiB free" in detail
    assert str(tmp_path) in detail

    usage.free = 64 * 1024**3
    passed, detail = driver.storage_preflight()
    assert passed is True


def test_storage_preflight_requires_nosuid_workspace(monkeypatch, active_config):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    fake_client.info_data = {"Driver": "overlay2", "DockerRootDir": "/var/lib/docker"}
    driver._client = fake_client
    Path(config.server.workspace_root).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(docker_driver, "os", SimpleNamespace(name="posix", path=os.path))
    monkeypatch.setattr(docker_driver.shutil, "which", lambda name: "/usr/bin/findmnt")

    def fake_run(command, **kwargs):
        options = (
            "xfs rw,relatime,prjquota\n"
            if command[-1] == "/var/lib/docker"
            else "xfs rw,relatime\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=options, stderr="")

    monkeypatch.setattr(docker_driver.subprocess, "run", fake_run)
    passed, detail = driver.storage_preflight()
    assert passed is False
    assert "nosuid" in detail


def test_prepare_container_ssh_installs_keys_as_root(active_config):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    driver._client = fake_client
    driver.prepare_container_ssh(
        "container-1",
        1000,
        1000,
        "alice",
        ["ssh-ed25519 AAAATEST alice@example"],
        "/home/alice",
    )
    kwargs = fake_client.api.exec_create_kwargs
    assert kwargs is not None
    assert kwargs["user"] == "0:0"
    script = kwargs["cmd"][2]
    assert "ssh-ed25519 AAAATEST alice@example" in script
    assert "authorized_keys" in script
    assert "mkdir -p /run/sshd" in script
    assert "ssh-keygen -A" in script
    assert "chown 1000:1000" in script
    assert "awk -F: -v OFS=:" in script
    assert "$3 == uid" in script
    assert "conflicting system user" in script
    assert "flock 9" in script


def test_prepare_container_ssh_rejects_root_uid(active_config):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
        ),
    )
    driver = DockerDriver(config)
    driver._client = FakeClient()
    with pytest.raises(DockerError, match="non-root"):
        driver.prepare_container_ssh(
            "container-1",
            0,
            0,
            "root",
            ["ssh-ed25519 AAAATEST"],
            "/home/root",
        )


def test_prepare_container_ssh_requires_development_profile(active_config):
    driver = DockerDriver(active_config)
    driver._client = FakeClient()
    with pytest.raises(DockerError, match="development"):
        driver.prepare_container_ssh(
            "container-1",
            1000,
            1000,
            "alice",
            ["ssh-ed25519 AAAATEST"],
            "/home/alice",
        )


def test_docker_tunnel_session_demultiplexes_stdout_and_stderr():
    def frame(stream: int, payload: bytes) -> bytes:
        return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload

    sock = FakeSocket(frame(1, b"abc") + frame(2, b"error") + frame(1, b"def"))
    session = DockerTunnelSession(sock)
    assert session.read(3) == b"abc"
    assert session.read(10) == b"def"
    assert bytes(session._stderr) == b"error"
    assert session.read(10) == b""
    session.write(b"input")
    session.resize(80, 24)
    session.close()
    assert bytes(sock.sent) == b"input"
    assert sock.closed is True


def test_docker_tunnel_session_truncated_header_closes_stream():
    sock = FakeSocket(b"\x01\x00")
    session = DockerTunnelSession(sock)
    assert session.read(10) == b""


def test_docker_tunnel_session_returns_short_stdout_frame_without_waiting():
    def frame(stream: int, payload: bytes) -> bytes:
        return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload

    # Only one small SSH packet is available. A read must return it immediately
    # instead of waiting for the next frame, which cannot arrive until the peer
    # replies.
    sock = StrictSocket(frame(1, b"SSH-2.0-OpenSSH_9.6\r\n"))
    session = DockerTunnelSession(sock)
    assert session.read(65536) == b"SSH-2.0-OpenSSH_9.6\r\n"


def test_remove_container_stops_and_removes(active_config):
    driver = DockerDriver(active_config)
    fake_client = FakeClient()
    driver._client = fake_client
    container = FakeContainer()
    fake_client.containers.items[container.id] = container
    driver.remove_container(container.id)
    assert container.stopped is True
    assert container.removed is True
    driver.remove_container("missing")


def test_list_and_get_containers(active_config):
    driver = DockerDriver(active_config)
    fake_client = FakeClient()
    driver._client = fake_client
    fake_client.containers.items["container-1"] = FakeContainer(
        labels={"citespec.managed": "true", "citespec.allocation": "alloc-1"}
    )
    containers = driver.list_containers()
    assert len(containers) == 1
    assert containers[0].labels["citespec.allocation"] == "alloc-1"
    assert driver.get_container("container-1") is not None
    assert driver.get_container("missing") is None


def test_nvidia_smi_failure_is_docker_error(monkeypatch, active_config):
    def fake_run(command, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(docker_driver.subprocess, "run", fake_run)
    driver = DockerDriver(active_config)
    with pytest.raises(DockerError):
        driver.list_gpus()


def test_create_container_supports_root_and_userns(active_config, tmp_path):
    policy = replace(
        active_config.policy,
        container_user="root",
        userns_mode="host",
    )
    config = replace(active_config, policy=policy)
    driver = DockerDriver(config)
    fake_client = FakeClient()
    driver._client = fake_client
    driver.create_container(_allocation(tmp_path))
    kwargs = fake_client.containers.run_kwargs
    assert kwargs["user"] == "0:0"
    assert kwargs["userns_mode"] == "host"


def test_member_container_adds_gpu_group(monkeypatch, active_config, tmp_path):
    driver = DockerDriver(active_config)
    fake_client = FakeClient()
    driver._client = fake_client
    monkeypatch.setattr(driver, "_container_user", lambda allocation: "1000:1000")
    monkeypatch.setattr(driver, "_gpu_group_id", lambda: 12345)
    driver.create_container(_allocation(tmp_path))
    kwargs = fake_client.containers.run_kwargs
    assert kwargs["user"] == "1000:1000"
    assert kwargs["group_add"] == ["12345"]


def test_create_cpu_only_container_omits_device_requests(active_config, tmp_path):
    driver = DockerDriver(active_config)
    fake_client = FakeClient()
    driver._client = fake_client
    allocation = replace(_allocation(tmp_path), gpus=())
    driver.create_container(allocation)
    kwargs = fake_client.containers.run_kwargs
    assert kwargs is not None
    assert "device_requests" not in kwargs
    assert kwargs["labels"]["citespec.gpus"] == ""


class FakeShellSocket:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.timeout = None
        self.closed = False

    def recv(self, size: int) -> bytes:
        return b"shell bytes"

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def shutdown(self, how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeExecApi:
    def __init__(self) -> None:
        self.sock = FakeShellSocket()
        self.resizes: list[tuple[int | None, int | None]] = []
        self.create_kwargs: dict | None = None

    def exec_create(self, container: str, **kwargs):
        self.create_kwargs = kwargs
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, **kwargs):
        return self.sock

    def exec_resize(self, exec_id: str, *, height=None, width=None):
        self.resizes.append((height, width))


def test_open_shell_uses_docker_exec(active_config):
    driver = DockerDriver(active_config)
    client = FakeClient()
    client.api = FakeExecApi()
    driver._client = client
    session = driver.open_shell("container-1", 100, 30)
    assert session.read() == b"shell bytes"
    session.write(b"ls\n")
    session.resize(120, 40)
    session.close()
    assert client.api.create_kwargs["tty"] is True
    assert client.api.create_kwargs["cmd"] == ["/bin/bash", "-l"]
    assert "user" not in client.api.create_kwargs
    assert client.api.sock.sent == b"ls\n"
    assert client.api.resizes == [(30, 100), (40, 120)]
    assert client.api.sock.closed is True


def test_logs_reads_container_output(active_config):
    driver = DockerDriver(active_config)
    client = FakeClient()
    driver._client = client
    container = FakeContainer()
    container.logs = lambda **kwargs: b"line one\nline two\n"
    client.containers.items[container.id] = container
    assert driver.logs(container.id, 20) == "line one\nline two\n"


def test_container_info_prefers_device_requests_over_image_env(active_config):
    driver = DockerDriver(active_config)
    container = FakeContainer()
    container.attrs["HostConfig"] = {
        "DeviceRequests": [
            {"DeviceIDs": ["GPU-3"], "Count": 0, "Capabilities": [["gpu"]]},
        ],
        "Devices": [{"PathOnHost": "/dev/nvidia7", "PathInContainer": "/dev/nvidia7"}],
    }
    # CUDA base images inherit NVIDIA_VISIBLE_DEVICES=all; the authoritative
    # DeviceRequests must win instead of being unioned with the image env.
    container.attrs["Config"]["Env"] = ["NVIDIA_VISIBLE_DEVICES=all"]
    info = driver._container_info(container)
    assert set(info.gpu_refs) == {"GPU-3"}


def test_container_info_falls_back_to_devices_then_env(active_config):
    driver = DockerDriver(active_config)
    container = FakeContainer()
    container.attrs["HostConfig"] = {
        "Devices": [{"PathOnHost": "/dev/nvidia7", "PathInContainer": "/dev/nvidia7"}],
    }
    container.attrs["Config"]["Env"] = ["NVIDIA_VISIBLE_DEVICES=5"]
    info = driver._container_info(container)
    assert set(info.gpu_refs) == {"7"}

    container.attrs["HostConfig"] = {}
    info = driver._container_info(container)
    assert set(info.gpu_refs) == {"5"}


def test_container_info_ignores_void_env_without_devices(active_config):
    driver = DockerDriver(active_config)
    container = FakeContainer()
    container.attrs["HostConfig"] = {}
    container.attrs["Config"]["Env"] = [
        "NVIDIA_VISIBLE_DEVICES=void",
        "NVIDIA_DRIVER_CAPABILITIES=void",
    ]
    info = driver._container_info(container)
    assert info.gpu_refs == ()


def test_container_info_treats_count_minus_one_as_all(active_config):
    driver = DockerDriver(active_config)
    container = FakeContainer()
    container.attrs["HostConfig"] = {
        "DeviceRequests": [{"DeviceIDs": [], "Count": -1, "Capabilities": [["gpu"]]}],
    }
    container.attrs["Config"]["Env"] = []
    info = driver._container_info(container)
    assert info.gpu_refs == ("all",)


class FakeJobApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.exec_calls: list[tuple[str, dict]] = []
        self.next_id = 1
        self.outputs: dict[str, tuple[int, bytes]] = {}

    def exec_create(self, container: str, **kwargs):
        self.calls.append((container, kwargs))
        exec_id = f"exec-{self.next_id}"
        self.next_id += 1
        return {"Id": exec_id}

    def exec_start(self, exec_id: str, **kwargs):
        self.exec_calls.append((exec_id, kwargs))
        if kwargs.get("detach"):
            return None
        code, output = self.outputs.get(exec_id, (0, b""))
        self.outputs[exec_id] = (code, output)
        return output

    def exec_inspect(self, exec_id: str):
        code, _output = self.outputs.get(exec_id, (0, b""))
        return {"ExitCode": code}


def test_start_job_uses_tmux_and_workspace_log(active_config):
    driver = DockerDriver(active_config)
    client = FakeClient()
    api = FakeJobApi()
    client.api = api
    driver._client = client
    api.outputs["exec-1"] = (0, b"tmux 3.4\n")
    api.outputs["exec-2"] = (1, b"")
    driver.start_job("container-1", "train", ["python", "train.py", "--lr", "1e-4"])
    assert [call[1]["cmd"] for call in api.calls[:2]] == [
        ["tmux", "-V"],
        ["tmux", "has-session", "-t", "citespec-train"],
    ]
    detached = api.calls[2][1]
    assert detached["cmd"][:5] == ["tmux", "new-session", "-d", "-s", "citespec-train"]
    assert detached["cmd"][5] == "-c"
    assert "mkdir -p /workspace/.citespec-logs" in detached["cmd"][-1]
    assert ">> /workspace/.citespec-logs/train.log 2>&1" in detached["cmd"][-1]
    assert any(call[1].get("detach") for call in api.exec_calls)


def test_list_and_stop_jobs(active_config):
    driver = DockerDriver(active_config)
    client = FakeClient()
    api = FakeJobApi()
    client.api = api
    driver._client = client
    api.outputs["exec-1"] = (
        0,
        b"citespec-train|1700000000|0\nother|1700000001|1\n",
    )
    jobs = driver.list_jobs("container-1")
    assert jobs == [
        {
            "name": "train",
            "session": "citespec-train",
            "created": "1700000000",
            "attached": "0",
        }
    ]
    api.outputs["exec-2"] = (0, b"")
    driver.stop_job("container-1", "train")
    assert api.calls[1][1]["cmd"] == ["tmux", "kill-session", "-t", "citespec-train"]


def _working_bash() -> str | None:
    """Find a bash that actually runs.

    On Windows runners PATH may resolve to the WSL launcher stub
    (System32/bash.exe), which exits 1 without a distro. Fall back to the
    Git Bash install locations.
    """

    candidates = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    candidates.extend(
        [
            "C:\\Program Files\\Git\\bin\\bash.exe",
            "C:\\Program Files\\Git\\usr\\bin\\bash.exe",
        ]
    )
    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "exit 0"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def test_identity_script_installs_gpu_supplementary_group(active_config, tmp_path, monkeypatch):
    """SSH logins rebuild groups from the container /etc/group, so the member
    needs a supplementary group with the host citespec-gpu GID inside the container
    as well — docker's group_add only covers the docker-exec path."""
    bash = _working_bash()
    if bash is None:
        pytest.skip("a working bash is required to syntax-check the identity script")
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
        ),
    )
    driver = DockerDriver(config)
    driver._gpu_group_id = lambda: 64000
    fake_client = FakeClient()
    driver._client = fake_client
    monkeypatch.setattr(docker_driver, "os", SimpleNamespace(name="posix", path=os.path))
    driver.initialize_container_identity("container-1", _allocation(tmp_path))
    script = fake_client.api.exec_create_kwargs["cmd"][2]
    assert "citespec-gpu:x:64000:alice" in script
    assert 'grep -q "^[^:]*:[^:]*:64000:" /etc/group' in script
    # Write the script to an LF file instead of piping via stdin: Windows
    # text-mode pipes translate newlines and some bash builds choke on CRLF.
    script_file = tmp_path / "identity.sh"
    script_file.write_text(script, encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [bash, "-n", str(script_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_identity_script_without_gpu_group(active_config, tmp_path, monkeypatch):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    driver._client = fake_client
    monkeypatch.setattr(docker_driver, "os", SimpleNamespace(name="posix", path=os.path))
    driver.initialize_container_identity("container-1", _allocation(tmp_path))
    script = fake_client.api.exec_create_kwargs["cmd"][2]
    assert "citespec-gpu" not in script


def test_prepare_container_ssh_merges_instead_of_overwriting(active_config):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=0,
            docker_storage_budget_gb=0,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    driver._client = fake_client
    driver.prepare_container_ssh(
        "container-1",
        1000,
        1000,
        "alice",
        ["ssh-ed25519 AAAA-new-key alice"],
        "/home/alice",
    )
    script = fake_client.api.exec_create_kwargs["cmd"][2]
    # Existing keys are concatenated with the new ones, deduplicated, and the
    # file is capped instead of being overwritten per connection.
    assert 'cat "$home/.ssh/authorized_keys"' in script
    assert "awk '!seen[$0]++'" in script
    assert "tail -n 40" in script
    assert "authorized_keys.citespec" in script
    assert "ssh-ed25519 AAAA-new-key alice" in script


def test_storage_preflight_rejects_budget_larger_than_disk(active_config, tmp_path, monkeypatch):
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            min_free_docker_gb=10,
            docker_storage_budget_gb=500,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    fake_client.info_data = {"Driver": "overlay2", "DockerRootDir": str(tmp_path)}
    driver._client = fake_client
    usage = SimpleNamespace(free=64 * 1024**3, total=100 * 1024**3)
    monkeypatch.setattr(docker_driver.shutil, "disk_usage", lambda path: usage)
    passed, detail = driver.storage_preflight()
    assert passed is False
    assert "exceeds the 100 GiB filesystem" in detail

    usage.total = 1024 * 1024**3
    passed, detail = driver.storage_preflight()
    assert passed is True


def test_storage_preflight_rejects_entitlements_beyond_free_space(
    active_config,
    tmp_path,
    monkeypatch,
):
    """60 GiB free, 20 GiB reserve, two 40 GiB entitlements: the second must
    not be admitted even though the budget fits the filesystem's capacity."""
    config = replace(
        active_config,
        policy=replace(
            active_config.policy,
            min_free_docker_gb=20,
            docker_storage_budget_gb=500,
        ),
    )
    driver = DockerDriver(config)
    fake_client = FakeClient()
    fake_client.info_data = {"Driver": "overlay2", "DockerRootDir": str(tmp_path)}
    driver._client = fake_client
    usage = SimpleNamespace(free=60 * 1024**3, total=1024 * 1024**3)
    monkeypatch.setattr(docker_driver.shutil, "disk_usage", lambda path: usage)

    blocked = driver.storage_preflight(
        writable_layer_entitlements_bytes=80 * 1024**3,
    )
    assert blocked[0] is False
    assert "could still grow by 80 GiB" in blocked[1]

    allowed = driver.storage_preflight(
        writable_layer_entitlements_bytes=40 * 1024**3,
    )
    assert allowed[0] is True
