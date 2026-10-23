"""Command-line entry point for members and administrators."""

from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

import typer

from . import __version__
from .client import BrokerClient
from .config import LabConfig, load_config
from .errors import UdcError, exit_code_for
from .stream import (
    FRAME_DATA,
    FRAME_EXIT,
    FRAME_RESIZE,
    decode_frames,
    exit_payload,
    resize_payload,
    send_frame,
)

app = typer.Typer(no_args_is_help=True, help="Timed lab GPU resource manager.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect the local configuration.")
admin_app = typer.Typer(no_args_is_help=True, help="Administrator commands.")
job_app = typer.Typer(no_args_is_help=True, help="Long-running job commands.")
file_app = typer.Typer(no_args_is_help=True, help="Restricted workspace file transfer.")
app.add_typer(config_app, name="config")
app.add_typer(admin_app, name="admin")
app.add_typer(job_app, name="job")
app.add_typer(file_app, name="file")


def _config_path(path: Path | None) -> Path:
    if path is not None:
        return path
    configured = os.environ.get("UDC_RESOURCE_CONFIG")
    if configured:
        return Path(configured)
    installed = Path("/etc/udc-resource/config.toml")
    if installed.exists():
        return installed
    return Path("lab/resources.local.toml")


def _load(ctx: typer.Context) -> LabConfig:
    try:
        return load_config(Path(ctx.obj))
    except (OSError, ValueError) as exc:
        _fail("bad_request", f"configuration error: {exc}", False)


def _make_client(config: LabConfig) -> BrokerClient:
    return BrokerClient(config.server.broker_socket)


def _ssh_base(config: LabConfig) -> list[str]:
    ssh = shutil.which("ssh")
    if ssh is None:
        _fail("ssh_unavailable", "ssh executable is not available", False)
    argv = [
        ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={config.gui.connect_timeout}",
        "-o",
        "StrictHostKeyChecking=yes",
    ]
    known_hosts = config.server.known_hosts.strip()
    if known_hosts:
        argv.extend(["-o", f"UserKnownHostsFile={os.path.expanduser(known_hosts)}"])
    return argv


def _fail(code: str, message: str, json_output: bool) -> NoReturn:
    if json_output:
        typer.echo(
            json.dumps(
                {"ok": False, "error": {"code": code, "message": message}},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"{code}: {message}", err=True)
    raise typer.Exit(exit_code_for(code))


def _request(
    client: BrokerClient,
    command_name: str,
    json_output: bool,
    **args: Any,
) -> dict[str, Any]:
    try:
        response = client.request(command_name, **args)
    except UdcError as exc:
        _fail(exc.code, exc.message, json_output)
    if not response.get("ok"):
        error = response.get("error")
        if not isinstance(error, dict):
            error = {}
        _fail(
            str(error.get("code", "internal")),
            str(error.get("message", "broker request failed")),
            json_output,
        )
    if json_output:
        typer.echo(json.dumps(response, indent=2, sort_keys=True))
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _format_remaining(seconds: Any) -> str:
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "unknown"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes:02d}m"


def _run_shell_loop(sock: Any) -> None:
    if os.name != "posix":
        raise RuntimeError("interactive shell requires POSIX")
    import signal
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    resize_requested = [False]

    def on_winch(signum: int, frame: Any) -> None:
        resize_requested[0] = True

    old_handler = signal.signal(signal.SIGWINCH, on_winch)
    buffer = b""
    try:
        tty.setraw(fd)
        size = shutil.get_terminal_size()
        send_frame(sock, FRAME_RESIZE, resize_payload(size.columns, size.lines))
        while True:
            if resize_requested[0]:
                resize_requested[0] = False
                size = shutil.get_terminal_size()
                send_frame(sock, FRAME_RESIZE, resize_payload(size.columns, size.lines))
            readable, _, _ = select.select([fd, sock], [], [], 0.2)
            if fd in readable:
                data = os.read(fd, 65536)
                if not data:
                    send_frame(sock, FRAME_EXIT, exit_payload("input closed"))
                    break
                send_frame(sock, FRAME_DATA, data)
            if sock in readable:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                frames, buffer = decode_frames(buffer + chunk)
                for kind, payload in frames:
                    if kind == FRAME_DATA:
                        os.write(sys.stdout.fileno(), payload)
                    elif kind == FRAME_EXIT:
                        return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
        signal.signal(signal.SIGWINCH, old_handler)


