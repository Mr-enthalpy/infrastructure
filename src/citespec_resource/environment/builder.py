"""Two-phase image builder for saved environment versions.

Phase 1 runs a bounded, CPU-only builder container of the recorded base image
and lets it create a virtual environment at ``/opt/citespec-env`` on a
broker-owned directory. The container has no GPU, a read-only root filesystem,
cgroup limits, and exactly two bind mounts: the output directory and a fixed
requirements file. It never sees a member workspace.

Phase 2 writes a minimal Dockerfile (``FROM`` the pinned base image and
``COPY`` the finished virtual environment) and runs ``docker build`` with no
network and no ``RUN`` step. Nothing installed by the member is executed during
this second phase, and no ``docker commit`` or ``docker export`` is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..broker.docker_driver import DockerBackend
from ..config import LabConfig
from ..errors import Conflict, DockerError, InternalError
from .models import ManifestError

BUILDER_SCRIPT = r"""
import json, os, subprocess, sys

OUT = "/opt/citespec-env"
REQUIREMENTS = "/citespec/requirements.txt"

def fail(code, detail):
    print("CITESPEC_BUILD_FAILED " + json.dumps({"code": code, "detail": detail}), flush=True)
    sys.exit(1)

try:
    subprocess.run(
        [sys.executable, "-m", "venv", "--clear", "--copies", OUT],
        check=True, timeout=600,
    )
except Exception as exc:
    fail("venv", str(exc))

python = os.path.join(OUT, "bin", "python")
if not os.path.isfile(python):
    fail("venv", "virtual environment has no python executable")

install = subprocess.run(
    [python, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
     "--no-cache-dir", "-r", REQUIREMENTS],
    capture_output=True, text=True, timeout=7200,
)
details = (install.stdout + install.stderr)[-12000:]
if install.returncode != 0:
    fail("pip-install", details)

