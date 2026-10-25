"""Command-line entry point for members and administrators."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
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
queue_app = typer.Typer(no_args_is_help=True, help="Queued allocation requests.")
app.add_typer(config_app, name="config")
app.add_typer(admin_app, name="admin")
app.add_typer(job_app, name="job")
app.add_typer(file_app, name="file")
app.add_typer(queue_app, name="queue")

ALLOCATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _write_readiness(path: Path | None, stage: str, **extra: Any) -> None:
    """Publish VS Code startup progress for the GUI (best effort, atomic).

    Stages: ``starting``, ``listening`` (local tunnel bound), ``ready`` (IDE
    launch attempted), ``error`` (terminal failure with a message).
    """

    if path is None:
        return
    payload: dict[str, Any] = {"stage": stage, **extra}
    try:
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


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


def _relay_stdio_socket(sock: socket.socket) -> None:
    if os.name != "posix":

        def pump_input() -> None:
            try:
                stream: Any = sys.stdin.buffer
                while True:
                    data = stream.read1(65536)
                    if not data:
                        break
                    sock.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        def pump_output() -> None:
            try:
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
            except OSError:
                pass

        threads = [
            threading.Thread(target=pump_input, daemon=True),
            threading.Thread(target=pump_output, daemon=True),
        ]
        for thread in threads:
            thread.start()
        # The output side always terminates when the socket closes. The input
        # side may stay blocked on an idle console/pipe, so do not wait forever
        # for it; it is a daemon thread and dies with the process.
        threads[1].join()
        threads[0].join(timeout=1)
        return

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    fds = [stdin_fd, sock]
    stdin_open = True
    try:
        while True:
            readable, _, _ = select.select(fds, [], [])
            if sock in readable:
                data = sock.recv(65536)
                if not data:
                    break
                os.write(stdout_fd, data)
            if stdin_open and stdin_fd in readable:
                data = os.read(stdin_fd, 65536)
                if not data:
                    stdin_open = False
                    fds = [fd for fd in fds if fd != stdin_fd]
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    sock.sendall(data)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _default_identity() -> Path | None:
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        candidate = Path.home() / ".ssh" / name
        if candidate.is_file():
            return candidate
    return None


_MANAGED_CONFIG_NAME = "udc-vscode-config"


def _managed_config_file(ssh_config: Path) -> Path:
    """The fully-managed include file next to the user's SSH config.

    The per-allocation blocks live in their own file which is included from
    the TOP of the user config: OpenSSH applies first-match, so appending the
    blocks behind an existing `Host *` section would let wildcard options
    (User, IdentityFile, ...) silently override ours.
    """

    return _config_target(ssh_config).parent / _MANAGED_CONFIG_NAME


def _include_directive(managed_file: Path) -> str:
    """A relative Include resolves against the user's SSH directory on every
    ssh build (MSYS ssh cannot follow absolute Windows paths inside config
    files), so use it for the standard ~/.ssh location and fall back to an
    absolute path only for custom --ssh-config files."""
    try:
        if managed_file.parent.resolve() == (Path.home() / ".ssh").resolve():
            return f'Include "{_MANAGED_CONFIG_NAME}"'
    except OSError:
        pass
    return f'Include "{managed_file.as_posix()}"'


def _port_is_live(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        return probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()


@contextmanager
def _managed_config_lock(ssh_config: Path, timeout: float = 15.0):
    """Serialize managed-file read-modify-write across tunnel processes.

    Multiple VS Code tunnels (and the stale sweep) edit the same include
    file; without a cross-process lock their read-modify-write cycles can
    lose updates.
    """

    lock_path = _managed_config_file(ssh_config).parent / (_MANAGED_CONFIG_NAME + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            pass
        os.close(fd)


def _sweep_stale_managed_blocks(path: Path, ssh_config: Path | None = None) -> int:
    """Remove managed SSH blocks whose local tunnel port is no longer open.

    After a hard kill (or a crashed GUI) a leftover block only ever points at
    a dead local listener. Probing 127.0.0.1:<port> is cheap, fully local, and
    never touches the remote network; a live tunnel keeps its block.
    """

    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    pattern = re.compile(r"(?ms)^# BEGIN UDC VSCODE (\S+)\n.*?^# END UDC VSCODE \1\n?")
    removed = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal removed
        body = match.group(0)
        port_match = re.search(r"(?m)^\s*Port (\d+)\s*$", body)
        stale = not (port_match and _port_is_live(int(port_match.group(1))))
        if stale:
            removed += 1
            return ""
        return body

    updated = pattern.sub(_replace, existing)
    if not removed or updated == existing:
        return removed
    if updated.strip():
        _atomic_config_write(path, updated)
        return removed
    # Every block was stale: drop the file and, when asked, our Include
    # directive so the user's config returns to its pre-tunnel shape.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return removed
    if ssh_config is None:
        return removed
    user_config = _config_target(ssh_config)
    try:
        user_text = user_config.read_text(encoding="utf-8")
    except OSError:
        return removed
    directive = _include_directive(path)
    cleaned = re.sub(rf"(?m)^\s*{re.escape(directive)}\n?", "", user_text)
    if cleaned != user_text:
        _atomic_config_write(user_config, cleaned)
    return removed


def _managed_block(
    allocation_id: str,
    host: str,
    port: int,
    username: str,
    identity: Path | None,
) -> str:
    lines = [
        f"# BEGIN UDC VSCODE {allocation_id}",
        f"Host {host}",
        "  HostName 127.0.0.1",
        f"  Port {port}",
        f"  User {username}",
        "  StrictHostKeyChecking no",
        "  UserKnownHostsFile none",
        "  LogLevel ERROR",
        "  ServerAliveInterval 30",
        "  ServerAliveCountMax 3",
    ]
    if identity is not None:
        identity_text = identity.as_posix().replace('"', '\\"')
        lines.extend(
            [
                f'  IdentityFile "{identity_text}"',
                "  IdentitiesOnly yes",
            ]
        )
    lines.append(f"# END UDC VSCODE {allocation_id}")
    return "\n".join(lines)


def _atomic_config_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Windows OpenSSH refuses SSH config files carrying the OWNER RIGHTS
        # ACE (S-1-3-4) that CPython's tempfile hardening can leave behind.
        # Whatever the source, set an explicit ACL after the atomic rename:
        # only the current user, SYSTEM, and Administrators — exactly what
        # ssh's permission check accepts.
        temp_path = path.parent / f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.udc-tmp"
        try:
            fd = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temp_path, path)
            username = os.environ.get("USERNAME", "")
            if username:
                # /grant:r only ADDS to the protected DACL that os.open
                # created; the OWNER RIGHTS ACE must be removed explicitly.
                subprocess.run(
                    [
                        "icacls",
                        str(path),
                        "/inheritance:r",
                        "/remove:g",
                        "*S-1-3-4",
                        "/grant:r",
                        f"{username}:F",
                        "/grant:r",
                        "SYSTEM:F",
                        "/grant:r",
                        "Administrators:F",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                )
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".udc-tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _config_target(path: Path) -> Path:
    """Follow a user's dotfile symlink instead of replacing the link."""
    return path.resolve() if path.is_symlink() else path


