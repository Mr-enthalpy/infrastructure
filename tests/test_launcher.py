from __future__ import annotations

import json
import subprocess

from citespec_resource import launcher, update
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


def test_launcher_rolls_back_current_after_new_payload_cannot_start(tmp_path, monkeypatch, capsys):
    root = tmp_path / "app"
    old_payload = root / "versions" / "0.1.0" / update.payload_name()
    new_payload = root / "versions" / "0.2.0" / update.payload_name()
    old_payload.parent.mkdir(parents=True)
    new_payload.parent.mkdir(parents=True)
    old_payload.write_text("", encoding="utf-8")
    new_payload.write_text("", encoding="utf-8")
    (root / "current.json").write_text(json.dumps({"version": "0.2.0"}), encoding="utf-8")
    attempts: list[str] = []

    def fake_run(argv, **kwargs):
        attempts.append(argv[0])
        if argv[0] == str(new_payload):
            raise OSError("exec format error")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(launcher, "find_app_root", lambda: root)
    monkeypatch.setattr(launcher, "resolve_payload", lambda _root: new_payload)
    monkeypatch.setattr(launcher, "_maybe_update", lambda _root: "0.1.0")
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher.main(["list"]) == 0
    assert attempts == [str(new_payload), str(old_payload)]
    assert json.loads((root / "current.json").read_text(encoding="utf-8"))["version"] == "0.1.0"
    assert "rolled back" in capsys.readouterr().err
