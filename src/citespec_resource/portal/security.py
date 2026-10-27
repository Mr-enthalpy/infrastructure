"""Password, TOTP, secret-key, CSRF, and signed-token primitives for the portal.

Argon2id is used for local passwords.  TOTP secrets are encrypted at rest with
Fernet using a key derived from the root-owned portal secret file.  Session and
activation tokens are random and only their SHA-256 digests live in SQLite.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from pyotp import TOTP, random_base32

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 256
USERNAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


class PortalSecurityError(ValueError):
    pass


def load_secret(path: str | Path, *, create: bool = False) -> bytes:
    """Load the 32-byte portal secret, optionally creating it atomically."""

    secret_path = Path(path)
    if secret_path.is_file():
        try:
            raw = base64.urlsafe_b64decode(secret_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as exc:
            raise PortalSecurityError(f"cannot read portal secret file: {exc}") from exc
        if len(raw) < 32:
            raise PortalSecurityError("portal secret file must contain at least 32 bytes")
        return raw
    if not create:
        raise PortalSecurityError(f"portal secret file does not exist: {secret_path}")
    raw = os.urandom(32)
    secret_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        secret_path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = secret_path.with_name(f".{secret_path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(base64.urlsafe_b64encode(raw).decode("ascii") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, secret_path)
    finally:
        temporary.unlink(missing_ok=True)
    return raw


def fernet_for(secret: bytes) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret + b":totp").digest())
    return Fernet(key)


def seal(secret: bytes, plaintext: str) -> str:
    return fernet_for(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")


def unseal(secret: bytes, token: str) -> str:
    try:
        return fernet_for(secret).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise PortalSecurityError("stored TOTP secret cannot be decrypted") from exc


_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str, *, username: str = "") -> str:
    validate_password(password, username=username)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash or not password:
        return False
    try:
        return bool(_hasher.verify(password_hash, password))
    except (VerificationError, VerifyMismatchError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return bool(_hasher.check_needs_rehash(password_hash))
    except Exception:
        return True


def validate_password(password: str, *, username: str = "") -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PortalSecurityError(f"密码至少需要 {PASSWORD_MIN_LENGTH} 个字符")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise PortalSecurityError("密码过长")
    if username and password.casefold() == username.casefold():
        raise PortalSecurityError("密码不能与用户名相同")
    if password.strip() != password:
        raise PortalSecurityError("密码首尾不能有空白字符")


def validate_username(username: str) -> str:
    value = username.strip()
    if not USERNAME_RE.fullmatch(value):
        raise PortalSecurityError("用户名格式不正确：小写字母或下划线开头，仅含 a-z0-9_-")
    return value


def new_totp_secret() -> str:
    return random_base32()


def totp_uri(secret: str, *, username: str, issuer: str) -> str:
    return TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(
    secret: str,
    code: str,
    *,
    last_counter: int = 0,
    now: float | None = None,
) -> int | None:
    """Verify a six-digit TOTP and reject a previously used time step."""

    value = code.strip().replace(" ", "")
    if not value.isdigit() or len(value) != 6:
        return None
    current = time.time() if now is None else now
    current_counter = int(current // 30)
    totp = TOTP(secret)
    for offset in (-1, 0, 1):
        counter = current_counter + offset
        if counter <= last_counter:
            continue
        if hmac.compare_digest(totp.at(counter * 30), value):
            return counter
    return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def sign_token(secret: bytes, payload: dict[str, Any], *, ttl_seconds: int) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def verify_token(secret: bytes, token: str) -> dict[str, Any] | None:
    try:
        encoded, signature_b64 = token.split(".", 1)
        padding = "=" * (-len(signature_b64) % 4)
        signature = base64.urlsafe_b64decode(signature_b64 + padding)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload
