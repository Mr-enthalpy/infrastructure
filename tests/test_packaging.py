from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from citespec_resource import update

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PACKAGING / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_release = _load("verify_release")
make_manifest = _load("make_manifest")


def _bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


BASH = _bash()


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    return private, public


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def _signed_manifest(private: Ed25519PrivateKey, payload: dict) -> dict:
    payload["signature"] = base64.b64encode(
        private.sign(verify_release.manifest_signing_bytes(payload))
    ).decode("ascii")
    return payload


def _first_install_entry(package: Path, installer_name: str, installer: bytes) -> dict:
    return {
        "url": package.name,
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "size": package.stat().st_size,
        "installer": installer_name,
        "installer_sha256": hashlib.sha256(installer).hexdigest(),
        "installer_size": len(installer),
    }


def test_verifier_public_key_matches_the_client_constant():
    assert verify_release.UPDATE_PUBLIC_KEY_B64 == update.UPDATE_PUBLIC_KEY_B64


def test_verifier_accepts_a_signed_first_install_package(tmp_path):
    private, public = _keypair()
    installer = b"#!/usr/bin/env bash\necho install\n"
    package = _zip(
        tmp_path / "citespec-resource-9.9.9-linux-x64-full.zip",
        {"install.sh": installer, "README.txt": b"readme"},
    )
    manifest = _signed_manifest(
        private,
        {
            "schema": 1,
            "version": "9.9.9",
            "protocol": 1,
            "min_client_protocol": 1,
            "artifacts": {},
            "first_install": {"linux-x64": _first_install_entry(package, "install.sh", installer)},
        },
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    extracted = tmp_path / "install.sh"
    extracted.write_bytes(installer)

    result = verify_release.verify_release(
        manifest_path,
        package,
        platform="linux-x64",
        installer_path=extracted,
        public_key_b64=public,
    )

    assert result["version"] == "9.9.9"
    assert result["installer"] == "install.sh"
    assert result["sha256"] == hashlib.sha256(package.read_bytes()).hexdigest()


def test_verifier_rejects_a_tampered_package(tmp_path):
    private, public = _keypair()
    installer = b"echo install\n"
    package = _zip(
        tmp_path / "citespec-resource-9.9.9-linux-x64-full.zip",
        {"install.sh": installer},
    )
    manifest = _signed_manifest(
        private,
        {
            "schema": 1,
            "version": "9.9.9",
            "protocol": 1,
            "min_client_protocol": 1,
            "artifacts": {},
            "first_install": {"linux-x64": _first_install_entry(package, "install.sh", installer)},
        },
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    data = bytearray(package.read_bytes())
    data[-1] ^= 0xFF
    package.write_bytes(bytes(data))

    with pytest.raises(verify_release.VerificationError, match="SHA-256"):
        verify_release.verify_release(
            manifest_path, package, platform="linux-x64", public_key_b64=public
        )


def test_verifier_rejects_a_manifest_pointing_at_a_different_installer(tmp_path):
    private, public = _keypair()
    installer = b"echo install\n"
    package = _zip(
        tmp_path / "citespec-resource-9.9.9-linux-x64-full.zip",
        {"install.sh": installer},
    )
    entry = _first_install_entry(package, "install.sh", installer)
    entry["installer_sha256"] = "0" * 64
    manifest = _signed_manifest(
        private,
        {
            "schema": 1,
            "version": "9.9.9",
            "protocol": 1,
            "min_client_protocol": 1,
            "artifacts": {},
            "first_install": {"linux-x64": entry},
        },
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(verify_release.VerificationError, match="inside the package"):
        verify_release.verify_release(
            manifest_path, package, platform="linux-x64", public_key_b64=public
        )


def test_verifier_rejects_a_manifest_without_first_install_entry(tmp_path):
    private, public = _keypair()
    package = tmp_path / "citespec-resource-9.9.9-linux-x64-full.zip"
    package.write_bytes(b"not a zip")
    manifest = _signed_manifest(
        private,
        {
            "schema": 1,
            "version": "9.9.9",
            "protocol": 1,
            "min_client_protocol": 1,
            "artifacts": {},
        },
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(verify_release.VerificationError, match="first_install"):
        verify_release.verify_release(
            manifest_path, package, platform="linux-x64", public_key_b64=public
        )


def test_verifier_rejects_a_bad_signature(tmp_path):
    private, public = _keypair()
    installer = b"echo install\n"
    package = _zip(tmp_path / "pkg.zip", {"install.sh": installer})
    manifest = _signed_manifest(
        private,
        {
            "schema": 1,
            "version": "9.9.9",
            "protocol": 1,
            "min_client_protocol": 1,
            "artifacts": {},
            "first_install": {"linux-x64": _first_install_entry(package, "install.sh", installer)},
        },
    )
    manifest["notes"] = "changed after signing"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(verify_release.VerificationError, match="signature"):
        verify_release.verify_release(
            manifest_path, package, platform="linux-x64", public_key_b64=public
        )


def test_make_manifest_covers_full_package_and_installer(tmp_path, monkeypatch):
    version = make_manifest._package_version()
    artifacts = tmp_path / "dist"
    artifacts.mkdir()
    (artifacts / f"citespec-resource-{version}-windows-x64.zip").write_bytes(b"payload")
    installer = b"# install.ps1\n"
    full = artifacts / f"citespec-resource-{version}-windows-x64-full.zip"
    _zip(full, {"install.ps1": installer, "README.txt": b"readme"})
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "make_manifest.py",
            "--artifacts-dir",
            str(artifacts),
            "--version",
            version,
            "--output",
            str(output),
            "--unsigned",
        ],
    )
    assert make_manifest.main() == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    entry = manifest["first_install"]["windows-x64"]
    assert entry["installer"] == "install.ps1"
    assert entry["installer_sha256"] == hashlib.sha256(installer).hexdigest()
    assert entry["sha256"] == hashlib.sha256(full.read_bytes()).hexdigest()
    assert entry["size"] == full.stat().st_size


def test_make_manifest_requires_a_full_package_for_every_payload(tmp_path, monkeypatch):
    version = make_manifest._package_version()
    artifacts = tmp_path / "dist"
    artifacts.mkdir()
    (artifacts / f"citespec-resource-{version}-linux-x64.zip").write_bytes(b"payload")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "make_manifest.py",
            "--artifacts-dir",
            str(artifacts),
            "--version",
            version,
            "--output",
            str(tmp_path / "manifest.json"),
            "--unsigned",
        ],
    )
    with pytest.raises(SystemExit, match="missing full install package"):
        make_manifest.main()


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX install.sh")
def test_install_sh_verify_authenticates_the_script(tmp_path):
    assert BASH is not None
    package = tmp_path / "package"
    package.mkdir()
    shutil.copy2(PACKAGING / "install.sh", package / "install.sh")
    launcher = package / "citespec-resource"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    (package / "versions" / "1.0.0").mkdir(parents=True)
    installed_payload = package / "versions" / "1.0.0" / "citespec-resource-app"
    installed_payload.write_text("#!/bin/sh\n", encoding="utf-8")
    installed_payload.chmod(0o755)
    digest = hashlib.sha256((package / "install.sh").read_bytes()).hexdigest()
    install_dir = tmp_path / "install"
    bin_dir = tmp_path / "bin"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CITESPEC_CLIENT_HOME": str(install_dir),
        "CITESPEC_CLIENT_BIN": str(bin_dir),
    }

    wrong = subprocess.run(
        [BASH, str(package / "install.sh"), "--verify", "0" * 64],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert wrong.returncode == 1
    assert "SHA-256 mismatch" in wrong.stderr
    assert not install_dir.exists()

    verified = subprocess.run(
        [BASH, str(package / "install.sh"), "--verify", digest],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert verified.returncode == 0, verified.stderr
    assert "Verified install.sh SHA-256" in verified.stdout
    assert (install_dir / "citespec-resource").is_file()
    assert (bin_dir / "citespec-resource").exists()


def test_install_ps1_can_verify_its_own_hash():
    source = (PACKAGING / "install.ps1").read_text(encoding="utf-8")
    assert "[string]$Verify" in source
    assert "Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath" in source
    assert "$actual -ne $expected" in source
