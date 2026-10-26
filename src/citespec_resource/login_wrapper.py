"""SSH ForceCommand wrapper for members; no host shell is exposed.

Protocol safety (goal member-onboarding-and-ux.md §4, INV-09): machine
channels (--json, tunnel, file) pass through untouched; friendly Chinese
guidance goes to stderr only, and only for human-facing refusals. Pre-auth
guidance is an sshd Banner (deploy/sshd/), never injected here.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

ALLOWED = {
    "list",
    "status",
    "apply",
    "release",
    "shell",
    "logs",
    "job",
    "file",
    "queue",
    "tunnel",
    "menu",
    "version",
    "me",
}
DENIED_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "&"}

# Host-side programs members commonly reach for, mapped to the supported
# alternative (§4.1: refuse AND point at the new entry).
HOST_COMMAND_KINDS = {
    "bash": "shell",
    "sh": "shell",
    "zsh": "shell",
    "fish": "shell",
    "docker": "docker",
    "nvidia-smi": "gpu_tool",
    "scp": "file_transfer",
    "sftp": "file_transfer",
    "rsync": "file_transfer",
    "code": "vscode",
    "vscode": "vscode",
}

_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _safe_command_head(raw: str) -> str:
    """First token of the command, ANSI sequences and control characters
    stripped, bounded in length.

    Never echoes paths, arguments, or anything that could carry secrets
    (§4.2: do not echo the full raw command).
    """
    try:
        argv = shlex.split(raw)
    except ValueError:
        argv = raw.split()
    if not argv:
        return ""
    head = _CONTROL_CHARS.sub("", _ANSI_ESCAPES.sub("", argv[0]))
    # A program name never contains shell metacharacters; cut at the first
    # one so leftover fragments of mangled input are not echoed.
    head = re.split(r"[;|&]", head, maxsplit=1)[0]
    return head[:40]


def _load_onboarding() -> dict[str, Any]:
    """Read the [onboarding] section from the installed server config.

    These are PUBLIC values (URLs, support label); failures degrade to the
    honest default contact entry rather than an error.
    """
    config_path = os.environ.get("CITESPEC_RESOURCE_CONFIG", "/etc/citespec-resource/config.toml")
    try:
        import tomllib

        with open(config_path, "rb") as handle:
            data = tomllib.load(handle)
        section = data.get("onboarding", {})
        if isinstance(section, dict):
            return {
                "guide_url": str(section.get("guide_url", "")),
                "apply_url": str(section.get("apply_url", "")),
                "support_label": str(section.get("support_label", "实验室计算资源管理员")),
                "support_contact": str(section.get("support_contact", "")),
                "legacy_login_notice_enabled": bool(
                    section.get("legacy_login_notice_enabled", True)
                ),
            }
    except (OSError, ValueError, TypeError):
        pass
    return {
        "guide_url": "",
        "apply_url": "",
        "support_label": "实验室计算资源管理员",
        "support_contact": "",
        "legacy_login_notice_enabled": True,
    }


def classify(raw: str) -> tuple[str, list[str]]:
    """Classify an SSH command into (kind, parsed citespec-resource argv).

    kind is one of: "menu" (no command), "citespec" (allowed subcommand),
    "unknown_subcommand", a host-command kind from HOST_COMMAND_KINDS, or
    "other" for anything else.
    """
    raw = raw.strip()
    if not raw:
        return "menu", []
    try:
        argv = shlex.split(raw)
    except ValueError:
        return "other", []
    if not argv:
        return "menu", []
    if argv[0] != "citespec-resource":
        kind = HOST_COMMAND_KINDS.get(argv[0], "other")
        return kind, []
    if len(argv) < 2 or argv[1] not in ALLOWED:
        return "unknown_subcommand", argv
    return "citespec", argv


def render_denial(kind: str, command_head: str, onboarding: dict[str, Any]) -> str:
    """Human guidance for a refused command; stderr-only, Chinese, honest.

    The refused command is shown as its sanitized program name only.
    """
    guide = str(onboarding.get("guide_url") or "")
    support_label = str(onboarding.get("support_label", "实验室计算资源管理员"))
    support_contact = str(onboarding.get("support_contact", "")) or support_label
    guide_line = f"使用指南：{guide}" if guide else f"使用指南：请向{support_contact}索取"
    contact_line = f"管理员：{support_contact}" if support_contact else f"管理员：{support_label}"
    shown = f"`{command_head}`" if command_head else "该命令"

    if kind == "shell":
        return (
            f"{shown} 不能在宿主机上执行；你的申请和数据没有被修改。\n"
            "\n"
            "正确的用法：\n"
            "  直接 ssh <你的服务器别名> 打开资源菜单；\n"
            "  创建工作环境后在容器内使用终端，容器内可以 sudo 和安装软件。\n"
            f"{guide_line}\n"
            f"{contact_line}"
        )
    if kind == "docker":
        return (
            f"{shown} 不能在宿主机上执行；容器由资源系统统一管理。\n"
            "\n"
            "创建你的工作环境后，容器内已预装常用工具，可以 sudo 安装系统包。\n"
            f"{guide_line}\n"
            f"{contact_line}"
        )
    if kind == "gpu_tool":
        return (
            f"{shown} 不能在宿主机上执行。GPU 通过工作环境分配：\n"
            "  直接 ssh <你的服务器别名> → 创建工作环境 → 在容器内运行训练。\n"
            f"{guide_line}\n"
            f"{contact_line}"
        )
    if kind == "file_transfer":
        return (
            f"{shown} 不能用于宿主机文件传输。\n"
            "\n"
            "传输数据：在容器内使用 git / pip / 训练脚本访问 /workspace；\n"
            "或使用 citespec-resource file put/get（限额受策略控制）。\n"
            f"{guide_line}\n"
            f"{contact_line}"
        )
    if kind == "vscode":
        return (
            "VS Code 不能直接连接宿主机。\n"
            "\n"
            "正确入口：在你自己的电脑运行 citespec-resource gui，\n"
            "或 citespec-resource vscode <工作环境>；进入后即打开容器内 /workspace。\n"
            f"{guide_line}\n"
            f"{contact_line}"
        )
    return (
        f"{shown} 不能在宿主机上执行；你的申请和数据没有被修改。\n"
        "\n"
        "继续使用：\n"
        "  直接 ssh <你的服务器别名>，打开资源菜单；\n"
        "  或在你自己的电脑运行 citespec-resource gui。\n"
        f"{guide_line}\n"
        f"{contact_line}"
    )


NON_INTERACTIVE_USAGE = (
    "这是一台受管服务器：交互使用请直接 ssh 登录后选择菜单；\n"
    "机器协议请使用 citespec-resource 的 JSON/队列/文件命令（见 --help）。"
)


def parse_command(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return ["citespec-resource", "menu"]
    argv = shlex.split(raw)
    if not argv or argv[0] != "citespec-resource":
        raise ValueError("only the citespec-resource command is allowed")
    if any(token in DENIED_TOKENS for token in argv):
        raise ValueError("shell operators are not allowed")
    if len(argv) < 2 or argv[1] not in ALLOWED:
        raise ValueError("command not allowed")
    return argv


def cli_path() -> str:
    """Resolve the CLI next to this interpreter, independent of sshd's PATH."""
    candidate = Path(sys.executable).with_name("citespec-resource")
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("citespec-resource")
    if found is None:
        raise FileNotFoundError("citespec-resource executable not found next to the login wrapper")
    return found