def _install_managed_block(
    ssh_config: Path,
    allocation_id: str,
    block: str,
) -> bool:
    """Write the allocation block into the managed include file.

    Returns whether the Include directive was newly added to the user config,
    so the removal path can take it back out again.
    """

    managed_file = _managed_config_file(ssh_config)
    with _managed_config_lock(ssh_config):
        managed_file.parent.mkdir(parents=True, exist_ok=True)
        existed = managed_file.exists()
        existing = managed_file.read_text(encoding="utf-8") if existed else ""
        pattern = re.compile(
            rf"(?ms)^# BEGIN UDC VSCODE {re.escape(allocation_id)}\n.*?^# END UDC VSCODE "
            rf"{re.escape(allocation_id)}\n?"
        )
        existing = pattern.sub("", existing)
        updated = existing.rstrip()
        if updated:
            updated += "\n\n"
        updated += block.rstrip() + "\n"
        _atomic_config_write(managed_file, updated)
        user_config = _config_target(ssh_config)
        try:
            user_text = user_config.read_text(encoding="utf-8")
        except OSError:
            user_text = ""
        directive = _include_directive(managed_file)
        if not re.search(rf"(?m)^\s*{re.escape(directive)}\s*$", user_text):
            user_text = f"{directive}\n{user_text}" if user_text else f"{directive}\n"
            _atomic_config_write(user_config, user_text)
            return True
    return False


def _remove_managed_block(
    ssh_config: Path,
    allocation_id: str,
    *,
    own_port: int,
) -> None:
    """Drop this process's block — unless a newer tunnel for the SAME
    allocation overwrote it with a still-live port, in which case that
    process owns the block now and removing it would break its reconnects.
    """

    managed_file = _managed_config_file(ssh_config)
    with _managed_config_lock(ssh_config):
        try:
            existing = managed_file.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        pattern = re.compile(
            rf"(?ms)^# BEGIN UDC VSCODE {re.escape(allocation_id)}\n.*?^# END UDC VSCODE "
            rf"{re.escape(allocation_id)}\n?"
        )
        match = pattern.search(existing)
        if match is not None:
            port_match = re.search(r"(?m)^\s*Port (\d+)\s*$", match.group(0))
            if (
                port_match
                and int(port_match.group(1)) != own_port
                and _port_is_live(int(port_match.group(1)))
            ):
                return
        updated = pattern.sub("", existing)
        file_empty = not updated.strip()
        try:
            if file_empty:
                # ssh tolerates a missing Include target; dropping the empty
                # file keeps the user's config directory clean.
                managed_file.unlink(missing_ok=True)
            elif updated != existing:
                _atomic_config_write(managed_file, updated)
        except OSError:
            pass
        # The Include directive goes only when NO allocation block remains:
        # another allocation's tunnel may still need it regardless of which
        # process originally added the directive.
        if not file_empty:
            return
        user_config = _config_target(ssh_config)
        try:
            user_text = user_config.read_text(encoding="utf-8")
        except OSError:
            return
        directive = _include_directive(managed_file)
        cleaned = re.sub(rf"(?m)^\s*{re.escape(directive)}\n?", "", user_text)
        if cleaned != user_text:
            _atomic_config_write(user_config, cleaned)


