"""Read-only capture of containers that predate environment memory.

A legacy claim is evidence gathering, not a migration proof. The broker never
starts a stopped container, never runs member code, and never uses
``docker commit`` or ``docker export``. For a running container it executes the
same fixed probe as a normal capture, but as ``nobody`` so a compromised image
cannot use the member's UID; for a stopped container it only reads a small,
fixed set of paths out of the container archive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..broker.docker_driver import ContainerInfo, DockerBackend
from ..errors import DockerError
from . import capture
from .normalize import DEFAULT_MAX_PACKAGES

# The probe runs as the unprivileged ``nobody`` account on the host's UID/GID
# map. Nothing inside the container is trusted, so it must not run as the
# claimant or as root.
PROBE_USER = "65534:65534"

# Fixed archive paths for stopped containers. There is no directory walk and no
# attempt to guess a Python version: the operator can rebuild from these and
# mark the result partial.  ``/etc/os-release`` is a symlink to
# ``/usr/lib/os-release`` on current systemd distributions, and Docker's
# ``get_archive`` returns the symlink itself, so the target is listed too.
ARCHIVE_PATHS = ("/etc/os-release", "/usr/lib/os-release", "/opt/conda/conda-meta")
ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
ARCHIVE_MAX_ENTRIES = 4000

RUNNING_STATES = frozenset({"running", "restarting", "paused"})

STOPPED_NOTE = "容器已停止，只读取固定路径，Python 包清单不完整"


@dataclass(frozen=True)
class LegacyCapture:
    metadata: dict[str, Any]
    probe: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.metadata.get("state") in RUNNING_STATES


def sanitized_metadata(container: ContainerInfo) -> dict[str, Any]:
    """Record only fields that are safe to show to another member.

    Container names and labels can carry the previous owner's account name, so
    they are deliberately not copied into the member-visible metadata. Admins
    read the container name and ID from the source row instead.
    """

    return {
        "state": str(container.status)[:32],
        "image": str(container.image)[:200],
        "image_id": str(container.image_id)[:80],
        "created": str(container.created)[:40],
        "gpus": [str(item)[:64] for item in container.gpu_refs][:16],
    }


def capture_container(
    docker: DockerBackend,
    container: ContainerInfo,
    *,
    max_packages: int = DEFAULT_MAX_PACKAGES,
) -> LegacyCapture:
    metadata = sanitized_metadata(container)
    notes: list[str] = []
    probe = _empty_probe()

    if metadata["state"] in RUNNING_STATES:
        reason = ""
        try:
            code, output, _stderr = docker.exec_capture(
                container.id,
                list(capture.PROBE_ARGV),
                user=PROBE_USER,
            )
        except DockerError as exc:
            code = -1
            output = ""
            reason = f"只读探针失败：{exc}"
        if code == 0:
            probe = capture.parse_probe(output, max_packages=max_packages)
        else:
            # The image may lack python3 even though it has a conda prefix;
            # fall back to the fixed archive paths instead of starting it.
            probe = _archive_probe(
                docker,
                container.id,
                notes,
                max_packages=max_packages,
                reason=reason or "只读探针失败，改用固定路径归档读取",
            )
    else:
        probe = _archive_probe(
            docker,
            container.id,
            notes,
            max_packages=max_packages,
            reason=STOPPED_NOTE,
        )

    return LegacyCapture(metadata=metadata, probe=probe, notes=notes)


def _archive_probe(
    docker: DockerBackend,
    container_id: str,
    notes: list[str],
    *,
    max_packages: int,
    reason: str,
) -> dict[str, Any]:
    probe = _empty_probe()
    if reason:
        probe["errors"] = [reason]
    try:
        files = docker.read_container_paths(
            container_id,
            list(ARCHIVE_PATHS),
            max_bytes=ARCHIVE_MAX_BYTES,
            max_entries=ARCHIVE_MAX_ENTRIES,
        )
    except DockerError as exc:
        notes.append(f"只读归档读取失败：{exc}")
        files = {}
    os_release = files.get("/etc/os-release", b"") or files.get("/usr/lib/os-release", b"")
    if os_release:
        probe["os"] = _parse_os_release(os_release)
    else:
        notes.append("没有读到 /etc/os-release 或 /usr/lib/os-release")
    conda = _conda_packages(files, "/opt/conda/conda-meta", max_packages=max_packages)
    if conda:
        probe["conda"] = conda
        notes.append(f"从归档读取 {len(conda)} 个 conda 包")
    return probe


def _conda_packages(
    files: dict[str, bytes],
    prefix: str,
    *,
    max_packages: int,
) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in sorted(files):
        if not path.startswith(prefix + "/") or not path.endswith(".json"):
            continue
        try:
            value = json.loads(files[path].decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        version = value.get("version", "")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(version, str):
            version = ""
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        packages.append({"name": name[:128], "version": version[:64]})
        if len(packages) >= max_packages:
            break
    return packages


def _parse_os_release(raw: bytes) -> dict[str, str]:
    text = raw.decode("utf-8", errors="replace")[:8192]
    info: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()[:64]
        if not key:
            continue
        info[key] = value.strip().strip('"').strip("'")[:200]
    return info


def _empty_probe() -> dict[str, Any]:
    return {"os": {}, "interpreters": [], "conda": [], "dpkg": [], "errors": []}
