"""Read-only capture of a running allocation's software environment.

The probe is a fixed server-owned script. It never runs member code: it parses
distribution metadata files and asks dpkg for installed versions. Anything that
cannot be read is reported as missing instead of being guessed.
"""

from __future__ import annotations

import hashlib
import json
import platform as platform_module
import re
from dataclasses import dataclass, field
from typing import Any

from . import models
from .normalize import DEFAULT_MAX_PACKAGES

PROBE_SCRIPT = r"""
import glob, json, os, shutil, subprocess

def read(path, limit=200000):
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""

def kv(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out

def metadata(path):
    for name in ("METADATA", "PKG-INFO"):
        candidate = os.path.join(path, name)
        if os.path.isfile(candidate):
            return read(candidate, 16384)
    return ""

def parse_metadata(text):
    name = ""
    version = ""
    for line in text.splitlines():
        if not name and line.startswith("Name: "):
            name = line[6:].strip()
        elif not version and line.startswith("Version: "):
            version = line[9:].strip()
    return name, version

def packages(root):
    found = []
    for pattern in ("*.dist-info", "*.egg-info"):
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            name, version = parse_metadata(metadata(path))
            if not name:
                base = os.path.basename(path)
                name = base.rsplit("-", 1)[0]
            found.append({"name": name, "version": version})
    return found

# ``pip install --user`` (common in read-only containers) lands in the user
# site directory, which is not included in ``site.getsitepackages()``.  The
# interpreter searches it first, so record it first and let de-duplication keep
# the version the member actually imports.
def interpreter(path):
    try:
        result = subprocess.run(
            [path, "-I", "-S", "-c",
             "import json, site, sys; print(json.dumps({\n"
             "  'version': '%d.%d.%d' % sys.version_info[:3],\n"
             "  'paths': [site.getusersitepackages()] + site.getsitepackages(),\n"
             "}))"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
        version = info["version"]
        paths = [item for item in info.get("paths", []) if isinstance(item, str)]
    except (ValueError, KeyError, TypeError):
        return None
    short = version.rsplit(".", 1)[0]
    directories = paths + [
        "/usr/lib/python3/dist-packages",
        "/usr/local/lib/python" + short + "/site-packages",
        "/usr/lib/python" + short + "/site-packages",
    ]
    collected = []
    seen = set()
    for directory in directories:
        for item in packages(directory):
            key = item["name"].lower()
            if key in seen:
                continue
            seen.add(key)
            collected.append(item)
    return {"path": path, "version": version, "packages": collected}

candidates = []
for name in ("python3", "python"):
    found = shutil.which(name)
    if found:
        candidates.append(found)
candidates.extend([
    "/usr/local/bin/python3",
    "/usr/bin/python3",
    "/opt/conda/bin/python",
    "/opt/conda/bin/python3",
])
for path in sorted(glob.glob("/usr/bin/python3.*")):
    if not path.endswith("-config"):
        candidates.append(path)
interpreters = []
seen_paths = set()
for path in candidates:
    if path in seen_paths or not os.path.isfile(path) or not os.access(path, os.X_OK):
        continue
    seen_paths.add(path)
    item = interpreter(path)
    if item is not None:
        item["packages"] = item["packages"][:3000]
        interpreters.append(item)

conda = []
for path in sorted(glob.glob("/opt/conda/conda-meta/*.json"))[:3000]:
    try:
        data = json.loads(read(path, 65536) or "{}")
    except ValueError:
        continue
    if data.get("name"):
        conda.append({"name": data["name"], "version": data.get("version", "")})

dpkg = []
try:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\\t${Version}\\n"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines()[:4000]:
            if "\t" in line:
                name, version = line.split("\t", 1)
                dpkg.append({"name": name.strip(), "version": version.strip()})
except Exception:
    pass

print(json.dumps({
    "os": kv(read("/etc/os-release", 8192)),
    "interpreters": interpreters,
    "conda": conda,
    "dpkg": dpkg,
}, sort_keys=True))
"""

# Fixed probe invocation. ``-I`` isolates the interpreter and ``-S`` skips
# site-packages, so no member-installed module can run during capture.
PROBE_ARGV = ("python3", "-I", "-S", "-c", PROBE_SCRIPT)

SUPPORTED_PLATFORMS = {"x86_64": "linux/amd64", "amd64": "linux/amd64", "aarch64": "linux/arm64"}


@dataclass(frozen=True)
class Lockfile:
    kind: str
    name: str
    content: str

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))


