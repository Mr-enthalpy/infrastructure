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
            max_workspace_gb=0,
            workspace_quota_required=False,
            # Most broker tests focus on lifecycle behavior. The dedicated
            # audit-gate tests re-enable this and inject a runner.
            legacy_audit_required=False,
            gpu_audit_required=False,
            container_profile="restricted",
            container_read_only=True,
            container_sudo=False,
            writable_layer_gb=0,
            vscode_enabled=False,
        ),
    )
