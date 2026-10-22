from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from udc_resource.gui import server as gui_server
from udc_resource.login_wrapper import parse_command


class FakeRunner:
    def __init__(self, admin_alias: str | None = None) -> None:
        self.member_alias = "member-alias"
        self.admin_alias = admin_alias
        self.calls: list[tuple[str, list[str]]] = []

    async def run(self, alias: str, args: list[str]) -> dict[str, Any]:
        self.calls.append((alias, list(args)))
        if args[:2] == ["admin", "users"] and not self.admin_alias:
            return {
                "ok": False,
                "error": {"code": "forbidden", "message": "admin SSH alias is not configured"},
            }
        if args[:1] == ["list"]:
            data = {
                "server": "amax",
                "mode": "monitor",
                "gpus": [],
                "allocations": [],
                "unmanaged": {"containers": [], "processes": []},
            }
        elif args[:1] == ["status"]:
            data = {"allocations": []}
        elif args[:1] == ["apply"]:
            data = {
                "id": "alloc-1",
                "status": "active",
                "gpus": [3],
                "expires_at": "2026-10-23T00:00:00+00:00",
                "workspace": "/data1/udc-users/alice",
            }
        elif args[:1] == ["logs"]:
            data = {"id": args[1], "logs": "hello logs\n"}
        elif args[:2] == ["admin", "users"]:
            data = {"users": [{"uid": 1000, "username": "alice", "role": "member"}]}
        elif args[:2] == ["admin", "resources"]:
            data = {"server": "amax", "mode": "monitor", "gpus": [], "allocations": []}
        elif args[:2] == ["admin", "audit"]:
            data = {"events": []}
        elif args[:2] == ["admin", "mode"]:
            data = {"mode": args[2]}
        elif args[:2] == ["admin", "release"]:
            data = {"id": args[2], "status": "released"}
        elif args[:2] == ["admin", "reconcile"]:
            data = {"kept": 0}
        else:
            data = {}
        return {"ok": True, "data": data}


async def _client(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _app(monitor_config, runner: FakeRunner, admin_alias: str | None = None):
    return gui_server.create_app(
        monitor_config,
        member_alias=runner.member_alias,
        admin_alias=admin_alias,
        runner=runner,
        token="test-token",
    )


def _headers() -> dict[str, str]:
    return {"X-UDC-Token": "test-token"}


async def test_index_contains_token_and_server(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.get("/")
        assert response.status == 200
        text = await response.text()
        assert "test-token" in text
        assert monitor_config.server.name in text
    finally:
        await client.close()


async def test_api_requires_token(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        denied = await client.get("/api/overview")
        assert denied.status == 403
        allowed = await client.get("/api/overview", headers=_headers())
        assert allowed.status == 200
        assert runner.calls == [("member-alias", ["list"])]
    finally:
        await client.close()


async def test_apply_builds_cli_arguments(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.post(
            "/api/apply",
            headers=_headers(),
            data=json.dumps(
                {
                    "gpus": [3, 5],
                    "cpus": 8,
                    "memory_gb": 32,
                    "hours": 12,
                    "image": "udc-base:2026.10",
                    "note": "test",
                }
            ),
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["ok"] is True
        assert runner.calls[0][1] == [
            "apply",
            "--gpus",
            "3,5",
            "--cpus",
            "8",
            "--memory",
            "32",
            "--hours",
            "12",
            "--image",
            "udc-base:2026.10",
            "--note",
            "test",
        ]
    finally:
        await client.close()


async def test_logs_endpoint(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.get("/api/logs/alloc-1?tail=20", headers=_headers())
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["logs"] == "hello logs\n"
        assert runner.calls[0][1] == ["logs", "alloc-1", "--tail", "20"]
    finally:
        await client.close()


async def test_admin_disabled_without_alias(monitor_config):
    runner = FakeRunner(admin_alias=None)
    client = await _client(_app(monitor_config, runner, admin_alias=None))
    try:
        response = await client.get("/api/admin/users", headers=_headers())
        payload = await response.json()
        assert payload["ok"] is False
        assert payload["error"]["code"] == "forbidden"
        assert runner.calls == []
    finally:
        await client.close()


async def test_admin_uses_admin_alias(monitor_config):
    runner = FakeRunner(admin_alias="admin-alias")
    client = await _client(_app(monitor_config, runner, admin_alias="admin-alias"))
    try:
        response = await client.get("/api/admin/users", headers=_headers())
        payload = await response.json()
        assert payload["ok"] is True
        assert runner.calls == [("admin-alias", ["admin", "users"])]
    finally:
        await client.close()


@pytest.mark.parametrize("alias", ["bad alias", "-oProxyCommand=evil", ""])
def test_ssh_runner_rejects_invalid_aliases(monitor_config, monkeypatch, alias):
    monkeypatch.setattr(gui_server.shutil, "which", lambda name: "ssh")
    runner = gui_server.SSHRunner(monitor_config, "member", None)
    assert runner._argv(alias, ["list"]) is None


def test_ssh_runner_builds_json_cli_command(monitor_config, monkeypatch):
    monkeypatch.setattr(gui_server.shutil, "which", lambda name: "ssh")
    runner = gui_server.SSHRunner(monitor_config, "member-alias", None)
    argv = runner._argv("member-alias", ["list"])
    assert argv is not None
    assert argv[-2] == "member-alias"
    assert argv[-1] == "udc-resource list --json"
    assert "BatchMode=yes" in argv


async def test_vendored_xterm_assets_are_served(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        for path in (
            "/static/vendor/xterm.js",
            "/static/vendor/xterm.css",
            "/static/vendor/addon-fit.js",
            "/static/app.js",
            "/static/app.css",
        ):
            response = await client.get(path)
            assert response.status == 200, path
    finally:
        await client.close()


@pytest.mark.parametrize(
    "note",
    [
        "train new model",
        "quote ' and \" inside",
        "train; rm -rf /tmp/x",
        "a && b | c",
        "模型 训练",
        "$(whoami)",
    ],
)
def test_ssh_runner_preserves_note_through_forcecommand(monitor_config, monkeypatch, note):
    monkeypatch.setattr(gui_server.shutil, "which", lambda name: "ssh")
    runner = gui_server.SSHRunner(monitor_config, "member-alias", None)
    argv = runner._argv("member-alias", ["apply", "--gpus", "3", "--note", note])
    assert argv is not None
    remote = argv[-1]
    assert isinstance(remote, str)
    assert parse_command(remote) == [
        "udc-resource",
        "apply",
        "--gpus",
        "3",
        "--note",
        note,
        "--json",
    ]
