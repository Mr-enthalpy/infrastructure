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
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, NoReturn

import typer

from . import __version__, version_info
from .client import BrokerClient
from .config import (
    HardwareConfig,
    LabConfig,
    ServerConfig,
    client_config_path,
    load_client_config,
    load_config,
)
from .errors import CitespecError, exit_code_for
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

SERVER_CONFIG = Path("/etc/citespec-resource/config.toml")
me_app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    help="个人状态与公钥管理。",
)
file_app = typer.Typer(no_args_is_help=True, help="Restricted workspace file transfer.")
queue_app = typer.Typer(no_args_is_help=True, help="Queued allocation requests.")
project_app = typer.Typer(no_args_is_help=True, help="科研项目与长期环境记忆。")
env_app = typer.Typer(no_args_is_help=True, help="环境版本的保存、查看与重建。")
app.add_typer(config_app, name="config")
app.add_typer(admin_app, name="admin")
app.add_typer(job_app, name="job")
app.add_typer(me_app, name="me")
app.add_typer(file_app, name="file")
app.add_typer(queue_app, name="queue")
app.add_typer(project_app, name="project")
app.add_typer(env_app, name="env")

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
    configured = os.environ.get("CITESPEC_RESOURCE_CONFIG")
    if configured:
        return Path(configured)
    installed = SERVER_CONFIG
    if installed.exists():
        return installed
    local = Path("lab/resources.local.toml")
    if local.exists():
        return local
    # Member workstations use the small setup-generated profile instead of a
    # copy of the server's hardware/policy TOML (goal §5.4).
    return client_config_path()


def _load(ctx: typer.Context) -> LabConfig:
    path = Path(ctx.obj)
    try:
        return load_config(path)
    except ValueError as exc:
        if "missing [hardware] section" in str(exc):
            try:
                return load_client_config(path)
            except (OSError, ValueError) as profile_exc:
                _fail("bad_request", f"configuration error: {profile_exc}", False)
        _fail("bad_request", f"configuration error: {exc}", False)
    except OSError as exc:
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
    except CitespecError as exc:
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


_MANAGED_CONFIG_NAME = "citespec-vscode-config"


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
    pattern = re.compile(r"(?ms)^# BEGIN CITESPEC VSCODE (\S+)\n.*?^# END CITESPEC VSCODE \1\n?")
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
        f"# BEGIN CITESPEC VSCODE {allocation_id}",
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
    lines.append(f"# END CITESPEC VSCODE {allocation_id}")
    return "\n".join(lines)


def _atomic_config_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Windows OpenSSH refuses SSH config files carrying the OWNER RIGHTS
        # ACE (S-1-3-4) that CPython's tempfile hardening can leave behind.
        # Whatever the source, set an explicit ACL after the atomic rename:
        # only the current user, SYSTEM, and Administrators — exactly what
        # ssh's permission check accepts.
        temp_path = path.parent / f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.citespec-tmp"
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
        suffix=".citespec-tmp",
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
            rf"(?ms)^# BEGIN CITESPEC VSCODE {re.escape(allocation_id)}\n.*?^# END CITESPEC VSCODE "
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
            rf"(?ms)^# BEGIN CITESPEC VSCODE {re.escape(allocation_id)}\n.*?^# END CITESPEC VSCODE "
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
    ssh_command = shlex.join(["citespec-resource", "tunnel", allocation_id])
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
        envvar="CITESPEC_RESOURCE_CONFIG",
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
    _configure_output_encoding()
    ctx.obj = _config_path(config)
    rest = _strip_local_options(sys.argv[1:], {"--config", "--remote", "--local"})
    # Help and no-argument invocations are local UX, not remote requests.
    if not rest or any(token in {"-h", "--help"} for token in rest):
        return
    if remote is not None:
        _dispatch_remote(remote, rest)
        return
    if local:
        return
    if rest[0] not in AUTO_RELAY_COMMANDS:
        return
    if rest[0] == "job" and len(rest) > 1 and rest[1] == "attach":
        return
    # Workstation default: without a local broker socket, talk to the
    # configured server over SSH instead of failing on a missing socket.
    # ``setup`` writes the minimal client profile (no [hardware]), so fall
    # back to it instead of silently disabling relay on member workstations.
    try:
        cfg = load_config(ctx.obj)
    except ValueError as exc:
        if "missing [hardware] section" not in str(exc):
            return
        try:
            cfg = load_client_config(ctx.obj)
        except (OSError, ValueError):
            return
    except OSError:
        return
    if Path(cfg.server.broker_socket).exists():
        return
    # On the host that runs the broker, a missing socket means the broker is
    # stopped or still binding after a restart.  Relaying there would SSH to the
    # server's own alias and fail with "Could not resolve hostname <alias>",
    # which reads like a member-workstation problem.  The runtime directory only
    # exists on that host (a member profile points at the server's path), so it
    # distinguishes the two without relying on the config location.
    if Path(cfg.server.broker_socket).parent.is_dir() or Path(ctx.obj) == SERVER_CONFIG:
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
AUTO_RELAY_COMMANDS = {
    "list",
    "status",
    "apply",
    "release",
    "logs",
    "queue",
    "admin",
    "job",
    "me",
    # ``start`` only needs server configuration and the broker. It used to run
    # locally, which made a stale workstation package silently use old presets
    # and fail when no local broker socket existed.
    "start",
}


START_UNSUPPORTED_MARKERS = (
    "不支持的子命令 start",
    "no such command 'start'",
    'no such command "start"',
    "unsupported subcommand start",
)