def _parse_gpu_indices(raw: str) -> list[int]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    try:
        values = [int(part) for part in parts]
    except ValueError as exc:
        raise typer.BadParameter("gpus must be comma-separated integers") from exc
    if any(value < 0 for value in values):
        raise typer.BadParameter("GPU indices must not be negative")
    if len(set(values)) != len(values):
        raise typer.BadParameter("GPU indices must not contain duplicates")
    return values


@app.callback()
def app_callback(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        envvar="UDC_RESOURCE_CONFIG",
        help="Path to the local TOML configuration.",
    ),
) -> None:
    ctx.obj = _config_path(config)


@app.command()
def version() -> None:
    """Print package version."""
    typer.echo(__version__)


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    path: Path | None = None,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate and show the configuration."""
    try:
        cfg = load_config(path or Path(ctx.obj))
    except (OSError, ValueError) as exc:
        _fail("bad_request", f"configuration error: {exc}", json_output)
    data = asdict(cfg)
    if json_output:
        typer.echo(json.dumps(data, indent=2, sort_keys=True))
        return
    typer.echo(f"server: {cfg.server.name} ({cfg.server.ssh_alias})")
    typer.echo(f"hardware: {cfg.hardware.gpu_count}x {cfg.hardware.gpu_model}")
    typer.echo(f"mode: {cfg.policy.mode}")
    typer.echo(f"hours: {cfg.policy.default_hours}-{cfg.policy.max_hours}")
    typer.echo(f"cpu budget: {cfg.policy.max_cpus:g}")
    typer.echo(f"memory budget: {cfg.policy.max_memory_gb} GiB")
    typer.echo(f"deployment_allowed: {cfg.policy.deployment_allowed}")


@app.command("list")
def list_command(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List GPUs and allocations."""
    config = _load(ctx)
    data = _request(_make_client(config), "list", json_output)
    if json_output:
        return
    typer.echo(f"server: {data.get('server')}  mode: {data.get('mode')}")
    for gpu in data.get("gpus", []):
        if gpu.get("allocation_id"):
            typer.echo(
                f"GPU {gpu['index']}: {gpu['state']}  owner={gpu.get('owner')}  "
                f"allocation={gpu['allocation_id']}  "
                f"remaining={_format_remaining(gpu.get('remaining_seconds'))}"
            )
        else:
            typer.echo(f"GPU {gpu['index']}: {gpu['state']}")
    allocations = data.get("allocations", [])
    if allocations:
        typer.echo("")
        typer.echo("allocations:")
        for allocation in allocations:
            typer.echo(
                f"  {allocation['id']}  owner={allocation['owner']}  "
                f"status={allocation['status']}  gpus={allocation['gpus']}  "
                f"remaining={_format_remaining(allocation.get('remaining_seconds'))}"
            )
    unmanaged = data.get("unmanaged") or {}
    unmanaged_containers = unmanaged.get("containers", [])
    unmanaged_processes = unmanaged.get("processes", [])
    if unmanaged_containers or unmanaged_processes:
        typer.echo("")
        typer.echo("unmanaged:")
        for container in unmanaged_containers:
            typer.echo(
                f"  container {container['name']}  status={container['status']}  "
                f"owner={container.get('owner')}"
            )
        for process in unmanaged_processes:
            owner = process.get("username") or process.get("uid") or "unknown"
            container = process.get("container_name") or "-"
            typer.echo(
                f"  process gpu={process.get('gpu_index')}  pid={process['pid']}  "
                f"name={process.get('process_name')}  owner={owner}  container={container}"
            )


