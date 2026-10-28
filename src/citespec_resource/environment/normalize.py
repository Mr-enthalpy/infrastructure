"""Manifest normalization, validation, and content digests.

The server never stores a raw ``pip freeze`` blob as the environment memory.
It stores a canonical JSON document with an explicit schema so a later build
can tell exactly what was recorded, what was missing, and which lockfile
content it came from. Validation is intentionally strict: anything the broker
cannot interpret is rejected instead of being stored as an opaque string.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, NoReturn

from .models import ORIGINS, ManifestError

MANIFEST_SCHEMA = "citespec.environment.v1"
DEFAULT_MAX_PACKAGES = 2000
DEFAULT_MAX_NOTES = 32

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_APT_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}(:[a-z0-9-]{1,16})?$")
_APT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+~:_-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]{0,63}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{0,64}$")
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})
_PYTHON_MANAGERS = frozenset({"pip", "uv", "conda", "none"})
_PACKAGE_MANAGERS = frozenset({"apt", "none"})

_TOP_LEVEL_KEYS = frozenset(
    {"schema", "name", "platform", "base", "system", "python", "gpu", "origin", "notes"}
)


@dataclass(frozen=True)
class NormalizedManifest:
    data: dict[str, Any]
    text: str
    sha256: str


def canonical_json(payload: Any) -> str:
    """Stable JSON for hashing and storage (sorted keys, no stray whitespace)."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_manifest(
    payload: Any,
    *,
    max_packages: int = DEFAULT_MAX_PACKAGES,
    max_notes: int = DEFAULT_MAX_NOTES,
) -> NormalizedManifest:
    """Validate a manifest and return its canonical form plus digest."""

    if not isinstance(payload, dict):
        raise ManifestError("manifest 必须是 JSON 对象")
    unknown = sorted(set(payload) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ManifestError(f"manifest 包含未知字段: {', '.join(unknown)}")

    schema = _text(payload.get("schema"), "schema", 64)
    if schema != MANIFEST_SCHEMA:
        raise ManifestError(f"schema 必须是 {MANIFEST_SCHEMA}")

    name = _text(payload.get("name"), "name", 64, _NAME_RE)
    platform = _text(payload.get("platform"), "platform", 32)
    if platform not in _PLATFORMS:
        raise ManifestError(f"platform 必须是 {' 或 '.join(sorted(_PLATFORMS))}")

    base = _mapping(payload.get("base"), "base")
    _reject_unknown(base, {"image", "digest"}, "base")
    image = _text(base.get("image"), "base.image", 200)
    if any(char.isspace() for char in image) or "@" in image or ";" in image:
        raise ManifestError("base.image 只能填写镜像名，不能包含摘要、空白或 shell 字符")
    digest = _text(base.get("digest", ""), "base.digest", 80, allow_empty=True)
    if digest and not _DIGEST_RE.match(digest):
        raise ManifestError("base.digest 必须形如 sha256:<64 位十六进制>")

    system = _mapping(payload.get("system", {}), "system")
    _reject_unknown(system, {"package_manager", "packages"}, "system")
    package_manager = _text(system.get("package_manager", "none"), "system.package_manager", 16)
    if package_manager not in _PACKAGE_MANAGERS:
        raise ManifestError("system.package_manager 必须是 apt 或 none")
    system_packages = _system_package_list(
        system.get("packages", []),
        max_items=max_packages,
    )

    python = _mapping(payload.get("python", {}), "python")
    _reject_unknown(python, {"version", "manager", "packages", "lockfiles"}, "python")
    python_version = _text(python.get("version", ""), "python.version", 32, allow_empty=True)
    python_manager = _text(python.get("manager", "none"), "python.manager", 16)
    if python_manager not in _PYTHON_MANAGERS:
        raise ManifestError("python.manager 必须是 pip、uv、conda 或 none")
    python_packages = _package_list(
        python.get("packages", []),
        max_items=max_packages,
    )
    lockfiles = _lockfile_list(python.get("lockfiles", []), max_items=max_packages)

    gpu = _mapping(payload.get("gpu", {}), "gpu")
    _reject_unknown(gpu, {"framework", "cuda_userspace"}, "gpu")
    gpu_framework = _text(gpu.get("framework", ""), "gpu.framework", 64, allow_empty=True)
    cuda_userspace = _text(
        gpu.get("cuda_userspace", ""), "gpu.cuda_userspace", 32, allow_empty=True
    )

    origin = _mapping(payload.get("origin"), "origin")
    _reject_unknown(origin, {"type", "source_id"}, "origin")
    origin_type = _text(origin.get("type"), "origin.type", 32)
    if origin_type not in ORIGINS:
        raise ManifestError(f"origin.type 必须是 {'、'.join(sorted(ORIGINS))}")
    source_id = _text(
        origin.get("source_id", ""), "origin.source_id", 64, _SOURCE_ID_RE, allow_empty=True
    )

    notes = _string_list(payload.get("notes", []), "notes", max_items=max_notes, max_length=500)

    normalized: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "name": name,
        "platform": platform,
        "base": {"image": image, "digest": digest},
        "system": {"package_manager": package_manager, "packages": system_packages},
        "python": {
            "version": python_version,
            "manager": python_manager,
            "packages": python_packages,
            "lockfiles": lockfiles,
        },
        "gpu": {"framework": gpu_framework, "cuda_userspace": cuda_userspace},
        "origin": {"type": origin_type, "source_id": source_id},
        "notes": notes,
    }
    text = canonical_json(normalized)
    return NormalizedManifest(data=normalized, text=text, sha256=digest_text(text))


