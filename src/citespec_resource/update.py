"""Signed, per-user client updates.

The packaged client is a small stable launcher plus versioned payloads.  A
release manifest is fetched over HTTPS, verified with an Ed25519 signature and
SHA-256, and only then extracted into ``versions/<version>``.  The previous
payload is kept so a failed update can fall back to it.

This module deliberately uses only the standard library plus ``cryptography``
(the latter is optional for development but required for a signed update).  It
is safe to import from the CLI and from the standalone launcher.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform as platform_module
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

MANIFEST_SCHEMA = 1
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]{0,63})?")
DEFAULT_MANIFEST_URL = (
    "https://github.com/Mr-enthalpy/infrastructure/releases/latest/download/"
    "citespec-resource-manifest.json"
)
# Generated for this repository.  The private key is not tracked; releases are
# signed by the maintainer/CI secret.  Rotating the key requires a client
# release that embeds the new public key.
UPDATE_PUBLIC_KEY_B64 = "LcxxIJY0DcCdDVzMcuk5pIJ4jaTz2hSJ6I50n2E9MvU="
MAX_MANIFEST_BYTES = 1_000_000
MAX_ARTIFACT_BYTES = 500 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1 * 1024 * 1024 * 1024
UPDATE_LOCK_STALE_SECONDS = 6 * 60 * 60
PAYLOAD_NAME = "citespec-resource-app"
CURRENT_FILE = "current.json"
LAST_CHECK_FILE = ".last-update-check"
VERSIONS_DIR = "versions"
UPDATES_DIR = "updates"


class UpdateError(RuntimeError):
    """A user-actionable update failure."""


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    protocol: int
    min_client_protocol: int
    artifacts: Mapping[str, Artifact]
    notes: str = ""
    published_at: str = ""


@dataclass(frozen=True)
class UpdateInfo:
    available: bool
    current_version: str
    latest_version: str
    reason: str
    manifest: UpdateManifest | None = None
    artifact: Artifact | None = None


@dataclass(frozen=True)
class UpdateResult:
    version: str
    path: Path
    previous_version: str = ""


def default_manifest_url() -> str:
    return os.environ.get("CITESPEC_UPDATE_MANIFEST", "").strip() or DEFAULT_MANIFEST_URL


def platform_key(
    system: str | None = None,
    machine: str | None = None,
) -> str | None:
    """Map the running platform to a manifest artifact key."""

    system = (system or platform_module.system()).lower()
    machine = (machine or platform_module.machine()).lower()
    if machine in {"amd64", "x86_64"}:
        if system == "windows":
            return "windows-x64"
        if system == "linux":
            return "linux-x64"
    if machine in {"aarch64", "arm64"}:
        if system == "linux":
            return "linux-arm64"
    return None


def parse_manifest(payload: Mapping[str, Any]) -> UpdateManifest:
    if not isinstance(payload, Mapping):
        raise UpdateError("update manifest must be a JSON object")
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise UpdateError(f"unsupported update manifest schema: {payload.get('schema')!r}")
    version = payload.get("version")
    if not isinstance(version, str) or not _version_key(version):
        raise UpdateError("update manifest has an invalid version")
    protocol = _required_int(payload, "protocol")
    min_client_protocol = _required_int(payload, "min_client_protocol")
    if min_client_protocol < 1 or min_client_protocol > protocol:
        raise UpdateError("update manifest has inconsistent protocol fields")
    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, Mapping) or not raw_artifacts:
        raise UpdateError("update manifest has no artifacts")
    artifacts: dict[str, Artifact] = {}
    for key, raw in raw_artifacts.items():
        if not isinstance(key, str) or not isinstance(raw, Mapping):
            raise UpdateError("update manifest artifact is invalid")
        url = raw.get("url")
        digest = raw.get("sha256")
        size = raw.get("size")
        if not isinstance(url, str) or not url:
            raise UpdateError(f"artifact {key} has no URL")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise UpdateError(f"artifact {key} has an invalid SHA-256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise UpdateError(f"artifact {key} has an invalid size")
        if size > MAX_ARTIFACT_BYTES:
            raise UpdateError(f"artifact {key} is too large")
        artifacts[key] = Artifact(
            url=url,
            sha256=digest.lower(),
            size=size,
        )
    return UpdateManifest(
        version=version,
        protocol=protocol,
        min_client_protocol=min_client_protocol,
        artifacts=artifacts,
        notes=str(payload.get("notes") or ""),
        published_at=str(payload.get("published_at") or ""),
    )


def manifest_signing_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical bytes signed by the release key (everything except signature)."""

    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def verify_manifest(
    payload: Mapping[str, Any],
    *,
    public_key_b64: str = UPDATE_PUBLIC_KEY_B64,
) -> UpdateManifest:
    """Verify the Ed25519 signature, then validate the manifest contents."""

    signature = payload.get("signature")
    if not isinstance(signature, str) or not signature:
        raise UpdateError("update manifest is not signed")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - packaged builds include it
        raise UpdateError("signed updates require the cryptography package") from exc
    try:
        public_bytes = base64.b64decode(public_key_b64, validate=True)
        signature_bytes = base64.b64decode(signature, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        public_key.verify(signature_bytes, manifest_signing_bytes(payload))
    except (ValueError, InvalidSignature) as exc:
        raise UpdateError("update manifest signature is invalid") from exc
    return parse_manifest(payload)


def fetch_manifest(
    url: str | None = None,
    *,
    timeout: float = 10.0,
    allow_insecure: bool = False,
) -> Mapping[str, Any]:
    manifest_url = url or default_manifest_url()
    try:
        with _open_url(manifest_url, timeout=timeout, allow_insecure=allow_insecure) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"could not fetch update manifest: {exc}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("update manifest is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("update manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise UpdateError("update manifest must be a JSON object")
    return payload


def check_for_update(
    *,
    current_version: str = __version__,
    manifest_url: str | None = None,
    platform: str | None = None,
    timeout: float = 10.0,
    allow_insecure: bool = False,
    manifest_payload: Mapping[str, Any] | None = None,
    public_key_b64: str = UPDATE_PUBLIC_KEY_B64,
) -> UpdateInfo:
    payload = manifest_payload
    if payload is None:
        payload = fetch_manifest(
            manifest_url,
            timeout=timeout,
            allow_insecure=allow_insecure,
        )
    manifest = verify_manifest(payload, public_key_b64=public_key_b64)
    key = platform or platform_key()
    if not key:
        return UpdateInfo(
            available=False,
            current_version=current_version,
            latest_version=manifest.version,
            reason=f"no update artifact is published for {platform_module.system().lower()}",
        )
    artifact = manifest.artifacts.get(key)
    if artifact is None:
        return UpdateInfo(
            available=False,
            current_version=current_version,
            latest_version=manifest.version,
            reason=f"no update artifact is published for {key}",
        )
    latest_key = _version_key(manifest.version)
    current_key = _version_key(current_version)
    if latest_key is None or current_key is None:
        raise UpdateError(f"invalid client version: {current_version!r}")
    if latest_key <= current_key:
        return UpdateInfo(
            available=False,
            current_version=current_version,
            latest_version=manifest.version,
            reason="already up to date",
        )
    return UpdateInfo(
        available=True,
        current_version=current_version,
        latest_version=manifest.version,
        reason="new version available",
        manifest=manifest,
        artifact=artifact,
    )


def install_update(
    info: UpdateInfo,
    *,
    app_root: Path | str,
    allow_insecure: bool = False,
    timeout: float = 60.0,
) -> UpdateResult:
    if not info.available or info.manifest is None or info.artifact is None:
        raise UpdateError(info.reason or "no update is available")
    version = info.manifest.version
    if _version_key(version) is None:
        raise UpdateError("update manifest has an unsafe version")
    root = Path(app_root).expanduser().resolve()
    versions = root / VERSIONS_DIR
    updates = root / UPDATES_DIR
    versions.mkdir(parents=True, exist_ok=True)
    updates.mkdir(parents=True, exist_ok=True)
    destination = versions / version
    if not _is_within(destination, versions):
        raise UpdateError("update version escapes the versions directory")
    previous = installed_version(root)
    if _payload_path(destination).is_file():
        _ensure_payload_executable(_payload_path(destination))
        write_current(root, version)
        return UpdateResult(version, destination, previous)

    lock = updates / ".update.lock"
    lock_handle = _acquire_update_lock(lock)
    try:
        with lock_handle:
            lock_handle.write(str(os.getpid()))
        archive = updates / f"{version}.zip"
        _download_artifact(
            info.artifact,
            archive,
            timeout=timeout,
            allow_insecure=allow_insecure,
        )
        temporary = versions / f".{version}.tmp-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        try:
            try:
                _extract_zip(archive, temporary)
            except (OSError, zipfile.BadZipFile) as exc:
                raise UpdateError("update artifact is not a valid zip archive") from exc
            if not _payload_path(temporary).is_file():
                raise UpdateError("update artifact does not contain the client payload")
            _ensure_payload_executable(_payload_path(temporary))
            if _payload_path(destination).is_file():
                # Another process completed the same version while we were
                # downloading.  Keep the already-installed copy.
                shutil.rmtree(temporary)
            else:
                if destination.exists():
                    # An earlier interrupted extraction may have left an
                    # incomplete version directory.  Replace it only after the
                    # new payload has been fully verified.
                    shutil.rmtree(destination)
                temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            archive.unlink(missing_ok=True)
        write_current(root, version)
        return UpdateResult(version, destination, previous)
    finally:
        lock.unlink(missing_ok=True)


def read_current(app_root: Path | str) -> dict[str, Any]:
    path = Path(app_root) / CURRENT_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def installed_version(app_root: Path | str) -> str:
    value = read_current(app_root).get("version")
    return value if isinstance(value, str) else ""


def write_current(app_root: Path | str, version: str) -> None:
    if not _version_key(version):
        raise UpdateError(f"invalid client version: {version!r}")
    root = Path(app_root)
    payload = {
        "version": version,
        "path": f"{VERSIONS_DIR}/{version}",
        "installed_at": _now_iso(),
    }
    _atomic_write_json(root / CURRENT_FILE, payload)


def resolve_payload(app_root: Path | str) -> Path | None:
    root = Path(app_root)
    current = read_current(root)
    version = current.get("version")
    if isinstance(version, str) and _version_key(version):
        candidate = _payload_path(root / VERSIONS_DIR / version)
        if _is_within(candidate, root / VERSIONS_DIR) and candidate.is_file():
            return candidate
    fallback = _payload_path(root / VERSIONS_DIR / __version__)
    return fallback if fallback.is_file() else None


def payload_for_version(app_root: Path | str, version: str) -> Path | None:
    """Return the payload for one safe, already-installed version."""

    if not isinstance(version, str) or _version_key(version) is None:
        return None
    root = Path(app_root)
    versions = root / VERSIONS_DIR
    candidate = _payload_path(versions / version)
    if _is_within(candidate, versions) and candidate.is_file():
        return candidate
    return None


def find_app_root() -> Path | None:
    """Find the per-user app root, or ``None`` in a source checkout."""

    override = os.environ.get("CITESPEC_RESOURCE_APP_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        parent = executable.parent
        if parent.name == VERSIONS_DIR:
            return parent.parent
        if parent.parent.name == VERSIONS_DIR:
            return parent.parent.parent
        return parent
    return None


def update_check_due(app_root: Path | str, interval_seconds: int = 24 * 60 * 60) -> bool:
    path = Path(app_root) / LAST_CHECK_FILE
    try:
        return time.time() - path.stat().st_mtime >= interval_seconds
    except OSError:
        return True


def mark_update_checked(app_root: Path | str) -> None:
    path = Path(app_root) / LAST_CHECK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def payload_name() -> str:
    return f"{PAYLOAD_NAME}.exe" if os.name == "nt" else PAYLOAD_NAME


def _payload_path(version_dir: Path) -> Path:
    return version_dir / payload_name()


def _ensure_payload_executable(payload: Path) -> None:
    """Restore the executable bit that zip extraction does not preserve.

    ``zipfile`` writes regular files as 0600/0666 subject to umask regardless of
    the archive's Unix mode.  Without this the versioned launcher is installed
    but cannot be executed on Linux.  Windows uses file extension semantics, so
    leave its ACLs alone.
    """

    if os.name == "nt":
        return
    payload.chmod(0o755)


def _acquire_update_lock(lock: Path) -> Any:
    """Create the update lock, recovering from a crashed updater.

    A hard kill or power loss can leave the lock file behind.  Treat a lock
    older than six hours as stale so the client can recover without manual
    cleanup; an active download remains protected by the lock.
    """

    for attempt in range(2):
        try:
            return lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            try:
                stale = time.time() - lock.stat().st_mtime > UPDATE_LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if stale and attempt == 0:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            raise UpdateError("another update is already in progress") from exc
    raise UpdateError("another update is already in progress")


def _download_artifact(
    artifact: Artifact,
    destination: Path,
    *,
    timeout: float,
    allow_insecure: bool,
) -> None:
    digest = hashlib.sha256()
    written = 0
    try:
        with (
            _open_url(
                artifact.url,
                timeout=timeout,
                allow_insecure=allow_insecure,
            ) as response,
            destination.open("wb") as handle,
        ):
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > artifact.size:
                    raise UpdateError("update artifact is larger than the manifest says")
                digest.update(chunk)
                handle.write(chunk)
    except UpdateError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as exc:
        destination.unlink(missing_ok=True)
        raise UpdateError(f"could not download update: {exc}") from exc
    if written != artifact.size:
        destination.unlink(missing_ok=True)
        raise UpdateError("update artifact size does not match the manifest")
    if digest.hexdigest() != artifact.sha256:
        destination.unlink(missing_ok=True)
        raise UpdateError("update artifact SHA-256 does not match the manifest")


def _open_url(url: str, *, timeout: float, allow_insecure: bool) -> Any:
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme == "https":
        return urllib.request.urlopen(url, timeout=timeout)
    if allow_insecure and scheme in {"http", "file"}:
        return urllib.request.urlopen(url, timeout=timeout)
    raise UpdateError("update downloads must use HTTPS")


def _extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            name = item.filename
            normalized = name.replace("\\", "/")
            if any(ord(character) < 0x20 for character in normalized):
                raise UpdateError("update artifact contains an unsafe path")
            parts = PurePosixPath(normalized).parts
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise UpdateError("update artifact contains an unsafe path")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
                raise UpdateError("update artifact contains an absolute path")
            mode = (item.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UpdateError("update artifact contains a symbolic link")
            extracted += item.file_size
            if extracted > MAX_EXTRACTED_BYTES:
                raise UpdateError("update artifact expands to too much data")
            target = destination.joinpath(*parts)
            if not _is_within(target, destination):
                raise UpdateError("update artifact contains an unsafe path")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise UpdateError(f"update manifest has an invalid {key}")
    return value


def _version_key(value: str) -> tuple[int, ...] | None:
    if not isinstance(value, str) or value != value.strip() or len(value) > 80:
        return None
    match = _VERSION_RE.fullmatch(value)
    if not match:
        return None
    core = value.split("-", 1)[0].split("+", 1)[0]
    parts = [int(part) for part in core.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "Artifact",
    "CURRENT_FILE",
    "DEFAULT_MANIFEST_URL",
    "MANIFEST_SCHEMA",
    "PAYLOAD_NAME",
    "UPDATE_PUBLIC_KEY_B64",
    "UpdateError",
    "UpdateInfo",
    "UpdateManifest",
    "UpdateResult",
    "check_for_update",
    "default_manifest_url",
    "fetch_manifest",
    "find_app_root",
    "install_update",
    "installed_version",
    "manifest_signing_bytes",
    "mark_update_checked",
    "parse_manifest",
    "payload_for_version",
    "payload_name",
    "platform_key",
    "read_current",
    "resolve_payload",
    "update_check_due",
    "verify_manifest",
    "write_current",
]
