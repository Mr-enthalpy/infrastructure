import os
from pathlib import Path

import pytest

from citespec_resource.config import (
    HardwareConfig,
    ImageConfig,
    LabConfig,
    ServerConfig,
    load_client_config,
    load_config,
    write_client_config,
)

ROOT = Path(__file__).resolve().parents[1]


def _variant(tmp_path: Path, *replacements: tuple[str, str]) -> Path:
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / "variant.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_is_valid():
    cfg = load_config(ROOT / "lab/resources.example.toml")
    assert cfg.server.name == "example"
    assert cfg.hardware.gpu_count == 8
    assert cfg.policy.default_hours == 12
    assert cfg.policy.max_hours == 36
    assert cfg.policy.mode in {"monitor", "active"}
    assert cfg.policy.global_max_cpus == 120
    assert cfg.policy.global_max_memory_gb == 219
    assert cfg.policy.max_cpus_per_allocation == 32
    assert cfg.policy.max_memory_gb_per_allocation == 128
    assert cfg.policy.max_cpus_per_user == 64
    assert cfg.policy.max_memory_gb_per_user == 192
    assert cfg.policy.reserve_cpus == 8
    assert cfg.policy.reserve_memory_gb == 32
    assert cfg.policy.legacy_audit_required is True
    assert cfg.policy.queue_enabled is True
    assert cfg.policy.max_queue_length == 100
    assert cfg.policy.max_gpus_per_user == 4
    assert cfg.policy.max_active_allocations_per_user == 4
    assert cfg.policy.file_transfer_enabled is False
    assert cfg.policy.max_upload_bytes == 21474836480
    assert cfg.policy.pids_limit == 4096
    assert cfg.policy.container_user == "member"
    assert cfg.policy.container_read_only is False
    assert cfg.policy.container_profile == "development"
    assert cfg.policy.container_sudo is True
    assert cfg.policy.writable_layer_gb == 40
    assert cfg.policy.gpu_audit_required is True
    assert cfg.policy.workspace_quota_required is True
    assert cfg.policy.max_workspace_gb == 200
    assert cfg.policy.vscode_enabled is False
    assert cfg.server.members_file.endswith("members.toml")


def test_workspace_root_must_be_absolute(tmp_path):
    path = _variant(
        tmp_path,
        ('workspace_root = "/home/data1/citespec-users"', 'workspace_root = "citespec-users"'),
    )
    with pytest.raises(ValueError, match="workspace_root"):
        load_config(path)


def test_workspace_root_must_not_be_filesystem_root(tmp_path):
    path = _variant(
        tmp_path,
        ('workspace_root = "/home/data1/citespec-users"', 'workspace_root = "/"'),
    )
    with pytest.raises(ValueError, match="workspace_root"):
        load_config(path)


def test_admin_auth_key_must_be_absolute_and_contained(tmp_path):
    path = _variant(
        tmp_path,
        (
            'admin_auth_key_file = "/etc/citespec-resource/broker-admin.key"',
            'admin_auth_key_file = "relative/broker-admin.key"',
        ),
    )
    with pytest.raises(ValueError, match="admin_auth_key_file"):
        load_config(path)


def test_admin_auth_key_must_not_be_filesystem_root(tmp_path):
    path = _variant(
        tmp_path,
        (
            'admin_auth_key_file = "/etc/citespec-resource/broker-admin.key"',
            'admin_auth_key_file = "/"',
        ),
    )
    with pytest.raises(ValueError, match="filesystem root"):
        load_config(path)


def test_admin_attestation_ttl_is_bounded(tmp_path):
    path = _variant(
        tmp_path,
        ("admin_attestation_ttl_seconds = 120", "admin_attestation_ttl_seconds = 3600"),
    )
    with pytest.raises(ValueError, match="admin_attestation_ttl_seconds"):
        load_config(path)


def test_enabled_portal_requires_base_url(tmp_path):
    path = _variant(tmp_path, ("enabled = false", "enabled = true"))
    with pytest.raises(ValueError, match="portal.base_url"):
        load_config(path)


def test_enabled_portal_requires_https_base_url(tmp_path):
    path = _variant(
        tmp_path,
        ("enabled = false", "enabled = true"),
        ('base_url = ""', 'base_url = "http://portal.example.invalid"'),
    )
    with pytest.raises(ValueError, match="https portal.base_url"):
        load_config(path)


def test_enabled_portal_requires_secure_cookie(tmp_path):
    path = _variant(
        tmp_path,
        ("enabled = false", "enabled = true"),
        ('base_url = ""', 'base_url = "https://portal.example.invalid"'),
        ("cookie_secure = true", "cookie_secure = false"),
    )
    with pytest.raises(ValueError, match="cookie_secure"):
        load_config(path)


def test_enabled_portal_accepts_https_and_secure_cookie(tmp_path):
    path = _variant(
        tmp_path,
        ("enabled = false", "enabled = true"),
        ('base_url = ""', 'base_url = "https://portal.example.invalid"'),
    )
    cfg = load_config(path)
    assert cfg.portal.enabled is True
    assert cfg.portal.cookie_secure is True


