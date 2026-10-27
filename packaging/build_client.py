"""Build the per-user client payload and a stable launcher.

Run from the repository root after installing PyInstaller:

    python packaging/build_client.py --platform windows-x64 --output dist

The payload zip is the artifact referenced by the signed update manifest.  The
``-full`` zip is the first-install package: a stable launcher, an initial
version directory, ``current.json`` and a per-user install script.

This script intentionally does not sign anything.  Signing happens in the
release job after all platform artifacts have been collected.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from citespec_resource import __version__
from citespec_resource.update import PAYLOAD_NAME, platform_key

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "client"
PYINSTALLER_HIDDEN = [
    "cryptography.exceptions",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=__version__)
    parser.add_argument("--platform", default=platform_key() or "")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    version = args.version
    if version != __version__:
        raise SystemExit(f"--version {version!r} does not match package version {__version__!r}")
    pyproject_version = _pyproject_version()
    if pyproject_version != version:
        raise SystemExit(
            f"pyproject version {pyproject_version!r} does not match {version!r}; "
            "update both before tagging"
        )
    platform = args.platform
    if platform not in {"windows-x64", "linux-x64", "linux-arm64"}:
        raise SystemExit(f"unsupported platform: {platform!r}")
    if platform == "windows-x64" and sys.platform != "win32":
        raise SystemExit("windows-x64 must be built on Windows")
    if platform.startswith("linux") and not sys.platform.startswith("linux"):
        raise SystemExit("linux artifacts must be built on Linux")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = BUILD / platform
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    payload_dist = work / "payload-dist"
    launcher_dist = work / "launcher-dist"
    payload_work = work / "payload-work"
    launcher_work = work / "launcher-work"
    payload_spec = work / "spec-payload"
    launcher_spec = work / "spec-launcher"

    _pyinstaller(
        [
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            PAYLOAD_NAME,
            "--distpath",
            str(payload_dist),
            "--workpath",
            str(payload_work),
            "--specpath",
            str(payload_spec),
            "--collect-data",
            "citespec_resource",
            "--hidden-import",
            "citespec_resource.gui.server",
            "--hidden-import",
            "citespec_resource.onboarding",
            "--hidden-import",
            "citespec_resource.enrollment.store",
            *[item for name in PYINSTALLER_HIDDEN for item in ("--hidden-import", name)],
            str(ROOT / "packaging" / "app_entry.py"),
        ]
    )
    _pyinstaller(
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            "citespec-resource",
            "--distpath",
            str(launcher_dist),
            "--workpath",
            str(launcher_work),
            "--specpath",
            str(launcher_spec),
            *[item for name in PYINSTALLER_HIDDEN for item in ("--hidden-import", name)],
            str(ROOT / "packaging" / "launcher_entry.py"),
        ]
    )

    payload_dir = payload_dist / PAYLOAD_NAME
    if not payload_dir.is_dir():
        raise SystemExit(f"payload directory was not built: {payload_dir}")
    launcher = launcher_dist / _executable_name("citespec-resource")
    if not launcher.is_file():
        raise SystemExit(f"launcher was not built: {launcher}")
    payload_exe = payload_dir / _executable_name(PAYLOAD_NAME)
    if not payload_exe.is_file():
        raise SystemExit(f"payload executable was not built: {payload_exe}")

    payload_zip = output / f"citespec-resource-{version}-{platform}.zip"
    _zip_tree(payload_dir, payload_zip)
    full_zip = output / f"citespec-resource-{version}-{platform}-full.zip"
    _make_full_zip(
        full_zip,
        version=version,
        platform=platform,
        launcher=launcher,
        payload_dir=payload_dir,
    )
    print(f"payload: {payload_zip} ({payload_zip.stat().st_size} bytes)")
    print(f"full:    {full_zip} ({full_zip.stat().st_size} bytes)")
    return 0


def _pyinstaller(args: list[str]) -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", *args],
            cwd=ROOT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"PyInstaller failed with exit code {exc.returncode}") from exc
    except FileNotFoundError as exc:
        raise SystemExit("PyInstaller is not installed in this interpreter") from exc


def _make_full_zip(
    destination: Path,
    *,
    version: str,
    platform: str,
    launcher: Path,
    payload_dir: Path,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        staging = Path(raw)
        shutil.copy2(launcher, staging / _executable_name("citespec-resource"))
        version_dir = staging / "versions" / version
        shutil.copytree(payload_dir, version_dir)
        (staging / "current.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "path": f"versions/{version}",
                    "installed_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        installer = "install.ps1" if platform == "windows-x64" else "install.sh"
        source_installer = ROOT / "packaging" / installer
        if source_installer.is_file():
            shutil.copy2(source_installer, staging / installer)
        # Ship the standalone verifier too, so a user who downloaded the
        # manifest can re-check this zip and its installer before running it.
        for name in ("verify_release.py", "verify_release.sh", "verify_release.ps1"):
            source = ROOT / "packaging" / name
            if source.is_file():
                shutil.copy2(source, staging / name)
        (staging / "README.txt").write_text(
            "Citespec Resource client\n"
            "\n"
            f"Version: {version}\n"
            "Verify the full zip against the signed manifest before installing:\n"
            "  python verify_release.py --manifest <manifest.json> --package <full.zip>\n"
            "Then run the platform install script once, passing the signed\n"
            "installer SHA-256 with --verify (install.sh) or -Verify (install.ps1).\n"
            "The launcher checks signed updates at most once per day.\n",
            encoding="utf-8",
        )
        _zip_tree(staging, destination)


def _zip_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def _executable_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        payload = tomllib.load(handle)
    return str(payload.get("project", {}).get("version", ""))


if __name__ == "__main__":
    raise SystemExit(main())
