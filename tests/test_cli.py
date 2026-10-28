import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from citespec_resource import cli
from citespec_resource.errors import BrokerUnavailable

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "lab/resources.example.toml"
runner = CliRunner()


class FakeClient:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response or {"ok": True, "data": {}}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def request(self, command_name: str, **args):
        self.calls.append((command_name, args))
        if self.error is not None:
            raise self.error
        return self.response


class SequenceClient(FakeClient):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__()
        self.responses = responses

    def request(self, command_name: str, **args):
        self.calls.append((command_name, args))
        return self.responses.pop(0)


def _invoke(monkeypatch, fake: FakeClient, args: list[str]):
    monkeypatch.setattr(cli, "_make_client", lambda config: fake)
    return runner.invoke(cli.app, ["--config", str(CONFIG), *args])


def test_list_json_envelope(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "server": "example-gpu",
                "mode": "monitor",
                "gpus": [],
                "allocations": [],
            },
        }
    )
    result = _invoke(monkeypatch, fake, ["list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["server"] == "example-gpu"
    assert fake.calls == [("list", {})]


def test_apply_parses_gpu_indices(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "id": "alloc-1",
                "status": "active",
                "gpus": [3, 5],
                "expires_at": "2026-10-23T00:00:00+00:00",
                "workspace": "/data1/citespec-users/alice",
            },
        }
    )
    result = _invoke(
        monkeypatch,
        fake,
        ["apply", "--gpus", "3,5", "--cpus", "8", "--memory", "32", "--hours", "12", "--json"],
    )
    assert result.exit_code == 0
    command, args = fake.calls[0]
    assert command == "apply"
    assert args["gpus"] == [3, 5]
    assert args["cpus"] == 8.0
    assert args["memory_gb"] == 32
    assert args["hours"] == 12
    assert args["note"] == ""


def test_apply_no_queue_flag_is_forwarded(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "id": "alloc-1",
                "status": "active",
                "gpus": [3],
                "expires_at": "2026-10-23T00:00:00+00:00",
                "workspace": "/data1/citespec-users/alice",
            },
        }
    )
    result = _invoke(
        monkeypatch,
        fake,
        ["apply", "--gpus", "3", "--cpus", "8", "--memory", "32", "--no-queue", "--json"],
    )
    assert result.exit_code == 0
    command, args = fake.calls[0]
    assert command == "apply"
    assert args["queue"] is False


def test_apply_wait_json_prints_one_envelope(monkeypatch):
    fake = SequenceClient(
        [
            {
                "ok": True,
                "data": {
                    "queue_id": "q-1",
                    "status": "queued",
                    "owner": "alice",
                    "gpus": [3],
                    "cpus": 8.0,
                    "memory_gb": 32,
                    "hours": 1,
                    "workspace": "/data1/citespec-users/alice",
                    "created_at": "2026-10-22T12:00:00+00:00",
                    "expires_at": "2026-10-23T12:00:00+00:00",
                    "position": 1,
                },
            },
            {
                "ok": True,
                "data": {
                    "queue": {
                        "queue_id": "q-1",
                        "status": "admitted",
                        "allocation_id": "alloc-1",
                    },
                    "allocation": {
                        "id": "alloc-1",
                        "status": "active",
                        "gpus": [3],
                    },
                },
            },
        ]
    )
    result = _invoke(
        monkeypatch,
        fake,
        [
            "apply",
            "--gpus",
            "3",
            "--cpus",
            "8",
            "--memory",
            "32",
            "--wait",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["allocation"]["id"] == "alloc-1"
    assert [call[0] for call in fake.calls] == ["apply", "queue_get"]


def test_queue_list_and_cancel_commands(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "queue": [
                    {
                        "queue_id": "q-1",
                        "owner": "alice",
                        "status": "queued",
                        "position": 1,
                        "gpus": [3],
                        "cpus": 8.0,
                        "memory_gb": 32,
                        "remaining_seconds": 3600,
                    }
                ]
            },
        }
    )
    result = _invoke(monkeypatch, fake, ["queue", "list", "--history", "--json"])
    assert result.exit_code == 0
    assert fake.calls == [("queue_list", {"all": False, "include_finished": True})]

    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "queue_id": "q-1",
                "status": "cancelled",
            },
        }
    )
    result = _invoke(monkeypatch, fake, ["queue", "cancel", "q-1", "--reason", "done"])
    assert result.exit_code == 0
    assert fake.calls == [("queue_cancel", {"id": "q-1", "reason": "done"})]
    assert "cancelled: q-1" in result.output