@dataclass(frozen=True)
class CaptureResult:
    manifest: dict[str, Any]
    fidelity: str
    artifacts: list[Lockfile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def platform_name() -> str:
    return SUPPORTED_PLATFORMS.get(platform_module.machine().lower(), "linux/amd64")


def parse_probe(raw: str, *, max_packages: int = DEFAULT_MAX_PACKAGES) -> dict[str, Any]:
    """Parse and bound the probe JSON without trusting its shape."""

    try:
        data = json.loads(raw)
    except ValueError:
        return {"os": {}, "interpreters": [], "conda": [], "dpkg": [], "errors": ["probe output"]}
    if not isinstance(data, dict):
        return {"os": {}, "interpreters": [], "conda": [], "dpkg": [], "errors": ["probe shape"]}
    errors = [str(item) for item in data.get("errors", []) if isinstance(item, str)][:32]
    os_info = data.get("os", {})
    if not isinstance(os_info, dict):
        os_info = {}
    interpreters: list[dict[str, Any]] = []
    raw_interpreters = data.get("interpreters", [])
    if isinstance(raw_interpreters, list):
        for item in raw_interpreters[:8]:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            version = item.get("version")
            if not isinstance(path, str) or not isinstance(version, str):
                continue
            interpreters.append(
                {
                    "path": path[:200],
                    "version": version[:32],
                    "packages": _packages(item.get("packages"), max_packages),
                }
            )
    return {
        "os": {str(key)[:64]: str(value)[:200] for key, value in list(os_info.items())[:32]},
        "interpreters": interpreters,
        "conda": _packages(data.get("conda"), max_packages),
        "dpkg": _packages(data.get("dpkg"), max_packages),
        "errors": errors,
    }


def build_manifest(
    *,
    project_name: str,
    probe: dict[str, Any],
    base_image: str,
    base_digest: str,
    lockfiles: list[Lockfile],
    max_packages: int = DEFAULT_MAX_PACKAGES,
    origin: str = models.ORIGIN_CAPTURE,
    origin_source_id: str = "",
) -> CaptureResult:
    """Turn probe output into a validated manifest plus a fidelity verdict."""

    interpreters = [
        item
        for item in probe.get("interpreters", [])
        if isinstance(item, dict) and item.get("packages")
    ]
    primary = interpreters[0] if interpreters else {}
    python_packages = _packages(primary.get("packages", []), max_packages)
    # Conda metadata is recorded for fidelity, but it is not a pip
    # requirements list: names such as ``_libgcc_mutex`` or ``bzip2`` are
    # conda-only and would make every build fail.  V1 installs the
    # distributions that are actually present in the interpreter's
    # site-packages; conda-only packages must come from the base image.
    conda_packages = _packages(probe.get("conda", []), max_packages)
    system_packages = probe.get("dpkg", [])[:max_packages]

    notes: list[str] = []
    os_info = probe.get("os", {})
    if os_info.get("PRETTY_NAME"):
        notes.append(f"系统镜像: {os_info['PRETTY_NAME']}")
    if interpreters:
        notes.append(f"发现 {len(interpreters)} 个 Python 解释器")
    else:
        notes.append("未发现可用的 Python 解释器")
    for error in probe.get("errors", []):
        notes.append(f"探针提示: {error}")
    if probe.get("conda"):
        notes.append(f"从 conda-meta 读取 {len(probe['conda'])} 个包")
    if lockfiles:
        notes.append("锁文件: " + "、".join(sorted(item.name for item in lockfiles)))
    else:
        notes.append("工作区没有发现 uv.lock / requirements / environment.yml / pyproject.toml")
    if not base_digest:
        notes.append("基础镜像没有可记录的 RepoDigest，重建时需要固定镜像 ID")

    manifest: dict[str, Any] = {
        "schema": "citespec.environment.v1",
        "name": project_name,
        "platform": platform_name(),
        "base": {"image": base_image, "digest": base_digest},
        "system": {
            "package_manager": "apt" if system_packages else "none",
            "packages": system_packages,
        },
        "python": {
            "version": primary.get("version", ""),
            "manager": "conda" if probe.get("conda") else ("pip" if python_packages else "none"),
            "packages": python_packages,
            "conda_packages": conda_packages,
            "lockfiles": [
                {
                    "name": item.name,
                    "sha256": _digest(item.content),
                    "size_bytes": item.size_bytes,
                }
                for item in lockfiles
            ],
        },
        "gpu": _gpu_summary(base_image),
        "origin": {"type": origin, "source_id": origin_source_id},
        "notes": notes[:32],
    }

    total = len(python_packages) + len(conda_packages) + len(system_packages)
    if total == 0:
        fidelity = models.CAPTURE_MISSING
    elif python_packages and base_digest and probe.get("errors") == []:
        fidelity = models.CAPTURE_COMPLETE
    else:
        fidelity = models.CAPTURE_PARTIAL
    return CaptureResult(
        manifest=manifest, fidelity=fidelity, artifacts=list(lockfiles), notes=notes
    )


def _packages(value: Any, max_items: int) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    packages: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        version = item.get("version", "")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(version, str):
            version = ""
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        packages.append({"name": name[:128], "version": version[:64]})
        if len(packages) >= max_items:
            break
    return packages


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _gpu_summary(base_image: str) -> dict[str, str]:
    lowered = base_image.lower()
    match = re.search(r"cuda[_-]?(\d+\.\d+)", lowered)
    return {"framework": "", "cuda_userspace": match.group(1) if match else ""}
