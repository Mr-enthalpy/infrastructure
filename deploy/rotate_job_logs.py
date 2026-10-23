#!/usr/bin/env python3
"""Rotate per-allocation job logs without following workspace symlinks.

The workspace is member-writable.  logrotate's normal copy/truncate path uses
text paths and can be redirected with a symlink, so this helper opens every
directory and file relative to a descriptor with O_NOFOLLOW.  It is intended to
be run by a root systemd timer, never by a member.
"""

from __future__ import annotations

import argparse
import errno
import os
import secrets
import stat
import sys
from pathlib import Path

DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
FILE_FLAGS = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


def _open_dir(parent_fd: int, name: str) -> int | None:
    try:
        return os.open(
            name,
            DIR_FLAGS,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EACCES}:
            return None
        raise


def _rotate(fd: int, logs_fd: int, name: str, keep: int) -> None:
    info = os.fstat(fd)
    # Never rename the active log: the job keeps writing through its open
    # descriptor.  Shift archived generations only, then copy/truncate the
    # active file in place.
    for number in range(keep, 1, -1):
        source = f"{name}.{number - 1}"
        destination = f"{name}.{number}"
        try:
            os.replace(
                source,
                destination,
                src_dir_fd=logs_fd,
                dst_dir_fd=logs_fd,
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.EISDIR, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                print(f"skipping unsafe archive {name}.{number - 1}: {exc}")
                continue
            raise

    temp_name = f".rotate-{os.getpid()}-{secrets.token_hex(8)}"
    temp_fd = os.open(temp_name, TEMP_FLAGS, 0o600, dir_fd=logs_fd)
    try:
        copied = 0
        while chunk := os.read(fd, 1024 * 1024):
            written = 0
            while written < len(chunk):
                written += os.write(temp_fd, chunk[written:])
            copied += len(chunk)
        os.fchmod(temp_fd, stat.S_IMODE(info.st_mode))
        if os.geteuid() == 0:
            os.fchown(temp_fd, info.st_uid, info.st_gid)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.replace(
            temp_name,
            f"{name}.1",
            src_dir_fd=logs_fd,
            dst_dir_fd=logs_fd,
        )
        os.ftruncate(fd, 0)
        print(f"rotated {copied} bytes: {name}")
    except Exception:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=logs_fd)
        except OSError:
            pass
        raise


def rotate_workspace(workspace_root: Path, *, max_bytes: int, keep: int) -> int:
    if os.name != "posix":
        print("descriptor-relative rotation is only supported on Linux", file=sys.stderr)
        return 2
    try:
        root_fd = os.open(workspace_root, DIR_FLAGS)
    except OSError as exc:
        print(f"cannot open workspace root: {exc}", file=sys.stderr)
        return 1
    rotated = 0
    try:
        for username in os.listdir(root_fd):
            user_fd = _open_dir(root_fd, username)
            if user_fd is None:
                continue
            try:
                logs_fd = _open_dir(user_fd, ".udc-logs")
                if logs_fd is None:
                    continue
                try:
                    for name in os.listdir(logs_fd):
                        if not name.endswith(".log"):
                            continue
                        try:
                            fd = os.open(name, FILE_FLAGS, dir_fd=logs_fd)
                        except OSError as exc:
                            if exc.errno in {
                                errno.ELOOP,
                                errno.EACCES,
                                errno.ENOENT,
                                errno.EISDIR,
                                errno.ENOTDIR,
                                errno.ENXIO,
                                errno.EPERM,
                            }:
                                print(f"skipping unsafe log {username}/{name}: {exc}")
                                continue
                            raise
                        try:
                            info = os.fstat(fd)
                            if not stat.S_ISREG(info.st_mode):
                                print(f"skipping non-regular log {username}/{name}")
                                continue
                            if info.st_size <= max_bytes:
                                continue
                            _rotate(fd, logs_fd, name, keep)
                            rotated += 1
                        finally:
                            os.close(fd)
                finally:
                    os.close(logs_fd)
            finally:
                os.close(user_fd)
    finally:
        os.close(root_fd)
    print(f"rotated {rotated} oversized job log(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", default="/data1/udc-users")
    parser.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_bytes <= 0 or args.keep <= 0:
        parser.error("--max-bytes and --keep must be positive")
    if args.dry_run:
        print("dry-run: validation only")
        return 0
    return rotate_workspace(
        Path(args.workspace_root),
        max_bytes=args.max_bytes,
        keep=args.keep,
    )


if __name__ == "__main__":
    raise SystemExit(main())
