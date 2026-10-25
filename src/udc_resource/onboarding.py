"""Workstation setup and diagnostics (goal member-onboarding-and-ux.md §5.4).

``udc-resource setup`` configures the LOCAL workstation only:
- detects ssh / VS Code availability;
- writes a managed include fragment into the user's ~/.ssh/config (never
  overwrites existing content, never touches private keys);
- tests identity, reachability, member status, and server capabilities.

``udc-resource doctor`` reports the environment without changing anything.
Neither command accepts a private key path for upload — private keys never
leave the workstation.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from .client import BrokerClient
from .config import LabConfig
from .errors import UdcError

MANAGED_FRAGMENT_NAME = "udc-workstation.conf"


def _check_program(name: str) -> str | None:
    return shutil.which(name)


def _ssh_fingerprint_probe(host: str, config: LabConfig) -> tuple[bool, str]:
    """Try a BatchMode SSH connection and report (ok, detail)."""
    ssh = _check_program("ssh")
    if ssh is None:
        return False, "本机没有 ssh 客户端"
    argv = [
        ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={config.gui.connect_timeout}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        host,
        "udc-resource version --json",
    ]
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
        return False, f"SSH 认证或连接失败：{detail}"
    return True, "SSH 认证成功"


def _managed_fragment_lines(config: LabConfig, host: str) -> list[str]:
    server = config.server
    lines = [
        "# BEGIN udc-workstation (managed by udc-resource setup)",
        f"Host {server.ssh_alias}",
        f"  HostName {host}",
    ]
    if server.ssh_port != 22:
        lines.append(f"  Port {server.ssh_port}")
    if server.ssh_user:
        lines.append(f"  User {server.ssh_user}")
    if server.known_hosts:
        import os

        lines.append(f"  UserKnownHostsFile {os.path.expanduser(server.known_hosts)}")
    lines.append("# END udc-workstation")
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


def run_setup(config: LabConfig, *, host: str, yes: bool = False) -> int:
    """Interactive setup; returns a process exit code."""
    typer.echo("=== 工作站配置向导 ===")
    ssh = _check_program("ssh")
    typer.echo(f"ssh 客户端：{'可用' if ssh else '缺失（请安装 OpenSSH 客户端）'}")
    code = _check_program("code")
    typer.echo(f"VS Code CLI：{'可用' if code else '未找到（可选；不影响终端使用）'}")

    if not yes:
        typer.confirm(
            f"将把 {config.server.ssh_alias} 的连接信息写入 ~/.ssh/{MANAGED_FRAGMENT_NAME}"
            "（不会覆盖你已有的其他配置），继续？",
            abort=True,
        )
    fragment = _write_managed_fragment(config, host)
    typer.echo(f"已写入：{fragment}")

    ok, detail = _ssh_fingerprint_probe(host, config)
    typer.echo(f"服务器连接：{detail}")
    if not ok:
        typer.echo("下一步：确认公钥已安装（加入申请批准后自动安装）；然后重跑本向导。")
        return 1

    try:
        client = BrokerClient(config.server.broker_socket)
        data = client.request("me")
        membership = data.get("membership", {})
        status = membership.get("status", "?")
        typer.echo(f"成员状态：{status}")
        if status != "active" and status != "admin":
            typer.echo("下一步：等待管理员开通，或联系管理员。")
            return 1
    except UdcError as exc:
        typer.echo(f"Broker 状态查询失败：{exc.message}")
        typer.echo("下一步：确认你已 ssh 登录过一次服务器，或联系管理员。")
        return 1
    typer.echo("完成！创建工作环境：udc-resource start；或打开工作台：udc-resource gui")
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
    ok, detail = _ssh_fingerprint_probe(host, config)
    typer.echo(f"服务器连接：{detail}")
    try:
        client = BrokerClient(config.server.broker_socket)
        data = client.request("me")
        typer.echo(f"成员状态：{data.get('membership', {}).get('status', '?')}")
    except UdcError as exc:
        typer.echo(f"Broker：不可用（{exc.message}）")
        typer.echo("注：工作站上通常没有本地 Broker；本地命令会自动经 SSH 中继。")
    return 0 if ok else 1
