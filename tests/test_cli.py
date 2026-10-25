import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from udc_resource import cli
from udc_resource.errors import BrokerUnavailable

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
                "server": "amax",
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
    assert payload["data"]["server"] == "amax"
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
                "workspace": "/data1/udc-users/alice",
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
                "workspace": "/data1/udc-users/alice",
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
                    "workspace": "/data1/udc-users/alice",
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
                    "workspace": "/data1/udc-users/alice",
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
                "workspace": "/data1/udc-users/alice",
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
    monkeypatch.setenv("UDC_RESOURCE_CONFIG", str(path))
    assert cli._config_path(None) == path


def test_logs_command(monkeypatch):
    fake = FakeClient({"ok": True, "data": {"id": "alloc-1", "logs": "line one\nline two\n"}})
    result = _invoke(monkeypatch, fake, ["logs", "alloc-1", "--tail", "10"])
    assert result.exit_code == 0
    assert "line one" in result.output
    assert fake.calls == [("logs", {"id": "alloc-1", "tail": 10})]


def test_gui_command_uses_ssh_aliases(monkeypatch):
    from udc_resource.gui import server as gui_server

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
                "session": "udc-train",
                "log": "/data1/udc-users/alice/.udc-logs/train.log",
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

    from udc_resource.config import load_config
    from udc_resource.gui import server as gui_server

    config = load_config(CONFIG)
    ssh_runner = gui_server.SSHRunner(config, "member-alias", None)
    argv = ssh_runner._argv(
        "member-alias",
        ["job", "start", "alloc-1", "--name", "train", "--", "python", "train.py", "-e", "10"],
    )
    remote_tokens = shlex.split(argv[-1])
    assert remote_tokens[0] == "udc-resource"
    fake = FakeClient(
        {"ok": True, "data": {"name": "train", "session": "udc-train", "log": "/workspace/log"}}
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
        {"ok": True, "data": {"name": "train", "session": "udc-train", "log": "/workspace/log"}}
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
        ["udc-resource", "--config", str(CONFIG), "--remote", "amax", "queue", "list"],
    )
    result = runner.invoke(cli.app, ["--config", str(CONFIG), "--remote", "amax", "queue", "list"])
    assert result.exit_code == 0
    assert recorded["argv"][-2] == "amax"
    assert recorded["argv"][-1] == "udc-resource queue list"
    assert "--config" not in recorded["argv"][-1]


def test_remote_mode_rejects_local_only_commands(monkeypatch):
    for head in ("shell", "vscode", "gui", "tunnel", "file", "connect"):
        result = runner.invoke(cli.app, ["--remote", "amax", head, "alloc-1"])
        assert result.exit_code != 0, head


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
    assert recorded["argv"][-1] == "udc-resource shell alloc-1 --job train"


def test_connect_command_rejects_bad_job_name(monkeypatch):
    result = _invoke(monkeypatch, FakeClient(), ["connect", "alloc-1", "--job", "bad job"])
    assert result.exit_code != 0


def test_remote_mode_rejects_job_attach(monkeypatch):
    result = runner.invoke(cli.app, ["--remote", "amax", "job", "attach", "alloc-1"])
    assert result.exit_code != 0


def test_vscode_tunnel_cleans_up_on_probe_failure(tmp_path, monkeypatch):
    """A failed probe must still remove the managed SSH block."""
    import asyncio as asyncio_module

    from udc_resource.config import load_config

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
                ssh_alias="amax",
                host="udc-abc123",
                requested_port=0,
                ssh_config=ssh_config_path,
                identity=None,
                launch_code=True,
            )
        )
    text = ssh_config_path.read_text(encoding="utf-8") if ssh_config_path.exists() else ""
    assert "udc-abc123" not in text


def test_sweep_stale_managed_blocks(tmp_path):
    import socket as socket_module

    live = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    live.bind(("127.0.0.1", 0))
    live.listen(1)
    live_port = live.getsockname()[1]
    try:
        config = tmp_path / "config"
        config.write_text(
            "# BEGIN UDC VSCODE dead1\n"
            "Host udc-dead1\n"
            "  HostName 127.0.0.1\n"
            "  Port 1\n"
            "# END UDC VSCODE dead1\n"
            "\n"
            f"# BEGIN UDC VSCODE live1\n"
            "Host udc-live1\n"
            "  HostName 127.0.0.1\n"
            f"  Port {live_port}\n"
            "# END UDC VSCODE live1\n",
            encoding="utf-8",
        )
        removed = cli._sweep_stale_managed_blocks(config)
        assert removed == 1
        text = config.read_text(encoding="utf-8")
        assert "dead1" not in text
        assert "udc-live1" in text
    finally:
        live.close()
