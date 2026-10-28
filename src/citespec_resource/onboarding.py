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


def _run_capture(
    argv: list[str],
    *,
    input_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a local helper and always return UTF-8 text, never bytes or None.

    SSH banners and remote diagnostics are UTF-8, or arbitrary bytes from a
    misconfigured endpoint. Decoding them with the Windows ANSI code page (GBK)
    crashed ``setup`` after the profile had already been written, so decode
    explicitly and replace undecodable bytes instead of raising. Captured
    streams are never ``None``, so callers can format them without guards.
    """
    result = subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.stdout is None:  # defensive: a caller may pass a fake CompletedProcess
        result.stdout = ""
    if result.stderr is None:
        result.stderr = ""
    return result


def _detail_line(value: str | None) -> str:
    """Last non-empty line of a diagnostic, or a stable placeholder."""
    lines = [line.strip() for line in (value or "").splitlines() if line.strip()]
    return lines[-1] if lines else "unknown"


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
    try:
        result = _run_capture([ssh_keygen, "-F", _host_key_name(host, config), "-f", str(path)])
    except (OSError, subprocess.SubprocessError):
        return False
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
    result = _run_capture(argv, timeout=config.gui.connect_timeout + 10)
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if result.returncode != 0 or not lines:
        raise RuntimeError(f"无法扫描服务器主机密钥：{_detail_line(result.stderr)}")
    return "\n".join(lines) + "\n"


def _scan_fingerprints(config: LabConfig, host: str) -> str:
    scan = _scan_host_keys(config, host)
    ssh_keygen = _check_program("ssh-keygen")
    if ssh_keygen is None:
        return scan
    try:
        result = _run_capture([ssh_keygen, "-lf", "-"], input_text=scan)
    except (OSError, subprocess.SubprocessError):
        return scan
    if result.returncode != 0 or not result.stdout.strip():
        return scan
    return result.stdout.strip()


_KEY_TYPE_LABELS = {
    "ssh-rsa": "RSA (ssh-rsa)",
    "rsa-sha2-256": "RSA (rsa-sha2-256)",
    "rsa-sha2-512": "RSA (rsa-sha2-512)",
    "ssh-ed25519": "ED25519 (ssh-ed25519)",
    "ecdsa-sha2-nistp256": "ECDSA (ecdsa-sha2-nistp256)",
    "ecdsa-sha2-nistp384": "ECDSA (ecdsa-sha2-nistp384)",
    "ecdsa-sha2-nistp521": "ECDSA (ecdsa-sha2-nistp521)",
    "ssh-dss": "DSA (ssh-dss)",
}


def _offered_key_types(scan: str) -> list[str]:
    """Key algorithms the endpoint actually offers, in scan order."""
    labels: list[str] = []
    for line in scan.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0].startswith("#"):
            continue
        key_type = fields[1]
        label = _KEY_TYPE_LABELS.get(key_type, key_type)
        if label not in labels:
            labels.append(label)
    return labels


def _fingerprint_matches(expected: str, fingerprints: str) -> bool:
    needle = expected.strip().lower()
    if not needle:
        return False
    haystack = fingerprints.lower()
    if needle in haystack:
        return True
    # Accept a bare base64 digest as well as the usual "SHA256:..." form.
    if needle.startswith("sha256:"):
        return needle[len("sha256:") :] in haystack
    return f"sha256:{needle}" in haystack


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
        if not _fingerprint_matches(expected, fingerprints):
            typer.echo("服务器提供的 SHA256 主机指纹（含密钥类型）：")
            typer.echo(fingerprints)
            offered = _offered_key_types(scan)
            if offered:
                typer.echo(f"端点实际提供的算法：{', '.join(offered)}")
            raise RuntimeError(
                "服务器主机密钥与管理员公布的指纹不一致。指纹必须对应端点实际提供的"
                "算法：例如端点只提供 RSA 主机密钥时，ED25519 指纹永远无法匹配。"
                "请把上面列出的指纹发给管理员确认，索取同一算法的 SHA256 指纹后重试。"
            )
    elif not known:
        typer.echo("首次连接：服务器主机密钥尚未被本机信任。")
        typer.echo("请通过管理员公布的渠道核对以下 SHA256 指纹（括号内为密钥类型）：")
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
    typer.echo(f"已固定服务器主机密钥：{fingerprints}")


def _ssh_fingerprint_probe(host: str, config: LabConfig) -> tuple[bool, str]:
    """Try a BatchMode SSH connection and report (ok, detail).

    The probe only needs a command that every supported server CLI understands.
    ``version --json`` is not available on older servers (and an unknown option
    exits non-zero), so this uses plain ``version`` and keeps the reply as
    informational detail.
    """
    ssh = _check_program("ssh")
    if ssh is None:
        return False, "本机没有 ssh 客户端"
    argv = [ssh, *_ssh_common_args(config, host), host, "citespec-resource version"]
    try:
        result = _run_capture(argv, timeout=config.gui.connect_timeout + 10)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        return False, f"SSH 连接失败：{exc}"
    if result.returncode != 0:
        if "Host key verification failed" in result.stderr:
            return False, (
                "服务器主机密钥未信任；请先运行 citespec-resource setup 核对指纹，"
                "或联系管理员获取主机指纹。"
            )
        return False, f"SSH 认证或连接失败：{_detail_line(result.stderr)}"
    version = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
    return True, f"SSH 认证成功（服务器 CLI {version}）" if version else "SSH 认证成功"


def _remote_me(config: LabConfig, host: str) -> dict:
    """Read member state through the real SSH path used by everyday commands."""
    if Path(config.server.broker_socket).exists():
        return _unwrap_me_envelope(BrokerClient(config.server.broker_socket).request("me"))
    ssh = _check_program("ssh")
    if ssh is None:
        raise CitespecError("本机没有 ssh 客户端")
    argv = [ssh, *_ssh_common_args(config, host), host, "citespec-resource me --json"]
    result = _run_capture(argv, timeout=config.gui.connect_timeout + 15)
    if result.returncode != 0:
        raise CitespecError(f"通过 SSH 查询成员状态失败：{_detail_line(result.stderr)}")
    import json

    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise CitespecError("服务器返回的成员状态不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise CitespecError("服务器返回的成员状态格式无效")
    return _unwrap_me_envelope(data)


def _unwrap_me_envelope(data: object) -> dict:
    if not isinstance(data, dict):
        raise CitespecError("member status response is not an object")
    if data.get("ok") is False:
        error = data.get("error")
        error_detail = error.get("message") if isinstance(error, dict) else None
        if not isinstance(error_detail, str) or not error_detail:
            error_detail = "member status request failed"
        raise CitespecError(error_detail)
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise CitespecError("member status envelope has no data object")
    return payload


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
    except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError) as exc:
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

    try:
        ok, detail = _ssh_fingerprint_probe(host, config)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        typer.echo(f"服务器连接检查失败：{exc}")
        typer.echo(
            "连接配置已写入；稍后可运行 citespec-resource doctor 复查，"
            "或确认 SSH 客户端可用后联系管理员。"
        )
        return 1
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