async def _wait_for_tunnel_ready(process: asyncio.subprocess.Process) -> tuple[bool, str]:
    assert process.stderr is not None
    lines: list[str] = []
    while True:
        try:
            line = await asyncio.wait_for(process.stderr.readline(), timeout=20)
        except TimeoutError:
            return False, "timed out waiting for the remote tunnel"
        if not line:
            return False, "".join(lines).strip() or "remote tunnel closed before it was ready"
        text = line.decode("utf-8", errors="replace").strip()
        if text == "TUNNEL-READY":
            return True, ""
        if text:
            lines.append(text)
        if len(lines) >= 20:
            return False, "\n".join(lines)


async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
    if process.stderr is None:
        return
    try:
        while await process.stderr.read(65536):
            pass
    except OSError:
        pass


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a child and make sure it is reaped."""

    if process.returncode is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except (TimeoutError, OSError):
        pass
    try:
        process.kill()
    except OSError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, OSError):
        pass


def _alias_identity(alias: str) -> Path | None:
    """The identity file the member's own SSH config resolves for `alias`.

    `ssh -G` prints the effective configuration without connecting. Using it
    keeps the VS Code block on the same key the member actually uses for the
    lab server instead of guessing a default key that may not be authorized.
    """

    ssh = shutil.which("ssh")
    if ssh is None:
        return None
    try:
        result = subprocess.run(
            [ssh, "-G", alias],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].strip().lower() == "identityfile":
            candidate = Path(parts[1].strip()).expanduser()
            if candidate.is_file():
                return candidate
    return None


async def _probe_container_ssh(host: str, ssh_config: Path, timeout: int) -> None:
    """Prove the whole VS Code chain end to end before reporting success.

    The probe connects through the managed SSH block: local listener -> host
    SSH -> broker tunnel -> container sshd -> member key auth -> command
    execution. A local bind alone proves none of that.
    """

    ssh = shutil.which("ssh")
    if ssh is None:
        raise RuntimeError("ssh executable is not available")
    try:
        process = await asyncio.create_subprocess_exec(
            ssh,
            "-F",
            str(ssh_config),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(5, timeout)}",
            host,
            "/bin/true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot start the SSH probe: {exc}") from exc
    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        # Cancelling the wait alone would leak the ssh child; kill and reap.
        process.kill()
        await process.wait()
        raise RuntimeError("container SSH probe timed out") from None
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"container SSH probe failed (exit {process.returncode}): {detail}")


async def _serve_vscode_tunnel(
    *,
    config: LabConfig,
    allocation_id: str,
    username: str,
    ssh_alias: str,
    host: str,
    requested_port: int,
    ssh_config: Path,
    identity: Path | None,
    launch_code: bool,
    readiness_file: Path | None = None,
) -> None:
    ssh_command = shlex.join(["udc-resource", "tunnel", allocation_id])
    ssh_argv = [
        *_ssh_base(config),
        "-T",
        "-o",
        "LogLevel=ERROR",
        ssh_alias,
        ssh_command,
    ]
    children: set[asyncio.subprocess.Process] = set()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            break
    if os.name == "nt":
        # asyncio loop signal handlers are unavailable on Windows. The GUI
        # stops the tunnel with CTRL_BREAK_EVENT; without this handler the
        # default disposition would hard-exit and skip the cleanup below.
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:

            def _on_sigbreak(signum: int, frame: Any) -> None:
                del signum, frame
                loop.call_soon_threadsafe(stop.set)

            try:
                signal.signal(sigbreak, _on_sigbreak)
            except (OSError, ValueError):
                pass

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        process = await asyncio.create_subprocess_exec(
            *ssh_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        children.add(process)
        try:
            ready, error = await _wait_for_tunnel_ready(process)
            if not ready:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                await _terminate_process(process)
                typer.echo(f"tunnel failed: {error}", err=True)
                return
            assert process.stdin is not None
            assert process.stdout is not None
            process_stdin = process.stdin
            process_stdout = process.stdout

            async def client_to_tunnel() -> None:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    process_stdin.write(data)
                    await process_stdin.drain()
                try:
                    process_stdin.close()
                except OSError:
                    pass

            async def tunnel_to_client() -> None:
                while True:
                    data = await process_stdout.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()

            drain = asyncio.create_task(_drain_stderr(process))
            tasks = [
                asyncio.create_task(client_to_tunnel()),
                asyncio.create_task(tunnel_to_client()),
            ]
            try:
                _done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
                await _terminate_process(process)
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        finally:
            # On cancellation leave the child in the shared set so the
            # top-level shutdown path still terminates and reaps it.
            current_task = asyncio.current_task()
            if (
                process.returncode is not None
                or current_task is None
                or not current_task.cancelled()
            ):
                await _terminate_process(process)
                children.discard(process)

    server = await asyncio.start_server(handle, "127.0.0.1", requested_port)
    sockets: list[Any] = list(server.sockets or [])
    if not sockets:
        server.close()
        raise RuntimeError("could not bind the local VS Code tunnel")
    port = int(sockets[0].getsockname()[1])
    remote_uri = f"vscode-remote://ssh-remote+{host}/workspace"
    try:
        block = _managed_block(allocation_id, host, port, username, identity)
        _install_managed_block(ssh_config, allocation_id, block)
        _write_readiness(
            readiness_file,
            "listening",
            port=port,
            uri=remote_uri,
        )
        typer.echo(f"local tunnel: 127.0.0.1:{port} -> {host} (allocation {allocation_id})")
        typer.echo(f"ssh config: {ssh_config}")
        probe_timeout = max(30, config.gui.connect_timeout * 2)
        try:
            typer.echo("verifying container SSH connection…")
            await _probe_container_ssh(host, ssh_config, probe_timeout)
        except (RuntimeError, OSError, TimeoutError) as exc:
            message = f"container SSH probe failed: {exc}"
            _write_readiness(readiness_file, "error", code="probe_failed", message=message)
            raise RuntimeError(str(exc)) from exc
        typer.echo("container SSH verified")
        # "ready" is reserved for the case where the IDE was actually launched;
        # a missing code CLI leaves the stage at tunnel_verified with a message
        # so the GUI can surface the copyable URI instead of a false success.
        code_launched = False
        if launch_code:
            code = shutil.which("code")
            if code is None:
                _write_readiness(
                    readiness_file,
                    "tunnel_verified",
                    port=port,
                    uri=remote_uri,
                    message="VS Code CLI not found; open the URI manually",
                )
                typer.echo(
                    f"VS Code CLI not found. Open this folder URI manually:\n  {remote_uri}",
                    err=True,
                )
            else:
                typer.echo(f"opening: {remote_uri}")
                command = [code, "--folder-uri", remote_uri]
                if os.name == "nt" and Path(code).suffix.lower() in {".cmd", ".bat"}:
                    command = ["cmd", "/c", *command]
                result = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    check=False,
                )
                if result.returncode != 0:
                    # A failed launcher must not be reported as "ready".
                    message = f"VS Code CLI exited with status {result.returncode}"
                    _write_readiness(
                        readiness_file,
                        "tunnel_verified",
                        port=port,
                        uri=remote_uri,
                        message=message,
                    )
                    typer.echo(message, err=True)
                else:
                    code_launched = True
        else:
            typer.echo(f"VS Code folder URI: {remote_uri}")
        if not launch_code or code_launched:
            _write_readiness(readiness_file, "ready", uri=remote_uri)
        typer.echo("Press Ctrl-C to close the tunnel.")
        await stop.wait()
    finally:
        # Every failure path after the bind — probe failure, IDE launch
        # problems, cancellation — must leave no managed SSH block, no
        # listener, and no leftover children behind.
        server.close()
        for process in list(children):
            await _terminate_process(process)
        _remove_managed_block(ssh_config, allocation_id, own_port=port)


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
    remote: str | None = typer.Option(
        None,
        "--remote",
        help="Run the command on the given SSH alias instead of the local broker socket.",
    ),
    local: bool = typer.Option(
        False,
        "--local",
        help="Force the local broker socket even when it is absent.",
    ),
) -> None:
    ctx.obj = _config_path(config)
    if remote is not None:
        rest = _strip_local_options(sys.argv[1:], {"--config", "--remote", "--local"})
        _dispatch_remote(remote, rest)
        return
    if local:
        return
    rest = _strip_local_options(sys.argv[1:], {"--config", "--remote", "--local"})
    if not rest or rest[0] not in AUTO_RELAY_COMMANDS:
        return
    if rest[0] == "job" and len(rest) > 1 and rest[1] == "attach":
        return
    # Workstation default: without a local broker socket, talk to the
    # configured server over SSH instead of failing on a missing socket.
    try:
        cfg = load_config(ctx.obj)
    except (OSError, ValueError):
        return
    if Path(cfg.server.broker_socket).exists():
        return
    _dispatch_remote(cfg.server.ssh_alias, rest)


def _strip_local_options(argv: list[str], names: set[str]) -> list[str]:
    """Drop local-only options (with space or = values) before relaying."""

    out: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token in names:
            skip = True
            continue
        if any(token.startswith(f"{name}=") for name in names):
            continue
        out.append(token)
    return out


# Commands that stream over the local broker socket or drive local apps cannot
# be relayed as plain remote CLI invocations. `connect` is itself the SSH path.
LOCAL_ONLY_COMMANDS = {"shell", "vscode", "gui", "tunnel", "file", "menu", "connect"}

# Plain request/response commands run fine through the --remote relay; on a
# workstation without a broker socket they are relayed automatically.
AUTO_RELAY_COMMANDS = {"list", "status", "apply", "release", "logs", "queue", "admin", "job"}


def _dispatch_remote(alias: str, rest: list[str]) -> NoReturn:
    """Run `udc-resource <rest>` on the given SSH alias and relay the result.

    This is the local-workstation path for everyday commands: the member's
    ForceCommand only allows `udc-resource`, so relaying stays inside the same
    whitelist as an interactive SSH session.
    """

    if not rest:
        _fail("bad_request", "no command given for --remote", False)
    if rest[0] == "job" and len(rest) > 1 and rest[1] == "attach":
        _fail(
            "bad_request",
            "job attach needs the local broker socket; run it on the server",
            False,
        )
    if rest[0] in LOCAL_ONLY_COMMANDS:
        _fail(
            "bad_request",
            f"'{rest[0]}' is a local/interactive command and cannot run via --remote",
            False,
        )
    ssh = shutil.which("ssh")
    if ssh is None:
        _fail("ssh_unavailable", "ssh executable is not available", False)
    argv = [
        ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "LogLevel=ERROR",
        alias,
        shlex.join(["udc-resource", *rest]),
    ]
    try:
        completed = subprocess.run(argv, check=False)
    except OSError as exc:
        _fail("ssh_unavailable", f"cannot start ssh: {exc}", False)
    raise SystemExit(completed.returncode)


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
    typer.echo(
        f"cpu budgets: global={cfg.policy.global_max_cpus:g} "
        f"allocation={cfg.policy.max_cpus_per_allocation:g} "
        f"user={cfg.policy.max_cpus_per_user:g}"
    )
    typer.echo(
        f"memory budgets: global={cfg.policy.global_max_memory_gb} GiB "
        f"allocation={cfg.policy.max_memory_gb_per_allocation} GiB "
        f"user={cfg.policy.max_memory_gb_per_user} GiB"
    )
    typer.echo(f"legacy audit: {cfg.policy.legacy_audit_required}")
    typer.echo(f"queue enabled: {cfg.policy.queue_enabled}")
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
    queued = data.get("queue", [])
    if queued:
        typer.echo("")
        typer.echo("queue:")
        for request in queued:
            typer.echo(
                f"  {request['queue_id']}  owner={request['owner']}  "
                f"position={request.get('position')}  gpus={request['gpus']}  "
                f"cpus={request['cpus']:g}  memory={request['memory_gb']} GiB"
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


def _print_queue(request: dict[str, Any]) -> None:
    typer.echo(f"queue: {request['queue_id']}")
    typer.echo(f"status: {request['status']}")
    typer.echo(f"owner: {request['owner']}")
    typer.echo(f"gpus: {request['gpus']}")
    typer.echo(f"cpus: {request['cpus']:g}")
    typer.echo(f"memory: {request['memory_gb']} GiB")
    typer.echo(f"hours: {request['hours']}")
    typer.echo(f"workspace: {request['workspace']}")
    typer.echo(f"created: {request['created_at']}")
    typer.echo(f"queue expires: {request['expires_at']}")
    if request.get("position") is not None:
        typer.echo(f"position: {request['position']}")
    if request.get("allocation_id"):
        typer.echo(f"allocation: {request['allocation_id']}")
    if request.get("reason"):
        typer.echo(f"reason: {request['reason']}")


def _silent_request(
    client: BrokerClient,
    command_name: str,
    *,
    json_output: bool = False,
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
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _wait_for_queue(
    config: LabConfig,
    queue_id: str,
    timeout: int,
    *,
    json_output: bool = False,
) -> dict[str, Any]:
    client = _make_client(config)
    deadline = time.monotonic() + timeout
    while True:
        data = _silent_request(
            client,
            "queue_get",
            json_output=json_output,
            id=queue_id,
        )
        request = data.get("queue", {})
        status = request.get("status")
        allocation = data.get("allocation")
        if status == "admitted":
            if isinstance(allocation, dict):
                if allocation.get("status") == "failed":
                    _fail(
                        "docker_error",
                        "queued allocation was admitted but container creation failed",
                        json_output,
                    )
                return allocation
        elif status in {"cancelled", "expired", "failed"}:
            reason = request.get("reason") or status
            _fail(
                "conflict",
                f"queued request {queue_id} is {status}: {reason}",
                json_output,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail(
                "conflict",
                f"timed out waiting for queue request {queue_id}",
                json_output,
            )
        time.sleep(min(2.0, remaining))


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
    queue: bool = typer.Option(
        True,
        "--queue/--no-queue",
        help="Queue the request when CPU, memory, or a requested GPU is unavailable.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for a queued request to be admitted.",
    ),
    wait_timeout: int = typer.Option(
        3600,
        "--wait-timeout",
        min=1,
        help="Maximum seconds to wait with --wait.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Apply for whole GPUs, or CPU-only when --gpus is omitted."""
    indices = _parse_gpu_indices(gpus)
    args: dict[str, Any] = {
        "gpus": indices,
        "cpus": cpus,
        "memory_gb": memory,
        "note": note,
        "queue": queue,
    }
    if hours is not None:
        args["hours"] = hours
    if image is not None:
        args["image"] = image
    config = _load(ctx)
    client = _make_client(config)
    if wait and json_output:
        data = _silent_request(client, "apply", json_output=True, **args)
    else:
        data = _request(client, "apply", json_output, **args)
    if "queue_id" in data:
        if wait:
            allocation = _wait_for_queue(
                config,
                str(data["queue_id"]),
                wait_timeout,
                json_output=json_output,
            )
            if json_output:
                typer.echo(
                    json.dumps(
                        {"ok": True, "data": {"allocation": allocation}},
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                _print_allocation(allocation)
            return
        if json_output:
            return
        _print_queue(data)
        return
    if json_output:
        if wait:
            typer.echo(json.dumps({"ok": True, "data": data}, indent=2, sort_keys=True))
        return
    typer.echo(f"allocation: {data['id']}")
    typer.echo(f"status: {data['status']}")
    typer.echo(f"gpus: {data['gpus']}")
    typer.echo(f"expires: {data['expires_at']}")
    typer.echo(f"workspace: {data['workspace']}")


@app.command("me")
def me_command(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看我的成员状态与公钥。"""
    config = _load(ctx)
    data = _request(_make_client(config), "me", json_output)
    if json_output:
        return
    membership = data.get("membership", {})
    status = membership.get("status", "?")
    typer.echo(f"账号：{data.get('username')}（UID {data.get('uid')}）")
    if status == "admin":
        typer.echo("成员状态：管理员（不受成员资格限制）")
        return
    typer.echo(f"成员状态：{status}")
    if membership.get("valid_until"):
        typer.echo(f"资格有效期至：{membership['valid_until']}")
    if status != "active":
        typer.echo("下一步：联系管理员；或使用 udc-resource join 提交加入申请。")
        return
    keys = data.get("keys", [])
    if keys:
        typer.echo("公钥：")
        for key in keys:
            state = "有效" if key.get("status") == "active" else "已撤销"
            typer.echo(f"  [{state}] {key['fingerprint']}")
    else:
        typer.echo("公钥：无（请联系管理员安装登录公钥）")


@app.command("start")
def start_command(
    ctx: typer.Context,
    gpu_count: int = typer.Option(
        1,
        "--gpu-count",
        min=0,
        help="申请 GPU 张数（服务端自动选卡）。",
    ),
    preset: str = typer.Option("", "--preset", help="使用服务端配置的预设名称。"),
    hours: int | None = typer.Option(None, "--hours", min=1, help="时长（小时）。"),
    open_with: str = typer.Option("", "--open", help="创建成功后打开方式：vscode / terminal。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """快捷创建工作环境：按张数申请 GPU，服务端自动选卡。"""
    config = _load(ctx)
    gpus_option = ""
    cpus: float | None = None
    memory: int | None = None
    resolved_hours = hours
    if preset:
        preset_table = {entry[0]: entry[1:] for entry in config.onboarding.presets}
        if preset not in preset_table:
            available = ", ".join(preset_table) or "（未配置）"
            _fail("bad_request", f"未知预设 {preset}；可用预设：{available}", json_output)
        preset_gpus, preset_cpus, preset_memory, preset_hours = preset_table[preset]
        gpus_option = str(preset_gpus) if preset_gpus else ""
        cpus = preset_cpus
        memory = preset_memory
        resolved_hours = hours if hours is not None else preset_hours
    resolved_hours = resolved_hours if resolved_hours is not None else config.policy.default_hours

    # Same broker path as `apply` — presets only fill defaults; the broker's
    # own validation and budget checks always decide.
    request_args: dict[str, Any] = {
        "cpus": cpus if cpus is not None else 8.0,
        "memory_gb": memory if memory is not None else 32,
        "hours": resolved_hours,
        "queue": True,
        "idempotency_key": uuid.uuid4().hex,
    }
    if gpu_count:
        request_args["gpu_count"] = gpu_count
    if gpus_option:
        request_args["gpus"] = _parse_gpu_indices(gpus_option)
    data = _request(_make_client(config), "apply", json_output, **request_args)
    if json_output:
        return
    if "queue_id" in data:
        _print_queue(data)
        return
    _print_allocation(data)
    allocation_id = str(data.get("id", ""))
    if open_with == "vscode":
        typer.echo("在 VS Code 中打开：udc-resource vscode " + allocation_id)
        typer.echo("或在你自己的电脑运行 udc-resource gui。")
    elif open_with == "terminal":
        typer.echo("进入终端：udc-resource connect " + allocation_id)


@app.command("menu")
def menu_command(ctx: typer.Context) -> None:
    """Small interactive menu for members; never exposes a host shell."""

    config = _load(ctx)
    client = _make_client(config)
    while True:
        typer.echo("")
        typer.echo("UDC resource menu")
        typer.echo("  1) list resources")
        typer.echo("  2) my allocations")
        typer.echo("  3) queue")
        typer.echo("  4) apply")
        typer.echo("  5) release")
        typer.echo("  6) help / commands")
        typer.echo("  0) exit")
        choice = typer.prompt("choice", default="0").strip()
        if choice in {"0", "q", "quit", "exit"}:
            return
        if choice == "1":
            data = _request(client, "list", False)
            for gpu in data.get("gpus", []):
                typer.echo(f"GPU {gpu['index']}: {gpu['state']} ({gpu.get('state_reason', '')})")
        elif choice == "2":
            data = _request(client, "status", False)
            allocations = data.get("allocations", [])
            if not allocations:
                typer.echo("no allocations")
            for allocation in allocations:
                typer.echo(
                    f"{allocation['id']}  {allocation['status']}  "
                    f"gpus={allocation['gpus']}  expires={allocation['expires_at']}"
                )
        elif choice == "3":
            data = _request(
                client,
                "queue_list",
                False,
                all=False,
                include_finished=False,
            )
            requests = data.get("queue", [])
            if not requests:
                typer.echo("no queued requests")
            for request in requests:
                typer.echo(
                    f"{request['queue_id']}  position={request.get('position')}  "
                    f"gpus={request['gpus']}  remaining="
                    f"{_format_remaining(request.get('remaining_seconds'))}"
                )
        elif choice == "4":
            gpus = typer.prompt("GPU indices (comma-separated, empty for CPU)", default="")
            cpus = typer.prompt("CPUs", default="8")
            memory = typer.prompt("memory GiB", default="32")
            hours = typer.prompt("hours", default=str(config.policy.default_hours))
            data = _request(
                client,
                "apply",
                False,
                gpus=_parse_gpu_indices(gpus),
                cpus=float(cpus),
                memory_gb=int(memory),
                hours=int(hours),
                queue=True,
            )
            if "queue_id" in data:
                _print_queue(data)
            else:
                _print_allocation(data)
        elif choice == "5":
            allocation_id = typer.prompt("allocation id")
            data = _request(client, "release", False, id=allocation_id)
            typer.echo(f"released: {data['id']}  status={data['status']}")
        elif choice == "6":
            typer.echo(
                "Use udc-resource --help, or: list, status, apply, shell, logs, "
                "job, queue, release."
            )
        else:
            typer.echo("unknown choice")


@app.command("maintenance")
def maintenance_command(
    ctx: typer.Context,
    disable: bool = typer.Option(
        False,
        "--disable",
        help="Leave maintenance mode and return to active mode.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Put the broker into maintenance mode (admin only)."""

    config = _load(ctx)
    mode = "active" if disable else "maintenance"
    data = _request(_make_client(config), "admin_mode", json_output, mode=mode)
    if not json_output:
        typer.echo(f"mode: {data['mode']}")


@queue_app.command("list")
def queue_list_command(
    ctx: typer.Context,
    all_users: bool = typer.Option(False, "--all", help="Show all users (admin only)."),
    history: bool = typer.Option(
        False,
        "--history",
        help="Include admitted, cancelled, expired, and failed requests.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List queued allocation requests."""
    config = _load(ctx)
    data = _request(
        _make_client(config),
        "queue_list",
        json_output,
        all=all_users,
        include_finished=history,
    )
    if json_output:
        return
    requests = data.get("queue", [])
    if not requests:
        typer.echo("no queued requests")
        return
    for request in requests:
        typer.echo(
            f"{request['queue_id']}  owner={request['owner']}  "
            f"status={request['status']}  position={request.get('position')}  "
            f"gpus={request['gpus']}  cpus={request['cpus']:g}  "
            f"memory={request['memory_gb']} GiB  remaining="
            f"{_format_remaining(request.get('remaining_seconds'))}"
        )


@queue_app.command("cancel")
def queue_cancel_command(
    ctx: typer.Context,
    queue_id: str = typer.Argument(..., help="Queue request id."),
    reason: str = typer.Option("", "--reason"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Cancel a queued allocation request."""
    config = _load(ctx)
    data = _request(
        _make_client(config),
        "queue_cancel",
        json_output,
        id=queue_id,
        reason=reason,
    )
    if json_output:
        return
    typer.echo(f"cancelled: {data['queue_id']}  status={data['status']}")


@queue_app.command("wait")
def queue_wait_command(
    ctx: typer.Context,
    queue_id: str = typer.Argument(..., help="Queue request id."),
    timeout: int = typer.Option(3600, "--timeout", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Wait until a queued request is admitted or fails."""
    config = _load(ctx)
    allocation = _wait_for_queue(config, queue_id, timeout, json_output=json_output)
    if json_output:
        typer.echo(
            json.dumps(
                {"ok": True, "data": {"allocation": allocation}},
                indent=2,
                sort_keys=True,
            )
        )
        return
    _print_allocation(allocation)


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
    shell: str | None = typer.Option(
        None,
        "--shell",
        help="Run this shell command inside the container via `bash -lc`.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Start a detached tmux job with a persistent workspace log."""
    command: str | list[str]
    if shell is not None:
        if ctx.args:
            raise typer.BadParameter("use either --shell or a command after --, not both")
        if not shell.strip():
            raise typer.BadParameter("shell command must not be empty")
        command = shell
    else:
        command = list(ctx.args)
        if not command:
            raise typer.BadParameter("provide a command after -- or use --shell")
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


@job_app.command("files")
def job_files(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id (any status)."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List persistent job log files, including released allocations."""
    config = _load(ctx)
    data = _request(_make_client(config), "job_files", json_output, id=allocation_id)
    if json_output:
        return
    files = data.get("files", [])
    if not files:
        typer.echo("no job log files")
        return
    for item in files:
        typer.echo(f"{item['name']}  {item.get('bytes', 0)} bytes")


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
    job: str | None = typer.Option(None, "--job", help="Attach to a job session."),
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
            job=job,
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


@app.command("connect")
def connect_command(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Active allocation id."),
    job: str = typer.Option("", "--job", help="Attach to a running job session instead."),
    server: str | None = typer.Option(
        None,
        "--server",
        help="SSH alias for the member account (default: config ssh_alias).",
    ),
) -> None:
    """Interactive container shell over SSH, for local workstations.

    Equivalent to `ssh <alias> udc-resource shell <id>`: the session streams
    through the member's own SSH connection, so no local broker socket is
    needed and this also works from Windows. The server-side ForceCommand
    whitelist already permits the remote side of this command.
    """

    config = _load(ctx)
    if not ALLOCATION_ID_RE.fullmatch(allocation_id):
        _fail("bad_request", "invalid allocation id", False)
    if job and (not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", job) or job in {".", ".."}):
        _fail("bad_request", "invalid job name", False)
    alias = server or config.server.ssh_alias
    remote_parts = ["udc-resource", "shell", allocation_id]
    if job:
        remote_parts.extend(["--job", job])
    # -t -t forces a TTY even when the local side has none; the interactive
    # remote shell needs one.
    argv = [*_ssh_base(config), "-t", "-t", alias, shlex.join(remote_parts)]
    try:
        completed = subprocess.run(argv, check=False)
    except OSError as exc:
        _fail("ssh_unavailable", f"cannot start ssh: {exc}", False)
    raise SystemExit(completed.returncode)


@app.command("tunnel")
def tunnel_command(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Allocation id."),
) -> None:
    """Relay a raw byte tunnel to the container sshd (used by VS Code)."""

    config = _load(ctx)
    if not ALLOCATION_ID_RE.fullmatch(allocation_id):
        _fail("bad_request", "invalid allocation id", False)
    client = _make_client(config)
    try:
        sock, response = client.connect_tunnel(allocation_id)
    except UdcError as exc:
        _fail(exc.code, exc.message, False)
    if not response.get("ok"):
        sock.close()
        error = response.get("error")
        if not isinstance(error, dict):
            error = {}
        _fail(
            str(error.get("code", "internal")),
            str(error.get("message", "could not open tunnel")),
            False,
        )
    sys.stderr.write("TUNNEL-READY\n")
    sys.stderr.flush()
    try:
        _relay_stdio_socket(sock)
    finally:
        try:
            sock.close()
        except OSError:
            pass


@app.command("vscode")
def vscode_command(
    ctx: typer.Context,
    allocation_id: str = typer.Argument(..., help="Active allocation id."),
    server: str | None = typer.Option(
        None,
        "--server",
        help="SSH alias for the member account (default: config ssh_alias).",
    ),
    identity: Path | None = typer.Option(
        None,
        "--identity",
        help="SSH private key used by VS Code Remote-SSH.",
    ),
    ssh_config: Path | None = typer.Option(
        None,
        "--ssh-config",
        help="SSH config file to update (default: ~/.ssh/config).",
    ),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Local tunnel port."),
    code: bool = typer.Option(
        True,
        "--code/--no-code",
        help="Launch the VS Code CLI after the tunnel is ready.",
    ),
    readiness_file: Path | None = typer.Option(
        None,
        "--readiness-file",
        help="Write JSON startup progress here (used by the GUI).",
    ),
) -> None:
    """Open a container through a Broker-mediated SSH tunnel in VS Code."""

    def fail(error_code: str, message: str) -> NoReturn:
        _write_readiness(readiness_file, "error", code=error_code, message=message)
        _fail(error_code, message, False)

    config = _load(ctx)
    if not ALLOCATION_ID_RE.fullmatch(allocation_id):
        fail("bad_request", "invalid allocation id")
    alias = server or config.server.ssh_alias
    remote = shlex.join(["udc-resource", "status", allocation_id, "--json"])
    _write_readiness(readiness_file, "starting")
    try:
        status = subprocess.run(
            [*_ssh_base(config), alias, remote],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=config.gui.connect_timeout + 15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail("ssh_unavailable", f"cannot query allocation: {exc}")
    if status.returncode != 0:
        fail(
            "ssh_error",
            status.stderr.strip() or "could not inspect the allocation",
        )
    try:
        envelope = json.loads(status.stdout)
        allocation = envelope["data"]["allocation"]
    except (json.JSONDecodeError, KeyError, TypeError):
        fail("protocol", "server returned an invalid allocation response")
    # Feature gating follows the SERVER, not the possibly stale local config.
    capabilities = envelope["data"].get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get("vscode") is False:
        fail("forbidden", "VS Code support is disabled on the server")
    if not isinstance(capabilities, dict) and not config.policy.vscode_enabled:
        # Older server without a capabilities block: fall back to local policy.
        fail("forbidden", "VS Code support is disabled by policy")
    if not isinstance(allocation, dict) or allocation.get("status") != "active":
        fail("conflict", "allocation is not active")
    username = str(allocation.get("owner") or "")
    if not username:
        fail("protocol", "allocation owner is missing")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        fail("protocol", "allocation owner name is not supported by the tunnel")
    configured_identity = config.policy.vscode_identity_file.strip()
    identity_path = identity
    if identity_path is None and configured_identity:
        identity_path = Path(configured_identity).expanduser()
    if identity_path is None:
        # Prefer the key the member's own SSH config resolves for this alias;
        # default key names are only a last resort.
        identity_path = _alias_identity(alias) or _default_identity()
    if identity_path is not None and not identity_path.is_file():
        fail("bad_request", f"SSH identity file not found: {identity_path}")
    config_path = (
        ssh_config.expanduser() if ssh_config is not None else Path.home() / ".ssh" / "config"
    )
    # Blocks from previous hard-killed tunnels would otherwise accumulate:
    # drop the ones whose local listener is already gone.
    _sweep_stale_managed_blocks(_managed_config_file(config_path), config_path)
    host = f"udc-{allocation_id}"
    try:
        asyncio.run(
            _serve_vscode_tunnel(
                config=config,
                allocation_id=allocation_id,
                username=username,
                ssh_alias=alias,
                host=host,
                requested_port=port,
                ssh_config=config_path,
                identity=identity_path,
                launch_code=code,
                readiness_file=readiness_file,
            )
        )
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        fail("internal", f"could not start the VS Code tunnel: {exc}")
    except RuntimeError as exc:
        fail("internal", f"could not start the VS Code tunnel: {exc}")


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
    os.environ["UDC_RESOURCE_CONFIG"] = str(Path(ctx.obj))
    from .gui.server import run_gui

    run_gui(
        config,
        member_alias=server or config.server.ssh_alias,
        admin_alias=admin_server or config.gui.admin_ssh or None,
        port=port,
        open_browser=not no_browser,
        config_path=str(Path(ctx.obj)),
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