def _configure_output_encoding() -> None:
    """Keep member output readable and crash-free on non-UTF-8 consoles.

    Windows consoles default to the ANSI code page (GBK on Chinese systems),
    which cannot encode symbols such as the warning sign and makes redirected
    output hard to read as UTF-8 (C4-8). An interactive console keeps its native
    encoding so Chinese text renders normally, with undecodable characters
    replaced; redirected streams become UTF-8. Test doubles and other
    non-file streams are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            stream.fileno()
        except (OSError, ValueError):
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def _relay_bytes(stream: Any, data: bytes) -> None:
    """Write relayed output without re-encoding it for the local console."""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
        return
    stream.write(data.decode("utf-8", "replace"))
    stream.flush()


def _start_unsupported(output: bytes) -> bool:
    text = output.decode("utf-8", "replace").lower()
    return any(marker in text for marker in START_UNSUPPORTED_MARKERS)


def _start_as_apply(rest: list[str]) -> list[str] | None:
    """Translate ``start`` options into the ``apply`` form older servers know.

    The workstation package always relays ``start`` to the server so presets
    and budgets come from server configuration. A server CLI that predates
    ``start`` (0.1.0) only rejects the subcommand; every option used here maps
    onto ``apply``. Presets and unknown/newer options cannot be translated
    without guessing, so those return ``None``.
    """
    translated = ["apply"]
    args = rest[1:]
    index = 0
    gpu_count_given = False
    while index < len(args):
        token = args[index]
        if token in {"--gpu-count", "--hours", "--idempotency-key"}:
            if index + 1 >= len(args):
                return None
            translated.extend([token, args[index + 1]])
            gpu_count_given = gpu_count_given or token == "--gpu-count"
            index += 2
        elif token.startswith(("--gpu-count=", "--hours=", "--idempotency-key=")):
            translated.append(token)
            gpu_count_given = gpu_count_given or token.startswith("--gpu-count=")
            index += 1
        elif token == "--json":
            translated.append(token)
            index += 1
        elif token == "--open" or token.startswith("--open="):
            index += 2 if token == "--open" else 1
        else:
            return None
    if not gpu_count_given:
        translated.extend(["--gpu-count", "1"])
    translated.extend(["--cpus", "8", "--memory", "32"])
    return translated


def _dispatch_remote(alias: str, rest: list[str]) -> NoReturn:
    """Run `citespec-resource <rest>` on the given SSH alias and relay the result.

    This is the local-workstation path for everyday commands: the member's
    ForceCommand only allows `citespec-resource`, so relaying stays inside the same
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
        shlex.join(["citespec-resource", *rest]),
    ]
    if rest[0] == "start":
        _dispatch_remote_start(argv, rest)
    try:
        completed = subprocess.run(argv, check=False)
    except OSError as exc:
        _fail("ssh_unavailable", f"cannot start ssh: {exc}", False)
    raise SystemExit(completed.returncode)


def _dispatch_remote_start(argv: list[str], rest: list[str]) -> NoReturn:
    """Relay ``start``; fall back to ``apply`` when the server predates it."""
    try:
        completed = subprocess.run(argv, capture_output=True, check=False)
    except OSError as exc:
        _fail("ssh_unavailable", f"cannot start ssh: {exc}", False)
    output = (completed.stdout or b"") + (completed.stderr or b"")
    if completed.returncode == 0 or not _start_unsupported(output):
        _relay_bytes(sys.stdout, completed.stdout or b"")
        _relay_bytes(sys.stderr, completed.stderr or b"")
        raise SystemExit(completed.returncode)
    translated = _start_as_apply(rest)
    if translated is None:
        _relay_bytes(sys.stdout, completed.stdout or b"")
        _relay_bytes(sys.stderr, completed.stderr or b"")
        typer.echo(
            "服务端 CLI 不支持 start，本次参数（如 --preset）无法自动换算为 apply；"
            "请改用 apply，或让管理员升级服务端。",
            err=True,
        )
        raise SystemExit(completed.returncode)
    typer.echo(
        "服务端 CLI 不支持 start，已自动改用 apply（默认 --cpus 8 --memory 32）；"
        "服务端升级后可继续使用 start。",
        err=True,
    )
    fallback = [*argv[:-1], shlex.join(["citespec-resource", *translated])]
    try:
        completed = subprocess.run(fallback, check=False)
    except OSError as exc:
        _fail("ssh_unavailable", f"cannot start ssh: {exc}", False)
    raise SystemExit(completed.returncode)


