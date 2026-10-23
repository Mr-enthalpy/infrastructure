from pathlib import Path

import pytest

from udc_resource.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_is_valid():
    cfg = load_config(ROOT / "lab/resources.example.toml")
    assert cfg.server.name == "amax"
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
    assert cfg.policy.container_read_only is True
    assert cfg.server.members_file.endswith("members.toml")


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
