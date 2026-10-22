"""Command-line entry point for members and administrators."""

from __future__ import annotations

import json
import os
import select
import shutil
import sys
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
app.add_typer(config_app, name="config")
app.add_typer(admin_app, name="admin")


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
    command: str,
    json_output: bool,
    **args: Any,
) -> dict[str, Any]:
    try:
        response = client.request(command, **args)
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
            typer.echo(
                f"  process gpu={process.get('gpu_index')}  pid={process['pid']}  "
                f"name={process.get('process_name')}"
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
    mode: str = typer.Argument(..., help="monitor or active"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Switch broker mode; active requires deployment_allowed=true."""
    if mode not in {"monitor", "active"}:
        raise typer.BadParameter("mode must be monitor or active")
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
