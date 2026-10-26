"""Configuration loading for the lab resource broker and client."""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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
    workspace_root: str = "/data1/citespec-users"
    broker_socket: str = "/run/citespec-resource/broker.sock"
    state_db: str = "/var/lib/citespec-resource/state.db"
    members_file: str = "/etc/citespec-resource/members.toml"


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
    global_max_cpus: float = 120.0
    global_max_memory_gb: int = 219
    max_cpus_per_allocation: float = 32.0
    max_memory_gb_per_allocation: int = 128
    max_cpus_per_user: float = 64.0
    max_memory_gb_per_user: int = 192
    reserve_cpus: float = 8.0
    reserve_memory_gb: int = 32
    max_gpus_per_user: int = 4
    max_active_allocations_per_user: int = 4
    file_transfer_enabled: bool = False
    max_upload_bytes: int = 20 * 1024 * 1024 * 1024
    min_free_gb: int = 0
    max_workspace_gb: int = 0
    workspace_quota_required: bool = False
    pids_limit: int = 4096
    container_user: str = "member"
    container_read_only: bool = True
    container_profile: str = "restricted"
    container_sudo: bool = False
    writable_layer_gb: int = 0
    container_home: str = ""
    container_home_tmpfs_gb: int = 0
    min_free_docker_gb: int = 20
    docker_storage_budget_gb: int = 0
    userns_mode: str = ""
    legacy_audit_required: bool = True
    legacy_audit_script: str = "/usr/local/libexec/citespec-resource/audit_legacy.sh"
    legacy_audit_timeout: int = 120
    gpu_audit_required: bool = False
    gpu_audit_script: str = "/usr/local/libexec/citespec-resource/verify_gpu_permissions.sh"
    gpu_audit_timeout: int = 180
    gpu_audit_interval: int = 300
    allow_root_ssh_keys: bool = False
    queue_enabled: bool = True
    max_queue_length: int = 100
    max_queued_allocations_per_user: int = 4
    max_queue_hours: int = 24
    queue_backfill_enabled: bool = True
    queue_backfill_reserve_minutes: int = 30
    vscode_enabled: bool = False
    vscode_max_tunnels: int = 8
    vscode_identity_file: str = ""
    # Hard caps for long-lived blocking shell/tunnel streams. The broker keeps
    # their I/O on dedicated bounded executors so idle terminals cannot exhaust
    # the default asyncio thread pool used by release/expiry/audit work.
    max_shells_per_user: int = 2
    max_shells_global: int = 16
    max_tunnels_per_user: int = 8
    max_tunnels_global: int = 16
    # Authoritative membership gating (goal §6): when true, normal resource
    # operations require an active member record. Admin identities keep the
    # trusted admin-group path and are exempt.
    membership_required: bool = True


@dataclass(frozen=True)
class GroupsConfig:
    members: str = "citespec-members"
    cli: str = "citespec-cli"
    admins: str = "citespec-admins"
    gpu: str = "citespec-gpu"


@dataclass(frozen=True)
class ImageConfig:
    base: str = "citespec-base:2026.10"
    allowlist: tuple[str, ...] = ()

    def image_allowed(self, image: str) -> bool:
        return image == self.base or image in self.allowlist


@dataclass(frozen=True)
class DeploymentConfig:
    reason: str = ""
    monitor_only_until: str = ""


@dataclass(frozen=True)
class GUIConfig:
    admin_ssh: str = ""
    connect_timeout: int = 15


@dataclass(frozen=True)
class OnboardingConfig:
    """Member-facing onboarding information (goal §4.2). Public data only.

    The URLs are published by the lab through its own channels; unset values
    mean the deployment preflight warns that the entry points are missing.
    """

    guide_url: str = ""
    apply_url: str = ""
    support_label: str = "实验室计算资源管理员"
    support_contact: str = ""
    legacy_login_notice_enabled: bool = True
    intake_db: str = "/var/lib/citespec-resource-intake/applications.db"
    provisioning_spool: str = "/var/lib/citespec-resource/provision"
    presets: tuple[tuple[str, int, float, int, int], ...] = ()


@dataclass(frozen=True)
class LabConfig:
    server: ServerConfig
    hardware: HardwareConfig
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    groups: GroupsConfig = field(default_factory=GroupsConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)
    gui: GUIConfig = field(default_factory=GUIConfig)
    onboarding: OnboardingConfig = field(default_factory=OnboardingConfig)


