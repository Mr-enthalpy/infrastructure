"""Configuration loading for the lab resource broker and client."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ServerConfig:
    name: str
    ssh_alias: str
    address: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    known_hosts: str = ""
    data_root: str = "/data1"
    workspace_root: str = "/data1/udc-users"
    broker_socket: str = "/run/udc-resource/broker.sock"
    state_db: str = "/var/lib/udc-resource/state.db"


@dataclass(frozen=True)
class HardwareConfig:
    gpu_count: int
    gpu_model: str
    gpu_memory_mib: int
    cpu_logical: int
    ram_gib: int
    docker_version: str = ""
    python_version: str = ""


@dataclass(frozen=True)
class PolicyConfig:
    default_hours: int = 12
    max_hours: int = 36
    mode: str = "monitor"
    deployment_allowed: bool = False
    max_cpus: float = 32.0
    max_memory_gb: int = 128
    reserve_cpus: float = 8.0
    reserve_memory_gb: int = 32
    max_gpus_per_user: int = 4
    max_active_allocations_per_user: int = 4


@dataclass(frozen=True)
class GroupsConfig:
    members: str = "udc-members"
    cli: str = "udc-cli"
    admins: str = "udc-admins"
    gpu: str = "udc-gpu"


@dataclass(frozen=True)
class ImageConfig:
    base: str = "udc-base:2026.10"


@dataclass(frozen=True)
class DeploymentConfig:
    reason: str = ""
    monitor_only_until: str = ""


@dataclass(frozen=True)
class GUIConfig:
    admin_ssh: str = ""
    connect_timeout: int = 15


@dataclass(frozen=True)
class LabConfig:
    server: ServerConfig
    hardware: HardwareConfig
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    groups: GroupsConfig = field(default_factory=GroupsConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)
    gui: GUIConfig = field(default_factory=GUIConfig)


def _require(data: dict[str, Any], key: str, section: str) -> Any:
    if key not in data:
        raise ValueError(f"missing [{section}] {key}")
    return data[key]


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"missing [{name}] section")
    return value


def load_config(path: str | Path) -> LabConfig:
    path = Path(path)
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    server_raw = _section(data, "server")
    hardware_raw = _section(data, "hardware")
    policy_raw = data.get("policy", {})
    groups_raw = data.get("groups", {})
    image_raw = data.get("image", {})
    deployment_raw = data.get("deployment", {})
    gui_raw = data.get("gui", {})

    server = ServerConfig(
        name=str(_require(server_raw, "name", "server")),
        ssh_alias=str(_require(server_raw, "ssh_alias", "server")),
        address=str(server_raw.get("address", "")),
        ssh_port=int(server_raw.get("ssh_port", 22)),
        ssh_user=str(server_raw.get("ssh_user", "")),
        known_hosts=str(server_raw.get("known_hosts", "")),
        data_root=str(server_raw.get("data_root", "/data1")),
        workspace_root=str(server_raw.get("workspace_root", "/data1/udc-users")),
        broker_socket=str(server_raw.get("broker_socket", "/run/udc-resource/broker.sock")),
        state_db=str(server_raw.get("state_db", "/var/lib/udc-resource/state.db")),
    )
    hardware = HardwareConfig(
        gpu_count=int(_require(hardware_raw, "gpu_count", "hardware")),
        gpu_model=str(_require(hardware_raw, "gpu_model", "hardware")),
        gpu_memory_mib=int(_require(hardware_raw, "gpu_memory_mib", "hardware")),
        cpu_logical=int(_require(hardware_raw, "cpu_logical", "hardware")),
        ram_gib=int(_require(hardware_raw, "ram_gib", "hardware")),
        docker_version=str(hardware_raw.get("docker_version", "")),
        python_version=str(hardware_raw.get("python_version", "")),
    )
    if hardware.gpu_count <= 0:
        raise ValueError("hardware.gpu_count must be positive")
    if hardware.cpu_logical <= 0:
        raise ValueError("hardware.cpu_logical must be positive")
    if hardware.ram_gib <= 0:
        raise ValueError("hardware.ram_gib must be positive")
    policy = PolicyConfig(
        default_hours=int(policy_raw.get("default_hours", 12)),
        max_hours=int(policy_raw.get("max_hours", 36)),
        mode=str(policy_raw.get("mode", "monitor")),
        deployment_allowed=bool(policy_raw.get("deployment_allowed", False)),
        max_cpus=float(policy_raw.get("max_cpus", 32.0)),
        max_memory_gb=int(policy_raw.get("max_memory_gb", 128)),
        reserve_cpus=float(policy_raw.get("reserve_cpus", 8.0)),
        reserve_memory_gb=int(policy_raw.get("reserve_memory_gb", 32)),
        max_gpus_per_user=int(policy_raw.get("max_gpus_per_user", 4)),
        max_active_allocations_per_user=int(policy_raw.get("max_active_allocations_per_user", 4)),
    )
    if not 1 <= policy.default_hours <= policy.max_hours:
        raise ValueError("default_hours must be between 1 and max_hours")
    if policy.max_hours > 36:
        raise ValueError("max_hours must not exceed 36")
    if policy.mode not in {"monitor", "maintenance", "active"}:
        raise ValueError("policy.mode must be monitor, maintenance, or active")
    if policy.mode == "active" and not policy.deployment_allowed:
        raise ValueError("policy.mode=active requires deployment_allowed=true")
    if policy.max_cpus <= 0 or policy.max_memory_gb <= 0:
        raise ValueError("policy.max_cpus and policy.max_memory_gb must be positive")
    if policy.reserve_cpus < 0 or policy.reserve_memory_gb < 0:
        raise ValueError("policy reserves must not be negative")
    if policy.max_gpus_per_user <= 0 or policy.max_active_allocations_per_user <= 0:
        raise ValueError("policy per-user limits must be positive")
    if policy.max_cpus + policy.reserve_cpus > hardware.cpu_logical:
        raise ValueError("max_cpus + reserve_cpus exceeds hardware.cpu_logical")
    if policy.max_memory_gb + policy.reserve_memory_gb > hardware.ram_gib:
        raise ValueError("max_memory_gb + reserve_memory_gb exceeds hardware.ram_gib")
    groups = GroupsConfig(
        members=str(groups_raw.get("members", "udc-members")),
        cli=str(groups_raw.get("cli", "udc-cli")),
        admins=str(groups_raw.get("admins", "udc-admins")),
        gpu=str(groups_raw.get("gpu", "udc-gpu")),
    )
    image = ImageConfig(base=str(image_raw.get("base", "udc-base:2026.10")))
    deployment = DeploymentConfig(
        reason=str(deployment_raw.get("reason", "")),
        monitor_only_until=str(deployment_raw.get("monitor_only_until", "")),
    )
    gui = GUIConfig(
        admin_ssh=str(gui_raw.get("admin_ssh", "")),
        connect_timeout=int(gui_raw.get("connect_timeout", 15)),
    )
    if gui.connect_timeout <= 0:
        raise ValueError("gui.connect_timeout must be positive")
    return LabConfig(
        server=server,
        hardware=hardware,
        policy=policy,
        groups=groups,
        image=image,
        deployment=deployment,
        gui=gui,
    )