def test_list_hides_remaining_time_for_released_allocations(monkeypatch):
    """Regression (C4-7): released work must not look like it is still running."""
    released = {
        "id": "alloc-released",
        "owner": "alice",
        "status": "released",
        "gpus": [0],
        "cpus": 8.0,
        "memory_gb": 32,
        "workspace": "/home/data1/citespec-users/alice",
        "starts_at": "2026-10-28T04:05:55+00:00",
        "expires_at": "2026-10-28T08:05:54+00:00",
        "remaining_seconds": 8303,
        "container_name": "citespec-alice-old",
    }
    active = dict(
        released,
        id="alloc-active",
        status="active",
        remaining_seconds=3600,
        container_name="citespec-alice-new",
    )
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "server": "example-gpu",
                "mode": "active",
                "gpus": [],
                "allocations": [released, active],
                "queue": [],
                "unmanaged": {},
            },
        }
    )
    result = _invoke(monkeypatch, fake, ["list"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    released_line = next(line for line in lines if "alloc-released" in line)
    active_line = next(line for line in lines if "alloc-active" in line)
    assert "remaining=" not in released_line
    assert "remaining=" in active_line

    fake.response = {"ok": True, "data": {"allocation": released, "events": []}}
    result = _invoke(monkeypatch, fake, ["status", "alloc-released"])
    assert result.exit_code == 0, result.output
    expires_line = next(line for line in result.output.splitlines() if line.startswith("expires:"))
    assert "(" not in expires_line


def test_configure_output_encoding_uses_utf8_for_redirected_streams(monkeypatch):
    """Regression (C4-8): redirected output must be UTF-8 and never crash."""
    import sys as sys_module

    class Stream:
        def __init__(self, *, tty: bool, has_fileno: bool = True) -> None:
            self._tty = tty
            self._has_fileno = has_fileno
            self.calls: list[dict] = []

        def fileno(self) -> int:
            if not self._has_fileno:
                raise OSError("no file descriptor")
            return 1

        def isatty(self) -> bool:
            return self._tty

        def reconfigure(self, **kwargs) -> None:
            self.calls.append(kwargs)

    redirected = Stream(tty=False)
    console = Stream(tty=True)
    detached = Stream(tty=False, has_fileno=False)
    monkeypatch.setattr(sys_module, "stdout", redirected)
    monkeypatch.setattr(sys_module, "stderr", console)
    cli._configure_output_encoding()
    assert redirected.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert console.calls == [{"errors": "replace"}]

    monkeypatch.setattr(sys_module, "stdout", detached)
    monkeypatch.setattr(sys_module, "stderr", detached)
    cli._configure_output_encoding()
    assert detached.calls == []


def test_broker_error_maps_to_exit_code(monkeypatch):
    fake = FakeClient({"ok": False, "error": {"code": "gpu_busy", "message": "GPU 3 is busy"}})
    result = _invoke(
        monkeypatch,
        fake,
        ["apply", "--gpus", "3", "--cpus", "8", "--memory", "32", "--json"],
    )
    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "gpu_busy"


def test_broker_unavailable_maps_to_exit_code(monkeypatch):
    fake = FakeClient(error=BrokerUnavailable("socket missing"))
    result = _invoke(monkeypatch, fake, ["list", "--json"])
    assert result.exit_code == 6
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "broker_unavailable"


def test_duplicate_gpu_indices_are_rejected_before_request(monkeypatch):
    fake = FakeClient()
    result = _invoke(
        monkeypatch,
        fake,
        ["apply", "--gpus", "3,3", "--cpus", "8", "--memory", "32"],
    )
    assert result.exit_code == 2
    assert fake.calls == []


def test_status_passes_optional_allocation_id(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "allocation": {
                    "id": "alloc-1",
                    "owner": "alice",
                    "status": "active",
                    "gpus": [3],
                    "cpus": 8.0,
                    "memory_gb": 32,
                    "workspace": "/data1/citespec-users/alice",
                    "starts_at": "2026-10-22T00:00:00+00:00",
                    "expires_at": "2026-10-22T12:00:00+00:00",
                    "remaining_seconds": 3600,
                },
                "events": [],
            },
        }
    )
    result = _invoke(monkeypatch, fake, ["status", "alloc-1", "--json"])
    assert result.exit_code == 0
    assert fake.calls == [("status", {"id": "alloc-1"})]


def test_admin_users_requires_no_extra_client_logic(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {"users": [{"uid": 1000, "username": "alice", "role": "member"}]},
        }
    )
    result = _invoke(monkeypatch, fake, ["admin", "users", "--json"])
    assert result.exit_code == 0
    assert fake.calls == [("admin_users", {})]


def test_admin_portal_create_uses_cli_only_broker_command(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "username": "firstadmin",
                "role": "admin",
                "activation_code": "one-time-code",
                "expires_at": "2026-10-28T12:00:00+00:00",
                "user": {"role": "admin"},
            },
        }
    )
    result = _invoke(
        monkeypatch,
        fake,
        [
            "admin",
            "portal",
            "create",
            "--username",
            "firstadmin",
            "--role",
            "admin",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert fake.calls == [
        (
            "admin_portal_accounts_create",
            {
                "username": "firstadmin",
                "role": "admin",
                "member_id": "",
                "linux_username": "",
                "display_name": "",
                "contact": "",
            },
        )
    ]


def test_admin_portal_set_role_requires_role(monkeypatch):
    fake = FakeClient({"ok": True, "data": {}})
    result = _invoke(
        monkeypatch,
        fake,
        ["admin", "portal", "set-role", "--username", "firstadmin", "--json"],
    )
    assert result.exit_code != 0
    assert fake.calls == []


def test_admin_portal_list_formats_accounts(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "accounts": [
                    {
                        "username": "firstadmin",
                        "role": "admin",
                        "status": "active",
                        "member_id": "",
                        "linux_username": "",
                    }
                ]
            },
        }
    )
    result = _invoke(monkeypatch, fake, ["admin", "portal", "list"])
    assert result.exit_code == 0
    assert "firstadmin" in result.output
    assert "role=admin" in result.output