def validate_active_mode_policy(policy: PolicyConfig) -> None:
    """Mandatory, non-skippable preconditions for active mode.

    Config loading, broker startup (when a persisted active mode is restored),
    and runtime monitor->active transitions must all enforce the SAME rule, so
    a laxer monitor-mode configuration cannot be switched into allocating
    state without the designed safety gates.
    """

    if not policy.deployment_allowed:
        raise ValueError("active mode requires deployment_allowed=true")
    if not policy.legacy_audit_required:
        raise ValueError("active mode requires legacy_audit_required=true")
    if not policy.gpu_audit_required:
        raise ValueError("active mode requires gpu_audit_required=true")
    if not policy.workspace_quota_required:
        raise ValueError("active mode requires workspace_quota_required=true")
    if policy.max_workspace_gb <= 0:
        raise ValueError("active mode requires max_workspace_gb > 0")
    if policy.container_profile == "development":
        if policy.min_free_docker_gb <= 0:
            raise ValueError(
                "active mode with the development profile requires min_free_docker_gb > 0"
            )
        if policy.docker_storage_budget_gb <= 0:
            raise ValueError(
                "active mode with the development profile requires docker_storage_budget_gb > 0"
            )


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
    onboarding_raw = data.get("onboarding", {})

    server = ServerConfig(
        name=str(_require(server_raw, "name", "server")),
        ssh_alias=str(_require(server_raw, "ssh_alias", "server")),
        address=str(server_raw.get("address", "")),
        ssh_port=int(server_raw.get("ssh_port", 22)),
        ssh_user=str(server_raw.get("ssh_user", "")),
        known_hosts=str(server_raw.get("known_hosts", "")),
        data_root=str(server_raw.get("data_root", "/data1")),
        workspace_root=str(server_raw.get("workspace_root", "/data1/citespec-users")),
        broker_socket=str(server_raw.get("broker_socket", "/run/citespec-resource/broker.sock")),
        state_db=str(server_raw.get("state_db", "/var/lib/citespec-resource/state.db")),
        members_file=str(server_raw.get("members_file", "/etc/citespec-resource/members.toml")),
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,254}", server.ssh_alias):
        raise ValueError("server.ssh_alias contains unsupported characters")
    if server.ssh_user and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", server.ssh_user):
        raise ValueError("server.ssh_user contains unsupported characters")
    workspace_path = PurePosixPath(server.workspace_root)
    if not workspace_path.is_absolute() or server.workspace_root.startswith("//"):
        raise ValueError("server.workspace_root must be an absolute path")
    if ".." in workspace_path.parts:
        raise ValueError("server.workspace_root must not contain '..'")
    if str(workspace_path) == "/":
        raise ValueError("server.workspace_root must not be the filesystem root")
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
    reserve_cpus = float(policy_raw.get("reserve_cpus", 8.0))
    reserve_memory_gb = int(policy_raw.get("reserve_memory_gb", 32))
    # max_cpus/max_memory_gb are accepted as legacy names for the per-allocation
    # cap. They no longer act as the global capacity.
    max_cpus_per_allocation = float(
        policy_raw.get("max_cpus_per_allocation", policy_raw.get("max_cpus", 32.0))
    )
    max_memory_gb_per_allocation = int(
        policy_raw.get("max_memory_gb_per_allocation", policy_raw.get("max_memory_gb", 128))
    )
    global_max_cpus = float(policy_raw.get("global_max_cpus", hardware.cpu_logical - reserve_cpus))
    global_max_memory_gb = int(
        policy_raw.get("global_max_memory_gb", hardware.ram_gib - reserve_memory_gb)
    )
    policy = PolicyConfig(
        membership_required=bool(policy_raw.get("membership_required", True)),
        default_hours=int(policy_raw.get("default_hours", 12)),
        max_hours=int(policy_raw.get("max_hours", 36)),
        mode=str(policy_raw.get("mode", "monitor")),
        deployment_allowed=bool(policy_raw.get("deployment_allowed", False)),
        global_max_cpus=global_max_cpus,
        global_max_memory_gb=global_max_memory_gb,
        max_cpus_per_allocation=max_cpus_per_allocation,
        max_memory_gb_per_allocation=max_memory_gb_per_allocation,
        max_cpus_per_user=float(policy_raw.get("max_cpus_per_user", global_max_cpus)),
        max_memory_gb_per_user=int(policy_raw.get("max_memory_gb_per_user", global_max_memory_gb)),
        reserve_cpus=reserve_cpus,
        reserve_memory_gb=reserve_memory_gb,
        max_gpus_per_user=int(policy_raw.get("max_gpus_per_user", 4)),
        max_active_allocations_per_user=int(policy_raw.get("max_active_allocations_per_user", 4)),
        file_transfer_enabled=bool(policy_raw.get("file_transfer_enabled", False)),
        max_upload_bytes=int(policy_raw.get("max_upload_bytes", 20 * 1024 * 1024 * 1024)),
        min_free_gb=int(policy_raw.get("min_free_gb", 0)),
        max_workspace_gb=int(policy_raw.get("max_workspace_gb", 0)),
        workspace_quota_required=bool(policy_raw.get("workspace_quota_required", False)),
        pids_limit=int(policy_raw.get("pids_limit", 4096)),
        container_user=str(policy_raw.get("container_user", "member")),
        container_read_only=bool(policy_raw.get("container_read_only", True)),
        container_profile=str(policy_raw.get("container_profile", "restricted")),
        container_sudo=bool(policy_raw.get("container_sudo", False)),
        writable_layer_gb=int(policy_raw.get("writable_layer_gb", 0)),
        container_home=str(policy_raw.get("container_home", "")),
        container_home_tmpfs_gb=int(policy_raw.get("container_home_tmpfs_gb", 0)),
        min_free_docker_gb=int(policy_raw.get("min_free_docker_gb", 20)),
        docker_storage_budget_gb=int(policy_raw.get("docker_storage_budget_gb", 0)),
        userns_mode=str(policy_raw.get("userns_mode", "")),
        legacy_audit_required=bool(policy_raw.get("legacy_audit_required", True)),
        legacy_audit_script=str(
            policy_raw.get(
                "legacy_audit_script",
                "/usr/local/libexec/citespec-resource/audit_legacy.sh",
            )
        ),
        legacy_audit_timeout=int(policy_raw.get("legacy_audit_timeout", 120)),
        gpu_audit_required=bool(policy_raw.get("gpu_audit_required", False)),
        gpu_audit_script=str(
            policy_raw.get(
                "gpu_audit_script",
                "/usr/local/libexec/citespec-resource/verify_gpu_permissions.sh",
            )
        ),
        gpu_audit_timeout=int(policy_raw.get("gpu_audit_timeout", 180)),
        gpu_audit_interval=int(policy_raw.get("gpu_audit_interval", 300)),
        allow_root_ssh_keys=bool(policy_raw.get("allow_root_ssh_keys", False)),
        queue_enabled=bool(policy_raw.get("queue_enabled", True)),
        max_queue_length=int(policy_raw.get("max_queue_length", 100)),
        max_queued_allocations_per_user=int(policy_raw.get("max_queued_allocations_per_user", 4)),
        max_queue_hours=int(policy_raw.get("max_queue_hours", 24)),
        queue_backfill_enabled=bool(policy_raw.get("queue_backfill_enabled", True)),
        queue_backfill_reserve_minutes=int(policy_raw.get("queue_backfill_reserve_minutes", 30)),
        vscode_enabled=bool(policy_raw.get("vscode_enabled", False)),
        vscode_max_tunnels=int(policy_raw.get("vscode_max_tunnels", 8)),
        vscode_identity_file=str(policy_raw.get("vscode_identity_file", "")),
        max_shells_per_user=int(policy_raw.get("max_shells_per_user", 2)),
        max_shells_global=int(policy_raw.get("max_shells_global", 16)),
        max_tunnels_per_user=int(policy_raw.get("max_tunnels_per_user", 8)),
        max_tunnels_global=int(policy_raw.get("max_tunnels_global", 16)),
    )
    if not 1 <= policy.default_hours <= policy.max_hours:
        raise ValueError("default_hours must be between 1 and max_hours")
    if policy.max_hours > 36:
        raise ValueError("max_hours must not exceed 36")
    if policy.mode not in {"monitor", "maintenance", "active"}:
        raise ValueError("policy.mode must be monitor, maintenance, or active")
    if policy.mode == "active":
        # The same rule the broker enforces at startup and on runtime mode
        # transitions; see validate_active_mode_policy.
        validate_active_mode_policy(policy)
    if policy.global_max_cpus <= 0 or policy.global_max_memory_gb <= 0:
        raise ValueError("global CPU and memory budgets must be positive")
    if policy.max_cpus_per_allocation <= 0 or policy.max_memory_gb_per_allocation <= 0:
        raise ValueError("per-allocation CPU and memory limits must be positive")
    if policy.max_cpus_per_user <= 0 or policy.max_memory_gb_per_user <= 0:
        raise ValueError("per-user CPU and memory limits must be positive")
    if policy.max_cpus_per_allocation > policy.global_max_cpus:
        raise ValueError("max_cpus_per_allocation exceeds global_max_cpus")
    if policy.max_memory_gb_per_allocation > policy.global_max_memory_gb:
        raise ValueError("max_memory_gb_per_allocation exceeds global_max_memory_gb")
    if policy.max_cpus_per_user > policy.global_max_cpus:
        raise ValueError("max_cpus_per_user exceeds global_max_cpus")
    if policy.max_memory_gb_per_user > policy.global_max_memory_gb:
        raise ValueError("max_memory_gb_per_user exceeds global_max_memory_gb")
    if policy.max_cpus_per_user < policy.max_cpus_per_allocation:
        raise ValueError("max_cpus_per_user is below max_cpus_per_allocation")
    if policy.max_memory_gb_per_user < policy.max_memory_gb_per_allocation:
        raise ValueError("max_memory_gb_per_user is below max_memory_gb_per_allocation")
    if policy.reserve_cpus < 0 or policy.reserve_memory_gb < 0:
        raise ValueError("policy reserves must not be negative")
    if policy.max_gpus_per_user <= 0 or policy.max_active_allocations_per_user <= 0:
        raise ValueError("policy per-user limits must be positive")
    if policy.max_upload_bytes <= 0:
        raise ValueError("policy.max_upload_bytes must be positive")
    if policy.min_free_gb < 0 or policy.max_workspace_gb < 0:
        raise ValueError("policy disk limits must not be negative")
    if policy.pids_limit <= 0:
        raise ValueError("policy.pids_limit must be positive")
    if policy.container_user not in {"root", "member"}:
        raise ValueError("policy.container_user must be root or member")
    if policy.container_profile not in {"restricted", "development"}:
        raise ValueError("policy.container_profile must be restricted or development")
    if policy.container_profile == "development" and policy.writable_layer_gb <= 0:
        raise ValueError("development container requires a positive writable_layer_gb")
    if policy.container_profile == "development" and not policy.container_sudo:
        raise ValueError("development container requires container_sudo=true")
    if policy.container_profile == "development" and policy.container_read_only:
        raise ValueError("development container requires container_read_only=false")
    if policy.container_profile == "development" and policy.container_user != "member":
        raise ValueError("development container requires container_user=member")
    if policy.container_profile == "restricted" and policy.container_sudo:
        raise ValueError("container_sudo is only allowed in the development profile")
    if policy.container_profile == "restricted" and policy.writable_layer_gb:
        raise ValueError("writable_layer_gb is only supported in the development profile")
    if policy.writable_layer_gb < 0:
        raise ValueError("policy.writable_layer_gb must not be negative")
    if policy.container_home_tmpfs_gb < 0:
        raise ValueError("policy.container_home_tmpfs_gb must not be negative")
    if policy.container_home_tmpfs_gb > 64:
        raise ValueError("policy.container_home_tmpfs_gb must not exceed 64")
    if policy.min_free_docker_gb < 0:
        raise ValueError("policy.min_free_docker_gb must not be negative")
    if policy.docker_storage_budget_gb < 0:
        raise ValueError("policy.docker_storage_budget_gb must not be negative")
    if policy.queue_backfill_reserve_minutes < 0:
        raise ValueError("policy.queue_backfill_reserve_minutes must not be negative")
    if policy.container_home:
        if not policy.container_home.startswith("/"):
            raise ValueError("policy.container_home must be an absolute path")
        if ".." in PurePosixPath(policy.container_home).parts:
            raise ValueError("policy.container_home must not contain '..'")
        if not policy.container_home.startswith("/home/"):
            raise ValueError("policy.container_home must be under /home/")
        if policy.container_home.endswith("/"):
            raise ValueError("policy.container_home must not end with /")
        if (
            policy.container_home == "/"
            or policy.container_home == "/workspace"
            or policy.container_home.startswith("/workspace/")
        ):
            raise ValueError("policy.container_home must not be /workspace")
    if policy.userns_mode not in {"", "host"}:
        raise ValueError("policy.userns_mode must be empty or host")
    if not policy.legacy_audit_script:
        raise ValueError("policy.legacy_audit_script must not be empty")
    if policy.legacy_audit_timeout <= 0:
        raise ValueError("policy.legacy_audit_timeout must be positive")
    if policy.gpu_audit_required and not policy.gpu_audit_script:
        raise ValueError("policy.gpu_audit_script must not be empty when auditing is required")
    if policy.gpu_audit_timeout <= 0:
        raise ValueError("policy.gpu_audit_timeout must be positive")
    if policy.gpu_audit_interval <= 0:
        raise ValueError("policy.gpu_audit_interval must be positive")
    if policy.workspace_quota_required and policy.max_workspace_gb <= 0:
        raise ValueError("workspace_quota_required requires max_workspace_gb > 0")
    if policy.vscode_enabled and policy.container_profile != "development":
        raise ValueError("vscode_enabled requires the development container profile")
    if policy.vscode_max_tunnels <= 0:
        raise ValueError("policy.vscode_max_tunnels must be positive")
    if policy.max_shells_per_user <= 0 or policy.max_shells_global <= 0:
        raise ValueError("policy shell limits must be positive")
    if policy.max_shells_per_user > policy.max_shells_global:
        raise ValueError("max_shells_per_user must not exceed max_shells_global")
    if policy.max_tunnels_per_user <= 0 or policy.max_tunnels_global <= 0:
        raise ValueError("policy tunnel limits must be positive")
    if policy.max_tunnels_per_user > policy.max_tunnels_global:
        raise ValueError("max_tunnels_per_user must not exceed max_tunnels_global")
    if policy.max_shells_global > 64 or policy.max_tunnels_global > 64:
        raise ValueError("global stream limits must not exceed 64")
    if policy.max_queue_length <= 0 or policy.max_queued_allocations_per_user <= 0:
        raise ValueError("policy queue limits must be positive")
    if policy.max_queue_hours <= 0:
        raise ValueError("policy.max_queue_hours must be positive")
    if policy.global_max_cpus + policy.reserve_cpus > hardware.cpu_logical:
        raise ValueError("global_max_cpus + reserve_cpus exceeds hardware.cpu_logical")
    if policy.global_max_memory_gb + policy.reserve_memory_gb > hardware.ram_gib:
        raise ValueError("global_max_memory_gb + reserve_memory_gb exceeds hardware.ram_gib")
    groups = GroupsConfig(
        members=str(groups_raw.get("members", "citespec-members")),
        cli=str(groups_raw.get("cli", "citespec-cli")),
        admins=str(groups_raw.get("admins", "citespec-admins")),
        gpu=str(groups_raw.get("gpu", "citespec-gpu")),
    )
    raw_allowlist = image_raw.get("allowlist", [])
    if not isinstance(raw_allowlist, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_allowlist
    ):
        raise ValueError("image.allowlist must be a list of non-empty strings")
    image = ImageConfig(
        base=str(image_raw.get("base", "citespec-base:2026.10")),
        allowlist=tuple(item.strip() for item in raw_allowlist),
    )
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
    onboarding = OnboardingConfig(
        guide_url=str(onboarding_raw.get("guide_url", "")),
        apply_url=str(onboarding_raw.get("apply_url", "")),
        support_label=str(onboarding_raw.get("support_label", "实验室计算资源管理员")),
        support_contact=str(onboarding_raw.get("support_contact", "")),
        legacy_login_notice_enabled=bool(onboarding_raw.get("legacy_login_notice_enabled", True)),
        intake_db=str(
            onboarding_raw.get("intake_db", "/var/lib/citespec-resource-intake/applications.db")
        ),
        provisioning_spool=str(
            onboarding_raw.get("provisioning_spool", "/var/lib/citespec-resource/provision")
        ),
    )
    raw_presets = onboarding_raw.get("presets", [])
    if not isinstance(raw_presets, list):
        raise ValueError("onboarding.presets must be a list of tables")
    presets: list[tuple[str, int, float, int, int]] = []
    for preset in raw_presets:
        if not isinstance(preset, dict):
            raise ValueError("onboarding.presets entries must be tables")
        name = str(preset.get("name", ""))
        gpus = int(preset.get("gpus", 0))
        cpus = float(preset.get("cpus", 0))
        memory_gb = int(preset.get("memory_gb", 0))
        hours = int(preset.get("hours", 0))
        if not name or gpus < 0 or cpus <= 0 or memory_gb <= 0 or hours <= 0:
            raise ValueError(
                "onboarding.presets entries need name, gpus >= 0, cpus, memory_gb, hours"
            )
        presets.append((name, gpus, cpus, memory_gb, hours))
    return LabConfig(
        server=server,
        hardware=hardware,
        policy=policy,
        groups=groups,
        image=image,
        deployment=deployment,
        gui=gui,
        onboarding=OnboardingConfig(
            guide_url=onboarding.guide_url,
            apply_url=onboarding.apply_url,
            support_label=onboarding.support_label,
            support_contact=onboarding.support_contact,
            legacy_login_notice_enabled=onboarding.legacy_login_notice_enabled,
            intake_db=onboarding.intake_db,
            provisioning_spool=onboarding.provisioning_spool,
            presets=tuple(presets),
        ),
    )