def main() -> None:
    original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    onboarding = _load_onboarding()
    kind, argv = classify(original)
    if kind == "citespec":
        try:
            parsed = parse_command(original)
        except ValueError as exc:
            print(f"citespec-resource: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        _exec(parsed)
        return
    if kind == "menu":
        stdin_tty = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False
        if stdin_tty:
            _exec(["citespec-resource", "menu"])
            return
        # No TTY and no command: a short usage hint and a non-zero exit, never
        # an interactive prompt hanging in a pipe (§4.1).
        print(NON_INTERACTIVE_USAGE, file=sys.stderr)
        raise SystemExit(1)
    if kind == "unknown_subcommand" and argv:
        sub = _CONTROL_CHARS.sub("", argv[1])[:40]
        print(f"citespec-resource: 不支持的子命令 {sub}", file=sys.stderr)
        print("可用命令见 citespec-resource --help", file=sys.stderr)
        raise SystemExit(2)
    # Host commands and anything else: friendly, command-specific refusal.
    head = _safe_command_head(original)
    if onboarding.get("legacy_login_notice_enabled", True):
        print(render_denial(kind, head, onboarding), file=sys.stderr)
    else:
        print(f"citespec-resource: {head or 'command'} is not allowed here", file=sys.stderr)
    raise SystemExit(2)


def _exec(parsed: list[str]) -> None:
    # Never honor a config path from the SSH session; members must use the
    # administrator-installed policy.
    os.environ["CITESPEC_RESOURCE_CONFIG"] = "/etc/citespec-resource/config.toml"
    executable = cli_path()
    os.execv(executable, parsed)


if __name__ == "__main__":
    main()