check = subprocess.run(
    [python, "-m", "pip", "check"], capture_output=True, text=True, timeout=300,
)
freeze = subprocess.run(
    [python, "-m", "pip", "freeze"], capture_output=True, text=True, timeout=300,
)
version = subprocess.run(
    [python, "-c", "import sys; print(sys.version.split()[0])"],
    capture_output=True, text=True, timeout=120,
)
evidence = {
    "python": (version.stdout or "").strip()[:32],
    "pip_check": check.returncode == 0,
    "pip_check_output": (check.stdout + check.stderr)[-4000:],
    "installed": [line for line in (freeze.stdout or "").splitlines() if line][:5000],
    "install_log": details[-4000:],
}
print("CITESPEC_BUILD_EVIDENCE " + json.dumps(evidence), flush=True)
"""

EVIDENCE_PREFIX = "CITESPEC_BUILD_EVIDENCE "
FAILED_PREFIX = "CITESPEC_BUILD_FAILED "
MAX_PACKAGE_LINES = 5000


class BuildError(Exception):
    """A build could not produce a usable image."""

    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence or {}


@dataclass(frozen=True)
class BuildOutcome:
    image_tag: str
    image_id: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildRequest:
    build_id: str
    image_tag: str
    version_id: str
    server_id: str
    base_image: str
    base_digest: str
    manifest_text: str
    requirements_text: str
    owner_uid: int
    owner_gid: int


def image_tag_for(version_id: str, build_id: str) -> str:
    short = re.sub(r"[^a-z0-9]", "", version_id.lower())[:40] or "env"
    attempt = re.sub(r"[^a-z0-9]", "", build_id.lower())[-8:] or "build"
    return f"citespec-env:{short}-{attempt}"


def requirements_from_manifest(manifest: dict[str, Any]) -> str:
    """Generate a pip requirements file from the recorded package list."""

    python = manifest.get("python", {})
    packages = python.get("packages", [])
    lines: list[str] = []
    if not isinstance(packages, list):
        return ""
    for item in packages[:MAX_PACKAGE_LINES]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        version = item.get("version")
        if isinstance(version, str) and version:
            lines.append(f"{name}=={version}")
        else:
            lines.append(name)
    return "\n".join(lines) + ("\n" if lines else "")


class EnvironmentBuilder:
    def __init__(self, config: LabConfig, docker: DockerBackend) -> None:
        self.config = config
        self.docker = docker
        self.root = Path(config.policy.env_state_dir)

    def build(self, request: BuildRequest) -> BuildOutcome:
        policy = self.config.policy
        if not policy.environment_builds_enabled:
            raise Conflict("环境镜像构建尚未启用（policy.environment_builds_enabled）")
        manifest = _load_manifest(request.manifest_text)
        system_packages = manifest.get("system", {}).get("packages", [])
        if isinstance(system_packages, list) and system_packages:
            raise BuildError(
                "V1 不支持自动安装系统包（apt）；请让管理员准备包含这些包的基础镜像后再采集。"
            )

        base_image = str(manifest.get("base", {}).get("image", "")) or request.base_image
        if not self.config.image.image_allowed(base_image):
            raise BuildError(f"基础镜像 {base_image} 不在允许列表内")
        try:
            details = self.docker.image_details(base_image)
        except DockerError as exc:
            raise BuildError(f"基础镜像在本机不存在或无法读取：{exc}") from exc
        base_id = str(details.get("id", ""))
        if not base_id:
            raise BuildError("无法确定基础镜像 ID")
        recorded = str(manifest.get("base", {}).get("digest", "")) or request.base_digest
        digests = details.get("digests") or []
        if recorded and isinstance(digests, list) and digests and recorded not in digests:
            raise BuildError(
                "基础镜像的 RepoDigest 与采集时记录的不一致，为避免重建出不同环境已停止构建"
            )

        requirements = request.requirements_text or requirements_from_manifest(manifest)
        work = self.root / "builds" / request.build_id
        try:
            returned = self._attempt(
                request,
                work=work,
                base_id=base_id,
                requirements=requirements,
                manifest=manifest,
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return returned

    def _attempt(
        self,
        request: BuildRequest,
        *,
        work: Path,
        base_id: str,
        requirements: str,
        manifest: dict[str, Any],
    ) -> BuildOutcome:
        policy = self.config.policy
        venv_dir = work / "venv"
        requirements_path = work / "requirements.txt"
        _reset_dir(work)
        venv_dir.mkdir(mode=0o750)
        _write_text(requirements_path, requirements, mode=0o644)
        # The builder runs as the member so files in the shared output
        # directory stay readable to that account and cannot be replaced by an
        # unrelated host user afterwards.
        _chown(venv_dir, request.owner_uid, request.owner_gid)
        _chown(requirements_path, request.owner_uid, request.owner_gid)

        code, logs = self.docker.run_builder(
            base_id,
            ["python3", "-I", "-c", BUILDER_SCRIPT],
            user=f"{request.owner_uid}:{request.owner_gid}",
            cpus=policy.env_build_cpus,
            memory_gb=policy.env_build_memory_gb,
            pids_limit=policy.pids_limit,
            timeout_seconds=policy.env_build_timeout_seconds,
            volumes={
                str(venv_dir): {"bind": "/opt/citespec-env", "mode": "rw"},
                str(requirements_path): {"bind": "/citespec/requirements.txt", "mode": "ro"},
            },
            labels={
                "citespec.managed": "true",
                "citespec.environment.build": request.build_id,
                "citespec.environment.version": request.version_id,
            },
        )
        evidence = _parse_evidence(logs)
        if code != 0:
            detail = _failure_detail(logs, code)
            raise BuildError(detail, evidence={**evidence, "logs": logs[-8000:]})
        if not evidence:
            raise BuildError(
                "构建脚本没有返回可验证的证据，停止发布镜像",
                evidence={"logs": logs[-8000:]},
            )
        if not venv_dir.is_dir() or not any(venv_dir.iterdir()):
            raise BuildError("构建完成后虚拟环境目录为空", evidence=evidence)

        dockerfile = (
            f"FROM {base_id}\n"
            "COPY venv /opt/citespec-env\n"
            "ENV VIRTUAL_ENV=/opt/citespec-env \\\n"
            "    PATH=/opt/citespec-env/bin:$PATH\n"
            'LABEL citespec.environment.version="'
            f'{request.version_id}" \\\n'
            '      citespec.environment.build="'
            f'{request.build_id}"\n'
        )
        _write_text(work / "Dockerfile", dockerfile, mode=0o644)
        _write_text(
            work / ".dockerignore",
            "requirements.txt\n",
            mode=0o644,
        )
        tag = request.image_tag
        try:
            image_id = self.docker.build_image(
                path=str(work),
                tag=tag,
                labels={
                    "citespec.managed": "true",
                    "citespec.environment.build": request.build_id,
                    "citespec.environment.version": request.version_id,
                },
            )
        except DockerError as exc:
            raise BuildError(str(exc), evidence=evidence) from exc

        warnings: list[str] = []
        if not evidence.get("pip_check", False):
            warnings.append("pip check 报告依赖冲突，环境已构建但请人工确认")
        if not requirements.strip():
            warnings.append("该版本没有记录 Python 包，只生成了空的虚拟环境")
        detail = "；".join(warnings) or "重建成功"
        return BuildOutcome(
            image_tag=tag,
            image_id=image_id,
            detail=detail,
            evidence={
                **evidence,
                "base_image": str(manifest.get("base", {}).get("image", "")),
                "base_id": base_id,
                "requirements_sha256": _sha256(requirements),
                "image_tag": tag,
            },
        )

    def prune(self, *, keep_images: set[str], tag_prefix: str = "citespec-env:") -> list[str]:
        """Delete built environment images beyond the configured cache budget.

        Images referenced by a kept build are never removed. Oldest images are
        deleted first until the total size fits the budget.
        """

        policy = self.config.policy
        try:
            images = self.docker.list_environment_images(tag_prefix)
        except DockerError:
            return []
        removable = [image for image in images if not (set(image.get("tags", [])) & keep_images)]
        removable.sort(key=lambda image: str(image.get("created", "")))
        total = sum(int(image.get("size", 0)) for image in images)
        budget = policy.env_build_cache_gb * 1024**3
        removed: list[str] = []
        while removable and total > budget:
            image = removable.pop(0)
            tags = list(image.get("tags", []))
            target = tags[0] if tags else str(image.get("id", ""))
            try:
                self.docker.remove_image(target)
            except DockerError:
                continue
            total -= int(image.get("size", 0))
            removed.append(target)
        return removed


def _load_manifest(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ManifestError("环境 manifest 不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ManifestError("环境 manifest 必须是对象")
    return value


def _parse_evidence(logs: str) -> dict[str, Any]:
    for line in reversed(logs.splitlines()):
        if line.startswith(EVIDENCE_PREFIX):
            try:
                value = json.loads(line[len(EVIDENCE_PREFIX) :])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return {}


def _failure_detail(logs: str, code: int) -> str:
    for line in reversed(logs.splitlines()):
        if line.startswith(FAILED_PREFIX):
            try:
                value = json.loads(line[len(FAILED_PREFIX) :])
            except ValueError:
                continue
            if isinstance(value, dict):
                stage = str(value.get("code", "build"))
                detail = str(value.get("detail", ""))[-1500:]
                return f"构建失败（{stage}）：{detail}"
    return f"构建容器退出码 {code}，没有可用的失败详情"


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, mode=0o700)
    # The broker may run as root; keep the directory traversable for the
    # member UID that fills the mounted venv directory.
    try:
        os.chmod(path, 0o750)
    except OSError:
        pass


def _write_text(path: Path, text: str, *, mode: int) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _chown(path: Path, uid: int, gid: int) -> None:
    if os.name != "posix":
        return
    try:
        os.chown(path, uid, gid)
    except OSError as exc:
        raise InternalError(f"无法把构建目录交给成员 UID：{exc}") from exc


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
