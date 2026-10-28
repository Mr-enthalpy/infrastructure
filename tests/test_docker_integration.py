import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from citespec_resource.broker.docker_driver import DockerDriver
from citespec_resource.models import Allocation

pytestmark = pytest.mark.skipif(
    os.environ.get("CITESPEC_RESOURCE_RUN_DOCKER_TESTS") != "1",
    reason="set CITESPEC_RESOURCE_RUN_DOCKER_TESTS=1 to run real Docker integration tests",
)


def test_gpu_container_can_query_nvidia_smi(active_config):
    """C4-3: the shim wrapper must resolve inside a GPU container."""
    if os.environ.get("CITESPEC_RESOURCE_RUN_GPU_TESTS") != "1":
        pytest.skip("set CITESPEC_RESOURCE_RUN_GPU_TESTS=1 on a GPU host")
    driver = DockerDriver(active_config)
    gpus = driver.list_gpus()
    if not gpus:
        pytest.skip("no NVIDIA GPU is visible")
    workspace = Path(active_config.server.workspace_root) / "gpu-integration"
    workspace.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    allocation = Allocation(
        id=uuid.uuid4().hex,
        owner_uid=0,
        owner_name="integration",
        gpus=(gpus[0].uuid,),
        cpus=1.0,
        memory_gb=2,
        image=active_config.image.base,
        workspace=str(workspace),
        note="gpu integration",
        starts_at=now,
        expires_at=now + timedelta(hours=1),
    )
    driver.client.images.pull(allocation.image)
    ref = driver.create_container(allocation)
    try:
        container = driver.client.containers.get(ref.id)
        listed = container.exec_run(["nvidia-smi", "-L"])
        assert listed.exit_code == 0, listed.output
        assert gpus[0].uuid.encode() in listed.output
    finally:
        driver.remove_container(ref.id)


def test_cpu_only_container_lifecycle(active_config):
    workspace = Path(active_config.server.workspace_root) / "integration"
    workspace.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    allocation = Allocation(
        id=uuid.uuid4().hex,
        owner_uid=0,
        owner_name="integration",
        gpus=(),
        cpus=1.0,
        memory_gb=1,
        image="python:3.12-slim",
        workspace=str(workspace),
        note="integration",
        starts_at=now,
        expires_at=now + timedelta(hours=1),
    )
    driver = DockerDriver(active_config)
    driver.client.images.pull(allocation.image)
    ref = driver.create_container(allocation)
    try:
        container = driver.get_container(ref.id)
        assert container is not None
        assert container.status == "running"
        assert container.labels["citespec.allocation"] == allocation.id
    finally:
        driver.remove_container(ref.id)
    assert driver.get_container(ref.id) is None