@app.command()
def version(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print package version and the client/server protocol handshake."""

    if json_output:
        typer.echo(
            json.dumps(
                {"ok": True, "data": version_info()},
                indent=2,
                sort_keys=True,
            )
        )
        return
    typer.echo(__version__)
    typer.echo(f"protocol: {version_info()['protocol']}")
    typer.echo(f"min client protocol: {version_info()['min_client_protocol']}")


@app.command("update")
def update_command(
    check: bool = typer.Option(False, "--check", help="只检查更新，不安装。"),
    manifest: str = typer.Option("", "--manifest", help="覆盖更新清单 URL（测试/镜像）。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """检查并安装打包客户端的签名更新（仅打包客户端可用）。"""

    from .update import (
        UpdateError,
        check_for_update,
        find_app_root,
        install_update,
    )

    root = find_app_root()
    if root is None:
        _fail(
            "bad_request",
            "update is only available in the packaged client; "
            "set CITESPEC_RESOURCE_APP_ROOT for a local test",
            json_output,
        )
    allow_insecure = os.environ.get("CITESPEC_UPDATE_ALLOW_HTTP", "").strip() in {
        "1",
        "true",
        "yes",
    }
    try:
        info = check_for_update(
            manifest_url=manifest or None,
            allow_insecure=allow_insecure,
        )
        if check or not info.available:
            data = {
                "available": info.available,
                "current": info.current_version,
                "latest": info.latest_version,
                "reason": info.reason,
            }
            if json_output:
                typer.echo(json.dumps({"ok": True, "data": data}, indent=2, sort_keys=True))
            else:
                typer.echo(
                    f"current: {info.current_version}  latest: {info.latest_version}  {info.reason}"
                )
            return
        result = install_update(
            info,
            app_root=root,
            allow_insecure=allow_insecure,
        )
        data = {
            "updated": True,
            "previous": result.previous_version,
            "version": result.version,
        }
        if json_output:
            typer.echo(json.dumps({"ok": True, "data": data}, indent=2, sort_keys=True))
        else:
            typer.echo(
                f"updated: {result.previous_version or 'none'} -> {result.version}; "
                "restart the client to use it"
            )
    except (OSError, UpdateError) as exc:
        _fail("protocol_error", str(exc), json_output)


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
            remaining = (
                f"  remaining={_format_remaining(allocation.get('remaining_seconds'))}"
                if allocation.get("status") == "active"
                else ""
            )
            typer.echo(
                f"  {allocation['id']}  owner={allocation['owner']}  "
                f"status={allocation['status']}  gpus={allocation['gpus']}{remaining}"
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
    expires = str(allocation["expires_at"])
    if allocation.get("status") == "active":
        expires = f"{expires} ({_format_remaining(allocation.get('remaining_seconds'))})"
    typer.echo(f"expires: {expires}")
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
    except CitespecError as exc:
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
                allocation_status = allocation.get("status")
                if allocation_status == "failed":
                    _fail(
                        "docker_error",
                        "queued allocation was admitted but container creation failed",
                        json_output,
                    )
                if allocation_status == "active":
                    return allocation
                if allocation_status in {"released", "expired"}:
                    _fail(
                        "conflict",
                        f"queued allocation is {allocation_status}",
                        json_output,
                    )
                # The queue request is admitted as soon as the allocation row is
                # reserved. Container creation and identity setup follow, so a
                # pending allocation is not yet usable: keep polling until it is
                # active instead of handing the caller a half-built allocation.
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
    gpu_count: int | None = typer.Option(
        None,
        "--gpu-count",
        min=0,
        help="Ask the broker to auto-select this many GPUs (0 = CPU-only).",
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
    idempotency_key: str = typer.Option(
        "",
        "--idempotency-key",
        help="Reuse this key when retrying the same logical apply.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Apply for whole GPUs, or CPU-only when --gpus is omitted."""
    indices = _parse_gpu_indices(gpus)
    if gpu_count is not None and indices:
        _fail("bad_request", "--gpus 与 --gpu-count 只能二选一", json_output)
    key = idempotency_key or uuid.uuid4().hex
    args: dict[str, Any] = {
        "gpus": indices,
        "cpus": cpus,
        "memory_gb": memory,
        "note": note,
        "queue": queue,
        "idempotency_key": key,
    }
    if gpu_count is not None:
        args["gpu_count"] = gpu_count
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
                        {
                            "ok": True,
                            "data": {
                                "allocation": allocation,
                                "idempotency_key": key,
                            },
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                _print_allocation(allocation)
                typer.echo(f"idempotency-key: {key}")
            return
        if json_output:
            return
        _print_queue(data)
        typer.echo(f"idempotency-key: {key}")
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
    typer.echo(f"idempotency-key: {key}")


@me_app.callback()
def me_command(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看我的成员状态与公钥。"""
    if ctx.invoked_subcommand is not None:
        return
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
        typer.echo("下一步：联系管理员；或使用 citespec-resource join 提交加入申请。")
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
    gpu_count: int | None = typer.Option(
        None,
        "--gpu-count",
        min=0,
        help="申请 GPU 张数（服务端自动选卡）；预设会提供默认值。",
    ),
    preset: str = typer.Option("", "--preset", help="使用服务端配置的预设名称。"),
    hours: int | None = typer.Option(None, "--hours", min=1, help="时长（小时）。"),
    open_with: str = typer.Option("", "--open", help="创建成功后打开方式：vscode / terminal。"),
    idempotency_key: str = typer.Option(
        "",
        "--idempotency-key",
        help="重复执行时复用同一逻辑请求，避免重复排队。",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """快捷创建工作环境：按张数申请 GPU，服务端自动选卡。"""
    config = _load(ctx)
    cpus: float | None = None
    memory: int | None = None
    resolved_hours = hours
    preset_gpu_count: int | None = None
    if preset:
        preset_table = {entry[0]: entry[1:] for entry in config.onboarding.presets}
        if preset not in preset_table:
            available = ", ".join(preset_table) or "（未配置）"
            _fail("bad_request", f"未知预设 {preset}；可用预设：{available}", json_output)
        preset_gpu_count, preset_cpus, preset_memory, preset_hours = preset_table[preset]
        cpus = preset_cpus
        memory = preset_memory
        resolved_hours = hours if hours is not None else preset_hours
    resolved_hours = resolved_hours if resolved_hours is not None else config.policy.default_hours
    resolved_gpu_count = (
        gpu_count
        if gpu_count is not None
        else (preset_gpu_count if preset_gpu_count is not None else 1)
    )
    key = idempotency_key or uuid.uuid4().hex

    # Same broker path as `apply` — presets only fill defaults; the broker's
    # own validation and budget checks always decide. The preset's GPU value is
    # a COUNT, not a GPU index; sending it as --gpus used to select the wrong
    # card and conflict with --gpu-count.
    request_args: dict[str, Any] = {
        "cpus": cpus if cpus is not None else 8.0,
        "memory_gb": memory if memory is not None else 32,
        "hours": resolved_hours,
        "queue": True,
        "idempotency_key": key,
    }
    if resolved_gpu_count:
        request_args["gpu_count"] = resolved_gpu_count
    data = _request(_make_client(config), "apply", json_output, **request_args)
    if json_output:
        return
    if "queue_id" in data:
        _print_queue(data)
        typer.echo(f"idempotency-key: {key}")
        return
    _print_allocation(data)
    typer.echo(f"idempotency-key: {key}")
    allocation_id = str(data.get("id", ""))
    if open_with == "vscode":
        typer.echo("在 VS Code 中打开：citespec-resource vscode " + allocation_id)
        typer.echo("或在你自己的电脑运行 citespec-resource gui。")
    elif open_with == "terminal":
        typer.echo("进入终端：citespec-resource connect " + allocation_id)


def _connection_config(
    ctx: typer.Context,
    *,
    host: str,
    user: str,
    port: int | None,
    yes: bool,
    require_user: bool,
) -> tuple[LabConfig, str]:
    """Resolve setup/doctor connection data with or without a server TOML."""
    path = Path(ctx.obj)
    config: LabConfig | None = None
    for loader in (load_config, load_client_config):
        try:
            config = loader(path)
            break
        except (OSError, ValueError):
            continue
    if config is None:
        if path.exists():
            _fail(
                "bad_request",
                f"configuration error: {path} exists but is not a valid server or client profile",
                False,
            )
        if not host:
            _fail(
                "bad_request",
                "尚未配置服务器；请使用 --host <服务器地址>（可选 --user/--port）",
                False,
            )
        config = LabConfig(
            server=ServerConfig(name=host, ssh_alias=host),
            hardware=HardwareConfig(
                gpu_count=1,
                gpu_model="client-profile",
                gpu_memory_mib=0,
                cpu_logical=1,
                ram_gib=1,
            ),
        )
    target = host or config.server.ssh_alias
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,254}", target):
        _fail("bad_request", "服务器地址包含不支持的字符", False)
    if user and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user):
        _fail("bad_request", "Linux 用户名包含不支持的字符", False)
    if require_user and not user:
        if yes:
            _fail(
                "bad_request",
                "非交互 setup 需要 --user <Linux 用户名>",
                False,
            )
        ssh_user = typer.prompt(
            "服务器用户名（批准通知中的 Linux 用户名）",
            default=config.server.ssh_user or os.environ.get("USER", ""),
        )
    else:
        ssh_user = user or config.server.ssh_user
    if ssh_user and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", ssh_user):
        _fail("bad_request", "Linux 用户名包含不支持的字符", False)
    server = replace(
        config.server,
        ssh_alias=config.server.ssh_alias or target,
        ssh_user=ssh_user,
        ssh_port=port or config.server.ssh_port,
    )
    return replace(config, server=server), target


@app.command("setup")
def setup_command(
    ctx: typer.Context,
    host: str = typer.Option(
        "",
        "--host",
        "--server",
        help="服务器地址或 SSH 别名；默认为配置的 ssh_alias。",
    ),
    user: str = typer.Option("", "--user", help="批准通知中的 Linux 用户名。"),
    port: int | None = typer.Option(None, "--port", min=1, max=65535),
    fingerprint: str = typer.Option(
        "",
        "--fingerprint",
        help="管理员通过可信渠道公布的主机 SHA256 指纹。",
    ),
    yes: bool = typer.Option(False, "--yes", help="跳过普通确认（仍需可信指纹）。"),
) -> None:
    """工作站配置向导：写入托管 SSH 片段并验证连接与成员状态。"""

    from .onboarding import run_setup

    config, target = _connection_config(
        ctx,
        host=host,
        user=user,
        port=port,
        yes=yes,
        require_user=True,
    )
    code = run_setup(
        config,
        host=target,
        yes=yes,
        expected_fingerprint=fingerprint,
    )
    raise SystemExit(code)


@app.command("doctor")
def doctor_command(
    ctx: typer.Context,
    host: str = typer.Option(
        "",
        "--host",
        "--server",
        help="服务器地址或 SSH 别名；默认为配置的 ssh_alias。",
    ),
    user: str = typer.Option("", "--user", help="Linux 用户名。"),
    port: int | None = typer.Option(None, "--port", min=1, max=65535),
) -> None:
    """只读诊断：本机环境、连接与成员状态。"""
    from .onboarding import run_doctor

    config, target = _connection_config(
        ctx,
        host=host,
        user=user,
        port=port,
        yes=True,
        require_user=False,
    )
    code = run_doctor(config, host=target)
    raise SystemExit(code)


@app.command("join")
def join_command(
    ctx: typer.Context,
    status_only: bool = typer.Option(False, "--status", help="查询已提交申请的状态。"),
    portal: str = typer.Option(
        "", "--portal", help="申请服务地址（如 https://lab.example.invalid/compute）。"
    ),
    invite_id: str = typer.Option("", "--invite-id", help="管理员发给你的邀请编号。"),
    interactive: bool = typer.Option(True, "--interactive/--non-interactive"),
    application_file: Path | None = typer.Option(
        None, "--application-file", help="提交 JSON 申请文件（离线/非交互模式）。"
    ),
) -> None:
    """新成员入口：提交加入申请或查询申请状态（无需服务器账号）。

    邀请密钥与回执密钥通过受保护输入提供，绝不出现在命令行参数或日志中。
    """
    import urllib.error
    import urllib.request

    # A new applicant has no server configuration yet: --portal alone must be
    # enough. Configuration is only a convenience default for guide/apply URLs.
    config = None
    try:
        config = load_config(Path(ctx.obj))
    except (OSError, ValueError):
        pass
    target = portal or (config.onboarding.apply_url if config is not None else "")
    if not target:
        _fail(
            "bad_request",
            "未配置申请入口地址；请使用 --portal 或联系管理员获取",
            False,
        )
    target = target.rstrip("/")

    def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{target}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except ValueError:
                return {"ok": False, "error": {"code": "http", "message": str(exc)}}
        except urllib.error.URLError as exc:
            _fail("unavailable", f"无法连接申请服务 {target}：{exc.reason}", False)

    if status_only:
        import getpass

        request_id = typer.prompt("申请编号")
        receipt = getpass.getpass("回执密钥：")
        # POST body, never a query string / proxy log / browser history.
        data = _post(
            "/api/requests/status",
            {"request_id": request_id, "receipt": receipt},
        )
        if not data.get("ok"):
            error = data.get("error", {})
            _fail(str(error.get("code", "error")), str(error.get("message", "")), False)
        info = data["data"]
        typer.echo(f"申请状态：{info.get('status_label', info['status'])}")
        if info.get("public_note"):
            typer.echo(f"管理员说明：{info['public_note']}")
        return

    import getpass

    if application_file is not None and interactive:
        _fail("bad_request", "--application-file 请配合 --non-interactive 使用", False)
    if not interactive:
        if application_file is None:
            _fail("bad_request", "非交互模式需要 --application-file", False)
        try:
            application = json.loads(application_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _fail("bad_request", f"申请文件无效：{exc}", False)
        if not invite_id:
            _fail("bad_request", "非交互模式需要 --invite-id", False)
        invite_secret = getpass.getpass("邀请密钥：")
    else:
        typer.echo("=== 实验室计算资源加入申请 ===")
        if config is not None and config.onboarding.guide_url:
            typer.echo(f"使用指南：{config.onboarding.guide_url}")
        application = {
            "display_name": typer.prompt("显示姓名"),
            "username": typer.prompt("期望用户名（小写字母开头）"),
            "affiliation": typer.prompt("实验室归属或联系人"),
            "purpose": typer.prompt("申请用途"),
            "contact": typer.prompt("用于核验的联系方式"),
            "nickname": typer.prompt("昵称（可选）", default=""),
            "public_contact": typer.prompt("是否公开协作联系方式？(y/N)", default="n"),
            "agree_terms": "1",
            "public_keys": [typer.prompt("SSH 公钥（.pub 文件内容，一行）")],
        }
        invite_id = typer.prompt("邀请编号")
        invite_secret = getpass.getpass("邀请密钥：")

    data = _post(
        "/api/requests",
        {
            "invite_id": invite_id,
            "invite_secret": invite_secret,
            "application": application,
        },
    )
    if not data.get("ok"):
        error = data.get("error", {})
        _fail(str(error.get("code", "error")), str(error.get("message", "")), False)
    result = data["data"]
    typer.echo("")
    typer.echo("申请已提交。请妥善保存以下信息（回执密钥仅显示一次）：")
    typer.echo(f"  申请编号：{result['request_id']}")
    typer.echo(f"  回执密钥：{result['receipt_secret']}")
    typer.echo("查询状态：citespec-resource join --status")


@me_app.command("keys")
def me_keys(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="list / add / revoke"),
    key_ref: str = typer.Argument(
        "",
        help="add 时为 .pub 文件；revoke 时为 key-id（也可用 --key-file/--key-id）。",
    ),
    key_file: Path | None = typer.Option(None, "--key-file", help="公钥 .pub 文件。"),
    key_id: str = typer.Option("", "--key-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """管理我的登录公钥（只提交 .pub 公钥）。"""
    config = _load(ctx)
    if action == "list":
        data = _request(_make_client(config), "me", json_output)
        if json_output:
            return
        for key in data.get("keys", []):
            state = "有效" if key.get("status") == "active" else "已撤销"
            typer.echo(f"[{state}] {key['key_id']}  {key['fingerprint']}")
        return
    if action == "add":
        if key_file is None and key_ref:
            key_file = Path(key_ref)
        if key_file is None:
            _fail("bad_request", "add 需要 .pub 公钥文件（位置参数或 --key-file）", json_output)
        try:
            public_key = key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            _fail("bad_request", f"无法读取公钥文件：{exc}", json_output)
        data = _request(
            _make_client(config),
            "me_keys_add",
            json_output,
            public_key=public_key,
        )
        if json_output:
            return
        typer.echo(f"已添加：{data['fingerprint']}")
        typer.echo("密钥已同步到服务器；下一次 SSH 连接即可使用。")
        return
    if action == "revoke":
        key_id = key_id or key_ref
        if not key_id:
            _fail("bad_request", "revoke 需要 key-id（位置参数或 --key-id）", json_output)
        if not json_output:
            typer.confirm(
                "撤销后该密钥立即失效；确认这是你的设备且你有其他可用密钥？",
                abort=True,
            )
        data = _request(
            _make_client(config),
            "me_keys_revoke",
            json_output,
            key_id=key_id,
        )
        if json_output:
            return
        typer.echo(f"已撤销：{data['key_id']}")
        return
    _fail("bad_request", f"未知操作 {action}", json_output)


@app.command("menu")
def menu_command(ctx: typer.Context) -> None:
    """Small interactive menu for members; never exposes a host shell."""

    config = _load(ctx)
    client = _make_client(config)
    while True:
        typer.echo("")
        typer.echo("=== 实验室计算资源 ===")
        typer.echo("  1) 查看资源")
        typer.echo("  2) 我的工作环境")
        typer.echo("  3) 等待队列")
        typer.echo("  4) 创建工作环境")
        typer.echo("  5) 结束工作环境并释放资源")
        typer.echo("  6) 帮助 / 命令列表")
        typer.echo("  0) 退出")
        choice = typer.prompt("请选择", default="0").strip()
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
                typer.echo("还没有工作环境；选择 4 创建。")
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
                typer.echo("当前没有等待中的申请。")
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
                "更多命令见 citespec-resource --help；"
                "工作台：在你自己的电脑运行 citespec-resource gui。"
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


def _print_env_version(summary: dict[str, Any]) -> None:
    typer.echo(
        f"{summary['ref']}  fidelity={summary['capture_fidelity']}  "
        f"verification={summary['verification']}  visibility={summary['visibility']}  "
        f"created={summary['created_at']}"
    )


@project_app.command("create")
def project_create_command(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="项目名，例如 reconstruction。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """创建一个科研项目（自动带一个默认环境档案）。"""
    config = _load(ctx)
    data = _request(_make_client(config), "project_create", json_output, name=name)
    if json_output:
        return
    project = data["project"]
    typer.echo(f"created: {project['name']}  versions={project['versions']}")


@project_app.command("list")
def project_list_command(
    ctx: typer.Context,
    all_users: bool = typer.Option(False, "--all", help="管理员查看所有项目。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出我的项目。"""
    config = _load(ctx)
    data = _request(_make_client(config), "project_list", json_output, all=all_users)
    if json_output:
        return
    projects = data.get("projects", [])
    if not projects:
        typer.echo("还没有项目；先运行 citespec-resource project create --name <名称>")
        return
    for project in projects:
        typer.echo(
            f"{project['name']}  status={project['status']}  versions={project['versions']}  "
            f"latest={project.get('latest') or '-'}"
        )


@project_app.command("show")
def project_show_command(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="项目名。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看项目及其环境版本。"""
    config = _load(ctx)
    data = _request(_make_client(config), "project_show", json_output, name=name)
    if json_output:
        return
    project = data["project"]
    typer.echo(f"{project['name']}  status={project['status']}  versions={project['versions']}")
    versions = data.get("versions", [])
    if not versions:
        typer.echo("  还没有保存的环境；在运行中的工作环境里执行 env capture")
    for version in versions:
        typer.echo(
            "  "
            + "  ".join(
                (
                    version["ref"],
                    f"fidelity={version['capture_fidelity']}",
                    f"verification={version['verification']}",
                    f"visibility={version['visibility']}",
                )
            )
        )


@project_app.command("archive")
def project_archive_command(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="项目名。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """归档项目（历史版本仍可查看）。"""
    config = _load(ctx)
    data = _request(_make_client(config), "project_archive", json_output, name=name)
    if json_output:
        return
    project = data["project"]
    typer.echo(f"archived: {project['name']}")


@env_app.command("capture")
def env_capture_command(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="目标项目名。"),
    allocation: str = typer.Option(..., "--allocation", help="正在运行的工作环境 id。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """只读采集当前工作环境的依赖，保存为新的不可变版本。"""
    config = _load(ctx)
    data = _request(
        _make_client(config),
        "env_capture",
        json_output,
        project=project,
        allocation=allocation,
    )
    if json_output:
        return
    version = data["version"]
    typer.echo(
        f"saved {version['ref']}  fidelity={version['capture_fidelity']}  "
        f"artifacts={version['artifacts']}"
    )
    for note in version.get("notes", []):
        typer.echo(f"  - {note}")


@env_app.command("list")
def env_list_command(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="项目名。"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出项目的环境版本。"""
    config = _load(ctx)
    data = _request(_make_client(config), "env_list", json_output, project=project, limit=limit)
    if json_output:
        return
    versions = data.get("versions", [])
    if not versions:
        typer.echo("还没有环境版本")
        return
    for version in versions:
        _print_env_version(version)


@env_app.command("show")
def env_show_command(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="环境引用，例如 reconstruction@v1。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看一个环境版本的 manifest、锁文件与构建记录。"""
    config = _load(ctx)
    data = _request(_make_client(config), "env_show", json_output, ref=ref)
    if json_output:
        return
    _print_env_version(data["version"])
    manifest = data.get("manifest", {})
    base = manifest.get("base", {})
    typer.echo(f"  base: {base.get('image', '')}  digest={base.get('digest') or '-'}")
    python = manifest.get("python", {})
    typer.echo(
        f"  python: {python.get('version') or 'unknown'} ({python.get('manager') or '-'})  "
        f"packages={len(python.get('packages', []))}"
    )
    system = manifest.get("system", {})
    typer.echo(f"  system: {len(system.get('packages', []))} packages")
    artifacts = data.get("artifacts", [])
    if artifacts:
        typer.echo("  lockfiles: " + ", ".join(str(item["name"]) for item in artifacts))
    builds = data.get("builds", [])
    if builds:
        latest = builds[0]
        typer.echo(f"  latest build: {latest['status']}  image={latest.get('image_id') or '-'}")
    for note in manifest.get("notes", []):
        typer.echo(f"  - {note}")


@env_app.command("diff")
def env_diff_command(
    ctx: typer.Context,
    left: str = typer.Argument(..., help="旧版本，例如 reconstruction@v1。"),
    right: str = typer.Argument(..., help="新版本，例如 reconstruction@v2。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """比较两个环境版本的依赖差异。"""
    config = _load(ctx)
    data = _request(_make_client(config), "env_diff", json_output, left=left, right=right)
    if json_output:
        return
    diff = data["diff"]
    if not diff["changed"]:
        typer.echo("两个环境版本内容相同")
        return
    base = diff.get("base", {})
    for key, change in base.items():
        typer.echo(f"base.{key}: {change['from']} -> {change['to']}")
    python = diff.get("python", {})
    version_change = python.get("version")
    if version_change:
        typer.echo(f"python.version: {version_change['from']} -> {version_change['to']}")
    for item in python.get("packages", {}).get("added", []):
        typer.echo(f"+ python {item['name']}=={item['version'] or '?'}")
    for item in python.get("packages", {}).get("removed", []):
        typer.echo(f"- python {item['name']}=={item['version'] or '?'}")
    for item in python.get("packages", {}).get("changed", []):
        before, after = item["from"], item["to"]
        typer.echo(
            f"~ python {before['name']} {before['version'] or '?'} -> {after['version'] or '?'}"
        )
    for item in diff.get("system", {}).get("packages", {}).get("added", []):
        typer.echo(f"+ system {item['name']} {item['version']}")
    for item in diff.get("system", {}).get("packages", {}).get("removed", []):
        typer.echo(f"- system {item['name']} {item['version']}")
    for item in diff.get("system", {}).get("packages", {}).get("changed", []):
        typer.echo(
            f"~ system {item['from']['name']} {item['from']['version']} -> {item['to']['version']}"
        )


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
    except CitespecError as exc:
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
    except CitespecError as exc:
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
    except CitespecError as exc:
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
    except CitespecError as exc:
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
    remote = shlex.join(["citespec-resource", "file", "put", allocation_id, remote_path])
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
    remote = shlex.join(["citespec-resource", "file", "get", allocation_id, remote_path])
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
    except CitespecError as exc:
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

    Equivalent to `ssh <alias> citespec-resource shell <id>`: the session streams
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
    remote_parts = ["citespec-resource", "shell", allocation_id]
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
    except CitespecError as exc:
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
    remote = shlex.join(["citespec-resource", "status", allocation_id, "--json"])
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
    host = f"citespec-{allocation_id}"
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
    os.environ["CITESPEC_RESOURCE_CONFIG"] = str(Path(ctx.obj))
    from .gui.server import run_gui

    run_gui(
        config,
        member_alias=server or config.server.ssh_alias,
        admin_alias=admin_server or config.gui.admin_ssh or None,
        port=port,
        open_browser=not no_browser,
        config_path=str(Path(ctx.obj)),
    )


@admin_app.command("invitations")
def admin_invitations(
    ctx: typer.Context,
    create: bool = typer.Option(False, "--create", help="生成一个新邀请。"),
    validity_hours: int = typer.Option(168, "--validity-hours", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """生成或列出加入邀请（直接写入 intake 存储）。"""
    config = _load(ctx)
    from .enrollment import store as intake_store

    connection = intake_store.connect(config.onboarding.intake_db)
    try:
        if create:
            invitation = intake_store.create_invitation(
                connection,
                created_by="admin-cli",
                validity_hours=validity_hours,
            )
            if json_output:
                typer.echo(json.dumps({"ok": True, "data": invitation}))
                return
            typer.echo(f"邀请编号：{invitation['invite_id']}")
            typer.echo(f"邀请密钥（仅显示一次，通过可信渠道发送）：{invitation['invite_secret']}")
            typer.echo(f"有效期至：{invitation['expires_at']}")
            return
        rows = connection.execute(
            "SELECT invite_id, created_by, created_at, expires_at, revoked, "
            "used_by_request FROM invitations ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        if json_output:
            typer.echo(
                json.dumps({"ok": True, "data": {"invitations": [dict(row) for row in rows]}})
            )
            return
        for row in rows:
            state = "已撤销" if row["revoked"] else ("已使用" if row["used_by_request"] else "有效")
            typer.echo(f"{row['invite_id']}  {state}  {row['created_at']}  by {row['created_by']}")
    finally:
        connection.close()


@admin_app.command("requests")
def admin_requests(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="list / show / approve / reject / needs-info / import"),
    request_id: str = typer.Option("", "--request-id"),
    valid_until: str = typer.Option("", "--valid-until", help="成员资格有效期（ISO 时间）。"),
    quota_gb: int = typer.Option(200, "--quota-gb", min=1),
    review_note: str = typer.Option("", "--review-note", help="申请人可见说明。"),
    internal_note: str = typer.Option("", "--internal-note", help="仅管理员可见备注。"),
    expected_version: str = typer.Option(
        "",
        "--expected-version",
        help="预览时的申请版本；内容变化后拒绝批准（推荐总是提供）。",
    ),
    status_filter: str = typer.Option("", "--status", help="list 时按状态筛选。"),
    file_path: Path | None = typer.Option(None, "--file", help="离线申请文件路径。"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """审核加入申请：list / show / approve / reject / needs-info / import。"""
    config = _load(ctx)

    if action == "list":
        args: dict[str, Any] = {}
        if status_filter:
            args["status"] = status_filter
        _request(_make_client(config), "admin_requests_list", json_output, **args)
        return
    if action == "show":
        if not request_id:
            _fail("bad_request", "show 需要 --request-id", json_output)
        _request(_make_client(config), "admin_requests_show", json_output, request_id=request_id)
        return
    if action == "approve":
        if not request_id:
            _fail("bad_request", "approve 需要 --request-id", json_output)
        data = _request(
            _make_client(config),
            "admin_requests_approve",
            json_output,
            request_id=request_id,
            valid_until=valid_until,
            quota_gb=quota_gb,
            review_note=review_note,
            internal_note=internal_note,
            expected_version=expected_version,
        )
        if json_output:
            return
        typer.echo(f"申请 {data.get('request_id')} 已批准并提交开户。")
        typer.echo(
            f"开户操作：{data.get('operation_id')}  "
            f"状态：{data.get('operation_status', data.get('status'))}"
        )
        return
    if action == "reject":
        if not request_id:
            _fail("bad_request", "reject 需要 --request-id", json_output)
        if not review_note:
            _fail("bad_request", "拒绝必须提供 --review-note（申请人可见原因）", json_output)
        data = _request(
            _make_client(config),
            "admin_requests_reject",
            json_output,
            request_id=request_id,
            review_note=review_note,
            internal_note=internal_note,
            expected_version=expected_version,
        )
        if json_output:
            return
        typer.echo(f"申请 {data.get('request_id')} 已拒绝；未创建任何账号或资源。")
        return
    if action == "needs-info":
        if not request_id:
            _fail("bad_request", "needs-info 需要 --request-id", json_output)
        if not review_note:
            _fail("bad_request", "needs-info 必须提供 --review-note", json_output)
        data = _request(
            _make_client(config),
            "admin_requests_needs_info",
            json_output,
            request_id=request_id,
            review_note=review_note,
            internal_note=internal_note,
            expected_version=expected_version,
        )
        if json_output:
            return
        typer.echo(f"申请 {data.get('request_id')} 已标记为需要补充材料。")
        return
    if action == "import":
        if file_path is None:
            _fail("bad_request", "import 需要 --file", json_output)
        from .enrollment import store as intake_store

        connection = intake_store.connect(config.onboarding.intake_db)
        try:
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                _fail("bad_request", f"申请文件无效：{exc}", json_output)
            try:
                result = intake_store.submit_application(
                    connection,
                    invite_id=str(raw.get("invite_id", "")),
                    invite_secret=str(raw.get("invite_secret", "")),
                    application=raw.get("application", {}),
                )
            except ValueError as exc:
                _fail("bad_request", str(exc), json_output)
            if json_output:
                typer.echo(json.dumps({"ok": True, "data": result}))
                return
            typer.echo(f"离线申请已导入：{result['request_id']}")
            if result.get("receipt_secret"):
                typer.echo(f"回执密钥（仅显示一次）：{result['receipt_secret']}")
        finally:
            connection.close()
        return
    _fail(
        "bad_request",
        f"未知操作 {action}；可用：list/show/approve/reject/needs-info/import",
        json_output,
    )


@admin_app.command("members")
def admin_members(
    ctx: typer.Context,
    action: str = typer.Argument(
        ...,
        help=("list / show / suspend / resume / revoke / key-add / key-revoke / import-legacy"),
    ),
    member_id: str = typer.Option("", "--member-id"),
    reason: str = typer.Option("", "--reason", help="状态变更原因（必填）。"),
    public_key: str = typer.Option("", "--public-key", help="成员公钥（key-add）。"),
    public_key_file: Path | None = typer.Option(
        None, "--public-key-file", help="从 .pub 文件读取成员公钥。"
    ),
    key_id: str = typer.Option("", "--key-id", help="要撤销的公钥记录 ID。"),
    file_path: Path | None = typer.Option(None, "--file", help="legacy import 的 JSON 清单。"),
    apply_import: bool = typer.Option(
        False, "--apply", help="真正执行 legacy import；默认只做 dry-run。"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看和管理已有成员。"""
    config = _load(ctx)
    if action == "list":
        _request(_make_client(config), "admin_members_list", json_output)
        return
    if action == "show":
        if not member_id:
            _fail("bad_request", "show 需要 --member-id", json_output)
        data = _request(
            _make_client(config),
            "admin_members_show",
            json_output,
            member_id=member_id,
        )
        if json_output:
            return
        member = data.get("member", {})
        typer.echo(f"成员：{member.get('member_id')}")
        typer.echo(f"Linux：{member.get('linux_username')} (uid {member.get('linux_uid')})")
        typer.echo(f"状态：{member.get('status')}  访问版本：{member.get('access_version')}")
        typer.echo(f"有效期：{member.get('valid_until') or '无固定期限'}")
        typer.echo("公钥：")
        for key in data.get("keys", []):
            typer.echo(f"  {key.get('key_id')}  {key.get('status')}  {key.get('fingerprint')}")
        typer.echo("操作：")
        for operation in data.get("operations", []):
            typer.echo(
                f"  {operation.get('operation_id')}  {operation.get('action')}  "
                f"{operation.get('status')}  attempts={operation.get('attempts')}"
            )
            if operation.get("last_error"):
                typer.echo(f"    error: {operation.get('last_error')}")
        return
    if action == "key-add":
        if not member_id:
            _fail("bad_request", "key-add 需要 --member-id", json_output)
        raw = public_key
        if public_key_file is not None:
            try:
                raw = public_key_file.read_text(encoding="utf-8")
            except OSError as exc:
                _fail("bad_request", f"无法读取公钥文件：{exc}", json_output)
        if not raw.strip():
            _fail("bad_request", "key-add 需要 --public-key 或 --public-key-file", json_output)
        data = _request(
            _make_client(config),
            "admin_members_keys_add",
            json_output,
            member_id=member_id,
            public_key=raw,
        )
        if json_output:
            return
        typer.echo(f"已添加公钥：{data.get('key_id')}（{data.get('fingerprint')}）")
        return
    if action == "key-revoke":
        if not member_id or not key_id:
            _fail("bad_request", "key-revoke 需要 --member-id 与 --key-id", json_output)
        if not json_output:
            typer.confirm(f"确认撤销成员 {member_id} 的公钥 {key_id}？", abort=True)
        data = _request(
            _make_client(config),
            "admin_members_keys_revoke",
            json_output,
            member_id=member_id,
            key_id=key_id,
        )
        if json_output:
            return
        typer.echo(f"已撤销公钥：{data.get('key_id')}")
        return
    if action == "import-legacy":
        if file_path is None:
            _fail("bad_request", "import-legacy 需要 --file", json_output)
        try:
            manifest = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _fail("bad_request", f"清单文件无效：{exc}", json_output)
        if not isinstance(manifest, dict):
            _fail("bad_request", "清单文件必须是 JSON 对象", json_output)
        public_keys = manifest.get("public_keys", [])
        if not isinstance(public_keys, list) or any(
            not isinstance(key, str) for key in public_keys
        ):
            _fail("bad_request", "public_keys 必须是字符串数组", json_output)
        if public_key_file is not None:
            try:
                public_keys = [
                    *public_keys,
                    public_key_file.read_text(encoding="utf-8"),
                ]
            except OSError as exc:
                _fail("bad_request", f"无法读取公钥文件：{exc}", json_output)
        if apply_import and not json_output:
            username = str(manifest.get("username", ""))
            typer.confirm(
                f"确认把已有 Linux 账号 {username} 纳管为正式成员？"
                "该账号的 shell/组/workspace/公钥会被改为受管状态。",
                abort=True,
            )
        try:
            quota_gb = int(manifest.get("quota_gb", 200))
        except (TypeError, ValueError):
            _fail("bad_request", "quota_gb 必须是整数", json_output)
        data = _request(
            _make_client(config),
            "admin_members_import_legacy",
            json_output,
            username=str(manifest.get("username", "")),
            display_name=str(manifest.get("display_name", "")),
            valid_until=str(manifest.get("valid_until", "")),
            quota_gb=quota_gb,
            public_keys=public_keys,
            dry_run=not apply_import,
        )
        if json_output:
            return
        report = data.get("report", data)
        mode = "DRY-RUN" if report.get("dry_run") else "APPLY"
        typer.echo(f"legacy import {mode}：{report.get('username')}")
        inspection = report.get("inspection") or {}
        if inspection.get("exists"):
            typer.echo(
                f"  账号：uid={inspection.get('uid')} home={inspection.get('home')} "
                f"managed={inspection.get('managed')}"
            )
            typer.echo(f"  组：{', '.join(inspection.get('groups') or [])}")
        for warning in report.get("warnings") or []:
            typer.echo(f"  警告：{warning}")
        conflicts = report.get("conflicts") or []
        if conflicts:
            for conflict in conflicts:
                typer.echo(f"  冲突：{conflict}")
            _fail("conflict", "legacy import 存在未解决冲突，未修改任何状态", False)
        if not apply_import:
            typer.echo("dry-run 通过；确认清单后使用 --apply 执行。")
            return
        typer.echo(
            f"成员：{report.get('member_id')}  操作：{report.get('operation_id')}  "
            f"状态：{report.get('operation_status')}"
        )
        if report.get("operation_error"):
            typer.echo(f"失败阶段：{report['operation_error']}")
        return
    if action in {"suspend", "resume", "revoke"}:
        if not member_id:
            _fail("bad_request", f"{action} 需要 --member-id", json_output)
        if not reason:
            _fail("bad_request", f"{action} 必须提供 --reason", json_output)
        if action in {"suspend", "revoke"} and not json_output:
            typer.confirm(
                f"确认对成员 {member_id} 执行 {action}？原因：{reason}",
                abort=True,
            )
        command = f"admin_members_{action}"
        data = _request(
            _make_client(config),
            command,
            json_output,
            member_id=member_id,
            reason=reason,
        )
        if json_output:
            return
        typer.echo(f"成员 {member_id}：{data.get('status')}")
        if data.get("credential_revocation") == "pending_retry":
            typer.echo("注意：凭据撤销待重试；访问已被状态阻断。")
        return
    _fail(
        "bad_request",
        "未知操作 "
        f"{action}；可用：list/show/suspend/resume/revoke/key-add/"
        "key-revoke/import-legacy",
        json_output,
    )


@admin_app.command("operations")
def admin_operations(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="show / retry"),
    operation_id: str = typer.Option("", "--operation-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看与重试开户/撤权操作。"""
    config = _load(ctx)
    if action == "show":
        if not operation_id:
            _fail("bad_request", "show 需要 --operation-id", json_output)
        _request(
            _make_client(config),
            "admin_operations_show",
            json_output,
            operation_id=operation_id,
        )
        return
    if action == "retry":
        if not operation_id:
            _fail("bad_request", "retry 需要 --operation-id", json_output)
        data = _request(
            _make_client(config),
            "admin_operations_retry",
            json_output,
            operation_id=operation_id,
        )
        if json_output:
            return
        typer.echo(f"操作 {operation_id}：{data.get('status')}")
        if data.get("stages"):
            typer.echo(f"阶段：{', '.join(data['stages'])}")
        return
    _fail("bad_request", f"未知操作 {action}", json_output)


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


@admin_app.command("portal")
def admin_portal(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="list / create / set-role"),
    username: str = typer.Option("", "--username", help="Portal 本地账号名。"),
    role: str = typer.Option("", "--role", help="member 或 admin（set-role 必填）。"),
    member_id: str = typer.Option("", "--member-id", help="关联的成员 ID（可选）。"),
    linux_username: str = typer.Option(
        "", "--linux-username", help="关联的 Linux 用户名（可选）。"
    ),
    display_name: str = typer.Option("", "--display-name"),
    contact: str = typer.Option("", "--contact"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """CLI-only Portal 账号引导与角色变更。"""
    config = _load(ctx)
    if action == "list":
        data = _request(_make_client(config), "admin_portal_accounts_list", json_output)
        if json_output:
            return
        for account in data.get("accounts", []):
            typer.echo(
                f"{account.get('username')}  role={account.get('role')}  "
                f"status={account.get('status')}  member={account.get('member_id') or '-'}  "
                f"linux={account.get('linux_username') or '-'}"
            )
        return
    if action == "create":
        if not username:
            _fail("bad_request", "create 需要 --username", json_output)
        target_role = role or "member"
        if target_role not in {"member", "admin"}:
            _fail("bad_request", "role 必须是 member 或 admin", json_output)
        if target_role == "admin" and not json_output:
            typer.confirm(
                f"确认创建 Portal 管理员 {username} 并生成一次性激活码？",
                abort=True,
            )
        data = _request(
            _make_client(config),
            "admin_portal_accounts_create",
            json_output,
            username=username,
            role=target_role,
            member_id=member_id,
            linux_username=linux_username,
            display_name=display_name,
            contact=contact,
        )
        if json_output:
            return
        typer.echo(f"Portal 账号：{data.get('username')}  角色：{target_role}")
        typer.echo(f"一次性激活码（仅显示一次）：{data.get('activation_code')}")
        typer.echo(f"有效期至：{data.get('expires_at')}")
        typer.echo("激活地址：https://citespec-portal.<lab-domain>/activate")
        return
    if action == "set-role":
        if not username or not role:
            _fail("bad_request", "set-role 需要 --username 与 --role", json_output)
        if role not in {"member", "admin"}:
            _fail("bad_request", "role 必须是 member 或 admin", json_output)
        if not json_output:
            typer.confirm(
                f"确认把 Portal 账号 {username} 的角色改为 {role}？现有会话将被注销。",
                abort=True,
            )
        data = _request(
            _make_client(config),
            "admin_portal_accounts_set_role",
            json_output,
            username=username,
            role=role,
        )
        if json_output:
            return
        typer.echo(f"Portal 账号 {data.get('username')}：{data.get('role')}")
        return
    _fail(
        "bad_request",
        f"未知操作 {action}；可用：list/create/set-role",
        json_output,
    )


@admin_app.command("portal-auth")
def admin_portal_auth(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="list / enroll / remove"),
    username: str = typer.Option("", "--username", help="Broker administrator username"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Manage Broker-side credentials used for Portal admin attestations.

    These credentials and TOTP enrollments are intentionally separate from the
    Portal login/reauth TOTP.  The Broker verifies them independently before it
    accepts a Portal admin mutation.
    """

    config = _load(ctx)
    if action == "list":
        data = _request(_make_client(config), "admin_portal_auth_list", json_output)
        if json_output:
            return
        for admin in data.get("admins", []):
            typer.echo(
                f"{admin.get('username')}  enabled={admin.get('enabled')}  "
                f"last_used={admin.get('last_used_at') or '-'}  "
                f"locked_until={admin.get('locked_until') or '-'}"
            )
        return
    if action == "enroll":
        if not username:
            _fail("bad_request", "enroll requires --username", json_output)
        if not json_output:
            typer.confirm(
                f"Enroll Broker administrator credentials for {username}? "
                "This replaces any existing Broker credential.",
                abort=True,
            )
        password = typer.prompt(
            "Broker administrator password",
            hide_input=True,
            confirmation_prompt=not json_output,
        )
        data = _request(
            _make_client(config),
            "admin_portal_auth_enroll",
            json_output,
            username=username,
            password=password,
        )
        if json_output:
            return
        typer.echo(f"Broker administrator: {data.get('username')}")
        typer.echo("Broker TOTP secret (show once; separate from Portal TOTP):")
        typer.echo(str(data.get("totp_secret", "")))
        typer.echo(f"OTP URI: {data.get('otp_uri', '')}")
        return
    if action == "remove":
        if not username:
            _fail("bad_request", "remove requires --username", json_output)
        if not json_output:
            typer.confirm(
                f"Remove Broker administrator credentials for {username}? "
                "Portal admin mutations by this account will fail closed.",
                abort=True,
            )
        data = _request(
            _make_client(config),
            "admin_portal_auth_remove",
            json_output,
            username=username,
        )
        if json_output:
            return
        typer.echo(f"Removed Broker administrator credentials: {data.get('username')}")
        return
    _fail(
        "bad_request",
        f"unknown action {action}; use list/enroll/remove",
        json_output,
    )


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
