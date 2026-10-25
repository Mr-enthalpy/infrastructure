from dataclasses import replace
from pathlib import Path

import pytest

from udc_resource.config import LabConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def monitor_config() -> LabConfig:
    return load_config(ROOT / "lab/resources.example.toml")


@pytest.fixture
def active_config(monitor_config: LabConfig, tmp_path: Path) -> LabConfig:
    return replace(
        monitor_config,
        server=replace(
            monitor_config.server,
            workspace_root=str(tmp_path / "workspaces"),
        ),
        policy=replace(
            monitor_config.policy,
            mode="active",
            deployment_allowed=True,
            file_transfer_enabled=True,
            min_free_gb=0,
            # Active mode is validated against the same mandatory gates the
            # broker enforces at startup and on runtime transitions, so the
            # test fixture must satisfy them. _broker injects passing audit
            # and quota runners by default; the dedicated gate tests override
            # them with failing/counting runners.
            max_workspace_gb=200,
            workspace_quota_required=True,
            legacy_audit_required=True,
            gpu_audit_required=True,
            container_profile="restricted",
            container_read_only=True,
            container_sudo=False,
            writable_layer_gb=0,
            vscode_enabled=False,
        ),
    )
