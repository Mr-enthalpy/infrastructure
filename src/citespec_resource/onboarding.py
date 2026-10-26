"""Workstation setup and diagnostics (goal member-onboarding-and-ux.md §5.4).

``citespec-resource setup`` configures the LOCAL workstation only:
- detects ssh / VS Code availability;
- trusts the server host key only after explicit first-use verification;
- writes a managed include fragment into the user's ~/.ssh/config (never
  overwrites existing content, never touches private keys);
- tests identity, reachability, member status, and server capabilities.

``citespec-resource doctor`` reports the environment without changing anything.
Neither command accepts a private key path for upload — private keys never
leave the workstation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import typer

from .client import BrokerClient
from .config import LabConfig
from .errors import CitespecError

MANAGED_FRAGMENT_NAME = "citespec-workstation.conf"
MANAGED_KNOWN_HOSTS_NAME = "citespec-workstation.known_hosts"


def _check_program(name: str) -> str | None:
    return shutil.which(name)


def _known_hosts_path(config: LabConfig) -> Path:
    """The local known_hosts file that the managed fragment pins.

    A configured ``server.known_hosts`` is respected (absolute or ``~``).
    Otherwise setup manages its own file under ~/.ssh so it never has to write
    to, or silently extend, the user's global known_hosts.
    """
    configured = config.server.known_hosts.strip()
    if configured:
        return Path(os.path.expanduser(configured))
    return Path.home() / ".ssh" / MANAGED_KNOWN_HOSTS_NAME


def _host_key_name(host: str, config: LabConfig) -> str:
    return host if config.server.ssh_port == 22 else f"[{host}]:{config.server.ssh_port}"


def _ssh_common_args(config: LabConfig, host: str) -> list[str]:
    """Strict host-key checking, with the same file the managed fragment uses."""
    argv = [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={config.gui.connect_timeout}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={_known_hosts_path(config)}",
    ]
    if config.server.ssh_user:
        argv.extend(["-l", config.server.ssh_user])
    if config.server.ssh_port != 22:
        argv.extend(["-p", str(config.server.ssh_port)])
    return argv


def _host_key_is_known(config: LabConfig, host: str) -> bool:
    ssh_keygen = _check_program("ssh-keygen")
    if ssh_keygen is None:
        return False
    path = _known_hosts_path(config)
    if not path.exists():
        return False
    result = subprocess.run(
        [ssh_keygen, "-F", _host_key_name(host, config), "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _scan_host_keys(config: LabConfig, host: str) -> str:
    keyscan = _check_program("ssh-keyscan")
    if keyscan is None:
        raise RuntimeError(
            "本机没有 ssh-keyscan；请先用管理员公布的指纹手动执行一次 ssh，或安装 OpenSSH 客户端。"
        )
    argv = [keyscan, "-T", str(config.gui.connect_timeout)]
    if config.server.ssh_port != 22:
        argv.extend(["-p", str(config.server.ssh_port)])
    argv.append(host)
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=config.gui.connect_timeout + 10,
        check=False,
    )
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if result.returncode != 0 or not lines:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no keys"
        raise RuntimeError(f"无法扫描服务器主机密钥：{detail}")
    return "\n".join(lines) + "\n"


def _scan_fingerprints(config: LabConfig, host: str) -> str:
    scan = _scan_host_keys(config, host)
    ssh_keygen = _check_program("ssh-keygen")
    if ssh_keygen is None:
        return scan
    result = subprocess.run(
        [ssh_keygen, "-lf", "-"],
        input=scan,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return scan
    return result.stdout.strip()


def _trust_host_key(
    config: LabConfig,
    host: str,
    *,
    interactive: bool,
    expected_fingerprint: str = "",
) -> None:
    """Explicit first-use host-key verification.

    We never use ``StrictHostKeyChecking=accept-new``: the user must either
    supply the fingerprint obtained out of band or see the SHA256 fingerprints
    and confirm them. The exact scanned keys are then pinned in the managed
    known_hosts file used by setup/doctor/CLI.
    """
    known = _host_key_is_known(config, host)
    expected = expected_fingerprint.strip()
    if known and not expected:
        return
    scan = _scan_host_keys(config, host)
    fingerprints = _scan_fingerprints(config, host)
    if expected:
        if not any(expected.lower() in line.lower() for line in fingerprints.splitlines()):
            raise RuntimeError("服务器主机密钥与管理员公布的指纹不一致")
    elif not known:
        typer.echo("首次连接：服务器主机密钥尚未被本机信任。")
        typer.echo("请通过管理员公布的渠道核对以下 SHA256 指纹：")
        typer.echo(fingerprints)
        if not interactive:
            raise RuntimeError(
                "主机密钥未信任；请先运行 citespec-resource setup 并核对指纹，"
                "或在 ~/.ssh/known_hosts 中加入管理员公布的密钥。"
            )
        typer.confirm("以上指纹与管理员公布的一致吗？", abort=True)
    if known:
        return
    path = _known_hosts_path(config)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(scan)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _ssh_fingerprint_probe(host: str, config: LabConfig) -> tuple[bool, str]:
    """Try a BatchMode SSH connection and report (ok, detail)."""
    ssh = _check_program("ssh")
    if ssh is None:
        return False, "本机没有 ssh 客户端"
    argv = [ssh, *_ssh_common_args(config, host), host, "citespec-resource version --json"]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=config.gui.connect_timeout + 10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"SSH 连接失败：{exc}"
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown"
        if "Host key verification failed" in result.stderr:
            return False, (
                "服务器主机密钥未信任；请先运行 citespec-resource setup 核对指纹，"
                "或联系管理员获取主机指纹。"
            )
        return False, f"SSH 认证或连接失败：{detail}"
    return True, "SSH 认证成功"


def _remote_me(config: LabConfig, host: str) -> dict:
    """Read member state through the real SSH path used by everyday commands."""
    if Path(config.server.broker_socket).exists():
        return BrokerClient(config.server.broker_socket).request("me")
    ssh = _check_program("ssh")
    if ssh is None:
        raise CitespecError("本机没有 ssh 客户端")
    argv = [ssh, *_ssh_common_args(config, host), host, "citespec-resource me --json"]
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=config.gui.connect_timeout + 15,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown"
        raise CitespecError(f"通过 SSH 查询成员状态失败：{detail}")
    import json

    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise CitespecError("服务器返回的成员状态不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise CitespecError("服务器返回的成员状态格式无效")
    return data


def _managed_fragment_lines(config: LabConfig, host: str) -> list[str]:
    server = config.server
    lines = [
        "# BEGIN citespec-workstation (managed by citespec-resource setup)",
        f"Host {server.ssh_alias}",
        f"  HostName {host}",
    ]
    if server.ssh_port != 22:
        lines.append(f"  Port {server.ssh_port}")
    if server.ssh_user:
        lines.append(f"  User {server.ssh_user}")
    lines.append(f"  UserKnownHostsFile {_known_hosts_path(config)}")
    lines.append("  StrictHostKeyChecking yes")
    lines.append("# END citespec-workstation")
    return lines


def _write_managed_fragment(config: LabConfig, host: str) -> Path:
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    user_config = ssh_dir / "config"
    fragment = ssh_dir / MANAGED_FRAGMENT_NAME
    fragment.write_text("\n".join(_managed_fragment_lines(config, host)) + "\n", encoding="utf-8")
    include_line = f"Include {MANAGED_FRAGMENT_NAME}"
    try:
        existing = user_config.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if include_line not in existing.splitlines():
        user_config.write_text(f"{include_line}\n{existing}", encoding="utf-8")
    return fragment


def run_setup(
    config: LabConfig,
    *,
    host: str,
    yes: bool = False,
    expected_fingerprint: str = "",
) -> int:
    """Interactive setup; returns a process exit code."""
    typer.echo("=== 工作站配置向导 ===")
    ssh = _check_program("ssh")
    typer.echo(f"ssh 客户端：{'可用' if ssh else '缺失（请安装 OpenSSH 客户端）'}")
    code = _check_program("code")
    typer.echo(f"VS Code CLI：{'可用' if code else '未找到（可选；不影响终端使用）'}")

    try:
        _trust_host_key(
            config,
            host,
            interactive=not yes,
            expected_fingerprint=expected_fingerprint,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        typer.echo(f"主机密钥校验失败：{exc}")
        return 1

    if not yes:
        typer.confirm(
            f"将把 {config.server.ssh_alias} 的连接信息写入 ~/.ssh/{MANAGED_FRAGMENT_NAME}"
            "（不会覆盖你已有的其他配置），继续？",
            abort=True,
        )
    fragment = _write_managed_fragment(config, host)
    typer.echo(f"已写入：{fragment}")
    from .config import client_config_path, write_client_config

    try:
        profile = write_client_config(client_config_path(), config)
    except OSError as exc:
        typer.echo(f"写入成员连接配置失败：{exc}")
        return 1
    typer.echo(f"已写入成员连接配置：{profile}")

    ok, detail = _ssh_fingerprint_probe(host, config)
    typer.echo(f"服务器连接：{detail}")
    if not ok:
        typer.echo("下一步：确认公钥已安装（加入申请批准后自动安装）；然后重跑本向导。")
        return 1

    try:
        data = _remote_me(config, host)
        membership = data.get("membership", {})
        status = membership.get("status", "?")
        typer.echo(f"成员状态：{status}")
        if status != "active" and status != "admin":
            typer.echo("下一步：等待管理员开通，或联系管理员。")
            return 1
    except CitespecError as exc:
        typer.echo(f"Broker 状态查询失败：{exc.message}")
        typer.echo("下一步：确认你已 ssh 登录过一次服务器，或联系管理员。")
        return 1
    typer.echo("完成！创建工作环境：citespec-resource start；或打开工作台：citespec-resource gui")
    return 0


def run_doctor(config: LabConfig, *, host: str) -> int:
    """Read-only diagnostics; no changes."""
    typer.echo("=== 工作站诊断 ===")
    typer.echo(f"配置文件：加载成功（服务器 {config.server.name}）")
    typer.echo(f"ssh：{'可用' if _check_program('ssh') else '缺失'}")
    typer.echo(f"code：{'可用' if _check_program('code') else '未找到（可选）'}")
    ssh_dir = Path.home() / ".ssh"
    fragment = ssh_dir / MANAGED_FRAGMENT_NAME
    typer.echo(f"托管配置片段：{'存在' if fragment.exists() else '未创建（先运行 setup）'}")
    typer.echo(f"托管主机密钥文件：{_known_hosts_path(config)}")
    ok, detail = _ssh_fingerprint_probe(host, config)
    typer.echo(f"服务器连接：{detail}")
    try:
        data = _remote_me(config, host)
        typer.echo(f"成员状态：{data.get('membership', {}).get('status', '?')}")
    except CitespecError as exc:
        typer.echo(f"Broker：不可用（{exc.message}）")
        typer.echo("注：工作站上通常没有本地 Broker；本命令会经 SSH 中继查询。")
    return 0 if ok else 1
