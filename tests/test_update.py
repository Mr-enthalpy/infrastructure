from __future__ import annotations

import base64
import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from citespec_resource import update


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    return private, public


def _artifact(path: Path, url: str | None = None) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "url": url or path.as_uri(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _signed_manifest(
    private: Ed25519PrivateKey,
    *,
    version: str,
    artifacts: dict[str, dict[str, object]],
    protocol: int = 1,
    min_client_protocol: int = 1,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema": 1,
        "version": version,
        "protocol": protocol,
        "min_client_protocol": min_client_protocol,
        "published_at": "2026-10-27T00:00:00+00:00",
        "notes": "test",
        "artifacts": artifacts,
    }
    manifest["signature"] = base64.b64encode(
        private.sign(update.manifest_signing_bytes(manifest))
    ).decode("ascii")
    return manifest


def _payload_zip(path: Path, *, content: bytes = b"fake payload") -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(update.payload_name(), content)
        archive.writestr("_internal/data.txt", "fixture")


def test_platform_key_maps_supported_targets():
    assert update.platform_key("Windows", "AMD64") == "windows-x64"
    assert update.platform_key("Linux", "x86_64") == "linux-x64"
    assert update.platform_key("Linux", "aarch64") == "linux-arm64"
    assert update.platform_key("Darwin", "arm64") is None


def test_verify_manifest_rejects_tampering():
    private, public = _keypair()
    manifest = _signed_manifest(
        private,
        version="0.2.0",
        artifacts={"test": {"url": "https://example.invalid/x.zip", "sha256": "0" * 64, "size": 1}},
    )
    verified = update.verify_manifest(manifest, public_key_b64=public)
    assert verified.version == "0.2.0"

    manifest["notes"] = "changed after signing"
    with pytest.raises(update.UpdateError, match="signature"):
        update.verify_manifest(manifest, public_key_b64=public)


def test_verify_manifest_requires_signature():
    with pytest.raises(update.UpdateError, match="not signed"):
        update.verify_manifest(
            {
                "schema": 1,
                "version": "0.2.0",
                "protocol": 1,
                "min_client_protocol": 1,
                "artifacts": {},
            }
        )


def test_check_for_update_selects_platform_artifact():
    private, public = _keypair()
    manifest = _signed_manifest(
        private,
        version="0.2.0",
        artifacts={
            "test": {"url": "https://example.invalid/x.zip", "sha256": "0" * 64, "size": 1},
        },
    )
    info = update.check_for_update(
        current_version="0.1.0",
        platform="test",
        manifest_payload=manifest,
        public_key_b64=public,
    )
    assert info.available is True
    assert info.latest_version == "0.2.0"
    assert info.artifact is not None
    assert info.artifact.url.endswith("x.zip")


def test_check_for_update_reports_up_to_date_and_missing_platform():
    private, public = _keypair()
    manifest = _signed_manifest(
        private,
        version="0.2.0",
        artifacts={
            "test": {"url": "https://example.invalid/x.zip", "sha256": "0" * 64, "size": 1},
        },
    )
    current = update.check_for_update(
        current_version="0.2.0",
        platform="test",
        manifest_payload=manifest,
        public_key_b64=public,
    )
    assert current.available is False
    assert current.reason == "already up to date"

    missing = update.check_for_update(
        current_version="0.1.0",
        platform="other",
        manifest_payload=manifest,
        public_key_b64=public,
    )
    assert missing.available is False
    assert "other" in missing.reason


def test_install_update_downloads_verifies_and_keeps_previous(tmp_path):
    private, public = _keypair()
    root = tmp_path / "app"
    root.mkdir()
    archive = tmp_path / "payload.zip"
    _payload_zip(archive, content=b"v2")
    manifest = _signed_manifest(
        private,
        version="0.2.0",
        artifacts={"test": _artifact(archive)},
    )
    info = update.check_for_update(
        current_version="0.1.0",
        platform="test",
        manifest_payload=manifest,
        public_key_b64=public,
    )
    installed = update.install_update(info, app_root=root, allow_insecure=True)
    assert installed.version == "0.2.0"
    assert installed.previous_version == ""
    payload = update.resolve_payload(root)
    assert payload is not None
    assert payload.read_bytes() == b"v2"
    assert update.installed_version(root) == "0.2.0"

    archive2 = tmp_path / "payload2.zip"
    _payload_zip(archive2, content=b"v3")
    manifest2 = _signed_manifest(
        private,
        version="0.3.0",
        artifacts={"test": _artifact(archive2)},
    )
    info2 = update.check_for_update(
        current_version="0.2.0",
        platform="test",
        manifest_payload=manifest2,
        public_key_b64=public,
    )
    installed2 = update.install_update(info2, app_root=root, allow_insecure=True)
    assert installed2.version == "0.3.0"
    assert installed2.previous_version == "0.2.0"
    assert update.resolve_payload(root) is not None
    assert (root / "versions" / "0.2.0").is_dir()


def test_install_update_replaces_incomplete_version_directory(tmp_path):
    private, public = _keypair()
    root = tmp_path / "app"
    incomplete = root / "versions" / "0.2.0"
    incomplete.mkdir(parents=True)
    (incomplete / "stale.txt").write_text("stale", encoding="utf-8")
    archive = tmp_path / "payload.zip"
    _payload_zip(archive, content=b"fresh")
    manifest = _signed_manifest(
        private,
        version="0.2.0",
        artifacts={"test": _artifact(archive)},
    )
    info = update.check_for_update(
        current_version="0.1.0",
        platform="test",
        manifest_payload=manifest,
        public_key_b64=public,
    )
    update.install_update(info, app_root=root, allow_insecure=True)
    assert update.resolve_payload(root) is not None
    assert not (incomplete / "stale.txt").exists()


def test_install_update_recovers_from_stale_lock(tmp_path):
    private, public = _keypair()
    root = tmp_path / "app"
    lock = root / "updates" / ".update.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("999999", encoding="utf-8")
    os.utime(lock, (0, 0))
    archive = tmp_path / "payload.zip"
    _payload_zip(archive)
    manifest = _signed_manifest(
        private,
        version="0.2.0",
        artifacts={"test": _artifact(archive)},
    )
    info = update.check_for_update(
        current_version="0.1.0",
        platform="test",
        manifest_payload=manifest,
        public_key_b64=public,
    )
    update.install_update(info, app_root=root, allow_insecure=True)
    assert update.resolve_payload(root) is not None
    assert not lock.exists()


def test_install_update_rejects_zip_slip_and_sha_mismatch(tmp_path):
    private, public = _keypair()
    root = tmp_path / "app"
    root.mkdir()
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(update.payload_name(), b"payload")
        bundle.writestr("../escaped.txt", b"no")
    manifest = _signed_manifest(
        private,
        version="0.2.0",
        artifacts={"test": _artifact(archive)},
    )
    info = update.check_for_update(
        current_version="0.1.0",
        platform="test",
        manifest_payload=manifest,
        public_key_b64=public,
    )
    with pytest.raises(update.UpdateError, match="unsafe path"):
        update.install_update(info, app_root=root, allow_insecure=True)
    assert not (tmp_path / "escaped.txt").exists()

    bad = _artifact(archive)
    bad["sha256"] = "0" * 64
    manifest2 = _signed_manifest(
        private,
        version="0.2.1",
        artifacts={"test": bad},
    )
    info2 = update.check_for_update(
        current_version="0.1.0",
        platform="test",
        manifest_payload=manifest2,
        public_key_b64=public,
    )
    with pytest.raises(update.UpdateError, match="SHA-256"):
        update.install_update(info2, app_root=root, allow_insecure=True)


def test_resolve_payload_ignores_unsafe_current_version(tmp_path):
    root = tmp_path / "app"
    (root / "versions").mkdir(parents=True)
    (root / update.CURRENT_FILE).write_text(
        json.dumps({"version": "../../outside"}),
        encoding="utf-8",
    )
    assert update.resolve_payload(root) is None