def test_admin_portal_auth_list_and_remove_use_cli_commands(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "admins": [
                    {
                        "username": "labadmin",
                        "enabled": True,
                        "last_used_at": "",
                        "locked_until": "",
                    }
                ]
            },
        }
    )
    result = _invoke(monkeypatch, fake, ["admin", "portal-auth", "list"])
    assert result.exit_code == 0
    assert "labadmin" in result.output
    assert fake.calls == [("admin_portal_auth_list", {})]

    fake.response = {"ok": True, "data": {"username": "labadmin", "removed": True}}
    result = _invoke(
        monkeypatch,
        fake,
        ["admin", "portal-auth", "remove", "--username", "labadmin", "--json"],
    )
    assert result.exit_code == 0
    assert fake.calls[-1] == ("admin_portal_auth_remove", {"username": "labadmin"})


def test_admin_portal_auth_enroll_prompts_without_exposing_password(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "username": "labadmin",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "otp_uri": "otpauth://totp/example",
            },
        }
    )
    monkeypatch.setattr(cli, "_make_client", lambda config: fake)
    result = runner.invoke(
        cli.app,
        [
            "--config",
            str(CONFIG),
            "admin",
            "portal-auth",
            "enroll",
            "--username",
            "labadmin",
            "--json",
        ],
        input="broker admin password 123\n",
    )
    assert result.exit_code == 0, result.output
    command, args = fake.calls[0]
    assert command == "admin_portal_auth_enroll"
    assert args == {
        "username": "labadmin",
        "password": "broker admin password 123",
    }
    assert "broker admin password 123" not in result.output


def test_release_command(monkeypatch):
    fake = FakeClient({"ok": True, "data": {"id": "alloc-1", "status": "released"}})
    result = _invoke(monkeypatch, fake, ["release", "alloc-1", "--json"])
    assert result.exit_code == 0
    assert fake.calls == [("release", {"id": "alloc-1"})]


def test_admin_release_requires_reason(monkeypatch):
    fake = FakeClient({"ok": True, "data": {"id": "alloc-1", "status": "released"}})
    result = _invoke(
        monkeypatch,
        fake,
        ["admin", "release", "alloc-1", "--reason", "maintenance", "--json"],
    )
    assert result.exit_code == 0
    assert fake.calls == [("admin_release", {"id": "alloc-1", "reason": "maintenance"})]


def test_admin_reconcile(monkeypatch):
    fake = FakeClient({"ok": True, "data": {"kept": 1, "lost": 0}})
    result = _invoke(monkeypatch, fake, ["admin", "reconcile", "--json"])
    assert result.exit_code == 0
    assert fake.calls == [("admin_reconcile", {})]


def test_apply_allows_cpu_only(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "id": "alloc-cpu",
                "status": "active",
                "gpus": [],
                "expires_at": "2026-10-22T13:00:00+00:00",
                "workspace": "/data1/citespec-users/alice",
            },
        }
    )
    result = _invoke(
        monkeypatch,
        fake,
        ["apply", "--cpus", "4", "--memory", "8", "--json"],
    )
    assert result.exit_code == 0
    command, args = fake.calls[0]
    assert command == "apply"
    assert args["gpus"] == []
    assert args["cpus"] == 4.0
    assert args["memory_gb"] == 8


def test_config_path_uses_environment(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("CITESPEC_RESOURCE_CONFIG", str(path))
    assert cli._config_path(None) == path


def test_logs_command(monkeypatch):
    fake = FakeClient({"ok": True, "data": {"id": "alloc-1", "logs": "line one\nline two\n"}})
    result = _invoke(monkeypatch, fake, ["logs", "alloc-1", "--tail", "10"])
    assert result.exit_code == 0
    assert "line one" in result.output
    assert fake.calls == [("logs", {"id": "alloc-1", "tail": 10})]


def test_gui_command_uses_ssh_aliases(monkeypatch):
    from citespec_resource.gui import server as gui_server

    captured: dict = {}

    def fake_run_gui(config, **kwargs):
        captured["config"] = config
        captured.update(kwargs)

    monkeypatch.setattr(gui_server, "run_gui", fake_run_gui)
    result = _invoke(
        monkeypatch,
        FakeClient(),
        ["gui", "--server", "member-alias", "--admin-server", "admin-alias", "--no-browser"],
    )
    assert result.exit_code == 0
    assert captured["member_alias"] == "member-alias"
    assert captured["admin_alias"] == "admin-alias"
    assert captured["open_browser"] is False


def test_job_start_passes_command_argv(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "id": "alloc-1",
                "name": "train",
                "session": "citespec-train",
                "log": "/data1/citespec-users/alice/.citespec-logs/train.log",
            },
        }
    )
    result = _invoke(
        monkeypatch,
        fake,
        ["job", "start", "alloc-1", "--name", "train", "--json", "--", "python", "train.py"],
    )
    assert result.exit_code == 0, result.output
    command, args = fake.calls[0]
    assert command == "job_start"
    assert args["id"] == "alloc-1"
    assert args["name"] == "train"
    assert args["command"] == ["python", "train.py"]