@app.command("status")
def status_command(
    ctx: typer.Context,
    allocation_id: str | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show one allocation, or all allocations owned by the caller."""
    config = _load(ctx)
    args = {} if allocation_id is None else {"id": allocation_id}
    data = _request(_make_client(config), "status", json_output, **args)
    if json_output:
        return
    if "allocation" in data:
        _print_allocation(data["allocation"])
        events = data.get("events", [])
        if events:
            typer.echo("events:")
            for event in events:
                typer.echo(f"  {event.get('ts')}  {event.get('action')}")
        return
    allocations = data.get("allocations", [])
    if not allocations:
        typer.echo("no allocations")
    for allocation in allocations:
        _print_allocation(allocation)


def _print_allocation(allocation: dict[str, Any]) -> None:
    typer.echo(f"id: {allocation['id']}")
    typer.echo(f"owner: {allocation['owner']}")
    typer.echo(f"status: {allocation['status']}")
    typer.echo(f"gpus: {allocation['gpus']}")
    typer.echo(f"cpus: {allocation['cpus']:g}")
    typer.echo(f"memory: {allocation['memory_gb']} GiB")
    typer.echo(f"workspace: {allocation['workspace']}")
    typer.echo(f"starts: {allocation['starts_at']}")
    remaining = _format_remaining(allocation.get("remaining_seconds"))
    typer.echo(f"expires: {allocation['expires_at']} ({remaining})")
    if allocation.get("container_name"):
        typer.echo(f"container: {allocation['container_name']}")


@app.command("apply")
def apply_command(
    ctx: typer.Context,
    gpus: str = typer.Option(
        "",
        "--gpus",
        help="Comma-separated GPU indices; empty for a CPU-only allocation.",
    ),
    cpus: float = typer.Option(..., "--cpus", help="CPU cores."),
    memory: int = typer.Option(..., "--memory", help="Memory in GiB."),
    hours: int | None = typer.Option(None, "--hours", help="Lease hours (1-36)."),
    image: str | None = typer.Option(None, "--image", help="Container image."),
    note: str = typer.Option("", "--note", help="Free-form note."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Apply for whole GPUs, or CPU-only when --gpus is omitted."""
    indices = _parse_gpu_indices(gpus)
    args: dict[str, Any] = {
        "gpus": indices,
        "cpus": cpus,
        "memory_gb": memory,
        "note": note,
    }
    if hours is not None:
        args["hours"] = hours
    if image is not None:
        args["image"] = image
    config = _load(ctx)
    data = _request(_make_client(config), "apply", json_output, **args)
    if json_output:
        return
    typer.echo(f"allocation: {data['id']}")
    typer.echo(f"status: {data['status']}")
    typer.echo(f"gpus: {data['gpus']}")
    typer.echo(f"expires: {data['expires_at']}")
    typer.echo(f"workspace: {data['workspace']}")


@app.command("release")
def release_command(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Release an allocation owned by the caller."""
    config = _load(ctx)
    data = _request(_make_client(config), "release", json_output, id=allocation_id)
    if json_output:
        return
    typer.echo(f"released: {data['id']}  status={data['status']}")


@app.command("logs")
def logs_command(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    tail: int = typer.Option(200, "--tail", help="Number of trailing log lines."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show recent container logs for an allocation."""
    config = _load(ctx)
    data = _request(
        _make_client(config),
        "logs",
        json_output,
        id=allocation_id,
        tail=tail,
    )
    if json_output:
        return
    typer.echo(str(data.get("logs", "")), nl=False)


@job_app.command(
    "start",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def job_start(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    name: str = typer.Option(..., "--name", help="Job name."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Start a detached tmux job with a persistent workspace log."""
    command = list(ctx.args)
    if not command:
        raise typer.BadParameter("provide a command after --")
    config = _load(ctx)
    data = _request(
        _make_client(config),
        "job_start",
        json_output,
        id=allocation_id,
        name=name,
        command=command,
    )
    if json_output:
        return
    typer.echo(f"job: {data['name']}")
    typer.echo(f"session: {data['session']}")
    typer.echo(f"log: {data['log']}")


@job_app.command("list")
def job_list(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List jobs in an allocation container."""
    config = _load(ctx)
    data = _request(_make_client(config), "job_list", json_output, id=allocation_id)
    if json_output:
        return
    jobs = data.get("jobs", [])
    if not jobs:
        typer.echo("no jobs")
    for job in jobs:
        typer.echo(
            f"{job['name']}  session={job['session']}  "
            f"log_bytes={job.get('log_bytes', 0)}  attached={job.get('attached', '0')}"
        )


@job_app.command("logs")
def job_logs(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    name: str = typer.Option(..., "--name", help="Job name."),
    tail: int = typer.Option(200, "--tail"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read a persistent job log from the allocation workspace."""
    config = _load(ctx)
    data = _request(
        _make_client(config),
        "job_logs",
        json_output,
        id=allocation_id,
        name=name,
        tail=tail,
    )
    if json_output:
        return
    typer.echo(str(data.get("logs", "")), nl=False)


@job_app.command("attach")
def job_attach(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    name: str = typer.Option(..., "--name", help="Job name."),
) -> None:
    """Attach to a detached tmux job."""
    if os.name != "posix":
        _fail("internal", "interactive attach requires a POSIX client", False)
    config = _load(ctx)
    size = shutil.get_terminal_size()
    client = _make_client(config)
    try:
        sock, response = client.connect_shell(
            allocation_id,
            size.columns,
            size.lines,
            job=name,
        )
    except UdcError as exc:
        _fail(exc.code, exc.message, False)
    if not response.get("ok"):
        sock.close()
        error = response.get("error")
        if not isinstance(error, dict):
            error = {}
        _fail(
            str(error.get("code", "internal")),
            str(error.get("message", "could not attach to job")),
            False,
        )
    try:
        _run_shell_loop(sock)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        _fail("internal", f"job attach failed: {exc}", False)
    finally:
        sock.close()


@job_app.command("stop")
def job_stop(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    name: str = typer.Option(..., "--name", help="Job name."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Stop a detached tmux job."""
    config = _load(ctx)
    data = _request(
        _make_client(config),
        "job_stop",
        json_output,
        id=allocation_id,
        name=name,
    )
    if json_output:
        return
    typer.echo(f"stopped: {data['name']}")


@file_app.command("put")
def file_put(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    remote_path: str = typer.Argument(..., help="Relative path under /workspace."),
) -> None:
    """Receive a file from stdin into the allocation workspace (remote side)."""
    config = _load(ctx)
    client = _make_client(config)
    try:
        sock, response = client.connect_file(
            allocation_id,
            remote_path,
            upload=True,
        )
    except UdcError as exc:
        _fail(exc.code, exc.message, False)
    if not response.get("ok"):
        sock.close()
        typer.echo(json.dumps(response, indent=2, sort_keys=True), err=True)
        error = response.get("error") or {}
        raise typer.Exit(exit_code_for(str(error.get("code", "internal"))))
    try:
        while True:
            chunk = sys.stdin.buffer.read(65536)
            if not chunk:
                break
            send_frame(sock, FRAME_DATA, chunk)
        send_frame(sock, FRAME_EXIT, exit_payload("upload complete"))
        buffer = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("broker closed before upload acknowledgement")
            frames, buffer = decode_frames(buffer + chunk)
            for kind, payload in frames:
                if kind != FRAME_EXIT:
                    continue
                result = json.loads(payload.decode("utf-8"))
                if not result.get("ok"):
                    error = result.get("error") or {}
                    typer.echo(json.dumps(result, indent=2, sort_keys=True), err=True)
                    raise typer.Exit(exit_code_for(str(error.get("code", "internal"))))
                typer.echo(json.dumps(result, indent=2, sort_keys=True))
                return
    except UdcError as exc:
        _fail(exc.code, exc.message, False)
    except (OSError, RuntimeError) as exc:
        _fail("internal", f"file upload failed: {exc}", False)
    finally:
        sock.close()


@file_app.command("get")
def file_get(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    remote_path: str = typer.Argument(..., help="Relative path under /workspace."),
) -> None:
    """Write a workspace file to stdout (remote side)."""
    config = _load(ctx)
    client = _make_client(config)
    try:
        sock, response = client.connect_file(
            allocation_id,
            remote_path,
            upload=False,
        )
    except UdcError as exc:
        _fail(exc.code, exc.message, False)
    if not response.get("ok"):
        sock.close()
        typer.echo(json.dumps(response, indent=2, sort_keys=True), err=True)
        error = response.get("error") or {}
        raise typer.Exit(exit_code_for(str(error.get("code", "internal"))))
    try:
        buffer = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("broker closed before file download completed")
            frames, buffer = decode_frames(buffer + chunk)
            for kind, payload in frames:
                if kind == FRAME_DATA:
                    sys.stdout.buffer.write(payload)
                    sys.stdout.buffer.flush()
                elif kind == FRAME_EXIT:
                    result = json.loads(payload.decode("utf-8"))
                    if not result.get("ok"):
                        error = result.get("error") or {}
                        typer.echo(
                            json.dumps(result, indent=2, sort_keys=True),
                            err=True,
                        )
                        raise typer.Exit(exit_code_for(str(error.get("code", "internal"))))
                    return
    except (OSError, RuntimeError) as exc:
        _fail("internal", f"file download failed: {exc}", False)
    finally:
        sock.close()


@app.command("push")
def push_command(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    local_path: Path = typer.Argument(..., help="Local file to upload."),
    remote_path: str = typer.Argument(..., help="Relative path under /workspace."),
    server: str | None = typer.Option(None, "--server", help="SSH alias."),
) -> None:
    """Upload a local file into an allocation workspace."""
    config = _load(ctx)
    if not local_path.is_file():
        _fail("bad_request", f"local file not found: {local_path}", False)
    remote = shlex.join(["udc-resource", "file", "put", allocation_id, remote_path])
    argv = [*_ssh_base(config), server or config.server.ssh_alias, remote]
    try:
        with local_path.open("rb") as handle:
            process = subprocess.run(
                argv,
                stdin=handle,
                capture_output=True,
                check=False,
            )
    except OSError as exc:
        _fail("ssh_unavailable", f"cannot start ssh: {exc}", False)
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        _fail("ssh_error", detail or "upload failed", False)
    typer.echo(process.stdout.decode("utf-8", errors="replace").strip())


@app.command("pull")
def pull_command(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    remote_path: str = typer.Argument(..., help="Relative path under /workspace."),
    local_path: Path = typer.Argument(..., help="Local destination file."),
    server: str | None = typer.Option(None, "--server", help="SSH alias."),
) -> None:
    """Download a file from an allocation workspace."""
    config = _load(ctx)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    remote = shlex.join(["udc-resource", "file", "get", allocation_id, remote_path])
    argv = [*_ssh_base(config), server or config.server.ssh_alias, remote]
    temp: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{local_path.name}.",
            suffix=".part",
            dir=local_path.parent,
            delete=False,
        )
        temp = Path(handle.name)
        with handle:
            process = subprocess.run(
                argv,
                stdout=handle,
                stderr=subprocess.PIPE,
                check=False,
            )
    except OSError as exc:
        if temp is not None:
            temp.unlink(missing_ok=True)
        _fail("ssh_unavailable", f"cannot start ssh: {exc}", False)
    if process.returncode != 0:
        temp.unlink(missing_ok=True)
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        _fail("ssh_error", detail or "download failed", False)
    temp.replace(local_path)
    typer.echo(f"downloaded: {local_path}")


@app.command("shell")
def shell_command(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
) -> None:
    """Open an interactive shell inside the allocation container."""
    if os.name != "posix":
        _fail("internal", "interactive shell requires a POSIX client", False)
    config = _load(ctx)
    size = shutil.get_terminal_size()
    client = _make_client(config)
    try:
        sock, response = client.connect_shell(
            allocation_id,
            size.columns,
            size.lines,
        )
    except UdcError as exc:
        _fail(exc.code, exc.message, False)
    if not response.get("ok"):
        sock.close()
        error = response.get("error")
        if not isinstance(error, dict):
            error = {}
        _fail(
            str(error.get("code", "internal")),
            str(error.get("message", "could not open shell")),
            False,
        )
    try:
        _run_shell_loop(sock)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        _fail("internal", f"shell connection failed: {exc}", False)
    finally:
        sock.close()


@app.command("gui")
def gui_command(
    ctx: typer.Context,
    server: str | None = typer.Option(
        None,
        "--server",
        help="SSH alias for the member account (default: config ssh_alias).",
    ),
    admin_server: str | None = typer.Option(
        None,
        "--admin-server",
        help="SSH alias for the admin account (default: config gui.admin_ssh).",
    ),
    port: int = typer.Option(0, "--port", help="Local port; 0 selects a free port."),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Do not open the browser automatically.",
    ),
) -> None:
    """Start the local Web GUI."""
    config = _load(ctx)
    from .gui.server import run_gui

    run_gui(
        config,
        member_alias=server or config.server.ssh_alias,
        admin_alias=admin_server or config.gui.admin_ssh or None,
        port=port,
        open_browser=not no_browser,
    )


@admin_app.command("users")
def admin_users(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List known users."""
    config = _load(ctx)
    data = _request(_make_client(config), "admin_users", json_output)
    if json_output:
        return
    for user in data.get("users", []):
        typer.echo(f"{user['uid']}  {user['username']}  {user['role']}")


@admin_app.command("resources")
def admin_resources(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show GPU, allocation, and user inventory."""
    config = _load(ctx)
    data = _request(_make_client(config), "admin_resources", json_output)
    if json_output:
        return
    typer.echo(f"server: {data.get('server')}  mode: {data.get('mode')}")
    for gpu in data.get("gpus", []):
        typer.echo(f"GPU {gpu['index']}: {gpu['state']}  allocation={gpu.get('allocation_id')}")
    for allocation in data.get("allocations", []):
        typer.echo(
            f"allocation {allocation['id']}: {allocation['status']}  "
            f"owner={allocation['owner']}  gpus={allocation['gpus']}"
        )
    unmanaged = data.get("unmanaged") or {}
    for container in unmanaged.get("containers", []):
        typer.echo(f"unmanaged container {container['name']}: {container['status']}")
    for process in unmanaged.get("processes", []):
        typer.echo(f"unmanaged process gpu={process.get('gpu_index')}  pid={process['pid']}")


@admin_app.command("audit")
def admin_audit(
    ctx: typer.Context,
    limit: int = typer.Option(100, "--limit"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show recent audit events."""
    config = _load(ctx)
    data = _request(_make_client(config), "admin_audit", json_output, limit=limit)
    if json_output:
        return
    for event in data.get("events", []):
        typer.echo(
            f"{event['ts']}  actor={event.get('actor_uid')}  "
            f"action={event['action']}  allocation={event.get('allocation_id')}"
        )


@admin_app.command("mode")
def admin_mode(
    ctx: typer.Context,
    mode: str = typer.Argument(..., help="monitor, maintenance, or active"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Switch broker mode; active requires deployment_allowed=true."""
    if mode not in {"monitor", "maintenance", "active"}:
        raise typer.BadParameter("mode must be monitor, maintenance, or active")
    config = _load(ctx)
    data = _request(_make_client(config), "admin_mode", json_output, mode=mode)
    if json_output:
        return
    typer.echo(f"mode: {data['mode']}")


@admin_app.command("release")
def admin_release(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
    reason: str = typer.Option(..., "--reason", help="Audit reason."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Force-release any allocation."""
    config = _load(ctx)
    data = _request(
        _make_client(config),
        "admin_release",
        json_output,
        id=allocation_id,
        reason=reason,
    )
    if json_output:
        return
    typer.echo(f"released: {data['id']}  status={data['status']}  reason={reason}")


@admin_app.command("reconcile")
def admin_reconcile(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reconcile allocations with the current Docker state."""
    config = _load(ctx)
    data = _request(_make_client(config), "admin_reconcile", json_output)
    if json_output:
        return
    for key, value in data.items():
        typer.echo(f"{key}: {value}")


if __name__ == "__main__":
    app()
