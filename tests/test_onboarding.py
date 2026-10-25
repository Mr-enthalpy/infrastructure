"""Workstation onboarding tests (goal §5.4, AC-05): no host/SSH mutation."""

from __future__ import annotations

from pathlib import Path

import pytest

from udc_resource import onboarding
from udc_resource.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "lab/resources.example.toml"


@pytest.fixture
def config() -> object:
    return load_config(CONFIG)


def test_managed_fragment_lines_contain_alias_and_hostname(config):
    lines = onboarding._managed_fragment_lines(config, "10.0.0.5")
    joined = "\n".join(lines)
    assert "Host amax" in joined
    assert "HostName 10.0.0.5" in joined
    assert lines[0].startswith("# BEGIN udc-workstation")
    assert lines[-1] == "# END udc-workstation"


def test_write_managed_fragment_never_touches_existing_config(tmp_path, monkeypatch, config):
    """AC-05: setup must not overwrite existing SSH config or private keys."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    user_config = ssh_dir / "config"
    user_config.write_text(
        "Host myserver\n  User me\n  IdentityFile ~/.ssh/id_real\n",
        encoding="utf-8",
    )
    private_key = ssh_dir / "id_real"
    private_key.write_text("PRIVATE MATERIAL", encoding="utf-8")

    fragment = onboarding._write_managed_fragment(config, "10.0.0.5")

    assert fragment.exists()
    assert "udc-workstation" in fragment.read_text(encoding="utf-8")
    # Existing user config is intact apart from the Include line on top.
    text = user_config.read_text(encoding="utf-8")
    assert text.splitlines()[0] == f"Include {onboarding.MANAGED_FRAGMENT_NAME}"
    assert "Host myserver" in text
    assert "IdentityFile ~/.ssh/id_real" in text
    # Private keys are untouched.
    assert private_key.read_text(encoding="utf-8") == "PRIVATE MATERIAL"


def test_ssh_probe_reports_missing_client(config, monkeypatch):
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: None)
    ok, detail = onboarding._ssh_fingerprint_probe("amax", config)
    assert ok is False
    assert "ssh" in detail
