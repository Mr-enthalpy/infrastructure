from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from citespec_resource.gui import server as gui_server
from citespec_resource.login_wrapper import parse_command


class FakeRunner:
    def __init__(self, admin_alias: str | None = None) -> None:
        self.member_alias = "member-alias"
        self.admin_alias = admin_alias
        # None = the fake server reports no capabilities block (old server).
        self.vscode_capability: bool | None = None
        self.server_protocol: int | None = None
        self.min_client_protocol: int | None = None
        self.server_version = ""
        self.calls: list[tuple[str, list[str]]] = []
        self.timeouts: list[float | None] = []

    async def run(
        self, alias: str, args: list[str], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((alias, list(args)))
        self.timeouts.append(timeout)
        if args[:2] == ["admin", "users"] and not self.admin_alias:
            return {
                "ok": False,
                "error": {"code": "forbidden", "message": "admin SSH alias is not configured"},
            }
        if args[:2] == ["admin", "requests"]:
            if args[2] == "list":
                data = {"requests": [{"request_id": "req-1", "status": "pending_review"}]}
            else:
                decided = "approved" if args[2] == "approve" else "rejected"
                data = {"request_id": args[3], "status": decided}
        elif args[:2] == ["admin", "members"]:
            if args[2] == "list":
                data = {
                    "members": [{"member_id": "m1", "status": "active", "linux_username": "alice"}]
                }
            else:
                data = {"member_id": args[3], "status": "suspended"}
        elif args[:2] == ["admin", "operations"]:
            data = {"operation_id": args[3], "status": "succeeded"}
        elif args[:1] == ["list"]:
            data = {
                "server": "example-gpu",
                "mode": "monitor",
                "gpus": [],
                "allocations": [],
                "unmanaged": {"containers": [], "processes": []},
            }
        elif args[:1] == ["status"]:
            data = {"allocations": []}
            if self.vscode_capability is not None:
                data["capabilities"] = {
                    "vscode": self.vscode_capability,
                    "file_transfer": False,
                }
            if self.server_protocol is not None:
                data["version"] = self.server_version
                data["protocol"] = self.server_protocol
                data["min_client_protocol"] = self.min_client_protocol
        elif args[:1] == ["apply"]:
            data = {
                "id": "alloc-1",
                "status": "active",
                "gpus": [3],
                "expires_at": "2026-10-23T00:00:00+00:00",
                "workspace": "/data1/citespec-users/alice",
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
            data = {"jobs": [{"name": "train", "session": "citespec-train", "log": "/tmp/x"}]}
        elif args[:2] == ["job", "start"]:
            data = {
                "name": args[4],
                "session": "citespec-train",
                "log": "/workspace/.citespec-logs/train.log",
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
            data = {"server": "example-gpu", "mode": "monitor", "gpus": [], "allocations": []}
        elif args[:2] == ["admin", "audit"]:
            data = {"events": []}
        elif args[:2] == ["admin", "mode"]:
            data = {"mode": args[2]}
        elif args[:2] == ["admin", "release"]:
            data = {"id": args[2], "status": "released"}
        elif args[:2] == ["admin", "reconcile"]:
            data = {"kept": 0}
        elif args[:2] == ["admin", "legacy-scan"]:
            data = {"scanned": 2, "dismissed": 1}
        elif args[:2] == ["admin", "legacy-sources"]:
            data = {
                "sources": [
                    {
                        "source_id": "src_1",
                        "container_id": "c" * 64,
                        "container_name": "old-workspace",
                        "image": "citespec-base:2024.09",
                        "state": "running",
                        "status": "available",
                        "created_at": "2025-03-01T10:00:00Z",
                        "scanned_at": "2026-10-28T00:00:00Z",
                    }
                ]
            }
        elif args[:2] == ["admin", "legacy-claims"]:
            data = {
                "claims": [
                    {
                        "claim_id": "clm_1",
                        "status": "pending",
                        "member_id": "m1",
                        "project_id": "prj_1",
                        "reason": "my old runs",
                        "source": {"source_id": "src_1", "container_name": "old-workspace"},
                        "version_id": None,
                    }
                ]
            }
        elif args[:2] == ["admin", "legacy-decide"]:
            data = {
                "claim": {
                    "claim_id": args[2],
                    "status": "approved" if "--approve" in args else "rejected",
                }
            }
        elif args[:2] == ["admin", "legacy-capture"]:
            data = {
                "version": {"ref": "alice-proj@v1", "capture_fidelity": "partial"},
                "claim": {"claim_id": args[2], "status": "approved"},
            }
        elif args[:1] == ["project"]:
            if args[1] == "list":
                data = {
                    "projects": [
                        {
                            "name": "alice-proj",
                            "status": "active",
                            "versions": 1,
                            "latest": "alice-proj@v1",
                        }
                    ]
                }
            elif args[1] == "create":
                data = {"project": {"name": args[3], "status": "active", "versions": 0}}
            else:
                data = {
                    "project": {"name": args[2], "status": "active", "versions": 0},
                    "versions": [],
                }
        elif args[:1] == ["env"]:
            if args[1] == "list":
                data = {
                    "versions": [
                        {
                            "version_id": "ver_1",
                            "ref": "alice-proj@v1",
                            "origin": "capture",
                            "capture_fidelity": "complete",
                            "verification": "built",
                            "visibility": "private",
                            "created_at": "2026-10-28T00:00:00Z",
                            "sha256": "a" * 64,
                        }
                    ]
                }
            elif args[1] == "show":
                data = {
                    "version": {"ref": args[2], "verification": "built"},
                    "manifest": {"base": {}, "python": {"packages": []}, "system": {}},
                    "artifacts": [],
                    "builds": [],
                }
            elif args[1] == "diff":
                data = {"left": {}, "right": {}, "diff": {"changed": False}}
            elif args[1] == "build":
                data = {
                    "build": {"status": "built", "image_tag": "citespec-env:x"},
                    "version": {"ref": args[2], "verification": "built"},
                }
            elif args[1] == "verify":
                data = {"version": {"ref": args[2], "verification": "functional_passed"}}
            else:
                data = {"version": {"ref": "alice-proj@v1", "capture_fidelity": "complete"}}
        elif args[:1] == ["legacy"]:
            if args[1] == "list":
                data = {
                    "sources": [
                        {
                            "source_id": "src_1",
                            "container": "c" * 12,
                            "image": "citespec-base:2024.09",
                            "state": "running",
                            "status": "available",
                            "metadata": {"image": "citespec-base:2024.09"},
                        }
                    ]
                }
            elif args[1] == "claim":
                data = {"claim": {"claim_id": "clm_1", "status": "pending"}}
            elif args[1] == "claims":
                data = {"claims": [{"claim_id": "clm_1", "status": "pending"}]}
            else:
                data = {"claim": {"claim_id": args[2], "status": "pending"}}
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
    return {"X-CITESPEC-Token": "test-token"}


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
        assert "tab-environments" in text
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
                    "image": "citespec-base:2026.10",
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
            "citespec-base:2026.10",
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


async def test_apply_forwards_gpu_count_and_idempotency(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.post(
            "/api/apply",
            headers=_headers(),
            data=json.dumps(
                {
                    "gpus": [],
                    "gpu_count": 2,
                    "cpus": 4,
                    "memory_gb": 8,
                    "hours": 2,
                    "idempotency_key": "gui-1",
                }
            ),
        )
        assert response.status == 200
        args = runner.calls[0][1]
        assert args[args.index("--gpu-count") + 1] == "2"
        assert args[args.index("--idempotency-key") + 1] == "gui-1"
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


async def test_config_exposes_server_capability_over_local_policy(monitor_config):
    runner = FakeRunner()
    runner.vscode_capability = True
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.get("/api/config", headers=_headers())
        assert response.status == 200
        payload = await response.json()
        assert payload["data"]["vscode_enabled"] is True
        assert payload["data"]["capability_source"] == "server"
    finally:
        await client.close()


async def test_config_falls_back_to_local_policy_without_capabilities(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.get("/api/config", headers=_headers())
        payload = await response.json()
        assert payload["data"]["vscode_enabled"] is False
        assert payload["data"]["capability_source"] == "local"
    finally:
        await client.close()


async def test_config_reports_protocol_compatibility(monitor_config):
    runner = FakeRunner()
    runner.server_version = "0.1.0"
    runner.server_protocol = 1
    runner.min_client_protocol = 1
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.get("/api/config", headers=_headers())
        payload = await response.json()
        compatibility = payload["data"]["compatibility"]
        assert compatibility["status"] == "ok"
        assert compatibility["server_protocol"] == 1
        assert payload["data"]["compatibility_warning"] == ""
    finally:
        await client.close()


async def test_config_warns_when_client_must_update(monitor_config):
    runner = FakeRunner()
    runner.server_version = "9.0.0"
    runner.server_protocol = 2
    runner.min_client_protocol = 2
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.get("/api/config", headers=_headers())
        payload = await response.json()
        assert payload["data"]["compatibility"]["status"] == "client_update_required"
        assert "update" in payload["data"]["compatibility_warning"].lower()
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
    assert argv[-1] == "citespec-resource list --json"
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
        "citespec-resource",
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
        assert payload["data"]["log"] == "/workspace/.citespec-logs/train.log"
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
    assert remote[0] == "citespec-resource"
    # --json must be consumed by citespec-resource itself; everything after the
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
                "uri": "vscode-remote://ssh-remote+citespec-alloc-1/workspace",
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
        config_path="/etc/citespec-resource/config.toml",
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
        assert argv[argv.index("--config") + 1] == "/etc/citespec-resource/config.toml"
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


async def test_vscode_endpoint_defers_to_server_capability(monitor_config, monkeypatch):
    # Server says enabled even though the local policy is stale: must proceed
    # past the local-policy gate instead of returning 403.
    runner = FakeRunner()
    runner.vscode_capability = True
    app = gui_server.create_app(
        monitor_config,
        member_alias=runner.member_alias,
        runner=runner,
        token="test-token",
    )
    client = await _client(app)
    try:
        response = await client.post("/api/vscode/alloc-1", headers=_headers())
        assert response.status != 403
    finally:
        await client.close()


async def test_vscode_endpoint_disabled_on_server(monitor_config):
    runner = FakeRunner()
    runner.vscode_capability = False
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
        payload = await response.json()
        assert "disabled on the server" in payload["error"]["message"]
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
        payload = await response.json()
        assert "disabled by policy" in payload["error"]["message"]
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


async def test_membership_admin_endpoints(monitor_config):
    runner = FakeRunner()
    runner.admin_alias = "admin-alias"
    client = await _client(_app(monitor_config, runner))
    try:
        requests = await client.get("/api/admin/membership/requests", headers=_headers())
        assert requests.status == 200
        payload = await requests.json()
        assert payload["data"]["requests"][0]["request_id"] == "req-1"
        approve = await client.post(
            "/api/admin/membership/requests/approve",
            headers=_headers(),
            data=json.dumps({"request_id": "req-1", "review_note": "ok"}),
        )
        assert approve.status == 200
        reject_missing = await client.post(
            "/api/admin/membership/requests/reject",
            headers=_headers(),
            data=json.dumps({"request_id": "req-1"}),
        )
        assert reject_missing.status == 400
        members = await client.get("/api/admin/membership/members", headers=_headers())
        assert (await members.json())["data"]["members"][0]["member_id"] == "m1"
        suspend_missing_reason = await client.post(
            "/api/admin/membership/suspend",
            headers=_headers(),
            data=json.dumps({"member_id": "m1"}),
        )
        assert suspend_missing_reason.status == 400
        retry = await client.post(
            "/api/admin/membership/retry",
            headers=_headers(),
            data=json.dumps({"operation_id": "op-1"}),
        )
        assert retry.status == 200
        key_add = await client.post(
            "/api/admin/membership/keys/add",
            headers=_headers(),
            data=json.dumps({"member_id": "m1", "public_key": "ssh-ed25519 AAAA test"}),
        )
        assert key_add.status == 200
        assert runner.calls[-1][1][:3] == ["admin", "members", "key-add"]
        assert runner.calls[-1][1][-1] == "ssh-ed25519 AAAA test"
        key_revoke = await client.post(
            "/api/admin/membership/keys/revoke",
            headers=_headers(),
            data=json.dumps({"member_id": "m1", "key_id": "k1"}),
        )
        assert key_revoke.status == 200
        assert runner.calls[-1][1][:3] == ["admin", "members", "key-revoke"]
        key_missing = await client.post(
            "/api/admin/membership/keys/add",
            headers=_headers(),
            data=json.dumps({"member_id": "m1"}),
        )
        assert key_missing.status == 400
    finally:
        await client.close()


async def test_project_and_environment_routes(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        listed = await client.get("/api/projects", headers=_headers())
        assert listed.status == 200
        assert (await listed.json())["data"]["projects"][0]["name"] == "alice-proj"
        created = await client.post(
            "/api/projects",
            headers=_headers(),
            data=json.dumps({"name": "new-proj"}),
        )
        assert created.status == 200
        bad = await client.post(
            "/api/projects",
            headers=_headers(),
            data=json.dumps({"name": "../escape"}),
        )
        assert bad.status == 400
        versions = await client.get(
            "/api/environments?project=alice-proj",
            headers=_headers(),
        )
        assert versions.status == 200
        assert (await versions.json())["data"]["versions"][0]["ref"] == "alice-proj@v1"
        shown = await client.get("/api/environments/alice-proj%40v1", headers=_headers())
        assert shown.status == 200
        diffed = await client.get(
            "/api/environments/alice-proj%40v1/diff?other=alice-proj%40v0",
            headers=_headers(),
        )
        assert diffed.status == 200
        built = await client.post(
            "/api/environments/alice-proj%40v1/build",
            headers=_headers(),
        )
        assert built.status == 200
        verified = await client.post(
            "/api/environments/alice-proj%40v1/verify",
            headers=_headers(),
            data=json.dumps({"allocation": "alloc-1"}),
        )
        assert verified.status == 200
        captured = await client.post(
            "/api/environments",
            headers=_headers(),
            data=json.dumps({"project": "alice-proj", "allocation": "alloc-1"}),
        )
        assert captured.status == 200
        assert runner.timeouts[5] == gui_server.ENV_BUILD_TIMEOUT_SECONDS
        assert runner.timeouts[6] == gui_server.ENV_SLOW_OPERATION_TIMEOUT_SECONDS
        assert runner.calls == [
            ("member-alias", ["project", "list"]),
            ("member-alias", ["project", "create", "--name", "new-proj"]),
            ("member-alias", ["env", "list", "--project", "alice-proj", "--limit", "50"]),
            ("member-alias", ["env", "show", "alice-proj@v1"]),
            ("member-alias", ["env", "diff", "alice-proj@v0", "alice-proj@v1"]),
            ("member-alias", ["env", "build", "alice-proj@v1"]),
            (
                "member-alias",
                ["env", "verify", "alice-proj@v1", "--allocation", "alloc-1"],
            ),
            (
                "member-alias",
                ["env", "capture", "--allocation", "alloc-1", "--project", "alice-proj"],
            ),
        ]
    finally:
        await client.close()


async def test_apply_forwards_environment_version(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        response = await client.post(
            "/api/apply",
            headers=_headers(),
            data=json.dumps(
                {"gpus": [], "cpus": 4, "memory_gb": 8, "hours": 2, "env": "alice-proj@v1"}
            ),
        )
        assert response.status == 200
        applied = runner.calls[-1][1]
        assert applied[applied.index("--env") + 1] == "alice-proj@v1"
        conflict = await client.post(
            "/api/apply",
            headers=_headers(),
            data=json.dumps(
                {
                    "gpus": [],
                    "cpus": 4,
                    "memory_gb": 8,
                    "image": "citespec-base:2026.10",
                    "env": "alice-proj@v1",
                }
            ),
        )
        assert conflict.status == 400
        invalid = await client.post(
            "/api/apply",
            headers=_headers(),
            data=json.dumps({"gpus": [], "cpus": 4, "memory_gb": 8, "env": "../../x"}),
        )
        assert invalid.status == 400
        assert len(runner.calls) == 1
    finally:
        await client.close()


async def test_legacy_member_routes(monitor_config):
    runner = FakeRunner()
    client = await _client(_app(monitor_config, runner))
    try:
        sources = await client.get("/api/legacy/sources", headers=_headers())
        assert sources.status == 200
        claims = await client.get("/api/legacy/claims?status=pending", headers=_headers())
        assert claims.status == 200
        claim = await client.post(
            "/api/legacy/claims",
            headers=_headers(),
            data=json.dumps({"source": "src_1", "project": "alice-proj", "reason": "my old runs"}),
        )
        assert claim.status == 200
        bad_status = await client.get(
            "/api/legacy/claims?status=unknown",
            headers=_headers(),
        )
        assert bad_status.status == 400
        shown = await client.get("/api/legacy/claims/clm_1", headers=_headers())
        assert shown.status == 200
        assert runner.calls == [
            ("member-alias", ["legacy", "list"]),
            ("member-alias", ["legacy", "claims", "--status", "pending"]),
            (
                "member-alias",
                ["legacy", "claim", "src_1", "--project", "alice-proj", "--reason", "my old runs"],
            ),
            ("member-alias", ["legacy", "show", "clm_1"]),
        ]
    finally:
        await client.close()


async def test_admin_legacy_routes_use_admin_alias(monitor_config):
    runner = FakeRunner(admin_alias="admin-alias")
    client = await _client(_app(monitor_config, runner, admin_alias="admin-alias"))
    try:
        scan = await client.post("/api/admin/legacy/scan", headers=_headers())
        assert scan.status == 200
        sources = await client.get("/api/admin/legacy/sources?status=available", headers=_headers())
        assert sources.status == 200
        claims = await client.get("/api/admin/legacy/claims?status=pending", headers=_headers())
        assert claims.status == 200
        approve = await client.post(
            "/api/admin/legacy/decision",
            headers=_headers(),
            data=json.dumps({"id": "clm_1", "approve": True, "note": "ok"}),
        )
        reject = await client.post(
            "/api/admin/legacy/decision",
            headers=_headers(),
            data=json.dumps({"id": "clm_1", "approve": False}),
        )
        capture = await client.post(
            "/api/admin/legacy/capture",
            headers=_headers(),
            data=json.dumps({"id": "clm_1"}),
        )
        assert approve.status == 200
        assert reject.status == 200
        assert capture.status == 200
        assert runner.calls == [
            ("admin-alias", ["admin", "legacy-scan"]),
            ("admin-alias", ["admin", "legacy-sources", "--status", "available"]),
            ("admin-alias", ["admin", "legacy-claims", "--status", "pending"]),
            ("admin-alias", ["admin", "legacy-decide", "clm_1", "--approve", "--note", "ok"]),
            ("admin-alias", ["admin", "legacy-decide", "clm_1", "--reject"]),
            ("admin-alias", ["admin", "legacy-capture", "clm_1"]),
        ]
    finally:
        await client.close()
