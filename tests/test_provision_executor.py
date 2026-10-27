"""Executor spool-mode and idempotency tests (AC-24, run without root)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

EXECUTOR = Path(__file__).resolve().parents[1] / "deploy/membership/provision_executor.py"


def _load_executor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("citespec_provision_executor_test", EXECUTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_stdin(request: dict) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(EXECUTOR)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_executor_rejects_unknown_action_without_privileges():
    code, result = _run_stdin({"operation_id": "op1", "action": "run_shell", "frozen_input": {}})
    assert code == 1
    assert result["status"] == "failed"
    assert result["stage"] == "validate"


def test_executor_rejects_reserved_account():
    code, result = _run_stdin(
        {
            "operation_id": "op2",
            "action": "provision_member",
            "frozen_input": {"username": "root", "public_keys": []},
        }
    )
    assert code == 1
    assert "reserved" in result["error"] or "refusing" in result["error"]


def test_executor_inspect_is_read_only_and_accepts_known_username():
    code, result = _run_stdin(
        {
            "operation_id": "op-inspect",
            "action": "inspect_member",
            "frozen_input": {"username": "definitely-absent"},
        }
    )
    assert code == 0
    assert result["status"] == "succeeded"
    assert result["exists"] is False
    assert result["username"] == "definitely-absent"


def test_executor_invalid_json_answers_safely():
    result = subprocess.run(
        [sys.executable, str(EXECUTOR)],
        input="{not json",
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["stage"] == "validate"


def test_spool_mode_processes_once_and_is_idempotent(tmp_path: Path):
    spool = tmp_path / "provision"
    (spool / "requests").mkdir(parents=True)
    (spool / "requests").joinpath("op-1.json").write_text(
        json.dumps(
            {
                "operation_id": "op-1",
                "action": "disable_member",
                "frozen_input": {"username": "ghostuser", "reason": "gone"},
            }
        ),
        encoding="utf-8",
    )
    # "ghostuser" does not exist locally: disable reports account_absent and
    # succeeds (idempotent), WITHOUT creating or touching any account.
    run = subprocess.run(
        [sys.executable, str(EXECUTOR), "--spool", str(spool)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0
    result_file = spool / "results" / "op-1.json"
    assert result_file.exists()
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["status"] == "succeeded"
    assert "account_absent" in payload["stages"]
    assert not list((spool / "requests").glob("*.json"))
    assert not list((spool / "processing").glob("*.json"))

    # Second pass: result exists -> request skipped (idempotent).
    result_file.unlink()
    (spool / "results").mkdir(exist_ok=True)
    (spool / "results").joinpath("op-1.json").write_text(
        json.dumps({"status": "succeeded", "stages": ["cached"]}),
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(EXECUTOR), "--spool", str(spool)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert json.loads((spool / "results" / "op-1.json").read_text(encoding="utf-8"))["stages"] == [
        "cached"
    ]


def test_spool_rejects_invalid_json_without_leaving_request(tmp_path: Path):
    spool = tmp_path / "provision"
    (spool / "requests").mkdir(parents=True)
    request = spool / "requests" / "op-bad.json"
    request.write_text("{not json", encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(EXECUTOR), "--spool", str(spool)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0
    payload = json.loads((spool / "results" / "op-bad.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["stage"] == "validate"
    assert not request.exists()


def test_spool_recovers_a_crash_claimed_request(tmp_path: Path):
    spool = tmp_path / "provision"
    (spool / "requests").mkdir(parents=True)
    (spool / "processing").mkdir(parents=True)
    claimed = spool / "processing" / "op-claimed.json"
    claimed.write_text(
        json.dumps(
            {
                "operation_id": "op-claimed",
                "action": "disable_member",
                "frozen_input": {"username": "ghostuser", "reason": "crash recovery"},
            }
        ),
        encoding="utf-8",
    )
    run = subprocess.run(
        [sys.executable, str(EXECUTOR), "--spool", str(spool)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0
    payload = json.loads((spool / "results" / "op-claimed.json").read_text(encoding="utf-8"))
    assert payload["status"] == "succeeded"
    assert not claimed.exists()


def test_spool_rejects_operation_id_mismatch(tmp_path: Path):
    spool = tmp_path / "provision"
    (spool / "requests").mkdir(parents=True)
    (spool / "requests" / "op-file.json").write_text(
        json.dumps(
            {
                "operation_id": "op-other",
                "action": "disable_member",
                "frozen_input": {"username": "ghostuser", "reason": "mismatch"},
            }
        ),
        encoding="utf-8",
    )
    run = subprocess.run(
        [sys.executable, str(EXECUTOR), "--spool", str(spool)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0
    payload = json.loads((spool / "results" / "op-file.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert "filename" in payload["error"]


def test_executor_has_no_broad_pkill_path():
    source = EXECUTOR.read_text(encoding="utf-8")
    assert "pkill" not in source
    assert "_kill_member_sessions" not in source


def test_session_scan_matches_only_exact_member_root_sshd(monkeypatch, tmp_path):
    executor = _load_executor()
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid in (10, 11, 12, 13, 14, 15):
        (proc / str(pid)).mkdir()

    real_uids = {10: 0, 11: 0, 12: 0, 13: 1000, 14: 0, 15: 0}
    executables = {pid: pid not in {12} for pid in real_uids}
    cmdlines = {
        10: "sshd: alice",
        11: "sshd: alice [priv]",
        12: "sshd: alice2",
        13: "sshd: alice",
        14: "sshd: /usr/sbin/sshd -D",
        15: "sshd: alice@pts/0",
    }
    monkeypatch.setattr(executor, "_process_uid", lambda pid: real_uids[pid])
    monkeypatch.setattr(executor, "_is_sshd_executable", lambda pid: executables[pid])
    monkeypatch.setattr(executor, "_process_cmdline", lambda pid: cmdlines[pid])

    assert executor._member_ssh_monitor_pids("alice", proc_root=proc) == [10, 11, 15]


def test_session_scan_recognizes_openssh_98_sshd_session(monkeypatch, tmp_path):
    executor = _load_executor()
    source = EXECUTOR.read_text(encoding="utf-8")
    assert '{"sshd", "sshd-session"}' in source
    assert "sshd-session: " in source

    proc = tmp_path / "proc"
    proc.mkdir()
    for pid in (20, 21, 22):
        (proc / str(pid)).mkdir()
    real_uids = {20: 0, 21: 0, 22: 0}
    cmdlines = {
        20: "sshd-session: alice",
        21: "sshd-session: alice [priv]",
        22: "sshd-session: alice2",
    }
    monkeypatch.setattr(executor, "_process_uid", lambda pid: real_uids[pid])
    monkeypatch.setattr(executor, "_is_sshd_executable", lambda pid: True)
    monkeypatch.setattr(executor, "_process_cmdline", lambda pid: cmdlines[pid])

    assert executor._member_ssh_monitor_pids("alice", proc_root=proc) == [20, 21]


def test_session_scan_fails_closed_when_proc_is_unreadable(monkeypatch, tmp_path):
    executor = _load_executor()
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "1").mkdir()
    monkeypatch.setattr(executor, "_process_uid", lambda pid: None)

    with pytest.raises(executor.StageError, match="cannot inspect /proc"):
        executor._member_ssh_monitor_pids("alice", proc_root=proc)


def test_set_keys_closes_existing_ssh_sessions(monkeypatch):
    executor = _load_executor()
    closed: list[str] = []

    monkeypatch.setattr(executor, "_is_managed_account", lambda username: True)
    monkeypatch.setattr(executor, "_scrub_home_keys", lambda username, stages: None)
    monkeypatch.setattr(executor, "_write_central_keys", lambda username, keys: None)
    monkeypatch.setattr(executor, "_verify_central_keys", lambda username, keys: None)

    def close(username: str, stages: list[str], *, stage: str) -> None:
        closed.append(username)
        stages.append(f"{stage}:1")

    monkeypatch.setattr(executor, "_close_member_ssh_sessions", close)
    stages: list[str] = []
    executor.ACTIONS["set_keys"](
        {"username": "alice", "public_keys": ["ssh-ed25519 AAAA alice"]},
        stages,
    )
    assert closed == ["alice"]
    assert "ssh_sessions_reauthenticated:1" in stages


def test_disable_member_closes_sessions_after_locking(monkeypatch):
    executor = _load_executor()
    calls: list[object] = []

    monkeypatch.setattr(executor, "_user_exists", lambda username: True)
    monkeypatch.setattr(executor, "_is_managed_account", lambda username: True)
    monkeypatch.setattr(executor, "_write_central_keys", lambda username, keys: None)
    monkeypatch.setattr(executor, "_scrub_home_keys", lambda username, stages: None)
    monkeypatch.setattr(
        executor,
        "_run",
        lambda argv, check=True: calls.append(tuple(argv)),
    )

    def close(username: str, stages: list[str], *, stage: str) -> None:
        calls.append(("close", username, stage))
        stages.append(f"{stage}:1")

    monkeypatch.setattr(executor, "_close_member_ssh_sessions", close)
    stages: list[str] = []
    executor.ACTIONS["disable_member"](
        {"username": "alice", "reason": "suspended"},
        stages,
    )
    assert ("usermod", "-L", "alice") in calls
    assert ("close", "alice", "sessions_terminated") in calls
    assert calls.index(("usermod", "-L", "alice")) < calls.index(
        ("close", "alice", "sessions_terminated")
    )