def client_config_path() -> Path:
    """Member-workstation profile written by ``citespec-resource setup``.

    It intentionally contains connection preferences only. Hardware budgets,
    policy, and admin paths remain server configuration and are never copied
    to a member workstation (goal §5.4).
    """
    override = os.environ.get("CITESPEC_RESOURCE_CLIENT_CONFIG", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(os.path.expanduser(base)) if base else Path.home() / ".config"
    return root / "citespec-resource" / "client.toml"


def load_client_config(path: str | Path) -> LabConfig:
    """Load the minimal member profile (``[server]`` plus optional UX data)."""
    path = Path(path)
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if "hardware" in data:
        # A full server file reached the client loader; use the strict parser.
        return load_config(path)
    server_raw = _section(data, "server")
    server = ServerConfig(
        name=str(_require(server_raw, "name", "server")),
        ssh_alias=str(_require(server_raw, "ssh_alias", "server")),
        address=str(server_raw.get("address", "")),
        ssh_port=int(server_raw.get("ssh_port", 22)),
        ssh_user=str(server_raw.get("ssh_user", "")),
        known_hosts=str(server_raw.get("known_hosts", "")),
        broker_socket=str(server_raw.get("broker_socket", "/run/citespec-resource/broker.sock")),
    )
    if not 1 <= server.ssh_port <= 65535:
        raise ValueError("server.ssh_port must be between 1 and 65535")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,254}", server.ssh_alias):
        raise ValueError("server.ssh_alias contains unsupported characters")
    if server.ssh_user and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", server.ssh_user):
        raise ValueError("server.ssh_user contains unsupported characters")
    gui_raw = data.get("gui", {})
    onboarding_raw = data.get("onboarding", {})
    return LabConfig(
        server=server,
        # Client-side values are unused; keeping placeholders makes the
        # profile type-compatible with everyday CLI commands.
        hardware=HardwareConfig(
            gpu_count=1,
            gpu_model="client-profile",
            gpu_memory_mib=0,
            cpu_logical=1,
            ram_gib=1,
        ),
        gui=GUIConfig(
            admin_ssh=str(gui_raw.get("admin_ssh", "")),
            connect_timeout=int(gui_raw.get("connect_timeout", 15)),
        ),
        onboarding=OnboardingConfig(
            guide_url=str(onboarding_raw.get("guide_url", "")),
            apply_url=str(onboarding_raw.get("apply_url", "")),
            support_label=str(onboarding_raw.get("support_label", "实验室计算资源管理员")),
            support_contact=str(onboarding_raw.get("support_contact", "")),
        ),
    )


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_client_config(path: str | Path, config: LabConfig) -> Path:
    """Atomically write the setup-generated member profile with mode 0600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    server = config.server
    lines = [
        "# Managed by `citespec-resource setup`; connection settings only.",
        "[server]",
        f"name = {_toml_string(server.name)}",
        f"ssh_alias = {_toml_string(server.ssh_alias)}",
    ]
    if server.address:
        lines.append(f"address = {_toml_string(server.address)}")
    lines.extend(
        [
            f"ssh_port = {server.ssh_port}",
            f"ssh_user = {_toml_string(server.ssh_user)}",
        ]
    )
    if server.known_hosts:
        lines.append(f"known_hosts = {_toml_string(server.known_hosts)}")
    lines.append(f"broker_socket = {_toml_string(server.broker_socket)}")
    if config.onboarding.apply_url or config.onboarding.guide_url:
        lines.append("")
        lines.append("[onboarding]")
        if config.onboarding.guide_url:
            lines.append(f"guide_url = {_toml_string(config.onboarding.guide_url)}")
        if config.onboarding.apply_url:
            lines.append(f"apply_url = {_toml_string(config.onboarding.apply_url)}")
        if config.onboarding.support_contact:
            lines.append(f"support_contact = {_toml_string(config.onboarding.support_contact)}")
    content = "\n".join(lines) + "\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=".client.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return path
