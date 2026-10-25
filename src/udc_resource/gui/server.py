"""Local-only Web GUI. All data is obtained by invoking the CLI over SSH."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

import aiohttp
import asyncssh
from aiohttp import WSMsgType, web

from ..config import LabConfig

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
CONFIG_KEY: web.AppKey[LabConfig] = web.AppKey("config", LabConfig)
RUNNER_KEY: web.AppKey[Any] = web.AppKey("runner", Any)
TOKEN_KEY: web.AppKey[str] = web.AppKey("token", str)
STATIC_KEY: web.AppKey[Path] = web.AppKey("static_dir", Path)
VSCODE_KEY: web.AppKey[list[Any]] = web.AppKey("vscode_processes", list)
VSCODE_TEMP_KEY: web.AppKey[list[str]] = web.AppKey("vscode_temp_dirs", list)
VSCODE_CONFIG_PATH_KEY: web.AppKey[str] = web.AppKey("vscode_config_path", str)
ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ALLOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JOB_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
# The tunnel must come up through SSH, the broker, and the container sshd;
# anything slower than this is treated as a failure instead of a false
# success.
VSCODE_READY_TIMEOUT_SECONDS = 90
VSCODE_READY_POLL_SECONDS = 0.25


class SSHRunner:
    def __init__(
        self,
        config: LabConfig,
        member_alias: str,
        admin_alias: str | None,
    ) -> None:
        self.config = config
        self.member_alias = member_alias
        self.admin_alias = admin_alias or None

    def _argv(self, alias: str, args: list[str]) -> list[str] | None:
        ssh = shutil.which("ssh")
        if ssh is None or not ALIAS_RE.fullmatch(alias):
            return None
        argv = [
            ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.gui.connect_timeout}",
            "-o",
            "StrictHostKeyChecking=yes",
        ]
        known_hosts = self.config.server.known_hosts.strip()
        if known_hosts:
            argv.extend(["-o", f"UserKnownHostsFile={os.path.expanduser(known_hosts)}"])
        argv_items = [*args]
        if "--" in argv_items:
            # The `--` separator ends udc-resource option parsing; --json must
            # be consumed by the CLI itself, not appended to the job command.
            argv_items.insert(argv_items.index("--"), "--json")
        else:
            argv_items.append("--json")
        remote_command = shlex.join(["udc-resource", *argv_items])
        argv.extend([alias, remote_command])
        return argv

    async def run(self, alias: str, args: list[str]) -> dict[str, Any]:
        argv = self._argv(alias, args)
        if argv is None:
            return _error("ssh_unavailable", "ssh executable or SSH alias is unavailable")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return _error("ssh_unavailable", f"cannot start ssh: {exc}")
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.gui.connect_timeout + 30,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return _error("ssh_timeout", f"ssh command timed out for {alias}")
        text = stdout.decode("utf-8", errors="replace").strip()
        if text:
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                detail = stderr.decode("utf-8", errors="replace").strip()[:500]
                return _error(
                    "protocol_error",
                    f"remote CLI returned invalid JSON: {detail or 'no stderr'}",
                )
            if isinstance(decoded, dict):
                return decoded
            return _error("protocol_error", "remote CLI returned a non-object JSON value")
        detail = stderr.decode("utf-8", errors="replace").strip()[:500]
        return _error(
            "ssh_error",
            detail or f"remote CLI exited with status {process.returncode}",
        )


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _json(response: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        response,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


@web.middleware
async def _security_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    if request.path.startswith("/api/") or request.path.startswith("/ws/"):
        host = request.host.split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost", "[::1]"}:
            return _json(_error("forbidden", "local access only"), 403)
        token = request.headers.get("X-UDC-Token") or request.query.get("token")
        if token != request.app[TOKEN_KEY]:
            return _json(_error("forbidden", "invalid GUI token"), 403)
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            return _json(_error("forbidden", "invalid Origin"), 403)
    return await handler(request)


def create_app(
    config: LabConfig,
    *,
    member_alias: str,
    admin_alias: str | None = None,
    runner: Any | None = None,
    token: str | None = None,
    static_dir: Path = STATIC_DIR,
    config_path: str | None = None,
) -> web.Application:
    app = web.Application(middlewares=[_security_middleware])
    app[CONFIG_KEY] = config
    app[RUNNER_KEY] = runner or SSHRunner(config, member_alias, admin_alias)
    app[TOKEN_KEY] = token or secrets.token_urlsafe(32)
    app[STATIC_KEY] = static_dir
    app[VSCODE_KEY] = []
    app[VSCODE_TEMP_KEY] = []
    app[VSCODE_CONFIG_PATH_KEY] = (config_path or "").strip()
    app.on_cleanup.append(_cleanup_vscode)
    app.router.add_get("/", _index)
    app.router.add_static("/static/", static_dir)
    app.router.add_get("/api/config", _api_config)
    app.router.add_get("/api/overview", _api_overview)
    app.router.add_get("/api/status", _api_status)
    app.router.add_get("/api/status/{allocation_id}", _api_status)
    app.router.add_post("/api/apply", _api_apply)
    app.router.add_get("/api/queue", _api_queue)
    app.router.add_post("/api/queue/cancel", _api_queue_cancel)
    app.router.add_post("/api/release", _api_release)
    app.router.add_get("/api/logs/{allocation_id}", _api_logs)
    app.router.add_get("/api/jobs/{allocation_id}", _api_jobs)
    app.router.add_post("/api/jobs/{allocation_id}/start", _api_job_start)
    app.router.add_post("/api/jobs/{allocation_id}/stop", _api_job_stop)
    app.router.add_get("/api/job-logs/{allocation_id}", _api_job_logs)
    app.router.add_get("/api/job-history/{allocation_id}", _api_job_history)
    app.router.add_post("/api/vscode/{allocation_id}", _api_vscode)
    app.router.add_get("/api/admin/users", _api_admin_users)
    app.router.add_get("/api/admin/resources", _api_admin_resources)
    app.router.add_get("/api/admin/audit", _api_admin_audit)
    app.router.add_post("/api/admin/mode", _api_admin_mode)
    app.router.add_post("/api/admin/release", _api_admin_release)
    app.router.add_post("/api/admin/reconcile", _api_admin_reconcile)
    app.router.add_get("/api/admin/membership/requests", _api_admin_requests)
    app.router.add_post("/api/admin/membership/requests/approve", _api_admin_request_approve)
    app.router.add_post("/api/admin/membership/requests/reject", _api_admin_request_reject)
    app.router.add_get("/api/admin/membership/members", _api_admin_members)
    app.router.add_post("/api/admin/membership/suspend", _api_admin_member_suspend)
    app.router.add_post("/api/admin/membership/resume", _api_admin_member_resume)
    app.router.add_post("/api/admin/membership/revoke", _api_admin_member_revoke)
    app.router.add_post("/api/admin/membership/retry", _api_admin_operation_retry)
    app.router.add_get("/ws/shell/{allocation_id}", _ws_shell)
    return app


async def _index(request: web.Request) -> web.Response:
    path = Path(request.app[STATIC_KEY]) / "index.html"
    content = path.read_text(encoding="utf-8")
    content = content.replace("__UDC_TOKEN__", request.app[TOKEN_KEY])
    content = content.replace(
        "__SERVER_NAME__",
        html.escape(request.app[CONFIG_KEY].server.name),
    )
    return web.Response(
        text=content,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def _server_capabilities(runner: SSHRunner) -> dict[str, Any]:
    try:
        payload = await runner.run(runner.member_alias, ["status"])
    except Exception:
        return {}
    if not payload.get("ok"):
        return {}
    data = payload.get("data")
    capabilities = data.get("capabilities") if isinstance(data, dict) else None
    return capabilities if isinstance(capabilities, dict) else {}


async def _api_config(request: web.Request) -> web.Response:
    runner = request.app[RUNNER_KEY]
    config = request.app[CONFIG_KEY]
    vscode_enabled = config.policy.vscode_enabled
    capability_source = "local"
    capabilities = await _server_capabilities(runner)
    if "vscode" in capabilities:
        # The server decides; the member's local config may be stale.
        vscode_enabled = bool(capabilities["vscode"])
        capability_source = "server"
    return _json(
        {
            "ok": True,
            "data": {
                "server_name": config.server.name,
                "member_alias": runner.member_alias,
                "admin_enabled": bool(runner.admin_alias),
                "vscode_enabled": vscode_enabled,
                "capability_source": capability_source,
            },
        }
    )


async def _remote(request: web.Request, args: list[str], *, admin: bool = False) -> web.Response:
    runner = request.app[RUNNER_KEY]
    alias = runner.admin_alias if admin else runner.member_alias
    if not alias:
        return _json(_error("forbidden", "admin SSH alias is not configured"))
    return _json(await runner.run(alias, args))


async def _api_overview(request: web.Request) -> web.Response:
    return await _remote(request, ["list"])


async def _api_status(request: web.Request) -> web.Response:
    allocation_id = request.match_info.get("allocation_id")
    if allocation_id:
        return await _remote(request, ["status", allocation_id])
    return await _remote(request, ["status"])


async def _api_apply(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError):
        return _json(_error("bad_request", "apply body must be JSON"), 400)
    if not isinstance(body, dict):
        return _json(_error("bad_request", "apply body must be an object"), 400)
    gpus = body.get("gpus", [])
    if not isinstance(gpus, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in gpus
    ):
        return _json(_error("bad_request", "gpus must be a list of integers"), 400)
    try:
        cpus = float(body["cpus"])
        memory = int(body["memory_gb"])
    except (KeyError, TypeError, ValueError):
        return _json(_error("bad_request", "cpus and memory_gb are required"), 400)
    args = ["apply"]
    if gpus:
        args.extend(["--gpus", ",".join(str(item) for item in gpus)])
    args.extend(["--cpus", f"{cpus:g}", "--memory", str(memory)])
    hours = body.get("hours")
    if hours is not None:
        try:
            hours_value = int(hours)
        except (TypeError, ValueError):
            return _json(_error("bad_request", "hours must be an integer"), 400)
        args.extend(["--hours", str(hours_value)])
    image = body.get("image")
    if isinstance(image, str) and image:
        args.extend(["--image", image])
    note = body.get("note")
    if isinstance(note, str) and note:
        args.extend(["--note", note])
    if body.get("queue") is False:
        args.append("--no-queue")
    return await _remote(request, args)


async def _api_queue(request: web.Request) -> web.Response:
    history = request.query.get("history") == "1"
    args = ["queue", "list"]
    if history:
        args.append("--history")
    return await _remote(request, args)


async def _api_queue_cancel(request: web.Request) -> web.Response:
    body = await _json_body(request)
    queue_id = body.get("id")
    reason = body.get("reason", "")
    if not isinstance(queue_id, str) or not queue_id:
        return _json(_error("bad_request", "id is required"), 400)
    if not isinstance(reason, str):
        return _json(_error("bad_request", "reason must be a string"), 400)
    args = ["queue", "cancel", queue_id]
    if reason:
        args.extend(["--reason", reason])
    return await _remote(request, args)


async def _api_release(request: web.Request) -> web.Response:
    body = await _json_body(request)
    allocation_id = body.get("id")
    if not isinstance(allocation_id, str) or not allocation_id:
        return _json(_error("bad_request", "id is required"), 400)
    return await _remote(request, ["release", allocation_id])


async def _api_logs(request: web.Request) -> web.Response:
    allocation_id = request.match_info["allocation_id"]
    try:
        tail = int(request.query.get("tail", "200"))
    except ValueError:
        return _json(_error("bad_request", "tail must be an integer"), 400)
    return await _remote(request, ["logs", allocation_id, "--tail", str(tail)])


async def _api_jobs(request: web.Request) -> web.Response:
    allocation_id = request.match_info["allocation_id"]
    if not ALLOCATION_RE.fullmatch(allocation_id):
        return _json(_error("bad_request", "invalid allocation id"), 400)
    return await _remote(request, ["job", "list", allocation_id])


async def _api_job_start(request: web.Request) -> web.Response:
    allocation_id = request.match_info["allocation_id"]
    if not ALLOCATION_RE.fullmatch(allocation_id):
        return _json(_error("bad_request", "invalid allocation id"), 400)
    body = await _json_body(request)
    name = body.get("name")
    if not isinstance(name, str) or not JOB_RE.fullmatch(name):
        return _json(_error("bad_request", "invalid job name"), 400)
    command = body.get("command")
    # A copy-pasteable shell string. It runs inside the member's own container
    # as `bash -lc` with a fixed argv; the GUI never splits it on whitespace
    # and it grants nothing beyond the member's existing shell access.
    if not isinstance(command, str) or not command.strip() or len(command) > 4096:
        return _json(_error("bad_request", "command must be a non-empty string"), 400)
    return await _remote(
        request,
        [
            "job",
            "start",
            allocation_id,
            "--name",
            name,
            f"--shell={command}",
        ],
    )


async def _api_job_logs(request: web.Request) -> web.Response:
    allocation_id = request.match_info["allocation_id"]
    if not ALLOCATION_RE.fullmatch(allocation_id):
        return _json(_error("bad_request", "invalid allocation id"), 400)
    name = request.query.get("name", "")
    if not JOB_RE.fullmatch(name):
        return _json(_error("bad_request", "invalid job name"), 400)
    try:
        tail = int(request.query.get("tail", "200"))
    except ValueError:
        return _json(_error("bad_request", "tail must be an integer"), 400)
    tail = max(1, min(10000, tail))
    return await _remote(
        request,
        ["job", "logs", allocation_id, "--name", name, "--tail", str(tail)],
    )


async def _api_job_history(request: web.Request) -> web.Response:
    allocation_id = request.match_info["allocation_id"]
    if not ALLOCATION_RE.fullmatch(allocation_id):
        return _json(_error("bad_request", "invalid allocation id"), 400)
    return await _remote(request, ["job", "files", allocation_id])


async def _api_job_stop(request: web.Request) -> web.Response:
    allocation_id = request.match_info["allocation_id"]
    if not ALLOCATION_RE.fullmatch(allocation_id):
        return _json(_error("bad_request", "invalid allocation id"), 400)
    body = await _json_body(request)
    name = body.get("name")
    if not isinstance(name, str) or not JOB_RE.fullmatch(name):
        return _json(_error("bad_request", "invalid job name"), 400)
    return await _remote(request, ["job", "stop", allocation_id, "--name", name])


def _read_readiness(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_stderr_tail(path: Path, limit: int = 500) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[-limit:]
    except OSError:
        return ""


def _cleanup_vscode_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


async def _api_vscode(request: web.Request) -> web.Response:
    allocation_id = request.match_info["allocation_id"]
    if not ALLOCATION_RE.fullmatch(allocation_id):
        return _json(_error("bad_request", "invalid allocation id"), 400)
    config: LabConfig = request.app[CONFIG_KEY]
    # Gate on the server's capability, not the possibly stale local config.
    capabilities = await _server_capabilities(request.app[RUNNER_KEY])
    if capabilities.get("vscode") is False:
        return _json(_error("forbidden", "VS Code support is disabled on the server"), 403)
    if "vscode" not in capabilities and not config.policy.vscode_enabled:
        return _json(_error("forbidden", "VS Code support is disabled by policy"), 403)
    executable = shutil.which("udc-resource")
    base = [executable] if executable is not None else [sys.executable, "-m", "udc_resource.cli"]
    argv = [*base]
    # Pass the config path explicitly: the GUI may have been started with a
    # --config flag that the child would not otherwise see.
    config_path = (
        request.app[VSCODE_CONFIG_PATH_KEY]
        or os.environ.get(
            "UDC_RESOURCE_CONFIG",
            "",
        ).strip()
    )
    if config_path:
        argv.extend(["--config", config_path])
    runner: SSHRunner = request.app[RUNNER_KEY]
    if not ALIAS_RE.fullmatch(runner.member_alias):
        return _json(_error("internal", "invalid SSH alias"), 500)
    argv.extend(["vscode", allocation_id, "--server", runner.member_alias, "--code"])

    temp_dir = tempfile.mkdtemp(prefix="udc-vscode-")
    request.app[VSCODE_TEMP_KEY].append(temp_dir)
    readiness_file = Path(temp_dir) / "readiness.json"
    stderr_file = Path(temp_dir) / "stderr.log"
    argv.extend(["--readiness-file", str(readiness_file)])
    processes: list[Any] = request.app[VSCODE_KEY]
    processes[:] = [process for process in processes if process.poll() is None]
    try:
        with stderr_file.open("wb") as stderr_handle:
            if os.name == "posix":
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
            else:
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_handle,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
    except OSError as exc:
        return _json(_error("internal", f"could not start VS Code: {exc}"), 500)

    # Wait for the CLI to report real readiness (local tunnel bound, SSH
    # config installed) instead of assuming success after a fixed delay.
    loop = asyncio.get_running_loop()
    timeout = max(60, config.gui.connect_timeout * 3)
    deadline = loop.time() + min(timeout, VSCODE_READY_TIMEOUT_SECONDS)
    while True:
        if process.poll() is not None:
            detail = _read_stderr_tail(stderr_file) or "no diagnostics were captured"
            return _json(
                _error("internal", f"VS Code tunnel process exited before ready: {detail}"),
                500,
            )
        stage = _read_readiness(readiness_file)
        stage_name = str(stage.get("stage", ""))
        if stage_name == "error":
            message = str(stage.get("message") or "VS Code tunnel failed")
            await _stop_vscode_process(process)
            return _json(_error("internal", message), 500)
        if stage_name in {"tunnel_verified", "ready"}:
            # "listening" only means the local port is bound; success waits
            # for the CLI's SSH probe to prove the full chain (host SSH,
            # broker tunnel, container sshd, member key auth).
            processes.append(process)
            data: dict[str, Any] = {
                "pid": process.pid,
                "id": allocation_id,
                "stage": stage_name,
            }
            for key in ("port", "uri", "message"):
                if stage.get(key):
                    data[key] = stage[key]
            return _json({"ok": True, "data": data})
        if loop.time() >= deadline:
            detail = _read_stderr_tail(stderr_file) or "no diagnostics were captured"
            await _stop_vscode_process(process)
            return _json(
                _error(
                    "internal",
                    f"VS Code tunnel did not report readiness within {int(timeout)}s: {detail}",
                ),
                500,
            )
        await asyncio.sleep(VSCODE_READY_POLL_SECONDS)


async def _stop_vscode_process(process: Any) -> None:
    try:
        if process.poll() is not None:
            return
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            # The CLI installs a SIGBREAK handler and cleans up its managed
            # SSH config block, listener, and ssh children on the way out.
            # TerminateProcess would skip all of that and leak the block.
            process.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                await asyncio.to_thread(process.wait, 10)
                return
            except subprocess.TimeoutExpired:
                pass
        else:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 5)
                return
            except subprocess.TimeoutExpired:
                pass
        try:
            process.kill()
            await asyncio.to_thread(process.wait, 5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


async def _cleanup_vscode(app: web.Application) -> None:
    processes: list[Any] = app[VSCODE_KEY]
    for process in list(processes):
        await _stop_vscode_process(process)
    processes.clear()
    temp_dirs: list[str] = app[VSCODE_TEMP_KEY]
    for temp_dir in [*temp_dirs]:
        _cleanup_vscode_dir(temp_dir)
    temp_dirs.clear()


async def _api_admin_users(request: web.Request) -> web.Response:
    return await _remote(request, ["admin", "users"], admin=True)


async def _api_admin_resources(request: web.Request) -> web.Response:
    return await _remote(request, ["admin", "resources"], admin=True)


async def _api_admin_audit(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        return _json(_error("bad_request", "limit must be an integer"), 400)
    return await _remote(
        request,
        ["admin", "audit", "--limit", str(limit)],
        admin=True,
    )


async def _api_admin_mode(request: web.Request) -> web.Response:
    body = await _json_body(request)
    mode = body.get("mode")
    if mode not in {"monitor", "maintenance", "active"}:
        return _json(
            _error("bad_request", "mode must be monitor, maintenance, or active"),
            400,
        )
    return await _remote(request, ["admin", "mode", str(mode)], admin=True)


async def _api_admin_release(request: web.Request) -> web.Response:
    body = await _json_body(request)
    allocation_id = body.get("id")
    reason = body.get("reason")
    if not isinstance(allocation_id, str) or not allocation_id:
        return _json(_error("bad_request", "id is required"), 400)
    if not isinstance(reason, str) or not reason:
        return _json(_error("bad_request", "reason is required"), 400)
    return await _remote(
        request,
        ["admin", "release", allocation_id, "--reason", reason],
        admin=True,
    )


async def _api_admin_reconcile(request: web.Request) -> web.Response:
    return await _remote(request, ["admin", "reconcile"], admin=True)


async def _api_admin_requests(request: web.Request) -> web.Response:
    status = request.query.get("status", "")
    args = ["admin", "requests", "list"]
    if status:
        args.extend(["--status", status])
    return await _remote(request, args, admin=True)


async def _api_admin_request_decide(request: web.Request, action: str) -> web.Response:
    body = await _json_body(request)
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return _json(_error("bad_request", "request_id is required"), 400)
    args = ["admin", "requests", action, "--request-id", request_id]
    review_note = str(body.get("review_note", "") or "")
    internal_note = str(body.get("internal_note", "") or "")
    valid_until = str(body.get("valid_until", "") or "")
    quota_gb = body.get("quota_gb")
    if action == "approve":
        if valid_until:
            args.extend(["--valid-until", valid_until])
        if isinstance(quota_gb, int) and quota_gb > 0:
            args.extend(["--quota-gb", str(quota_gb)])
    if review_note:
        args.extend(["--review-note", review_note])
    if internal_note:
        args.extend(["--internal-note", internal_note])
    return await _remote(request, args, admin=True)


async def _api_admin_request_approve(request: web.Request) -> web.Response:
    return await _api_admin_request_decide(request, "approve")


async def _api_admin_request_reject(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if not str(body.get("review_note", "") or "").strip():
        return _json(_error("bad_request", "拒绝必须填写申请人可见的原因"), 400)
    return await _api_admin_request_decide(request, "reject")


async def _api_admin_members(request: web.Request) -> web.Response:
    return await _remote(request, ["admin", "members", "list"], admin=True)


async def _api_admin_member_transition(request: web.Request, action: str) -> web.Response:
    body = await _json_body(request)
    member_id = body.get("member_id")
    reason = str(body.get("reason", "") or "")
    if not isinstance(member_id, str) or not member_id:
        return _json(_error("bad_request", "member_id is required"), 400)
    if action != "resume" and not reason.strip():
        return _json(_error("bad_request", "状态变更必须填写原因"), 400)
    args = ["admin", "members", action, "--member-id", member_id, "--reason", reason]
    return await _remote(request, args, admin=True)


async def _api_admin_member_suspend(request: web.Request) -> web.Response:
    return await _api_admin_member_transition(request, "suspend")


async def _api_admin_member_resume(request: web.Request) -> web.Response:
    return await _api_admin_member_transition(request, "resume")


async def _api_admin_member_revoke(request: web.Request) -> web.Response:
    return await _api_admin_member_transition(request, "revoke")


async def _api_admin_operation_retry(request: web.Request) -> web.Response:
    body = await _json_body(request)
    operation_id = body.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        return _json(_error("bad_request", "operation_id is required"), 400)
    return await _remote(
        request,
        ["admin", "operations", "retry", "--operation-id", operation_id],
        admin=True,
    )


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError):
        return {}
    return body if isinstance(body, dict) else {}


async def _ws_shell(request: web.Request) -> web.StreamResponse:
    allocation_id = request.match_info["allocation_id"]
    if not ALLOCATION_RE.fullmatch(allocation_id):
        return web.Response(status=400, text="invalid allocation id")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
    await ws.prepare(request)
    runner: SSHRunner = request.app[RUNNER_KEY]
    config: LabConfig = request.app[CONFIG_KEY]
    try:
        cols = int(request.query.get("cols", "120"))
        rows = int(request.query.get("rows", "40"))
    except ValueError:
        cols, rows = 120, 40
    cols = max(20, min(400, cols))
    rows = max(5, min(200, rows))
    if not ALIAS_RE.fullmatch(runner.member_alias):
        await ws.send_str(json.dumps({"type": "error", "message": "invalid SSH alias"}))
        await ws.close()
        return ws
    process: asyncssh.SSHClientProcess[Any] | None = None
    connection: asyncssh.SSHClientConnection | None = None
    try:
        kwargs: dict[str, Any] = {"connect_timeout": config.gui.connect_timeout}
        known_hosts = config.server.known_hosts.strip()
        if known_hosts:
            kwargs["known_hosts"] = os.path.expanduser(known_hosts)
        connection = await asyncssh.connect(runner.member_alias, **kwargs)
        command = f"udc-resource shell {shlex.quote(allocation_id)}"
        job = request.query.get("job", "").strip()
        if job:
            if not JOB_RE.fullmatch(job):
                await ws.send_str(json.dumps({"type": "error", "message": "invalid job name"}))
                await ws.close()
                return ws
            command += f" --job {shlex.quote(job)}"
        process = await connection.create_process(
            command,
            term_type="xterm-256color",
            term_size=(cols, rows),
        )

        async def pump_output() -> None:
            assert process is not None
            while True:
                data = await process.stdout.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
            code = await process.wait()
            await ws.send_str(json.dumps({"type": "exit", "code": code}))
            await ws.close()

        async def pump_input() -> None:
            assert process is not None
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "input":
                        data = payload.get("data")
                        if isinstance(data, str):
                            process.stdin.write(data)
                            await process.stdin.drain()
                    elif payload.get("type") == "resize":
                        try:
                            new_cols = max(20, min(400, int(payload.get("cols", cols))))
                            new_rows = max(5, min(200, int(payload.get("rows", rows))))
                        except (TypeError, ValueError):
                            continue
                        process.change_terminal_size(new_cols, new_rows)
                elif message.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                    break

        tasks = [
            asyncio.create_task(pump_output()),
            asyncio.create_task(pump_input()),
        ]
        try:
            _done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
        finally:
            if process is not None:
                try:
                    process.terminate()
                except Exception:
                    pass
            if not ws.closed:
                await ws.close()
            await asyncio.gather(*tasks, return_exceptions=True)
    except asyncssh.Error as exc:
        if not ws.closed:
            await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
            await ws.close()
    except Exception as exc:
        logger.exception("shell websocket failed")
        if not ws.closed:
            await ws.send_str(json.dumps({"type": "error", "message": str(exc)}))
            await ws.close()
    finally:
        if connection is not None:
            connection.close()
    return ws


def run_gui(
    config: LabConfig,
    *,
    member_alias: str,
    admin_alias: str | None,
    port: int = 0,
    open_browser: bool = True,
    config_path: str | None = None,
) -> None:
    token = secrets.token_urlsafe(32)
    app = create_app(
        config,
        member_alias=member_alias,
        admin_alias=admin_alias,
        token=token,
        config_path=config_path,
    )

    async def serve() -> None:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="127.0.0.1", port=port)
        await site.start()
        actual_port = runner.addresses[0][1]
        url = f"http://127.0.0.1:{actual_port}/"
        print(f"udc-resource gui: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


def main() -> None:
    raise SystemExit("run 'udc-resource gui' instead")
