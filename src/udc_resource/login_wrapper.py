"""SSH ForceCommand wrapper for members; no host shell is exposed."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

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
}
DENIED_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "&"}


def parse_command(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return ["udc-resource", "menu"]
    argv = shlex.split(raw)
    if not argv or argv[0] != "udc-resource":
        raise ValueError("only the udc-resource command is allowed")
    if any(token in DENIED_TOKENS for token in argv):
        raise ValueError("shell operators are not allowed")
    if len(argv) < 2 or argv[1] not in ALLOWED:
        raise ValueError("command not allowed")
    return argv


def cli_path() -> str:
    """Resolve the CLI next to this interpreter, independent of sshd's PATH."""
    candidate = Path(sys.executable).with_name("udc-resource")
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("udc-resource")
    if found is None:
        raise FileNotFoundError("udc-resource executable not found next to the login wrapper")
    return found


def main() -> None:
    try:
        argv = parse_command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        executable = cli_path()
    except (ValueError, FileNotFoundError) as exc:
        print(f"udc-resource: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    # Never honor a config path from the SSH session; members must use the
    # administrator-installed policy.
    os.environ["UDC_RESOURCE_CONFIG"] = "/etc/udc-resource/config.toml"
    os.execv(executable, argv)


if __name__ == "__main__":
    main()
