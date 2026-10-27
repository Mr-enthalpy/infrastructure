from __future__ import annotations

import subprocess

from citespec_resource import launcher
from citespec_resource.update import UpdateInfo


def test_launcher_skips_update_for_version_and_update_commands():
    assert launcher._should_auto_update(["version"]) is False
    assert launcher._should_auto_update(["update", "--check"]) is False
    assert launcher._should_auto_update(["list"]) is True


def test_launcher_runs_resolved_payload(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    payload = root / "versions" / "0.1.0" / "citespec-resource-app"
    payload.parent.mkdir(parents=True)
    payload.write_text("", encoding="utf-8")
    recorded: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(launcher, "find_app_root", lambda: root)
    monkeypatch.setattr(launcher, "resolve_payload", lambda _root: payload)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert launcher.main(["version"]) == 7
    assert recorded["argv"] == [str(payload), "version"]


def test_launcher_checks_against_installed_payload_version(tmp_path, monkeypatch):
    root = tmp_path / "app"
    payload = root / "versions" / "0.2.0" / "citespec-resource-app"
    payload.parent.mkdir(parents=True)
    payload.write_text("", encoding="utf-8")
    recorded: dict[str, object] = {}

    def fake_check(**kwargs):
        recorded.update(kwargs)
        return UpdateInfo(
            available=False,
            current_version=str(kwargs["current_version"]),
            latest_version="0.1.0",
            reason="already up to date",
        )

    monkeypatch.setattr(launcher, "find_app_root", lambda: root)
    monkeypatch.setattr(launcher, "resolve_payload", lambda _root: payload)
    monkeypatch.setattr(launcher, "update_check_due", lambda _root: True)
    monkeypatch.setattr(launcher, "check_for_update", fake_check)
    monkeypatch.setattr(launcher, "mark_update_checked", lambda _root: None)
    launcher._maybe_update(root)
    assert recorded["current_version"] == "0.2.0"


def test_launcher_reports_missing_payload(tmp_path, monkeypatch, capsys):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(launcher, "find_app_root", lambda: root)
    monkeypatch.setattr(launcher, "resolve_payload", lambda _root: None)
    assert launcher.main(["version"]) == 1
    assert "no installed client payload" in capsys.readouterr().err
