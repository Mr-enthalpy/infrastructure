"""Race-resistant workspace file operations.

Linux does not offer a portable text-path equivalent to ``openat2`` from
Python, so the broker walks paths one component at a time with directory file
descriptors.  Every intermediate directory is opened with ``O_NOFOLLOW`` and
the final file is either opened with ``O_NOFOLLOW`` or atomically replaced
relative to an already-open parent descriptor.  A user can therefore replace a
workspace directory with a symlink, but the broker will never follow it.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from ..errors import BadRequest, Conflict, Forbidden, InternalError, NotFound

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_READ_FLAGS = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
_UPLOAD_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK


def supported() -> bool:
    """Return whether descriptor-relative operations are available."""

    return (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and all(
            function in os.supports_dir_fd
            # CPython records the renameat primitive as ``os.rename``;
            # ``os.replace`` is the same descriptor-relative operation with
            # the overwrite flag, so checking ``rename`` covers both.
            for function in (os.open, os.mkdir, os.rename, os.stat, os.unlink)
        )
        and os.stat in os.supports_follow_symlinks
    )


def relative_parts(raw_path: object) -> tuple[str, ...]:
    """Validate and split a client path below ``/workspace``."""

    if not isinstance(raw_path, str) or not raw_path:
        raise BadRequest("path must be a non-empty relative path")
    if "\x00" in raw_path:
        raise BadRequest("path must not contain NUL bytes")
    if raw_path == "/workspace":
        raise BadRequest("path must name a file below /workspace")
    if raw_path.startswith("/workspace/"):
        raw_path = raw_path[len("/workspace/") :]
    if raw_path.startswith("/"):
        raise Forbidden("path must stay inside the allocation workspace")
    parts = tuple(raw_path.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise Forbidden("path must stay inside the allocation workspace")
    return parts


def _map_error(exc: OSError, *, target: str) -> Exception:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM, errno.ENXIO}:
        return Forbidden(f"refusing unsafe workspace path: {target}")
    if exc.errno in {errno.ENOENT}:
        return NotFound(f"workspace path not found: {target}")
    if exc.errno in {errno.EISDIR, errno.EEXIST, errno.ENOTEMPTY}:
        return Conflict(f"workspace target is not a regular file: {target}")
    return InternalError(f"workspace operation failed for {target}: {exc}")


def _open_root(workspace: str | Path) -> int:
    path = os.fspath(workspace)
    try:
        return os.open(path, _DIR_FLAGS)
    except OSError as exc:
        raise _map_error(exc, target=path) from exc


def ensure_workspace(
    workspace: str | Path,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> None:
    """Create/open the member workspace without following its final symlink.

    The parent is administrator-controlled.  The final component is created
    with ``mkdir`` and then opened with ``O_NOFOLLOW``; a member cannot win a
    check/use race by swapping the directory for a symlink before chmod/chown.
    """

    path = Path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _map_error(exc, target=str(path.parent)) from exc

    if not supported():
        # Unit tests on Windows exercise the broker, but production brokers
        # only run on Linux.  Fail closed rather than falling back to a
        # path-based check on a POSIX host with incomplete openat support.
        if os.name == "posix":
            raise InternalError("descriptor-relative workspace access is unavailable")
        try:
            path.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise _map_error(exc, target=str(path)) from exc
        if path.is_symlink() or not path.is_dir():
            raise Forbidden(f"workspace path is not a real directory: {path}")
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise _map_error(exc, target=str(path)) from exc
        if owner_uid is not None and os.name == "posix" and os.geteuid() == 0:
            try:
                os.chown(path, owner_uid, owner_gid if owner_gid is not None else -1)
            except OSError as exc:
                raise _map_error(exc, target=str(path)) from exc
        return

    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise _map_error(exc, target=str(path)) from exc
    fd = _open_root(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise Forbidden(f"workspace path is not a directory: {path}")
        os.fchmod(fd, 0o700)
        if owner_uid is not None and os.geteuid() == 0:
            os.fchown(fd, owner_uid, owner_gid if owner_gid is not None else -1)
    except OSError as exc:
        raise _map_error(exc, target=str(path)) from exc
    finally:
        os.close(fd)


def _open_child_dir(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _map_error(exc, target=name) from exc


def _ensure_child_dir(
    parent_fd: int,
    name: str,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> int:
    try:
        child_fd = _open_child_dir(parent_fd, name)
    except NotFound:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _map_error(exc, target=name) from exc
        child_fd = _open_child_dir(parent_fd, name)
    if owner_uid is not None and os.name == "posix" and os.geteuid() == 0:
        try:
            os.fchown(
                child_fd,
                owner_uid,
                owner_gid if owner_gid is not None else -1,
            )
        except OSError as exc:
            os.close(child_fd)
            raise _map_error(exc, target=name) from exc
    return child_fd


def _open_parent(
    workspace: str | Path,
    parts: tuple[str, ...],
    *,
    create: bool,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> int:
    if not parts:
        raise BadRequest("path must name a file")
    current = _open_root(workspace)
    try:
        for part in parts[:-1]:
            child = (
                _ensure_child_dir(
                    current,
                    part,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
                if create
                else _open_child_dir(current, part)
            )
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def open_read_file(workspace: str | Path, parts: tuple[str, ...]) -> int:
    """Open a regular file for reading without following symlinks."""

    parent_fd = _open_parent(workspace, parts, create=False)
    try:
        try:
            fd = os.open(parts[-1], _READ_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise _map_error(exc, target=parts[-1]) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise Forbidden("refusing to read a non-regular workspace file")
            return fd
        except Exception:
            os.close(fd)
            raise
    finally:
        os.close(parent_fd)


@dataclass
class UploadHandle:
    parent_fd: int
    fd: int
    temp_name: str
    final_name: str
    owner_uid: int | None = None
    owner_gid: int | None = None
    _closed: bool = False
    _parent_closed: bool = False
    replaced: bool = False

    def _close_fd(self) -> None:
        if not self._closed:
            try:
                os.close(self.fd)
            finally:
                self._closed = True

    def _close_parent(self) -> None:
        if not self._parent_closed:
            try:
                os.close(self.parent_fd)
            finally:
                self._parent_closed = True

    def commit(self) -> None:
        if self._closed:
            raise RuntimeError("upload is already closed")
        try:
            os.fchmod(self.fd, 0o600)
            if self.owner_uid is not None and os.name == "posix" and os.geteuid() == 0:
                os.fchown(
                    self.fd,
                    self.owner_uid,
                    self.owner_gid if self.owner_gid is not None else -1,
                )
            os.fsync(self.fd)
            self._close_fd()
            try:
                os.replace(
                    self.temp_name,
                    self.final_name,
                    src_dir_fd=self.parent_fd,
                    dst_dir_fd=self.parent_fd,
                )
                self.replaced = True
            except OSError as exc:
                raise _map_error(exc, target=self.final_name) from exc
        finally:
            self._close_fd()
            if not self.replaced:
                try:
                    os.unlink(self.temp_name, dir_fd=self.parent_fd)
                except OSError:
                    pass
            self._close_parent()

    def abort(self) -> None:
        self._close_fd()
        if not self._parent_closed:
            try:
                os.unlink(self.temp_name, dir_fd=self.parent_fd)
            except OSError:
                pass
        self._close_parent()


def open_upload(
    workspace: str | Path,
    parts: tuple[str, ...],
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> UploadHandle:
    """Create a same-directory temporary file for an atomic upload."""

    parent_fd = _open_parent(
        workspace,
        parts,
        create=True,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    for _ in range(32):
        temp_name = f".udc-upload-{secrets.token_hex(16)}"
        try:
            fd = os.open(temp_name, _UPLOAD_FLAGS, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            os.close(parent_fd)
            raise _map_error(exc, target=parts[-1]) from exc
        return UploadHandle(
            parent_fd=parent_fd,
            fd=fd,
            temp_name=temp_name,
            final_name=parts[-1],
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
    os.close(parent_fd)
    raise InternalError("could not allocate a temporary upload file")


def ensure_job_log(
    workspace: str | Path,
    name: str,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> None:
    """Create the log directory/file without following user-created symlinks."""

    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise BadRequest("invalid job log name")
    parts = (".udc-logs", f"{name}.log")
    parent_fd = _open_parent(
        workspace,
        parts,
        create=True,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    try:
        try:
            fd = os.open(parts[-1], _LOG_FLAGS, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            raise _map_error(exc, target=parts[-1]) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise Forbidden("refusing a non-regular job log")
            os.fchmod(fd, 0o600)
            if owner_uid is not None and os.name == "posix" and os.geteuid() == 0:
                os.fchown(fd, owner_uid, owner_gid if owner_gid is not None else -1)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def regular_file_size(workspace: str | Path, parts: tuple[str, ...]) -> int | None:
    """Return a regular file's size without following the final symlink."""

    parent_fd = _open_parent(workspace, parts, create=False)
    try:
        try:
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EACCES}:
                return None
            raise _map_error(exc, target=parts[-1]) from exc
        return info.st_size if stat.S_ISREG(info.st_mode) else None
    finally:
        os.close(parent_fd)


def tail_file(workspace: str | Path, parts: tuple[str, ...], tail: int) -> str:
    if tail <= 0:
        return ""
    fd = open_read_file(workspace, parts)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, max(0, size - 2 * 1024 * 1024), os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(lines[-tail:])
