#!/usr/bin/env python3
"""Verify a first-install package against the signed release manifest.

The client launcher only trusts the signed *update* manifest.  A first install
has no launcher yet, so this standalone verifier closes that gap: it checks the
Ed25519 signature with the public key embedded in every client, then checks the
full zip, the install script inside it, and (optionally) an extracted
``install.sh``/``install.ps1`` by SHA-256.

    python verify_release.py \
      --manifest citespec-resource-manifest.json \
      --package citespec-resource-<version>-linux-x64-full.zip

The public key is duplicated here instead of imported so this file can run from
a bare checkout or release download without installing the package.  A test
asserts it stays identical to ``citespec_resource.update.UPDATE_PUBLIC_KEY_B64``.

Residual risk: an attacker who controls the download path could replace this
verifier along with the package.  Publish its SHA-256 next to the release notes
and compare that fingerprint out of band on the first install; afterwards the
launcher's built-in signed-update path is used.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

UPDATE_PUBLIC_KEY_B64 = "LcxxIJY0DcCdDVzMcuk5pIJ4jaTz2hSJ6I50n2E9MvU="
MANIFEST_SCHEMA = 1


class VerificationError(RuntimeError):
    """Raised when a release asset does not match the signed manifest."""


def manifest_signing_bytes(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def verify_manifest_signature(
    payload: dict[str, Any],
    *,
    public_key_b64: str = UPDATE_PUBLIC_KEY_B64,
) -> None:
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise VerificationError(f"unsupported manifest schema: {payload.get('schema')!r}")
    signature = payload.get("signature")
    if not isinstance(signature, str) or not signature:
        raise VerificationError("manifest is not signed")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise VerificationError("cryptography is required to verify the release signature") from exc
    try:
        public_bytes = base64.b64decode(public_key_b64, validate=True)
        signature_bytes = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature_bytes, manifest_signing_bytes(payload)
        )
    except (ValueError, InvalidSignature) as exc:
        raise VerificationError("manifest signature is invalid") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_install_entry(payload: dict[str, Any], platform: str) -> dict[str, Any]:
    section = payload.get("first_install")
    if not isinstance(section, dict):
        raise VerificationError("manifest has no first_install section")
    entry = section.get(platform)
    if not isinstance(entry, dict):
        raise VerificationError(f"manifest has no first_install entry for {platform}")
    for field in ("sha256", "size", "installer", "installer_sha256"):
        if field not in entry:
            raise VerificationError(f"first_install.{platform} has no {field}")
    digest = entry["sha256"]
    installer_digest = entry["installer_sha256"]
    if not isinstance(digest, str) or not _is_sha256(digest):
        raise VerificationError(f"first_install.{platform}.sha256 is invalid")
    if not isinstance(installer_digest, str) or not _is_sha256(installer_digest):
        raise VerificationError(f"first_install.{platform}.installer_sha256 is invalid")
    size = entry["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise VerificationError(f"first_install.{platform}.size is invalid")
    installer = entry["installer"]
    if installer not in {"install.sh", "install.ps1"}:
        raise VerificationError(f"first_install.{platform}.installer is invalid")
    return entry


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def verify_release(
    manifest_path: str | Path,
    package_path: str | Path,
    *,
    platform: str,
    installer_path: str | Path | None = None,
    public_key_b64: str = UPDATE_PUBLIC_KEY_B64,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path)
    package = Path(package_path)
    try:
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read manifest {manifest_file}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationError("manifest must be a JSON object")
    verify_manifest_signature(payload, public_key_b64=public_key_b64)
    entry = _first_install_entry(payload, platform)
    if not package.is_file():
        raise VerificationError(f"package does not exist: {package}")
    expected_size = int(entry["size"])
    actual_size = package.stat().st_size
    if actual_size != expected_size:
        raise VerificationError(
            f"package size mismatch: {actual_size} bytes, expected {expected_size}"
        )
    actual_digest = _sha256(package)
    if actual_digest.lower() != str(entry["sha256"]).lower():
        raise VerificationError("package SHA-256 does not match the signed manifest")
    installer_name = str(entry["installer"])
    installer_digest = _sha256_from_zip(package, installer_name)
    if installer_digest.lower() != str(entry["installer_sha256"]).lower():
        raise VerificationError(
            f"{installer_name} inside the package does not match the signed manifest"
        )
    if installer_path is not None:
        extracted = Path(installer_path)
        if not extracted.is_file():
            raise VerificationError(f"installer does not exist: {extracted}")
        if _sha256(extracted).lower() != installer_digest.lower():
            raise VerificationError(
                f"extracted {installer_name} does not match the signed manifest"
            )
    return {
        "platform": platform,
        "version": payload.get("version"),
        "package": str(package),
        "sha256": actual_digest,
        "size": actual_size,
        "installer": installer_name,
        "installer_sha256": installer_digest,
    }


def _sha256_from_zip(package: Path, installer_name: str) -> str:
    try:
        with zipfile.ZipFile(package) as archive:
            data = archive.read(installer_name)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise VerificationError(
            f"package does not contain a valid {installer_name}: {exc}"
        ) from exc
    return hashlib.sha256(data).hexdigest()


def _platform_key() -> str | None:
    import platform as platform_module

    system = platform_module.system().lower()
    machine = platform_module.machine().lower()
    if machine in {"amd64", "x86_64"}:
        if system == "windows":
            return "windows-x64"
        if system == "linux":
            return "linux-x64"
    if machine in {"aarch64", "arm64"} and system == "linux":
        return "linux-arm64"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument(
        "--platform",
        default=_platform_key() or "",
        help="Artifact key (windows-x64, linux-x64, linux-arm64)",
    )
    parser.add_argument(
        "--installer",
        type=Path,
        default=None,
        help="Extracted install.sh/install.ps1 to compare with the signed hash",
    )
    parser.add_argument(
        "--public-key",
        default=UPDATE_PUBLIC_KEY_B64,
        help="Base64 raw Ed25519 public key (defaults to the embedded client key)",
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON result")
    args = parser.parse_args(argv)
    if not args.platform:
        print("could not determine the platform; pass --platform", file=sys.stderr)
        return 2
    try:
        result = verify_release(
            args.manifest,
            args.package,
            platform=args.platform,
            installer_path=args.installer,
            public_key_b64=args.public_key,
        )
    except VerificationError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"verified {result['package']}")
        print(f"  version:    {result['version']}")
        print(f"  platform:   {result['platform']}")
        print(f"  sha256:     {result['sha256']}")
        print(f"  installer:  {result['installer']} ({result['installer_sha256']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
