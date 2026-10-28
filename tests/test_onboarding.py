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


def test_run_capture_decodes_non_utf8_output(tmp_path):
    """Regression (C4-2): GBK hosts must not crash on non-GBK SSH output."""
    import sys

    script = (
        "import sys;"
        "sys.stdout.buffer.write(b'\\xff\\xac');"
        "sys.stderr.buffer.write(b'\\xff');"
        "sys.stdout.flush();sys.stderr.flush()"
    )
    result = onboarding._run_capture([sys.executable, "-c", script])
    assert result.returncode == 0
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)
    assert "\ufffd" in result.stdout


def test_run_capture_requests_utf8_replace(config, monkeypatch):
    seen: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return Result()

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)
    onboarding._run_capture(["ssh", "-V"])
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"
    assert seen["text"] is True
    assert seen["capture_output"] is True


def test_ssh_probe_reports_server_version_with_utf8_banner(config, monkeypatch):
    """Regression (C4-2/C4-4): a UTF-8 login banner is informational, not fatal."""
    seen: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "0.1.0\n"
        stderr = "本服务器已启用个人账号\n"

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return Result()

    monkeypatch.setattr(onboarding.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)
    ok, detail = onboarding._ssh_fingerprint_probe("192.0.2.5", config)
    assert ok is True
    assert "0.1.0" in detail
    # Older servers reject `version --json`; the probe must stay compatible.
    assert seen[0][-1] == "citespec-resource version"


def test_ssh_probe_tolerates_missing_streams(config, monkeypatch):
    """Regression (C4-2): a probe result without captured streams must not crash."""

    class Result:
        returncode = 1
        stdout = None
        stderr = None

    monkeypatch.setattr(onboarding.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(onboarding.subprocess, "run", lambda argv, **kwargs: Result())
    ok, detail = onboarding._ssh_fingerprint_probe("192.0.2.5", config)
    assert ok is False
    assert "SSH 认证或连接失败" in detail


def test_ssh_probe_handles_decode_failure_as_connection_error(config, monkeypatch):
    """Regression (C4-2): decode problems are reported, never raised as a traceback."""

    def fake_run(argv, **kwargs):
        raise UnicodeDecodeError("gbk", b"\xac", 2, 3, "illegal multibyte sequence")

    monkeypatch.setattr(onboarding.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)
    ok, detail = onboarding._ssh_fingerprint_probe("192.0.2.5", config)
    assert ok is False
    assert "SSH 连接失败" in detail


def test_fingerprint_mismatch_reports_observed_algorithms(monkeypatch, config, tmp_path, capsys):
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(onboarding, "_known_hosts_path", lambda _config: known_hosts)
    monkeypatch.setattr(onboarding, "_host_key_is_known", lambda _config, _host: False)
    monkeypatch.setattr(
        onboarding,
        "_scan_host_keys",
        lambda _config, _host: "192.0.2.5 ssh-rsa AAAAB3NzaC1yc2E\n",
    )
    monkeypatch.setattr(
        onboarding,
        "_scan_fingerprints",
        lambda _config, _host: "3072 SHA256:actual (RSA)",
    )
    with pytest.raises(RuntimeError, match="RSA"):
        onboarding._trust_host_key(
            config,
            "192.0.2.5",
            interactive=False,
            expected_fingerprint="SHA256:ed25519only",
        )
    output = capsys.readouterr().out
    assert "SHA256:actual" in output
    assert "RSA (ssh-rsa)" in output
    assert not known_hosts.exists()


def test_fingerprint_match_accepts_bare_digest():
    fingerprints = "3072 SHA256:abc123def (RSA)"
    assert onboarding._fingerprint_matches("SHA256:abc123def", fingerprints)
    assert onboarding._fingerprint_matches("abc123def", fingerprints)
    assert not onboarding._fingerprint_matches("SHA256:zzz", fingerprints)


def test_setup_returns_zero_after_writing_profile(monkeypatch, config, tmp_path):
    from citespec_resource import config as config_module

    profile = tmp_path / "client.toml"
    monkeypatch.setattr(onboarding, "_check_program", lambda name: name)
    monkeypatch.setattr(onboarding, "_trust_host_key", lambda *a, **k: None)
    monkeypatch.setattr(
        onboarding,
        "_write_managed_fragment",
        lambda *a, **k: (tmp_path / "fragment").write_text("x", encoding="utf-8"),
    )
    monkeypatch.setattr(config_module, "client_config_path", lambda: profile)
    monkeypatch.setattr(
        onboarding, "_ssh_fingerprint_probe", lambda *a, **k: (True, "SSH 认证成功")
    )
    monkeypatch.setattr(
        onboarding,
        "_remote_me",
        lambda *a, **k: {"membership": {"status": "active"}},
    )
    code = onboarding.run_setup(config, host="192.0.2.5", yes=True)
    assert code == 0
    assert profile.exists()


def test_setup_reports_written_profile_when_probe_fails(monkeypatch, config, tmp_path, capsys):
    from citespec_resource import config as config_module

    profile = tmp_path / "client.toml"
    monkeypatch.setattr(onboarding, "_check_program", lambda name: name)
    monkeypatch.setattr(onboarding, "_trust_host_key", lambda *a, **k: None)
    monkeypatch.setattr(
        onboarding,
        "_write_managed_fragment",
        lambda *a, **k: (tmp_path / "fragment").write_text("x", encoding="utf-8"),
    )
    monkeypatch.setattr(config_module, "client_config_path", lambda: profile)

    def boom(*args, **kwargs):
        raise UnicodeDecodeError("gbk", b"\xac", 2, 3, "illegal multibyte sequence")

    monkeypatch.setattr(onboarding, "_ssh_fingerprint_probe", boom)
    code = onboarding.run_setup(config, host="192.0.2.5", yes=True)
    assert code == 1
    output = capsys.readouterr().out
    assert "已写入成员连接配置" in output
    assert "doctor" in output
    assert profile.exists()
