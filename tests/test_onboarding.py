"""Workstation onboarding tests (goal §5.4, AC-05): no host/SSH mutation."""

from __future__ import annotations

from pathlib import Path

import pytest

from citespec_resource import onboarding
from citespec_resource.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "lab/resources.example.toml"


@pytest.fixture
def config() -> object:
    return load_config(CONFIG)


def test_managed_fragment_lines_contain_alias_and_hostname(config):
    lines = onboarding._managed_fragment_lines(config, "192.0.2.5")
    joined = "\n".join(lines)
    assert "Host example" in joined
    assert "HostName 192.0.2.5" in joined
    assert lines[0].startswith("# BEGIN citespec-workstation")
    assert lines[-1] == "# END citespec-workstation"


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

    fragment = onboarding._write_managed_fragment(config, "192.0.2.5")

    assert fragment.exists()
    assert "citespec-workstation" in fragment.read_text(encoding="utf-8")
    # Existing user config is intact apart from the Include line on top.
    text = user_config.read_text(encoding="utf-8")
    assert text.splitlines()[0] == f"Include {onboarding.MANAGED_FRAGMENT_NAME}"
    assert "Host myserver" in text
    assert "IdentityFile ~/.ssh/id_real" in text
    # Private keys are untouched.
    assert private_key.read_text(encoding="utf-8") == "PRIVATE MATERIAL"


def test_ssh_probe_reports_missing_client(config, monkeypatch):
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: None)
    ok, detail = onboarding._ssh_fingerprint_probe("example-gpu", config)
    assert ok is False
    assert "ssh" in detail


def test_ssh_probe_never_uses_accept_new(monkeypatch, config, tmp_path):
    seen: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return Result()

    monkeypatch.setattr(onboarding.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)
    ok, _detail = onboarding._ssh_fingerprint_probe("192.0.2.5", config)
    assert ok is True
    flat = " ".join(seen[0])
    assert "StrictHostKeyChecking=yes" in flat
    assert "accept-new" not in flat
    assert f"UserKnownHostsFile={onboarding._known_hosts_path(config)}" in flat


def test_remote_me_unwraps_local_broker_envelope(tmp_path, monkeypatch, config):
    from dataclasses import replace

    socket_path = tmp_path / "broker.sock"
    socket_path.write_bytes(b"")
    local_config = replace(
        config,
        server=replace(config.server, broker_socket=str(socket_path)),
    )

    class FakeBroker:
        def __init__(self, path):
            self.path = path

        def request(self, name):
            assert name == "me"
            return {
                "ok": True,
                "data": {
                    "username": "alice",
                    "membership": {"status": "active", "valid_until": ""},
                },
            }

    monkeypatch.setattr(onboarding, "BrokerClient", FakeBroker)
    data = onboarding._remote_me(local_config, "192.0.2.5")
    assert data["username"] == "alice"
    assert data["membership"]["status"] == "active"


def test_remote_me_rejects_error_envelope(tmp_path, monkeypatch, config):
    from dataclasses import replace

    socket_path = tmp_path / "broker.sock"
    socket_path.write_bytes(b"")
    local_config = replace(
        config,
        server=replace(config.server, broker_socket=str(socket_path)),
    )

    class FakeBroker:
        def __init__(self, path):
            self.path = path

        def request(self, name):
            return {
                "ok": False,
                "error": {"code": "not_found", "message": "member not found"},
            }

    monkeypatch.setattr(onboarding, "BrokerClient", FakeBroker)
    with pytest.raises(onboarding.CitespecError, match="member not found"):
        onboarding._remote_me(local_config, "192.0.2.5")


def test_first_use_requires_explicit_confirmation(monkeypatch, config, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(onboarding, "_known_hosts_path", lambda _config: known_hosts)
    monkeypatch.setattr(onboarding, "_host_key_is_known", lambda _config, _host: False)
    monkeypatch.setattr(
        onboarding,
        "_scan_host_keys",
        lambda _config, _host: "192.0.2.5 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest\n",
    )
    monkeypatch.setattr(onboarding, "_scan_fingerprints", lambda _config, _host: "SHA256:test")
    monkeypatch.setattr(
        onboarding.typer, "confirm", lambda *a, **k: (_ for _ in ()).throw(SystemExit(1))
    )
    with pytest.raises(SystemExit):
        onboarding._trust_host_key(config, "192.0.2.5", interactive=True)
    assert not known_hosts.exists()

    # Non-interactive setup must fail closed instead of trusting a scan.
    with pytest.raises(RuntimeError, match="主机密钥未信任"):
        onboarding._trust_host_key(config, "192.0.2.5", interactive=False)


def test_host_key_can_be_pinned_with_trusted_fingerprint(monkeypatch, config, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(onboarding, "_known_hosts_path", lambda _config: known_hosts)
    monkeypatch.setattr(onboarding, "_host_key_is_known", lambda _config, _host: False)
    monkeypatch.setattr(
        onboarding,
        "_scan_host_keys",
        lambda _config, _host: "192.0.2.5 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest\n",
    )
    monkeypatch.setattr(
        onboarding,
        "_scan_fingerprints",
        lambda _config, _host: "192.0.2.5 ssh-ed25519 SHA256:test",
    )
    onboarding._trust_host_key(
        config,
        "192.0.2.5",
        interactive=False,
        expected_fingerprint="SHA256:test",
    )
    assert "192.0.2.5 ssh-ed25519" in known_hosts.read_text(encoding="utf-8")


def test_host_key_rejects_wrong_trusted_fingerprint(monkeypatch, config, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(onboarding, "_known_hosts_path", lambda _config: known_hosts)
    monkeypatch.setattr(onboarding, "_host_key_is_known", lambda _config, _host: False)
    monkeypatch.setattr(
        onboarding,
        "_scan_host_keys",
        lambda _config, _host: "192.0.2.5 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest\n",
    )
    monkeypatch.setattr(
        onboarding,
        "_scan_fingerprints",
        lambda _config, _host: "192.0.2.5 ssh-ed25519 SHA256:actual",
    )
    with pytest.raises(RuntimeError, match="不一致"):
        onboarding._trust_host_key(
            config,
            "192.0.2.5",
            interactive=False,
            expected_fingerprint="SHA256:expected",
        )
    assert not known_hosts.exists()
