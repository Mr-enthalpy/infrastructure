"""Domain constants and records for environment memory (architecture §3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROJECT_ACTIVE = "active"
PROJECT_ARCHIVED = "archived"
PROJECT_STATUSES = frozenset({PROJECT_ACTIVE, PROJECT_ARCHIVED})

CAPTURE_COMPLETE = "complete"
CAPTURE_PARTIAL = "partial"
CAPTURE_MISSING = "missing"
CAPTURE_FIDELITIES = frozenset({CAPTURE_COMPLETE, CAPTURE_PARTIAL, CAPTURE_MISSING})

# Verification is deliberately separate from capture fidelity and from the
# admin decision that allowed a legacy claim: a faithful capture is not proof
# that the environment rebuilds or that the project still trains.
VERIFY_UNVERIFIED = "unverified"
VERIFY_BUILT = "built"
VERIFY_FUNCTIONAL_PASSED = "functional_passed"
VERIFY_FUNCTIONAL_FAILED = "functional_failed"
VERIFICATION_STATES = frozenset(
    {VERIFY_UNVERIFIED, VERIFY_BUILT, VERIFY_FUNCTIONAL_PASSED, VERIFY_FUNCTIONAL_FAILED}
)
# A verification result only moves forward; a failed functional run can be
# retried but must never silently turn back into "built" or "unverified".
_VERIFICATION_TRANSITIONS: dict[str, frozenset[str]] = {
    VERIFY_UNVERIFIED: frozenset({VERIFY_BUILT}),
    VERIFY_BUILT: frozenset({VERIFY_FUNCTIONAL_PASSED, VERIFY_FUNCTIONAL_FAILED}),
    VERIFY_FUNCTIONAL_FAILED: frozenset({VERIFY_BUILT, VERIFY_FUNCTIONAL_PASSED}),
    VERIFY_FUNCTIONAL_PASSED: frozenset(),
}

VISIBILITY_PRIVATE = "private"
VISIBILITY_PROJECT = "project"
VISIBILITY_ADMIN_SHARED = "admin_shared"
VISIBILITIES = frozenset({VISIBILITY_PRIVATE, VISIBILITY_PROJECT, VISIBILITY_ADMIN_SHARED})

ORIGIN_CAPTURE = "capture"
ORIGIN_LEGACY = "legacy_claim"
ORIGIN_MANUAL = "manual"
ORIGINS = frozenset({ORIGIN_CAPTURE, ORIGIN_LEGACY, ORIGIN_MANUAL})

BUILD_PENDING = "pending"
BUILD_RUNNING = "running"
BUILD_BUILT = "built"
BUILD_VERIFIED = "verified"
BUILD_FAILED = "failed"
BUILD_STATUSES = frozenset(
    {BUILD_PENDING, BUILD_RUNNING, BUILD_BUILT, BUILD_VERIFIED, BUILD_FAILED}
)

SOURCE_AVAILABLE = "available"
SOURCE_CLAIMED = "claimed"
SOURCE_DISMISSED = "dismissed"
SOURCE_STATUSES = frozenset({SOURCE_AVAILABLE, SOURCE_CLAIMED, SOURCE_DISMISSED})

CLAIM_PENDING = "pending"
CLAIM_APPROVED = "approved"
CLAIM_REJECTED = "rejected"
CLAIM_CONFLICT = "conflict"
CLAIM_WITHDRAWN = "withdrawn"
CLAIM_STATUSES = frozenset(
    {CLAIM_PENDING, CLAIM_APPROVED, CLAIM_REJECTED, CLAIM_CONFLICT, CLAIM_WITHDRAWN}
)


class EnvironmentError(Exception):
    """Base class for environment-memory failures."""


class EnvironmentNotFound(EnvironmentError):
    """The project, version, build, source, or claim does not exist."""


class EnvironmentConflict(EnvironmentError):
    """The requested transition races with the stored state."""


class EnvironmentForbidden(EnvironmentError):
    """The caller may not read or change this record."""


class ManifestError(EnvironmentError, ValueError):
    """The submitted manifest is malformed, unsafe, or too large."""


@dataclass(frozen=True)
class Project:
    project_id: str
    owner_member_id: str
    name: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Profile:
    profile_id: str
    project_id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class EnvironmentVersion:
    version_id: str
    profile_id: str
    version: int
    manifest: str
    sha256: str
    origin: str
    capture_fidelity: str
    verification: str
    visibility: str
    created_by_member_id: str
    created_at: str


@dataclass(frozen=True)
class EnvironmentBuild:
    build_id: str
    version_id: str
    server_id: str
    base_image: str
    base_image_id: str
    image_tag: str
    image_id: str
    status: str
    detail: str
    evidence: str
    created_at: str
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class LegacySource:
    source_id: str
    server_id: str
    container_id: str
    image_ref: str
    image_id: str
    container_name: str
    state_at_scan: str
    container_created_at: str
    sanitized_metadata: str
    status: str
    scanned_at: str


@dataclass(frozen=True)
class LegacyClaim:
    claim_id: str
    source_id: str
    member_id: str
    project_id: str
    reason: str
    status: str
    reviewer: str
    review_note: str
    reviewed_at: str
    version_id: str | None
    created_at: str
    updated_at: str


def verification_allows(current: str, new: str) -> bool:
    """Whether a verification state may move from ``current`` to ``new``."""

    if current not in VERIFICATION_STATES or new not in VERIFICATION_STATES:
        return False
    return new in _VERIFICATION_TRANSITIONS.get(current, frozenset())


def project_from_row(row: Any) -> Project:
    return Project(
        project_id=str(row["project_id"]),
        owner_member_id=str(row["owner_member_id"]),
        name=str(row["name"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def profile_from_row(row: Any) -> Profile:
    return Profile(
        profile_id=str(row["profile_id"]),
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        created_at=str(row["created_at"]),
    )


def version_from_row(row: Any) -> EnvironmentVersion:
    return EnvironmentVersion(
        version_id=str(row["version_id"]),
        profile_id=str(row["profile_id"]),
        version=int(row["version"]),
        manifest=str(row["manifest"]),
        sha256=str(row["sha256"]),
        origin=str(row["origin"]),
        capture_fidelity=str(row["capture_fidelity"]),
        verification=str(row["verification"]),
        visibility=str(row["visibility"]),
        created_by_member_id=str(row["created_by_member_id"]),
        created_at=str(row["created_at"]),
    )