def artifact_name(value: Any, field: str = "name") -> str:
    """Validate a lockfile/artifact name (never a path)."""

    name = _text(value, field, 128, _ARTIFACT_NAME_RE)
    if name in {".", ".."}:
        raise ManifestError(f"{field} 不能是路径片段")
    return name


def safe_text(value: Any, field: str, limit: int, *, allow_empty: bool = True) -> str:
    """Single-line user text without control characters."""

    text = _text(value, field, limit, allow_empty=allow_empty)
    if any(ord(char) < 32 for char in text):
        raise ManifestError(f"{field} 不能包含控制字符")
    return text


def _fail(message: str) -> NoReturn:
    raise ManifestError(message)


def _text(
    value: Any,
    field: str,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        _fail(f"{field} 必须是字符串")
    if not value:
        if allow_empty:
            return ""
        _fail(f"{field} 不能为空")
    if len(value) > max_length:
        _fail(f"{field} 超过 {max_length} 个字符")
    if pattern is not None and not pattern.match(value):
        _fail(f"{field} 含不支持的字符")
    return value


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(f"{field} 必须是对象")
    return value


def _reject_unknown(value: dict[str, Any], allowed: set[str] | frozenset[str], field: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail(f"{field} 包含未知字段: {', '.join(unknown)}")


def _string_list(
    value: Any,
    field: str,
    *,
    max_items: int,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{field} 必须是数组")
    if len(value) > max_items:
        _fail(f"{field} 超过 {max_items} 项")
    items: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _text(item, f"{field}[{index}]", max_length, pattern)
        if text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _package_list(value: Any, *, max_items: int) -> list[dict[str, str]]:
    if not isinstance(value, list):
        _fail("python.packages 必须是数组")
    if len(value) > max_items:
        _fail(f"python.packages 超过 {max_items} 项")
    packages: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail(f"python.packages[{index}] 必须是对象")
        _reject_unknown(item, {"name", "version"}, f"python.packages[{index}]")
        name = _text(item.get("name"), f"python.packages[{index}].name", 128, _PACKAGE_RE)
        version = _text(
            item.get("version", ""),
            f"python.packages[{index}].version",
            64,
            _VERSION_RE,
            allow_empty=True,
        )
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        packages.append({"name": name, "version": version})
    return packages


def _system_package_list(value: Any, *, max_items: int) -> list[dict[str, str]]:
    if not isinstance(value, list):
        _fail("system.packages 必须是数组")
    if len(value) > max_items:
        _fail(f"system.packages 超过 {max_items} 项")
    packages: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail(f"system.packages[{index}] 必须是对象")
        _reject_unknown(item, {"name", "version"}, f"system.packages[{index}]")
        name = _text(item.get("name"), f"system.packages[{index}].name", 128, _APT_PACKAGE_RE)
        version = _text(
            item.get("version", ""),
            f"system.packages[{index}].version",
            128,
            _APT_VERSION_RE,
            allow_empty=True,
        )
        if name in seen:
            continue
        seen.add(name)
        packages.append({"name": name, "version": version})
    return packages


def _lockfile_list(value: Any, *, max_items: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _fail("python.lockfiles 必须是数组")
    if len(value) > max_items:
        _fail(f"python.lockfiles 超过 {max_items} 项")
    lockfiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail(f"python.lockfiles[{index}] 必须是对象")
        _reject_unknown(item, {"name", "sha256", "size_bytes"}, f"python.lockfiles[{index}]")
        name = artifact_name(item.get("name"), f"python.lockfiles[{index}].name")
        digest = _text(item.get("sha256"), f"python.lockfiles[{index}].sha256", 64, _SHA256_RE)
        size = item.get("size_bytes", 0)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail(f"python.lockfiles[{index}].size_bytes 必须是非负整数")
        if name in seen:
            continue
        seen.add(name)
        lockfiles.append({"name": name, "sha256": digest, "size_bytes": size})
    return lockfiles