def test_portal_service_account_defaults_to_portal_group():
    cfg = load_config(ROOT / "lab/resources.example.toml")
    assert cfg.portal.service_account == cfg.groups.portal


def test_portal_service_account_must_be_an_account_name(tmp_path):
    path = _variant(
        tmp_path,
        (
            'service_account = "citespec-portal"',
            'service_account = "../citespec-portal"',
        ),
    )
    with pytest.raises(ValueError, match="portal.service_account"):
        load_config(path)


def test_invalid_duration_is_rejected(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace("default_hours = 12", "default_hours = 40")
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_invalid_mode_is_rejected(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace('mode = "monitor"', 'mode = "automatic"')
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_cpu_limit_cannot_exceed_host(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace("global_max_cpus = 120", "global_max_cpus = 200")
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_memory_limit_cannot_exceed_host(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace("global_max_memory_gb = 219", "global_max_memory_gb = 999")
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_legacy_max_cpus_names_map_to_per_allocation_limits(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace("max_cpus_per_allocation = 32", "max_cpus = 24")
    text = text.replace("max_memory_gb_per_allocation = 128", "max_memory_gb = 96")
    path = tmp_path / "legacy.toml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.policy.max_cpus_per_allocation == 24
    assert cfg.policy.max_memory_gb_per_allocation == 96
    assert cfg.policy.global_max_cpus == 120
    assert cfg.policy.global_max_memory_gb == 219


def test_per_allocation_limit_cannot_exceed_global(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace("max_cpus_per_allocation = 32", "max_cpus_per_allocation = 121")
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_active_mode_requires_deployment_allowed(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace('mode = "monitor"', 'mode = "active"')
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_active_mode_requires_legacy_audit(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace('mode = "monitor"', 'mode = "active"')
    text = text.replace("deployment_allowed = false", "deployment_allowed = true")
    text = text.replace("legacy_audit_required = true", "legacy_audit_required = false")
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_active_mode_requires_gpu_audit(tmp_path):
    path = _variant(
        tmp_path,
        ('mode = "monitor"', 'mode = "active"'),
        ("deployment_allowed = false", "deployment_allowed = true"),
        ("gpu_audit_required = true", "gpu_audit_required = false"),
    )
    with pytest.raises(ValueError, match="gpu_audit_required"):
        load_config(path)


def test_active_mode_requires_workspace_quota(tmp_path):
    path = _variant(
        tmp_path,
        ('mode = "monitor"', 'mode = "active"'),
        ("deployment_allowed = false", "deployment_allowed = true"),
        ("workspace_quota_required = true", "workspace_quota_required = false"),
    )
    with pytest.raises(ValueError, match="workspace_quota_required"):
        load_config(path)


def test_restricted_profile_rejects_writable_layer_quota(tmp_path):
    path = _variant(
        tmp_path,
        ('container_profile = "development"', 'container_profile = "restricted"'),
        ("container_read_only = false", "container_read_only = true"),
        ("container_sudo = true", "container_sudo = false"),
    )
    with pytest.raises(ValueError, match="writable_layer_gb"):
        load_config(path)


def test_development_profile_requires_writable_layer(tmp_path):
    path = _variant(tmp_path, ("writable_layer_gb = 40", "writable_layer_gb = 0"))
    with pytest.raises(ValueError, match="writable_layer_gb"):
        load_config(path)


def test_development_profile_requires_container_sudo(tmp_path):
    path = _variant(tmp_path, ("container_sudo = true", "container_sudo = false"))
    with pytest.raises(ValueError, match="container_sudo"):
        load_config(path)


def test_development_profile_requires_member_container_user(tmp_path):
    path = _variant(tmp_path, ('container_user = "member"', 'container_user = "root"'))
    with pytest.raises(ValueError, match="container_user=member"):
        load_config(path)


def test_development_profile_requires_writable_rootfs(tmp_path):
    path = _variant(
        tmp_path,
        ("container_read_only = false", "container_read_only = true"),
    )
    with pytest.raises(ValueError, match="container_read_only"):
        load_config(path)


def test_vscode_requires_development_profile(tmp_path):
    path = _variant(
        tmp_path,
        ('container_profile = "development"', 'container_profile = "restricted"'),
        ("container_read_only = false", "container_read_only = true"),
        ("container_sudo = true", "container_sudo = false"),
        ("writable_layer_gb = 40", "writable_layer_gb = 0"),
        ("vscode_enabled = false", "vscode_enabled = true"),
    )
    with pytest.raises(ValueError, match="vscode_enabled"):
        load_config(path)


@pytest.mark.parametrize("value", ["/.", "//", "///"])
def test_workspace_root_rejects_normalized_root(tmp_path, value):
    path = _variant(
        tmp_path,
        ('workspace_root = "/home/data1/citespec-users"', f'workspace_root = "{value}"'),
    )
    with pytest.raises(ValueError, match="workspace_root"):
        load_config(path)


def test_container_home_cannot_be_workspace(tmp_path):
    path = _variant(tmp_path, ('container_home = ""', 'container_home = "/workspace"'))
    with pytest.raises(ValueError, match="container_home"):
        load_config(path)


def test_container_home_cannot_traverse(tmp_path):
    path = _variant(
        tmp_path,
        ('container_home = ""', 'container_home = "/home/../workspace"'),
    )
    with pytest.raises(ValueError, match="container_home"):
        load_config(path)


def test_container_home_must_be_under_home(tmp_path):
    path = _variant(tmp_path, ('container_home = ""', 'container_home = "/opt/member"'))
    with pytest.raises(ValueError, match="under /home"):
        load_config(path)


def test_container_home_must_not_end_with_slash(tmp_path):
    path = _variant(tmp_path, ('container_home = ""', 'container_home = "/home/alice/"'))
    with pytest.raises(ValueError, match="must not end with /"):
        load_config(path)


def test_image_allowlist_must_be_a_list_of_strings(tmp_path):
    path = _variant(tmp_path, ("allowlist = []", 'allowlist = "extra:1"'))
    with pytest.raises(ValueError, match="image.allowlist"):
        load_config(path)


def test_image_allowlist_includes_base():
    image = ImageConfig(base="base:1", allowlist=("extra:1",))
    assert image.image_allowed("base:1") is True
    assert image.image_allowed("extra:1") is True
    assert image.image_allowed("other:1") is False


def test_container_home_tmpfs_must_be_sane(tmp_path):
    negative = _variant(
        tmp_path,
        ('container_profile = "development"', 'container_profile = "development"'),
    )
    # Base variant is valid; only the mutated fields are under test.
    assert load_config(negative).policy.container_home_tmpfs_gb == 0


def test_new_storage_policies_have_defaults(tmp_path):
    cfg = load_config(ROOT / "lab/resources.example.toml")
    assert cfg.policy.container_home_tmpfs_gb == 0
    assert cfg.policy.min_free_docker_gb == 20


def test_active_mode_requirements_cannot_be_disabled(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace('mode = "monitor"', 'mode = "active"')
    text = text.replace("deployment_allowed = false", "deployment_allowed = true")
    text = text.replace("gpu_audit_required = true", "gpu_audit_required = false")
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="gpu_audit_required"):
        load_config(path)


def test_monitor_mode_may_disable_gates_until_activation(tmp_path):
    path = _variant(
        tmp_path,
        ("deployment_allowed = false", "deployment_allowed = true"),
        ("gpu_audit_required = true", "gpu_audit_required = false"),
    )
    cfg = load_config(path)
    assert cfg.policy.mode == "monitor"
    assert cfg.policy.gpu_audit_required is False


def test_member_client_profile_round_trip(tmp_path):
    config = LabConfig(
        server=ServerConfig(
            name="lab",
            ssh_alias="lab",
            address="lab.example.invalid",
            ssh_port=2222,
            ssh_user="alice",
        ),
        hardware=HardwareConfig(1, "client", 0, 1, 1),
    )
    path = write_client_config(tmp_path / "client.toml", config)
    loaded = load_client_config(path)
    assert loaded.server.ssh_alias == "lab"
    assert loaded.server.ssh_user == "alice"
    assert loaded.server.ssh_port == 2222
    assert loaded.hardware.gpu_model == "client-profile"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_client_profile_does_not_accept_a_partial_server_file(tmp_path):
    # A file with [hardware] is treated as a server file and must satisfy the
    # strict server schema; it must not silently become a client profile.
    path = tmp_path / "partial.toml"
    path.write_text("[server]\nname='x'\nssh_alias='x'\n[hardware]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing \\[hardware\\]"):
        load_client_config(path)


def test_active_development_profile_requires_storage_protection(tmp_path):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace('mode = "monitor"', 'mode = "active"')
    text = text.replace("deployment_allowed = false", "deployment_allowed = true")
    text = text.replace("docker_storage_budget_gb = 500", "docker_storage_budget_gb = 0")
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="docker_storage_budget_gb"):
        load_config(path)


def test_active_development_sudo_requires_a_passing_gpu_isolation_test(tmp_path, monkeypatch):
    text = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    text = text.replace('mode = "monitor"', 'mode = "active"')
    text = text.replace("deployment_allowed = false", "deployment_allowed = true")
    path = tmp_path / "active.toml"
    path.write_text(text, encoding="utf-8")

    marker = tmp_path / "gpu-isolation.ok"
    monkeypatch.setenv("CITESPEC_GPU_ISOLATION_MARKER", str(marker))
    with pytest.raises(ValueError, match="GPU isolation test"):
        load_config(path)

    marker.write_text("created_at=test\n", encoding="utf-8")
    config = load_config(path)
    assert config.policy.mode == "active"
    assert config.policy.container_profile == "development"
    assert config.policy.container_sudo is True
