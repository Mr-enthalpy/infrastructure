import json
from pathlib import Path

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
