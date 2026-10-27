"""Create and sign the client update manifest after platform builds finish."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from citespec_resource import MIN_CLIENT_PROTOCOL, PROTOCOL_VERSION
from citespec_resource.update import (
    MANIFEST_SCHEMA,
    UPDATE_PUBLIC_KEY_B64,
    manifest_signing_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("windows-x64", "linux-x64", "linux-arm64")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--base-url",
        default="",
        help="Release asset base URL; defaults to the GitHub tag for this version.",
    )
    parser.add_argument(
        "--signing-key",
        default=os.environ.get("CITESPEC_UPDATE_SIGNING_KEY", ""),
        help="Base64 raw Ed25519 seed. Prefer the CI secret over a command-line value.",
    )
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--unsigned",
        action="store_true",
        help="Write an unsigned manifest for local inspection only.",
    )
    args = parser.parse_args()

    version = args.version.removeprefix("v")
    if version != _package_version():
        raise SystemExit(
            f"tag/version {version!r} does not match package version {_package_version()!r}"
        )
    base_url = args.base_url.rstrip("/") or (
        f"https://github.com/Mr-enthalpy/infrastructure/releases/download/v{version}"
    )
    artifacts: dict[str, dict[str, object]] = {}
    for platform in PLATFORMS:
        filename = f"citespec-resource-{version}-{platform}.zip"
        path = args.artifacts_dir / filename
        if not path.is_file():
            continue
        digest = _sha256(path)
        artifacts[platform] = {
            "url": f"{base_url}/{filename}",
            "sha256": digest,
            "size": path.stat().st_size,
        }
    if not artifacts:
        raise SystemExit(f"no client artifacts found in {args.artifacts_dir}")

    # A first install has no launcher to verify an update, so the signed
    # manifest must also cover the full zip and the install script inside it.
    first_install: dict[str, dict[str, object]] = {}
    for platform in PLATFORMS:
        filename = f"citespec-resource-{version}-{platform}-full.zip"
        path = args.artifacts_dir / filename
        if not path.is_file():
            continue
        installer = "install.ps1" if platform == "windows-x64" else "install.sh"
        try:
            with zipfile.ZipFile(path) as archive:
                installer_bytes = archive.read(installer)
        except (KeyError, zipfile.BadZipFile, OSError) as exc:
            raise SystemExit(f"{filename} does not contain {installer}: {exc}") from exc
        first_install[platform] = {
            "url": f"{base_url}/{filename}",
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "installer": installer,
            "installer_sha256": hashlib.sha256(installer_bytes).hexdigest(),
            "installer_size": len(installer_bytes),
        }
    missing_full = sorted(set(artifacts) - set(first_install))
    if missing_full:
        raise SystemExit(
            "missing full install package (needed to verify first install) for: "
            + ", ".join(missing_full)
        )

    manifest: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "protocol": PROTOCOL_VERSION,
        "min_client_protocol": MIN_CLIENT_PROTOCOL,
        "published_at": datetime.now(UTC).isoformat(),
        "notes": args.notes,
        "artifacts": artifacts,
        "first_install": first_install,
    }
    if args.unsigned:
        print(
            "warning: writing an unsigned manifest; the packaged client will reject it",
            file=sys.stderr,
        )
    else:
        if not args.signing_key:
            raise SystemExit(
                "CITESPEC_UPDATE_SIGNING_KEY is required; use --unsigned only for local inspection"
            )
        private_key = _decode_signing_key(args.signing_key)
        actual_public = base64.b64encode(
            private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).decode("ascii")
        if actual_public != UPDATE_PUBLIC_KEY_B64:
            raise SystemExit(
                "signing key does not match the public key embedded in the client; "
                f"expected {UPDATE_PUBLIC_KEY_B64}, got {actual_public}"
            )
        signature = private_key.sign(manifest_signing_bytes(manifest))
        manifest["signature"] = base64.b64encode(signature).decode("ascii")
        print(
            f"public key (must match the client): {UPDATE_PUBLIC_KEY_B64}",
            file=sys.stderr,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"manifest: {args.output}")
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        payload = tomllib.load(handle)
    return str(payload.get("project", {}).get("version", ""))


def _decode_signing_key(raw: str) -> Ed25519PrivateKey:
    value = raw.strip()
    if value.startswith("-----BEGIN"):
        raise SystemExit(
            "PEM keys are not supported here; export the raw 32-byte Ed25519 seed as base64"
        )
    try:
        seed = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise SystemExit("CITESPEC_UPDATE_SIGNING_KEY is not valid base64") from exc
    if len(seed) != 32:
        raise SystemExit("Ed25519 signing key must be exactly 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


if __name__ == "__main__":
    raise SystemExit(main())
