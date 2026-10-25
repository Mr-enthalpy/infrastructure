from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
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
        elif args[:2] == ["queue", "list"]:
            data = {
                "queue": [
                    {
                        "queue_id": "queue-1",
                        "status": "queued",
                        "position": 1,
                        "gpus": [3],
                        "cpus": 8,
                        "memory_gb": 32,
                    }
                ]
            }
        elif args[:2] == ["queue", "cancel"]:
            data = {"queue_id": args[2], "status": "cancelled"}
        elif args[:2] == ["job", "list"]:
            data = {"jobs": [{"name": "train", "session": "udc-train", "log": "/tmp/x"}]}
        elif args[:2] == ["job", "start"]:
            data = {
                "name": args[4],
                "session": "udc-train",
                "log": "/workspace/.udc-logs/train.log",
            }
        elif args[:2] == ["job", "files"]:
            data = {"id": args[2], "files": [{"name": "train.log", "bytes": 42}]}
        elif args[:2] == ["job", "logs"]:
            data = {"id": args[2], "name": args[4], "logs": "epoch 1\n"}
        elif args[:2] == ["job", "stop"]:
            data = {"name": args[3], "stopped": True}
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
        assert "my-queue" in text
        assert "my-jobs" in text
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


async def test_apply_no_queue_flag(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.post(
            "/api/apply",
            headers=_headers(),
            data=json.dumps(
                {
                    "gpus": [],
                    "cpus": 4,
                    "memory_gb": 8,
                    "hours": 2,
                    "queue": False,
                }
            ),
        )
        assert response.status == 200
        assert runner.calls[0][1][-1] == "--no-queue"
    finally:
        await client.close()


async def test_queue_endpoints(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        listed = await client.get("/api/queue", headers=_headers())
        assert listed.status == 200
        payload = await listed.json()
        assert payload["data"]["queue"][0]["queue_id"] == "queue-1"
        cancelled = await client.post(
            "/api/queue/cancel",
            headers=_headers(),
            data=json.dumps({"id": "queue-1", "reason": "done"}),
        )
        assert cancelled.status == 200
        assert runner.calls == [
            ("member-alias", ["queue", "list"]),
            ("member-alias", ["queue", "cancel", "queue-1", "--reason", "done"]),
        ]
    finally:
        await client.close()


async def test_jobs_endpoints(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        listed = await client.get("/api/jobs/alloc-1", headers=_headers())
        payload = await listed.json()
        assert payload["data"]["jobs"][0]["name"] == "train"
        stopped = await client.post(
            "/api/jobs/alloc-1/stop",
            headers=_headers(),
            data=json.dumps({"name": "train"}),
        )
        assert stopped.status == 200
        assert runner.calls == [
            ("member-alias", ["job", "list", "alloc-1"]),
            ("member-alias", ["job", "stop", "alloc-1", "--name", "train"]),
        ]
    finally:
        await client.close()


async def test_config_exposes_vscode_policy(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.get("/api/config", headers=_headers())
        payload = await response.json()
        assert payload["data"]["vscode_enabled"] is False
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


async def test_job_start_and_persistent_log_endpoints(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        started = await client.post(
            "/api/jobs/alloc-1/start",
            headers=_headers(),
            data=json.dumps({"name": "train", "command": "python train.py --output 'one two'"}),
        )
        assert started.status == 200
        payload = await started.json()
        assert payload["ok"] is True
        assert payload["data"]["log"] == "/workspace/.udc-logs/train.log"
        # The command travels verbatim as --shell; the GUI must not split it.
        assert runner.calls[-1][1] == [
            "job",
            "start",
            "alloc-1",
            "--name",
            "train",
            "--shell=python train.py --output 'one two'",
        ]
        bad = await client.post(
            "/api/jobs/alloc-1/start",
            headers=_headers(),
            data=json.dumps({"name": "bad name!", "command": "python"}),
        )
        assert bad.status == 400
        empty = await client.post(
            "/api/jobs/alloc-1/start",
            headers=_headers(),
            data=json.dumps({"name": "train", "command": "   "}),
        )
        assert empty.status == 400
        logs = await client.get("/api/job-logs/alloc-1?name=train&tail=50", headers=_headers())
        assert logs.status == 200
        assert (await logs.json())["data"]["logs"] == "epoch 1\n"
        history = await client.get("/api/job-history/alloc-1", headers=_headers())
        assert history.status == 200
        assert (await history.json())["data"]["files"][0]["name"] == "train.log"
    finally:
        await client.close()


def test_ssh_runner_puts_json_before_separator(monitor_config):
    import shlex

    runner = gui_server.SSHRunner(monitor_config, "member-alias", None)
    argv = runner._argv(
        "member-alias",
        ["job", "start", "alloc-1", "--name", "train", "--", "python", "train.py", "-e", "10"],
    )
    remote = shlex.split(argv[-1])
    assert remote[0] == "udc-resource"
    # --json must be consumed by udc-resource itself; everything after the
    # `--` separator belongs to the training command.
    assert remote.index("--json") < remote.index("--")
    assert remote[remote.index("--") + 1 :] == ["python", "train.py", "-e", "10"]
    assert remote[remote.index("--json") :] == ["--json", "--", "python", "train.py", "-e", "10"]


class FakeVscodeProcess:
    def __init__(self, argv, *, stage="listening", exit_code=None) -> None:
        self.argv = argv
        self.pid = 4242
        self.returncode = exit_code
        self.terminated = False
        readiness_index = argv.index("--readiness-file")
        readiness_path = Path(argv[readiness_index + 1])
        if stage == "error":
            payload = {"stage": "error", "message": "allocation is not active"}
        else:
            payload = {
                "stage": stage,
                "port": 39111,
                "uri": "vscode-remote://ssh-remote+udc-alloc-1/workspace",
            }
        readiness_path.write_text(json.dumps(payload), encoding="utf-8")
        FakeVscodeProcess.last = self

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signalled = sig
        if self.returncode is None:
            self.returncode = 0

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def kill(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


def _fake_vscode_config(monitor_config):
    from dataclasses import replace

    return replace(
        monitor_config,
        policy=replace(monitor_config.policy, vscode_enabled=True),
    )


async def test_vscode_endpoint_waits_for_real_readiness(monitor_config, monkeypatch):
    monkeypatch.setattr(
        gui_server.subprocess,
        "Popen",
        lambda argv, **kwargs: FakeVscodeProcess(argv, stage="tunnel_verified"),
    )
    runner = FakeRunner()
    app = gui_server.create_app(
        _fake_vscode_config(monitor_config),
        member_alias=runner.member_alias,
        runner=runner,
        token="test-token",
        config_path="/etc/udc-resource/config.toml",
    )
    client = await _client(app)
    try:
        response = await client.post("/api/vscode/alloc-1", headers=_headers())
        assert response.status == 200
        payload = await response.json()
        assert payload["ok"] is True
        assert payload["data"]["stage"] == "tunnel_verified"
        assert payload["data"]["port"] == 39111
        assert payload["data"]["pid"] == 4242
        argv = FakeVscodeProcess.last.argv
        assert "--config" in argv
        assert argv[argv.index("--config") + 1] == "/etc/udc-resource/config.toml"
        assert "--readiness-file" in argv
    finally:
        await client.close()


async def test_vscode_endpoint_keeps_waiting_while_only_listening(
    monitor_config,
    monkeypatch,
):
    """`listening` alone must not be reported as success anymore."""

    monkeypatch.setattr(
        gui_server.subprocess,
        "Popen",
        lambda argv, **kwargs: FakeVscodeProcess(argv, stage="listening"),
    )
    # Shrink the deadline so the test does not wait the production 90s.
    monkeypatch.setattr(gui_server, "VSCODE_READY_TIMEOUT_SECONDS", 1)
    runner = FakeRunner()
    app = gui_server.create_app(
        _fake_vscode_config(monitor_config),
        member_alias=runner.member_alias,
        runner=runner,
        token="test-token",
    )
    client = await _client(app)
    try:
        response = await client.post("/api/vscode/alloc-1", headers=_headers())
        assert response.status == 500
        payload = await response.json()
        assert "did not report readiness" in payload["error"]["message"]
        last = FakeVscodeProcess.last
        assert last.poll() is not None
        if os.name == "nt":
            import signal

            assert last.signalled == signal.CTRL_BREAK_EVENT
        else:
            assert last.terminated is True
    finally:
        await client.close()


async def test_vscode_endpoint_surfaces_readiness_error(monitor_config, monkeypatch):
    monkeypatch.setattr(
        gui_server.subprocess,
        "Popen",
        lambda argv, **kwargs: FakeVscodeProcess(argv, stage="error"),
    )
    runner = FakeRunner()
    app = gui_server.create_app(
        _fake_vscode_config(monitor_config),
        member_alias=runner.member_alias,
        runner=runner,
        token="test-token",
    )
    client = await _client(app)
    try:
        response = await client.post("/api/vscode/alloc-1", headers=_headers())
        assert response.status == 500
        payload = await response.json()
        assert payload["ok"] is False
        assert payload["error"]["message"] == "allocation is not active"
        last = FakeVscodeProcess.last
        assert last.poll() is not None
        if os.name == "nt":
            import signal

            assert last.signalled == signal.CTRL_BREAK_EVENT
        else:
            assert last.terminated is True
    finally:
        await client.close()


async def test_vscode_endpoint_reports_early_exit(monitor_config, monkeypatch):
    monkeypatch.setattr(
        gui_server.subprocess,
        "Popen",
        lambda argv, **kwargs: FakeVscodeProcess(argv, stage="starting", exit_code=3),
    )
    runner = FakeRunner()
    app = gui_server.create_app(
        _fake_vscode_config(monitor_config),
        member_alias=runner.member_alias,
        runner=runner,
        token="test-token",
    )
    client = await _client(app)
    try:
        response = await client.post("/api/vscode/alloc-1", headers=_headers())
        assert response.status == 500
        payload = await response.json()
        assert "exited before ready" in payload["error"]["message"]
    finally:
        await client.close()


async def test_vscode_endpoint_disabled_by_policy(monitor_config):
    runner = FakeRunner()
    app = gui_server.create_app(
        monitor_config,
        member_alias=runner.member_alias,
        runner=runner,
        token="test-token",
    )
    client = await _client(app)
    try:
        response = await client.post("/api/vscode/alloc-1", headers=_headers())
        assert response.status == 403
    finally:
        await client.close()


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows CTRL_BREAK path")
async def test_stop_vscode_process_lets_windows_child_clean_up(tmp_path):
    """terminate() would hard-kill the CLI and leak the SSH config block; the
    GUI must send CTRL_BREAK_EVENT so the child's cleanup handler runs."""
    import subprocess
    import sys

    started = tmp_path / "started"
    cleaned = tmp_path / "cleaned"
    child_code = (
        "import pathlib, signal, sys, time\n"
        "started, cleaned = sys.argv[1], sys.argv[2]\n"
        "pathlib.Path(started).write_text('ok')\n"
        "def handler(signum, frame):\n"
        "    pathlib.Path(cleaned).write_text('ok')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGBREAK, handler)\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(started), str(cleaned)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.05)
    assert started.exists(), "child did not start"

    await gui_server._stop_vscode_process(process)

    assert process.poll() is not None
    assert cleaned.exists(), "child cleanup handler did not run"
