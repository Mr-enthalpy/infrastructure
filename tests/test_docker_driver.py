import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from docker.errors import NotFound

from udc_resource.broker import docker_driver
from udc_resource.broker.docker_driver import DockerDriver
from udc_resource.errors import DockerError
from udc_resource.models import Allocation


class FakeContainer:
    def __init__(
        self,
        container_id: str = "container-1",
        name: str = "udc-alice-test",
        status: str = "running",
        labels: dict[str, str] | None = None,
    ) -> None:
        self.id = container_id
        self.name = name
        self.status = status
        self.attrs = {
            "Config": {"Labels": labels or {}, "Image": "udc-base:2026.10"},
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
            name=kwargs.get("name", "udc-test"),
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


def _allocation(tmp_path: Path) -> Allocation:
    return Allocation(
        id="abcdef1234567890",
        owner_uid=1000,
        owner_name="alice",
        gpus=("GPU-3", "GPU-5"),
        cpus=8.0,
        memory_gb=32,
        image="udc-base:2026.10",
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
        "udc.managed": "true",
        "udc.owner": "alice",
        "udc.allocation": allocation.id,
        "udc.gpus": "GPU-3,GPU-5",
    }
    assert kwargs["nano_cpus"] == 8_000_000_000
    assert kwargs["mem_limit"] == "32g"
    assert kwargs["memswap_limit"] == "32g"
    assert kwargs["volumes"] == {allocation.workspace: {"bind": "/workspace", "mode": "rw"}}
    assert kwargs["working_dir"] == "/workspace"
    assert kwargs["privileged"] is False
    assert kwargs["network_mode"] == "bridge"
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["user"] == "0:0"
    device_request = kwargs["device_requests"][0]
    assert device_request.device_ids == ["GPU-3", "GPU-5"]
    assert device_request.capabilities == [["gpu"]]


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
        labels={"udc.managed": "true", "udc.allocation": "alloc-1"}
    )
    containers = driver.list_containers()
    assert len(containers) == 1
    assert containers[0].labels["udc.allocation"] == "alloc-1"
    assert driver.get_container("container-1") is not None
    assert driver.get_container("missing") is None


def test_nvidia_smi_failure_is_docker_error(monkeypatch, active_config):
    def fake_run(command, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(docker_driver.subprocess, "run", fake_run)
    driver = DockerDriver(active_config)
    with pytest.raises(DockerError):
        driver.list_gpus()


def test_create_cpu_only_container_omits_device_requests(active_config, tmp_path):
    driver = DockerDriver(active_config)
    fake_client = FakeClient()
    driver._client = fake_client
    allocation = replace(_allocation(tmp_path), gpus=())
    driver.create_container(allocation)
    kwargs = fake_client.containers.run_kwargs
    assert kwargs is not None
    assert "device_requests" not in kwargs
    assert kwargs["labels"]["udc.gpus"] == ""


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


def test_container_info_extracts_gpu_refs(active_config):
    driver = DockerDriver(active_config)
    container = FakeContainer()
    container.attrs["HostConfig"] = {
        "DeviceRequests": [
            {"DeviceIDs": ["GPU-3"], "Count": 0, "Capabilities": [["gpu"]]},
        ],
        "Devices": [{"PathOnHost": "/dev/nvidia7", "PathInContainer": "/dev/nvidia7"}],
    }
    container.attrs["Config"]["Env"] = ["NVIDIA_VISIBLE_DEVICES=5"]
    info = driver._container_info(container)
    assert set(info.gpu_refs) == {"GPU-3", "7", "5"}


def test_container_info_treats_count_minus_one_as_all(active_config):
    driver = DockerDriver(active_config)
    container = FakeContainer()
    container.attrs["HostConfig"] = {
        "DeviceRequests": [{"DeviceIDs": [], "Count": -1, "Capabilities": [["gpu"]]}],
    }
    container.attrs["Config"]["Env"] = []
    info = driver._container_info(container)
    assert info.gpu_refs == ("all",)
