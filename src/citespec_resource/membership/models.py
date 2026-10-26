"""Authoritative membership state machine and small value helpers.

Statuses (docs/goals/member-onboarding-and-ux.md §6.1):

    provisioning -> active -> suspended -> active
                       ↘ expired
                       ↘ revoked
    provisioning -> provision_failed -> provisioning

"Approved" is not "provisioned": between approval and activation the member
stays in ``provisioning`` (or ``provision_failed``) and must not pass
authorization for normal resource operations.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

MEMBER_STATUSES = (
    "provisioning",
    "active",
    "suspended",
    "expired",
    "revoked",
    "provision_failed",
)

APPLICATION_STATUSES = (
    "pending_review",
    "needs_info",
    "approved",
    "rejected",
    "cancelled",
)

OPERATION_ACTIONS = (
    "provision_member",
    "set_keys",
    "disable_member",
    "enable_member",
)
OPERATION_STATUSES = ("pending", "running", "succeeded", "failed")

# Transitions that keep the record's history monotonic (§13): a revoked member
# can never drift back to active through a late provisioning task.
ALLOWED_MEMBER_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "provisioning": ("active", "provision_failed", "suspended", "revoked"),
    "provision_failed": ("provisioning", "suspended", "revoked"),
    "active": ("suspended", "expired", "revoked"),
    "suspended": ("active", "revoked", "expired"),
    "expired": ("active", "revoked"),
    "revoked": (),
}

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Public key types accepted for member keys (§8.3): plain OpenSSH public keys
# only — no certificates, no authorized_keys options.
KEY_TYPE_RE = re.compile(
    r"^(ssh-(rsa|dss|ed25519)|ecdsa-sha2-nistp(256|384|521)|"
    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)$"
)

PRIVATE_KEY_MARKERS = (
    "PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "ssh-rsa-base64-private",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.fullmatch(name))


def key_fingerprint(public_key: str) -> str:
    """OpenSSH-compatible SHA-256 fingerprint of the key blob.

    ``ssh-keygen -lf`` hashes the decoded wire-format key blob and prints the
    digest in unpadded base64, not the hex digest of the base64 text.  Keeping
    this identical to OpenSSH is important because administrators compare the
    value shown by ``citespec-resource me`` with ``ssh-keygen -lf`` out of band.
    """
    parts = public_key.split()
    material = parts[1] if len(parts) >= 2 else public_key
    try:
        blob = base64.b64decode(material, validate=True)
    except (ValueError, TypeError):
        # parse_public_key rejects malformed material before this point; keep a
        # deterministic fallback for callers that pass legacy/unvalidated data.
        blob = material.encode("utf-8")
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii")
    return f"SHA256:{digest.rstrip('=')}"


def parse_public_key(raw: str) -> tuple[str, str]:
    """Validate a public key submission; returns (normalized_key, fingerprint).

    Raises ValueError with a safe, non-echoing message on private keys,
    options injection, unknown types, or malformed input. The raw input is
    never included in the error (§5.2: logs must not record submitted keys).
    """
    text = " ".join(raw.strip().splitlines()).strip()
    if not text:
        raise ValueError("公钥为空")
    if len(text) > 4000:
        raise ValueError("公钥过长，请确认提交的是 .pub 公钥文件内容")
    if "PRIVATE KEY" in text or any(marker in text for marker in PRIVATE_KEY_MARKERS):
        raise ValueError("检测到疑似私钥。私钥绝不能提交：请重新生成密钥对并撤销可能泄露的凭据")
    parts = text.split()
    if len(parts) < 2:
        raise ValueError("公钥格式不正确")
    key_type, material = parts[0], parts[1]
    if not KEY_TYPE_RE.fullmatch(key_type):
        raise ValueError("不支持的公钥类型（仅支持普通 SSH 公钥，不含证书/选项）")
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", material):
        raise ValueError("公钥内容格式不正确")
    # authorized_keys options (command=, environment=, from=...) must never
    # be installed as authorization material (§8.3).
    for option in parts[2:]:
        if "=" in option or option.startswith('"'):
            raise ValueError("公钥不能包含 authorized_keys 选项")
    comment = " ".join(parts[2:])[:128]
    normalized = f"{key_type} {material}" + (f" {comment}" if comment else "")
    return normalized, key_fingerprint(normalized)


@dataclass(frozen=True)
class MemberRecord:
    member_id: str
    linux_uid: int | None
    linux_username: str | None
    status: str
    valid_until: str
    display_name: str
    nickname: str
    public_contact: str
    private_contact: str
    access_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AccessDecision:
    """Result of the unified authorization check (§6.1)."""

    allowed: bool
    reason: str  # "" | no_member_record | status:<status> | expired
    member_id: str
    access_version: int
    status: str
    valid_until: str


def is_valid_now(member: MemberRecord, *, now: datetime | None = None) -> bool:
    """Status AND valid_until are both checked on every authorization (§6.3);
    expiry never depends on a periodic cleanup task."""
    current = now or utcnow()
    if member.status != "active":
        return False
    if member.valid_until:
        try:
            until = datetime.fromisoformat(member.valid_until)
        except ValueError:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if until <= current:
            return False
    return True
