"""Executor spool-mode and idempotency tests (AC-24, run without root)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

EXECUTOR = Path(__file__).resolve().parents[1] / "deploy/membership/provision_executor.py"


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
