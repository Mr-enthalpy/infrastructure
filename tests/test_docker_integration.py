import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from udc_resource.broker.docker_driver import DockerDriver
from udc_resource.models import Allocation

pytestmark = pytest.mark.skipif(
    os.environ.get("UDC_RUN_DOCKER_TESTS") != "1",
    reason="set UDC_RUN_DOCKER_TESTS=1 to run real Docker integration tests",
)


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
        assert container.labels["udc.allocation"] == allocation.id
    finally:
        driver.remove_container(ref.id)
    assert driver.get_container(ref.id) is None