def test_gui_job_start_argv_parses_as_cli(monkeypatch):
    """End-to-end: SSHRunner._argv output must parse as the CLI expects.

    Regression test for the GUI job start bug: --json was appended after the
    `--` separator and ended up inside the training command.
    """
    import shlex

    from citespec_resource.config import load_config
    from citespec_resource.gui import server as gui_server

    config = load_config(CONFIG)
    ssh_runner = gui_server.SSHRunner(config, "member-alias", None)
    argv = ssh_runner._argv(
        "member-alias",
        ["job", "start", "alloc-1", "--name", "train", "--", "python", "train.py", "-e", "10"],
    )
    remote_tokens = shlex.split(argv[-1])
    assert remote_tokens[0] == "citespec-resource"
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "name": "train",
                "session": "citespec-train",
                "log": "/workspace/log",
            },
        }
    )
    monkeypatch.setattr(cli, "_make_client", lambda cfg: fake)
    result = runner.invoke(cli.app, ["--config", str(CONFIG), *remote_tokens[1:]])
    assert result.exit_code == 0, result.output
    assert fake.calls == [
        (
            "job_start",
            {
                "id": "alloc-1",
                "name": "train",
                "command": ["python", "train.py", "-e", "10"],
            },
        )
    ]


def test_job_start_shell_flag(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "name": "train",
                "session": "citespec-train",
                "log": "/workspace/log",
            },
        }
    )
    result = _invoke(
        monkeypatch,
        fake,
        [
            "job",
            "start",
            "alloc-1",
            "--name",
            "train",
            "--shell",
            "python train.py --output 'one two'",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.calls == [
        (
            "job_start",
            {
                "id": "alloc-1",
                "name": "train",
                "command": "python train.py --output 'one two'",
            },
        )
    ]


def test_job_start_rejects_shell_and_positional_together(monkeypatch):
    fake = FakeClient()
    result = _invoke(
        monkeypatch,
        fake,
        ["job", "start", "alloc-1", "--name", "train", "--shell", "x", "--", "y"],
    )
    assert result.exit_code != 0
    assert fake.calls == []


def test_job_files_command(monkeypatch):
    fake = FakeClient(
        {"ok": True, "data": {"id": "alloc-1", "files": [{"name": "train.log", "bytes": 42}]}}
    )
    result = _invoke(monkeypatch, fake, ["job", "files", "alloc-1"])
    assert result.exit_code == 0, result.output
    assert "train.log" in result.output
    assert "42 bytes" in result.output
    assert fake.calls == [("job_files", {"id": "alloc-1"})]


def test_remote_mode_relays_command(monkeypatch):
    import subprocess as subprocess_module
    import sys as sys_module

    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        return subprocess_module.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    # _dispatch_remote reads sys.argv directly; CliRunner does not patch it.
    monkeypatch.setattr(
        sys_module,
        "argv",
        ["citespec-resource", "--config", str(CONFIG), "--remote", "example-gpu", "queue", "list"],
    )
    result = runner.invoke(
        cli.app, ["--config", str(CONFIG), "--remote", "example-gpu", "queue", "list"]
    )
    assert result.exit_code == 0
    assert recorded["argv"][-2] == "example-gpu"
    assert recorded["argv"][-1] == "citespec-resource queue list"
    assert "--config" not in recorded["argv"][-1]


def test_remote_mode_rejects_local_only_commands(monkeypatch):
    for head in ("shell", "vscode", "gui", "tunnel", "file", "connect"):
        result = runner.invoke(cli.app, ["--remote", "example-gpu", head, "alloc-1"])
        assert result.exit_code != 0, head


def test_start_fallback_translation_covers_documented_defaults():
    translated = cli._start_as_apply(["start", "--hours", "4", "--json"])
    assert translated[:2] == ["apply", "--hours"]
    assert "--gpu-count" in translated and translated[translated.index("--gpu-count") + 1] == "1"
    assert translated[translated.index("--cpus") + 1] == "8"
    assert translated[translated.index("--memory") + 1] == "32"
    assert "--json" in translated


def test_start_fallback_translation_keeps_zero_gpu_count():
    translated = cli._start_as_apply(["start", "--gpu-count", "0"])
    assert translated is not None
    assert translated[translated.index("--gpu-count") + 1] == "0"


def test_start_fallback_translation_rejects_presets_and_unknown_options():
    assert cli._start_as_apply(["start", "--preset", "standard"]) is None
    assert cli._start_as_apply(["start", "--future-option", "x"]) is None


def test_start_falls_back_to_apply_when_server_lacks_it(monkeypatch):
    import subprocess as subprocess_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if len(calls) == 1:
            return subprocess_module.CompletedProcess(
                argv,
                2,
                stdout=b"",
                stderr="citespec-resource: 不支持的子命令 start\n".encode(),
            )
        return subprocess_module.CompletedProcess(argv, 0, stdout=b'{"ok": true}\n', stderr=b"")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(cli, "_relay_bytes", lambda stream, data: None)
    with pytest.raises(SystemExit) as exited:
        cli._dispatch_remote("example-gpu", ["start", "--gpu-count", "1", "--hours", "4", "--json"])
    assert exited.value.code == 0
    assert calls[0][1].get("capture_output") is True
    fallback = calls[1][0][-1]
    assert fallback.startswith("citespec-resource apply")
    assert "--gpu-count 1" in fallback
    assert "--hours 4" in fallback
    assert "--cpus 8" in fallback
    assert "--memory 32" in fallback
    assert "--json" in fallback


def test_start_relays_unchanged_when_server_supports_it(monkeypatch):
    import subprocess as subprocess_module

    calls = []
    relayed = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess_module.CompletedProcess(argv, 0, stdout=b"queued\n", stderr=b"")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(cli, "_relay_bytes", lambda stream, data: relayed.append(data))
    with pytest.raises(SystemExit) as exited:
        cli._dispatch_remote("example-gpu", ["start", "--gpu-count", "1"])
    assert exited.value.code == 0
    assert len(calls) == 1
    assert relayed == [b"queued\n", b""]


def test_start_fallback_reports_when_preset_cannot_be_translated(monkeypatch, capsys):
    import subprocess as subprocess_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess_module.CompletedProcess(
            argv,
            2,
            stdout=b"",
            stderr="citespec-resource: 不支持的子命令 start\n".encode(),
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(cli, "_relay_bytes", lambda stream, data: None)
    with pytest.raises(SystemExit) as exited:
        cli._dispatch_remote("example-gpu", ["start", "--preset", "standard"])
    assert exited.value.code == 2
    assert len(calls) == 1
    assert "apply" in capsys.readouterr().err


def test_connect_command_builds_ssh_relay(monkeypatch):
    import subprocess as subprocess_module

    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        return subprocess_module.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    result = _invoke(monkeypatch, FakeClient(), ["connect", "alloc-1", "--job", "train"])
    assert result.exit_code == 0
    assert "-t" in recorded["argv"] and recorded["argv"].count("-t") == 2
    assert recorded["argv"][-1] == "citespec-resource shell alloc-1 --job train"


def test_connect_command_rejects_bad_job_name(monkeypatch):
    result = _invoke(monkeypatch, FakeClient(), ["connect", "alloc-1", "--job", "bad job"])
    assert result.exit_code != 0


def test_remote_mode_rejects_job_attach(monkeypatch):
    result = runner.invoke(cli.app, ["--remote", "example-gpu", "job", "attach", "alloc-1"])
    assert result.exit_code != 0


def test_vscode_tunnel_cleans_up_on_probe_failure(tmp_path, monkeypatch):
    """A failed probe must still remove the managed SSH block."""
    import asyncio as asyncio_module

    from citespec_resource.config import load_config

    async def failing_probe(host, ssh_config, timeout):
        raise RuntimeError("probe boom")

    monkeypatch.setattr(cli, "_probe_container_ssh", failing_probe)
    config = load_config(CONFIG)
    ssh_config_path = tmp_path / "config"
    with pytest.raises(RuntimeError, match="probe boom"):
        asyncio_module.run(
            cli._serve_vscode_tunnel(
                config=config,
                allocation_id="abc123",
                username="alice",
                ssh_alias="example-gpu",
                host="citespec-abc123",
                requested_port=0,
                ssh_config=ssh_config_path,
                identity=None,
                launch_code=True,
            )
        )
    text = ssh_config_path.read_text(encoding="utf-8") if ssh_config_path.exists() else ""
    assert "citespec-abc123" not in text
    # The block lives in the managed include file, which must be gone too,
    # and the Include directive we added must be removed again.
    managed = ssh_config_path.parent / "citespec-vscode-config"
    assert not managed.exists() or "citespec-abc123" not in managed.read_text(encoding="utf-8")
    assert "citespec-vscode-config" not in text


def test_sweep_stale_managed_blocks(tmp_path):
    import socket as socket_module

    live = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    live.bind(("127.0.0.1", 0))
    live.listen(1)
    live_port = live.getsockname()[1]
    try:
        config = tmp_path / "config"
        config.write_text(
            "# BEGIN CITESPEC VSCODE dead1\n"
            "Host citespec-dead1\n"
            "  HostName 127.0.0.1\n"
            "  Port 1\n"
            "# END CITESPEC VSCODE dead1\n"
            "\n"
            f"# BEGIN CITESPEC VSCODE live1\n"
            "Host citespec-live1\n"
            "  HostName 127.0.0.1\n"
            f"  Port {live_port}\n"
            "# END CITESPEC VSCODE live1\n",
            encoding="utf-8",
        )
        removed = cli._sweep_stale_managed_blocks(config)
        assert removed == 1
        text = config.read_text(encoding="utf-8")
        assert "dead1" not in text
        assert "citespec-live1" in text
    finally:
        live.close()


def test_managed_include_wins_over_host_wildcard(tmp_path, monkeypatch):
    """Our per-allocation block must beat an earlier `Host *` section."""
    import shutil as shutil_module
    import subprocess as subprocess_module

    ssh = shutil_module.which("ssh")
    if ssh is None:
        pytest.skip("an ssh client is required")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    # ssh resolves the relative Include against its own home directory; point
    # HOME/USERPROFILE at the test home so both ssh builds agree with the
    # standard ~/.ssh layout that _include_directive detects.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    user_config = home / ".ssh" / "config"
    user_config.write_text("Host *\n  User otheruser\n", encoding="utf-8")
    block = cli._managed_block("abc123", "citespec-abc123", 22222, "alice", None)
    added = cli._install_managed_block(user_config, "abc123", block)
    assert added is True
    try:
        result = subprocess_module.run(
            [ssh, "-G", "-F", str(user_config), "citespec-abc123"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        values = {}
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] not in values:
                values[parts[0]] = parts[1]
        assert values.get("user") == "alice"
        assert values.get("port") == "22222"
    finally:
        cli._remove_managed_block(user_config, "abc123", own_port=22222)
    text = user_config.read_text(encoding="utf-8")
    assert "citespec-vscode-config" not in text


def test_vscode_config_removal_respects_live_block(tmp_path):
    """Reopening the same allocation overwrites the block with a new port;
    the older process's exit must not delete a block another live tunnel
    process owns."""
    import socket as socket_module

    live = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    live.bind(("127.0.0.1", 0))
    live.listen(1)
    live_port = live.getsockname()[1]
    try:
        config = tmp_path / "config"
        block = cli._managed_block("abc123", "citespec-abc123", live_port, "alice", None)
        assert cli._install_managed_block(config, "abc123", block) is True

        # A newer process overwrote the port and is still running: the older
        # process (own_port is dead) must leave the block alone.
        cli._remove_managed_block(config, "abc123", own_port=1)
        managed = config.parent / "citespec-vscode-config"
        assert "citespec-abc123" in managed.read_text(encoding="utf-8")
        assert "citespec-vscode-config" in config.read_text(encoding="utf-8")

        # Once the port is dead the block is stale and gets removed along
        # with the Include directive we added.
        cli._remove_managed_block(config, "abc123", own_port=live_port)
        assert not managed.exists() or "citespec-abc123" not in managed.read_text(encoding="utf-8")
        assert "citespec-vscode-config" not in config.read_text(encoding="utf-8")
    finally:
        live.close()


def test_vscode_config_install_is_locked(tmp_path):
    """Concurrent installers serialize on the lock file (no lost updates)."""
    import threading

    if os.name == "posix":
        started = threading.Event()
        release = threading.Event()
        # Hold a reference for the thread's lifetime: a discarded context
        # manager is closed by the garbage collector, releasing the lock.
        holder_cm = cli._managed_config_lock(tmp_path / "c", timeout=5)

        def hold():
            with holder_cm:
                started.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert started.wait(5)
            blocked = False
            try:
                with cli._managed_config_lock(tmp_path / "c", timeout=0.2):
                    pass
            except OSError:
                blocked = True
            assert blocked
        finally:
            release.set()
            holder.join(5)
        # After release the lock is acquirable again.
        with cli._managed_config_lock(tmp_path / "c", timeout=5):
            pass
    else:
        # Windows: exercise acquire/release ordering only (msvcrt.locking on
        # the same file descriptor set would deadlock within one process).
        with cli._managed_config_lock(tmp_path / "c", timeout=5):
            pass


_BASE_CONFIG = cli.load_config(CONFIG)


def _no_socket_config(tmp_path):
    from dataclasses import replace

    # A member workstation has no broker runtime directory at all; keep the
    # socket under a directory that does not exist so the CLI treats it as a
    # remote relay instead of a server whose broker is down.
    return replace(
        _BASE_CONFIG,
        server=replace(
            _BASE_CONFIG.server,
            broker_socket=str(tmp_path / "runtime" / "absent.sock"),
        ),
    )


def test_version_json_reports_protocol_handshake():
    result = runner.invoke(cli.app, ["version", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["version"]
    assert payload["data"]["protocol"] >= 1
    assert payload["data"]["min_client_protocol"] >= 1


def test_update_check_json_uses_packaged_root(tmp_path, monkeypatch):
    from citespec_resource import update

    monkeypatch.setattr(update, "find_app_root", lambda: tmp_path)
    monkeypatch.setattr(
        update,
        "check_for_update",
        lambda **kwargs: update.UpdateInfo(
            available=True,
            current_version="0.1.0",
            latest_version="0.2.0",
            reason="new version available",
        ),
    )
    result = runner.invoke(cli.app, ["update", "--check", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["data"]["available"] is True
    assert payload["data"]["latest"] == "0.2.0"


def test_update_installs_and_reports_previous(tmp_path, monkeypatch):
    from citespec_resource import update

    monkeypatch.setattr(update, "find_app_root", lambda: tmp_path)
    monkeypatch.setattr(
        update,
        "check_for_update",
        lambda **kwargs: update.UpdateInfo(
            available=True,
            current_version="0.1.0",
            latest_version="0.2.0",
            reason="new version available",
        ),
    )
    monkeypatch.setattr(
        update,
        "install_update",
        lambda info, **kwargs: update.UpdateResult("0.2.0", tmp_path / "versions", "0.1.0"),
    )
    result = runner.invoke(cli.app, ["update", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["data"] == {
        "previous": "0.1.0",
        "updated": True,
        "version": "0.2.0",
    }


def test_update_requires_packaged_app_root(monkeypatch):
    from citespec_resource import update

    monkeypatch.setattr(update, "find_app_root", lambda: None)
    result = runner.invoke(cli.app, ["update", "--check"])
    assert result.exit_code != 0
    assert "packaged client" in result.output


def test_client_profile_still_relays_start(tmp_path, monkeypatch):
    import subprocess as subprocess_module
    import sys as sys_module

    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        return subprocess_module.CompletedProcess(argv, 0)

    def missing_hardware(path):
        raise ValueError("missing [hardware] section")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "load_config", missing_hardware)
    monkeypatch.setattr(cli, "load_client_config", lambda path: _no_socket_config(tmp_path))
    monkeypatch.setattr(
        sys_module,
        "argv",
        ["citespec-resource", "start", "--gpu-count", "1", "--json"],
    )
    result = runner.invoke(cli.app, ["start", "--gpu-count", "1", "--json"])
    assert result.exit_code == 0
    assert recorded["argv"][-1] == "citespec-resource start --gpu-count 1 --json"


def test_workstation_defaults_to_relay_without_socket(tmp_path, monkeypatch):
    import subprocess as subprocess_module
    import sys as sys_module

    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        return subprocess_module.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "load_config", lambda path: _no_socket_config(tmp_path))
    monkeypatch.setattr(
        sys_module,
        "argv",
        ["citespec-resource", "--config", str(CONFIG), "list"],
    )
    result = runner.invoke(cli.app, ["--config", str(CONFIG), "list"])
    assert result.exit_code == 0
    config = _no_socket_config(tmp_path)
    assert recorded["argv"][-2] == config.server.ssh_alias
    assert recorded["argv"][-1] == "citespec-resource list"


def test_missing_socket_on_the_server_does_not_relay(tmp_path, monkeypatch):
    """A broker restart briefly removes the socket; the server must not SSH to
    itself (it fails with "Could not resolve hostname <alias>" instead of saying
    the broker is unavailable)."""

    import subprocess as subprocess_module
    import sys as sys_module
    from dataclasses import replace

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess_module.CompletedProcess(argv, 0)

    runtime = tmp_path / "run" / "citespec-resource"
    runtime.mkdir(parents=True)
    config = replace(
        _BASE_CONFIG,
        server=replace(_BASE_CONFIG.server, broker_socket=str(runtime / "broker.sock")),
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(sys_module, "argv", ["citespec-resource", "list"])

    result = runner.invoke(cli.app, ["list"])

    assert calls == []
    assert result.exit_code == 6
    assert "broker_unavailable" in (result.output + result.stderr)


def test_local_flag_forces_socket_mode(tmp_path, monkeypatch):
    import subprocess as subprocess_module
    import sys as sys_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess_module.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "load_config", lambda path: _no_socket_config(tmp_path))
    monkeypatch.setattr(
        sys_module,
        "argv",
        ["citespec-resource", "--config", str(CONFIG), "--local", "list"],
    )
    # --local means the CLI must not relay: no ssh call happens (the local
    # socket connection itself fails with the usual broker-unavailable error).
    runner.invoke(cli.app, ["--config", str(CONFIG), "--local", "list"])
    assert calls == []


def test_no_relay_when_socket_exists(tmp_path, monkeypatch):
    import subprocess as subprocess_module
    import sys as sys_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess_module.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    config = _no_socket_config(tmp_path)
    socket_path = Path(config.server.broker_socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.write_bytes(b"")
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        sys_module,
        "argv",
        ["citespec-resource", "--config", str(CONFIG), "list"],
    )
    runner.invoke(cli.app, ["--config", str(CONFIG), "list"])
    assert calls == []


def test_help_is_not_relayed(tmp_path, monkeypatch):
    import subprocess as subprocess_module
    import sys as sys_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess_module.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "load_config", lambda path: _no_socket_config(tmp_path))
    monkeypatch.setattr(
        sys_module,
        "argv",
        ["citespec-resource", "--config", str(CONFIG), "admin", "members", "--help"],
    )
    result = runner.invoke(cli.app, ["--config", str(CONFIG), "admin", "members", "--help"])
    assert result.exit_code == 0
    assert calls == []


def test_me_without_subcommand_queries_status(monkeypatch):
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "username": "alice",
                "uid": 1000,
                "membership": {"status": "active", "valid_until": ""},
                "keys": [],
            },
        }
    )
    result = _invoke(monkeypatch, fake, ["me", "--json"])
    assert result.exit_code == 0
    assert fake.calls[0][0] == "me"


def test_setup_works_with_minimal_connection_args(monkeypatch, tmp_path):
    from citespec_resource import onboarding

    captured = {}

    def fake_setup(config, *, host, yes, expected_fingerprint=""):
        captured["config"] = config
        captured["host"] = host
        captured["yes"] = yes
        captured["fingerprint"] = expected_fingerprint
        return 0

    monkeypatch.setattr(onboarding, "run_setup", fake_setup)
    result = runner.invoke(
        cli.app,
        [
            "--config",
            str(tmp_path / "missing.toml"),
            "setup",
            "--server",
            "lab.example.invalid",
            "--user",
            "alice",
            "--port",
            "2222",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["host"] == "lab.example.invalid"
    assert captured["config"].server.ssh_alias == "lab.example.invalid"
    assert captured["config"].server.ssh_user == "alice"
    assert captured["config"].server.ssh_port == 2222


def test_client_profile_without_hardware_is_loaded(tmp_path, monkeypatch):
    profile = tmp_path / "client.toml"
    profile.write_text(
        "\n".join(
            [
                "[server]",
                'name = "lab"',
                'ssh_alias = "lab"',
                'ssh_user = "alice"',
                "ssh_port = 22",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake = FakeClient(
        {
            "ok": True,
            "data": {
                "username": "alice",
                "uid": 1000,
                "membership": {"status": "active", "valid_until": ""},
                "keys": [],
            },
        }
    )
    monkeypatch.setattr(cli, "_make_client", lambda config: fake)
    result = runner.invoke(cli.app, ["--config", str(profile), "me", "--json"])
    assert result.exit_code == 0, result.output
    assert fake.calls[0][0] == "me"


def test_me_keys_accepts_positional_file_and_key_id(monkeypatch, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest test\n", encoding="utf-8")
    fake = FakeClient({"ok": True, "data": {"fingerprint": "SHA256:test", "key_id": "k1"}})
    result = _invoke(monkeypatch, fake, ["me", "keys", "add", str(key)])
    assert result.exit_code == 0
    assert fake.calls[0][0] == "me_keys_add"
    assert fake.calls[0][1]["public_key"].startswith("ssh-ed25519 ")

    result = _invoke(monkeypatch, fake, ["me", "keys", "revoke", "k1", "--json"])
    assert result.exit_code == 0
    assert fake.calls[1][0] == "me_keys_revoke"
    assert fake.calls[1][1]["key_id"] == "k1"


def test_join_portal_does_not_require_local_config(monkeypatch, tmp_path):
    import sys as sys_module

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            payload = {"ok": True, "data": {"request_id": "r1", "receipt_secret": "s"}}
            return json.dumps(payload).encode()

    def fake_urlopen(request, timeout=0):
        assert request.full_url.endswith("/api/requests")
        return Response()

    monkeypatch.setattr(sys_module, "argv", ["citespec-resource", "join"])
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret")
    # A missing local config must not block joining through --portal.
    result = runner.invoke(
        cli.app,
        [
            "--config",
            str(tmp_path / "missing.toml"),
            "join",
            "--portal",
            "https://lab.example.invalid/compute",
            "--invite-id",
            "invite-1",
        ],
        input="\n".join(
            [
                "Alice",
                "alice",
                "Lab",
                "research",
                "alice@example.invalid",
                "",
                "n",
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest test",
                "secret",
            ]
        )
        + "\n",
    )
    assert result.exit_code == 0, result.output
    assert "申请已提交" in result.output


def test_attach_is_never_relayed(tmp_path, monkeypatch):
    import subprocess as subprocess_module
    import sys as sys_module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess_module.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "load_config", lambda path: _no_socket_config(tmp_path))
    monkeypatch.setattr(
        sys_module,
        "argv",
        ["citespec-resource", "--config", str(CONFIG), "job", "attach", "alloc-1"],
    )
    runner.invoke(cli.app, ["--config", str(CONFIG), "job", "attach", "alloc-1"])
    assert calls == []


def test_vscode_config_include_survives_other_allocation(tmp_path):
    """Closing allocation A must not strand allocation B's tunnel alias."""
    config = tmp_path / "config"
    block_a = cli._managed_block("aaaa", "citespec-aaaa", 22001, "alice", None)
    block_b = cli._managed_block("bbbb", "citespec-bbbb", 22002, "alice", None)
    assert cli._install_managed_block(config, "aaaa", block_a) is True
    assert cli._install_managed_block(config, "bbbb", block_b) is False

    # A exits: its block goes, but B's block keeps the file non-empty, so the
    # Include directive must survive.
    cli._remove_managed_block(config, "aaaa", own_port=22001)
    managed = config.parent / "citespec-vscode-config"
    text = config.read_text(encoding="utf-8")
    assert "citespec-vscode-config" in text
    assert "citespec-bbbb" in managed.read_text(encoding="utf-8")

    # B exits last: the file empties and the Include goes with it.
    cli._remove_managed_block(config, "bbbb", own_port=22002)
    assert not managed.exists() or "citespec-bbbb" not in managed.read_text(encoding="utf-8")
    assert "citespec-vscode-config" not in config.read_text(encoding="utf-8")


def test_sweep_clears_include_when_all_blocks_stale(tmp_path):
    config = tmp_path / "config"
    block = cli._managed_block("cccc", "citespec-cccc", 1, "alice", None)
    cli._install_managed_block(config, "cccc", block)
    removed = cli._sweep_stale_managed_blocks(
        config.parent / "citespec-vscode-config",
        config,
    )
    assert removed == 1
    assert "citespec-vscode-config" not in config.read_text(encoding="utf-8")


def test_probe_timeout_kills_ssh_child(tmp_path, monkeypatch):
    """A probe timeout must terminate and reap the ssh child process."""

    class FakeAsyncProcess:
        def __init__(self):
            self.killed = False
            self.reaped = False
            self.returncode = None

        async def communicate(self):
            import asyncio as asyncio_module

            await asyncio_module.sleep(30)
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.reaped = True
            return -9

    fake = FakeAsyncProcess()

    async def fake_exec(*argv, **kwargs):
        return fake

    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", fake_exec)
    import asyncio as asyncio_module

    with pytest.raises(RuntimeError, match="timed out"):
        asyncio_module.run(cli._probe_container_ssh("citespec-x", tmp_path / "config", 1))
    assert fake.killed and fake.reaped
