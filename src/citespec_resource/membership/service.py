"""Membership approval and administration business logic (goal §7/§8).

The service is the ONLY path from "application approved" to "Linux account +
keys + workspace + active member". It freezes the reviewed snapshot, records
idempotent operations, delegates the OS work to the narrow provisioning
executor, and keeps every step recoverable (§8.1): a failure at any stage
leaves the member in provisioning/provision_failed with a retryable
operation, never a half-activated member.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import store as broker_store
from ..enrollment import store as intake_store
from ..errors import Conflict, NotFound
from . import store as member_store
from .provisioning import ProvisioningClient, ProvisioningError

# A running operation older than this is considered interrupted (broker or
# executor restart). It is failed closed and made retryable; it is never
# assumed successful just because a process died.
STALE_OPERATION_SECONDS = 600
logger = logging.getLogger(__name__)


def _digest(snapshot: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _request_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "display_name": request["display_name"],
        "username": request["username"],
        "affiliation": request["affiliation"],
        "purpose": request["purpose"],
        "contact": request["contact"],
        "nickname": request["nickname"],
        "public_contact": request["public_contact"],
        "public_keys": request["keys"],
    }


def _begin(connection: sqlite3.Connection) -> bool:
    if connection.in_transaction:
        return False
    connection.execute("BEGIN IMMEDIATE")
    return True


# Per-member serialization for operations that mutate host credentials.  This
# is intentionally NOT a database transaction: SQLite must never stay locked
# while the executor runs.  The lock only serializes work for the same member;
# different members continue in parallel.
_MEMBER_LOCKS: dict[str, threading.Lock] = {}
_MEMBER_LOCKS_GUARD = threading.Lock()


@contextmanager
def _member_lock(member_id: str) -> Any:
    with _MEMBER_LOCKS_GUARD:
        lock = _MEMBER_LOCKS.setdefault(member_id, threading.Lock())
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def list_requests(intake_db: str, *, status: str | None = None) -> list[dict[str, Any]]:
    """Broker pulls applications from the intake store (§9.2, pull-only)."""
    intake = intake_store.connect(intake_db)
    try:
        return intake_store.list_requests(intake, status=status)
    finally:
        intake.close()


def get_request(intake_db: str, request_id: str) -> dict[str, Any]:
    intake = intake_store.connect(intake_db)
    try:
        request = intake_store.get_request(intake, request_id)
        if request is None:
            raise NotFound(f"request not found: {request_id}")
        return request
    finally:
        intake.close()


def approve_request(
    connection: sqlite3.Connection,
    *,
    intake_db: str,
    request_id: str,
    reviewer_uid: int,
    valid_until: str = "",
    quota_gb: int = 200,
    review_note: str = "",
    internal_note: str = "",
    workspace_root: str = "/home/data1/citespec-users",
    provisioning: ProvisioningClient,
    expected_version: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Approve one application and provision the member (§7.2, §8.1).

    Concurrency/idempotency (§7.3): the frozen decision record is created
    under BEGIN IMMEDIATE keyed by request_id; a second approver gets a
    conflict; a retried approval reuses the same member and operation.

    Content binding (§7.3/AC-12): ``expected_version`` is the exact intake
    snapshot version the administrator reviewed. If the application changed
    after preview, approval is refused instead of provisioning unseen data.
    """
    intake = intake_store.connect(intake_db)
    try:
        request = intake_store.get_request(intake, request_id)
        if request is None:
            raise NotFound(f"request not found: {request_id}")
        if expected_version and str(request["version"]) != expected_version:
            raise Conflict("申请内容在预览后已变化；请刷新并重新核对版本后再批准")
        version = str(request["version"])
        snapshot = _request_snapshot(request)
        # Convergence first (§7.3): a retried/concurrent approval returns the
        # original member and operation instead of conflicting on the intake
        # projection, which the first approval already moved to approved.
        decision_check = member_store.get_application_decision(connection, request_id)
        if decision_check is not None:
            if decision_check["status"] == "approved":
                operation = (
                    member_store.get_operation(
                        connection, str(decision_check["operation_id"] or "")
                    )
                    if decision_check.get("operation_id")
                    else None
                )
                if operation is None:
                    raise Conflict("申请已批准但缺少开户操作记录；请联系管理员检查")
                return {
                    "request_id": request_id,
                    "status": "approved",
                    "member_id": operation.get("member_id"),
                    "operation_id": operation["operation_id"],
                    "operation_status": operation["status"],
                }
            if decision_check["status"] in ("rejected", "cancelled"):
                raise Conflict(f"申请已被处理为 {decision_check['status']}，不可批准")
        if request["status"] not in ("pending_review", "needs_info"):
            raise Conflict(f"申请当前状态为 {request['status']}，已被其他管理员处理或不可批准")
    finally:
        intake.close()

    frozen_input = {
        "username": snapshot["username"],
        "display_name": snapshot["display_name"],
        "public_keys": [key["public_key"] for key in snapshot["public_keys"]],
        "workspace_root": workspace_root,
        "quota_gb": quota_gb,
        "valid_until": valid_until,
    }
    idempotency_key = f"approve:{request_id}:{version}"

    own = _begin(connection)
    try:
        # Re-check the intake version inside the serialized decision window so
        # a change between preview/read and commit cannot slip through.
        latest = get_request(intake_db, request_id)
        if str(latest["version"]) != version:
            raise Conflict("申请内容在审批过程中发生变化；请重新预览")
        decision_now = member_store.get_application_decision(connection, request_id)
        if decision_now is not None and decision_now["status"] == "approved":
            operation_id = str(decision_now["operation_id"] or "")
        elif decision_now is not None and decision_now["status"] in ("rejected", "cancelled"):
            raise Conflict("申请已被其他管理员处理")
        else:
            existing_member = connection.execute(
                "SELECT member_id, status FROM members WHERE linux_username = ?",
                (snapshot["username"],),
            ).fetchone()
            if existing_member is not None:
                raise Conflict("该用户名已绑定现有成员；不会自动收编同名账号，请走显式迁移流程")
            existing_decision = connection.execute(
                "SELECT request_id FROM membership_applications "
                "WHERE requested_username = ? AND status = 'approved' AND request_id != ?",
                (snapshot["username"], request_id),
            ).fetchone()
            if existing_decision is not None:
                raise Conflict("该用户名已有已批准申请；请先核对历史记录")
            member_id = member_store.create_member(
                connection,
                status="provisioning",
                display_name=snapshot["display_name"],
                nickname=snapshot.get("nickname", ""),
                private_contact=snapshot.get("contact", ""),
                public_contact=(
                    snapshot.get("contact", "") if snapshot.get("public_contact") else ""
                ),
                valid_until=valid_until,
                now=now,
            )
            operation_id, _created = member_store.create_operation(
                connection,
                action="provision_member",
                frozen_input=frozen_input,
                member_id=member_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                now=now,
            )
            for key in snapshot["public_keys"]:
                member_store.add_key(
                    connection,
                    member_id,
                    public_key=key["public_key"],
                    fingerprint=key["fingerprint"],
                    now=now,
                )
            member_store.record_application_decision(
                connection,
                request_id=request_id,
                version=version,
                snapshot=snapshot,
                status="approved",
                requested_username=snapshot["username"],
                display_name=snapshot["display_name"],
                reviewer_uid=reviewer_uid,
                review_note=review_note,
                internal_note=internal_note,
                operation_id=operation_id,
                now=now,
            )
            broker_store.record_event(
                connection,
                actor_uid=reviewer_uid,
                action="membership_request_approved",
                detail={"request_id": request_id, "operation_id": operation_id},
                now=now,
            )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise

    return _execute_operation(
        connection,
        operation_id,
        provisioning=provisioning,
        intake_db=intake_db,
        now=now,
    )


def reject_request(
    connection: sqlite3.Connection,
    *,
    intake_db: str,
    request_id: str,
    reviewer_uid: int,
    review_note: str,
    internal_note: str = "",
    expected_version: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reject one application: no account, no key, no member, no quota (AC-15)."""
    intake = intake_store.connect(intake_db)
    try:
        request = intake_store.get_request(intake, request_id)
        if request is None:
            raise NotFound(f"request not found: {request_id}")
        if expected_version and str(request["version"]) != expected_version:
            raise Conflict("申请内容在预览后已变化；请刷新后重新审核")
        version = str(request["version"])
        snapshot = _request_snapshot(request)
        if request["status"] not in ("pending_review", "needs_info"):
            decision = member_store.get_application_decision(connection, request_id)
            if decision is not None and decision["status"] == "rejected":
                _project_decision(intake_db, request_id, "rejected", review_note)
                return {"request_id": request_id, "status": "rejected"}
            raise Conflict(f"申请当前状态为 {request['status']}，已被其他管理员处理")
    finally:
        intake.close()

    own = _begin(connection)
    try:
        decision = member_store.get_application_decision(connection, request_id)
        if decision is not None and decision["status"] == "approved":
            raise Conflict("申请已批准，不能再拒绝")
        if decision is not None and decision["status"] == "rejected":
            if own:
                connection.rollback()
            _project_decision(intake_db, request_id, "rejected", review_note)
            return {"request_id": request_id, "status": "rejected"}
        member_store.record_application_decision(
            connection,
            request_id=request_id,
            version=version,
            snapshot=snapshot,
            status="rejected",
            requested_username=snapshot["username"],
            display_name=snapshot["display_name"],
            reviewer_uid=reviewer_uid,
            review_note=review_note,
            internal_note=internal_note,
            operation_id=None,
            now=now,
        )
        broker_store.record_event(
            connection,
            actor_uid=reviewer_uid,
            action="membership_request_rejected",
            detail={"request_id": request_id, "version": version},
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    _project_decision(intake_db, request_id, "rejected", review_note)
    return {"request_id": request_id, "status": "rejected"}


def request_info(
    connection: sqlite3.Connection,
    *,
    intake_db: str,
    request_id: str,
    reviewer_uid: int,
    public_note: str,
    internal_note: str = "",
    expected_version: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a needs_info decision and project it to the applicant."""
    if not public_note.strip():
        raise Conflict("要求补充材料时必须给出申请人可见的说明")
    intake = intake_store.connect(intake_db)
    try:
        request = intake_store.get_request(intake, request_id)
        if request is None:
            raise NotFound(f"request not found: {request_id}")
        if expected_version and str(request["version"]) != expected_version:
            raise Conflict("申请内容在预览后已变化；请刷新后重新审核")
        if request["status"] not in ("pending_review", "needs_info"):
            raise Conflict(f"申请当前状态为 {request['status']}，不可要求补充材料")
        version = str(request["version"])
        snapshot = _request_snapshot(request)
    finally:
        intake.close()
    own = _begin(connection)
    try:
        decision = member_store.get_application_decision(connection, request_id)
        if decision is not None and decision["status"] in ("approved", "rejected", "cancelled"):
            raise Conflict("申请已有最终决定，不能改为补充材料")
        member_store.record_application_decision(
            connection,
            request_id=request_id,
            version=version,
            snapshot=snapshot,
            status="needs_info",
            requested_username=snapshot["username"],
            display_name=snapshot["display_name"],
            reviewer_uid=reviewer_uid,
            review_note=public_note,
            internal_note=internal_note,
            operation_id=None,
            now=now,
        )
        broker_store.record_event(
            connection,
            actor_uid=reviewer_uid,
            action="membership_request_needs_info",
            detail={"request_id": request_id, "version": version},
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    intake = intake_store.connect(intake_db)
    try:
        intake_store.set_needs_info(intake, request_id, public_note=public_note)
    finally:
        intake.close()
    return {"request_id": request_id, "status": "needs_info", "version": version}


def _project_decision(
    intake_db: str,
    request_id: str,
    status: str,
    public_note: str,
) -> bool:
    if not request_id:
        return False
    intake = intake_store.connect(intake_db)
    try:
        intake_store.set_decision(intake, request_id, status=status, public_note=public_note)
        return True
    except Exception:
        return False
    finally:
        intake.close()


def _execute_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    provisioning: ProvisioningClient,
    intake_db: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one persisted operation, converging state only after real success."""
    operation = member_store.get_operation(connection, operation_id)
    action = str(operation["action"])
    member_id = str(operation["member_id"] or "")
    frozen = operation["frozen_input"]
    if operation["status"] == "succeeded":
        return {
            "operation_id": operation_id,
            "status": "succeeded",
            "member_id": member_id,
            "stages": operation["stage"].split(",") if operation["stage"] else [],
        }

    own = _begin(connection)
    try:
        member_store.update_operation(
            connection, operation_id, status="running", stage="submit", now=now
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    if action == "set_keys":
        row = connection.execute(
            "SELECT status, access_version FROM members WHERE member_id = ?",
            (member_id,),
        ).fetchone()
        expected_version = int(operation.get("expected_access_version") or 0)
        if row is None or str(row["status"]) != "active":
            current = "missing" if row is None else str(row["status"])
            _fail_operation(
                connection,
                operation,
                f"member is {current}; refusing to install active login keys",
                now=now,
            )
            return {
                "operation_id": operation_id,
                "status": "failed",
                "stage": "member_not_active",
                "error": f"member is {current}",
                "member_id": member_id,
            }
        if int(row["access_version"]) != expected_version:
            _fail_operation(
                connection,
                operation,
                "member changed while preparing the key projection",
                now=now,
            )
            return {
                "operation_id": operation_id,
                "status": "failed",
                "stage": "stale_key_operation",
                "error": "member access version changed",
                "member_id": member_id,
            }
    try:
        result = provisioning.execute(action, frozen, operation_id=operation_id)
    except ProvisioningError as exc:
        _fail_operation(connection, operation, str(exc), now=now)
        return {
            "operation_id": operation_id,
            "status": "failed",
            "stage": str(exc),
            "error": str(exc),
            "member_id": member_id,
        }
    except Exception as exc:  # unexpected transport/programming failure
        _fail_operation(connection, operation, f"{type(exc).__name__}: {exc}", now=now)
        return {
            "operation_id": operation_id,
            "status": "failed",
            "stage": "internal",
            "error": f"{type(exc).__name__}",
            "member_id": member_id,
        }

    result_stages = [str(stage) for stage in result.get("stages", [])]
    if action == "set_keys":
        try:
            return _complete_key_projection(
                connection,
                operation,
                result_stages,
                now=now,
            )
        except Exception as exc:
            logger.exception("could not commit key projection for %s", member_id)
            stage = (
                "stale_key_operation" if "member changed" in str(exc) else "key_projection_commit"
            )
            _fail_operation(connection, operation, f"{stage}: {exc}", now=now)
            return {
                "operation_id": operation_id,
                "status": "failed",
                "stage": stage,
                "error": str(exc),
                "member_id": member_id,
            }
    if action == "provision_member":
        try:
            return _activate_provisioned_member(
                connection,
                operation,
                result,
                result_stages,
                intake_db=intake_db,
                now=now,
            )
        except Exception as exc:
            logger.exception("could not activate provisioned member %s", member_id)
            _fail_operation(connection, operation, f"activation: {exc}", now=now)
            return {
                "operation_id": operation_id,
                "status": "failed",
                "stage": "activation",
                "error": str(exc),
                "member_id": member_id,
            }

    own = _begin(connection)
    try:
        member_store.update_operation(
            connection,
            operation_id,
            status="succeeded",
            stage=",".join(result_stages),
            now=now,
        )
        broker_store.record_event(
            connection,
            actor_uid=None,
            action=f"membership_{action}_succeeded",
            detail={"operation_id": operation_id, "member_id": member_id},
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    return {
        "operation_id": operation_id,
        "status": "succeeded",
        "member_id": member_id,
        "stages": result_stages,
    }


def _complete_key_projection(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    stages: list[str],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Commit a host-verified key change in one short SQLite transaction."""
    from .models import parse_public_key

    operation_id = str(operation["operation_id"])
    member_id = str(operation["member_id"] or "")
    frozen = operation["frozen_input"]
    keys: list[dict[str, str]] = []
    for raw in frozen.get("public_keys", []):
        normalized, fingerprint = parse_public_key(str(raw))
        keys.append({"public_key": normalized, "fingerprint": fingerprint})

    own = _begin(connection)
    try:
        row = connection.execute(
            "SELECT status, access_version FROM members WHERE member_id = ?",
            (member_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"member not found: {member_id}")
        if str(row["status"]) != "active":
            raise Conflict(f"member {member_id} is {row['status']}, not active")
        expected_version = int(operation.get("expected_access_version") or 0)
        if int(row["access_version"]) != expected_version:
            raise Conflict("member changed while the key projection was running")
        member_store.set_member_keys(connection, member_id, keys, now=now)
        member_store.update_operation(
            connection,
            operation_id,
            status="succeeded",
            stage=",".join(stages),
            now=now,
        )
        broker_store.record_event(
            connection,
            actor_uid=None,
            action="membership_set_keys_succeeded",
            detail={
                "operation_id": operation_id,
                "member_id": member_id,
                "key_count": len(keys),
            },
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    return {
        "operation_id": operation_id,
        "status": "succeeded",
        "member_id": member_id,
        "stages": stages,
    }


def _activate_provisioned_member(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    result: dict[str, Any],
    stages: list[str],
    *,
    intake_db: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    operation_id = str(operation["operation_id"])
    member_id = str(operation["member_id"] or "")
    frozen = operation["frozen_input"]
    username = str(frozen.get("username", ""))
    uid = result.get("uid")
    if uid is None:
        uid = _uid_of_member(connection, member_id, username=username)

    own = _begin(connection)
    try:
        row = connection.execute(
            "SELECT status FROM members WHERE member_id = ?", (member_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"member not found: {member_id}")
        status = str(row["status"])
        if status in ("revoked", "suspended", "expired"):
            raise Conflict(
                f"member {member_id} cannot transition {status} -> active; "
                "late provisioning must not re-enable access"
            )
        connection.execute(
            "UPDATE members SET linux_uid = ?, linux_username = ?, updated_at = ? "
            "WHERE member_id = ?",
            (uid, username, (now or datetime.now(UTC)).isoformat(), member_id),
        )
        if status == "provision_failed":
            member_store.transition_member(
                connection,
                member_id,
                target="provisioning",
                reason="provisioning retry",
                actor_uid=None,
                now=now,
            )
            status = "provisioning"
        if status == "provisioning":
            member_store.transition_member(
                connection,
                member_id,
                target="active",
                reason="provisioning complete",
                actor_uid=None,
                now=now,
            )
        member_store.update_operation(
            connection,
            operation_id,
            status="succeeded",
            stage=",".join(stages),
            now=now,
        )
        broker_store.record_event(
            connection,
            actor_uid=None,
            action="membership_member_activated",
            detail={
                "operation_id": operation_id,
                "member_id": member_id,
                "linux_uid": uid,
                "username": username,
            },
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise

    projection = "done"
    request_id = str(operation.get("request_id") or "")
    if request_id and not _project_decision(intake_db, request_id, "approved", "已开通"):
        projection = "pending_retry"
        try:
            broker_store.record_event(
                connection,
                actor_uid=None,
                action="membership_intake_projection_failed",
                detail={"request_id": request_id, "operation_id": operation_id},
                now=now,
            )
            if own:
                connection.commit()
        except Exception:
            logger.exception("could not record intake projection failure for %s", request_id)
    return {
        "operation_id": operation_id,
        "status": "succeeded",
        "member_id": member_id,
        "stages": stages,
        "projection": projection,
    }


def _fail_operation(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    error: str,
    *,
    now: datetime | None = None,
) -> None:
    operation_id = str(operation["operation_id"])
    action = str(operation["action"])
    member_id = str(operation["member_id"] or "")
    own = _begin(connection)
    try:
        member_store.update_operation(
            connection,
            operation_id,
            status="failed",
            stage=str(error).split("stage=")[-1].split(":")[0] or "unknown",
            error=error,
            now=now,
        )
        if action == "provision_member" and member_id:
            row = connection.execute(
                "SELECT status FROM members WHERE member_id = ?", (member_id,)
            ).fetchone()
            if row is not None and str(row["status"]) == "provisioning":
                member_store.transition_member(
                    connection,
                    member_id,
                    target="provision_failed",
                    reason=error,
                    actor_uid=None,
                    now=now,
                )
        broker_store.record_event(
            connection,
            actor_uid=None,
            action=f"membership_{action}_failed",
            detail={"operation_id": operation_id, "member_id": member_id, "error": error},
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise


def _uid_of_member(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    username: str = "",
) -> int:
    """Resolve the provisioned account's UID from the real system state."""
    try:
        import pwd
    except ImportError as exc:  # Windows / non-POSIX hosts cannot provision members
        raise NotFound("cannot resolve a Linux account UID on this host") from exc

    if not username:
        row = connection.execute(
            "SELECT linux_username FROM members WHERE member_id = ?", (member_id,)
        ).fetchone()
        username = str(row["linux_username"] or "") if row else ""
    try:
        return pwd.getpwnam(username).pw_uid
    except KeyError as exc:
        raise NotFound(f"provisioned account not found: {username}") from exc


def retry_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    provisioning: ProvisioningClient,
    intake_db: str,
    actor_uid: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    operation = member_store.get_operation(connection, operation_id)
    member_id = str(operation.get("member_id") or "")
    if not member_id:
        return _retry_operation_locked(
            connection,
            operation_id,
            provisioning=provisioning,
            intake_db=intake_db,
            actor_uid=actor_uid,
            now=now,
        )
    with _member_lock(member_id):
        return _retry_operation_locked(
            connection,
            operation_id,
            provisioning=provisioning,
            intake_db=intake_db,
            actor_uid=actor_uid,
            now=now,
        )


def _retry_operation_locked(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    provisioning: ProvisioningClient,
    intake_db: str,
    actor_uid: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Retry a failed/interrupted operation or replay a projection (§7.3, AC-14)."""
    operation = member_store.get_operation(connection, operation_id)
    if operation["status"] == "succeeded":
        request_id = str(operation.get("request_id") or "")
        if request_id and operation["action"] == "provision_member":
            projection = _project_decision(intake_db, request_id, "approved", "已开通")
            return {
                "operation_id": operation_id,
                "status": "succeeded",
                "member_id": operation.get("member_id"),
                "projection": "done" if projection else "pending_retry",
            }
        raise Conflict(f"操作当前状态 {operation['status']} 不可重试")
    if operation["status"] not in ("failed", "pending", "running"):
        raise Conflict(f"操作当前状态 {operation['status']} 不可重试")
    member_id = str(operation.get("member_id") or "")
    if operation["action"] == "provision_member" and member_id:
        member = member_store.get_member(connection, member_id)
        status = member["member"]["status"]
        if status not in ("provisioning", "provision_failed", "active"):
            raise Conflict(
                f"member {member_id} cannot transition {status} -> active; "
                "late provisioning must not re-enable access"
            )
    broker_store.record_event(
        connection,
        actor_uid=actor_uid,
        action="membership_operation_retry",
        detail={"operation_id": operation_id, "attempts": operation["attempts"]},
        now=now,
    )
    connection.commit()
    return _execute_operation(
        connection,
        operation_id,
        provisioning=provisioning,
        intake_db=intake_db,
        now=now,
    )


def recover_stale_operations(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_OPERATION_SECONDS,
) -> list[str]:
    """Fail closed on operations interrupted by a crash/restart.

    A ``running`` row is never treated as successful. It becomes failed and
    retryable; a member still in provisioning becomes provision_failed.
    """
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(seconds=stale_after_seconds)
    rows = connection.execute(
        "SELECT * FROM member_operations WHERE status = 'running' ORDER BY updated_at"
    ).fetchall()
    failed: list[str] = []
    for row in rows:
        try:
            updated = datetime.fromisoformat(str(row["updated_at"]))
        except (TypeError, ValueError):
            updated = current - timedelta(seconds=stale_after_seconds + 1)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        if updated > cutoff:
            continue
        operation = member_store.get_operation(connection, str(row["operation_id"]))
        _fail_operation(
            connection,
            operation,
            "interrupted: operation was still running after restart",
            now=current,
        )
        failed.append(str(row["operation_id"]))
    return failed


def reconcile_restored_operations(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Mark every non-terminal operation from a restored backup retryable.

    A restore is a point-in-time rollback: an operation that was ``pending`` or
    ``running`` in the snapshot has no live executor waiting for it.  It is
    failed closed (and a member still in ``provisioning`` moves to
    ``provision_failed``) so an administrator must explicitly retry it instead
    of the broker assuming an unobserved result.
    """

    rows = connection.execute(
        "SELECT operation_id FROM member_operations "
        "WHERE status IN ('pending', 'running') ORDER BY created_at, operation_id"
    ).fetchall()
    reconciled: list[dict[str, str]] = []
    for row in rows:
        operation = member_store.get_operation(connection, str(row["operation_id"]))
        previous = str(operation["status"])
        _fail_operation(
            connection,
            operation,
            "stage=restore: operation was not terminal when the backup was restored; "
            "retry explicitly",
            now=now,
        )
        reconciled.append(
            {
                "operation_id": str(operation["operation_id"]),
                "action": str(operation["action"]),
                "previous_status": previous,
                "status": "failed",
            }
        )
    return reconciled


def _disable_operation(
    connection: sqlite3.Connection,
    *,
    member_id: str,
    username: str,
    access_version: int,
    reason: str,
    provisioning: ProvisioningClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    operation_id, _created = member_store.create_operation(
        connection,
        action="disable_member",
        frozen_input={"username": username, "reason": reason},
        member_id=member_id,
        idempotency_key=f"disable:{member_id}:{access_version}",
        now=now,
    )
    connection.commit()
    return _execute_operation(
        connection,
        operation_id,
        provisioning=provisioning,
        intake_db="",
        now=now,
    )


def suspend_member(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    actor_uid: int,
    reason: str,
    provisioning: ProvisioningClient,
    on_member_change: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    with _member_lock(member_id):
        return _suspend_member_locked(
            connection,
            member_id,
            actor_uid=actor_uid,
            reason=reason,
            provisioning=provisioning,
            on_member_change=on_member_change,
            now=now,
        )


def _suspend_member_locked(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    actor_uid: int,
    reason: str,
    provisioning: ProvisioningClient,
    on_member_change: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Suspend interactive access; running jobs keep their lease (§7.4)."""
    result = member_store.transition_member(
        connection,
        member_id,
        target="suspended",
        reason=reason,
        actor_uid=actor_uid,
        now=now,
    )
    # Close this member's already-authenticated broker streams before the
    # slower OS credential sync, so queued/tunneling work cannot continue.
    if on_member_change is not None:
        try:
            on_member_change(member_id)
        except Exception:
            broker_store.record_event(
                connection,
                actor_uid=actor_uid,
                action="membership_connection_close_failed",
                detail={"member_id": member_id},
                now=now,
            )
            connection.commit()
    member = member_store.get_member(connection, member_id)
    username = str(member["member"]["linux_username"] or "")
    if username:
        operation = _disable_operation(
            connection,
            member_id=member_id,
            username=username,
            access_version=int(result["access_version"]),
            reason=reason,
            provisioning=provisioning,
            now=now,
        )
        result["operation_id"] = operation["operation_id"]
        result["credential_revocation"] = (
            "done" if operation["status"] == "succeeded" else "pending_retry"
        )
    else:
        result["credential_revocation"] = "not_applicable"
    return result


def resume_member(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    actor_uid: int,
    reason: str,
    provisioning: ProvisioningClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    with _member_lock(member_id):
        return _resume_member_locked(
            connection,
            member_id,
            actor_uid=actor_uid,
            reason=reason,
            provisioning=provisioning,
            now=now,
        )


def _resume_member_locked(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    actor_uid: int,
    reason: str,
    provisioning: ProvisioningClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resume a suspended member: unlock and verify credentials first (§7.4)."""
    member = member_store.get_member(connection, member_id)
    status = member["member"]["status"]
    if status not in ("suspended", "expired"):
        raise Conflict(f"member {member_id} cannot resume from status {status}")
    username = str(member["member"]["linux_username"] or "")
    if not username:
        raise Conflict("该成员尚未完成开户，不能恢复；请先重试开户操作")
    # get_member() intentionally exposes only key metadata (fingerprint/status).
    # Use the internal full-key view so resume can reinstall the active keyset.
    keys = [
        str(key["public_key"])
        for key in member_store.list_keys(connection, member_id)
        if key["status"] == "active"
    ]
    if not keys:
        raise Conflict("该成员没有有效公钥；恢复前请先安装登录密钥")
    operation_id, _created = member_store.create_operation(
        connection,
        action="enable_member",
        frozen_input={"username": username, "public_keys": keys},
        member_id=member_id,
        idempotency_key=(f"enable:{member_id}:{member['member']['access_version']}"),
        now=now,
    )
    connection.commit()
    operation = _execute_operation(
        connection,
        operation_id,
        provisioning=provisioning,
        intake_db="",
        now=now,
    )
    if operation["status"] != "succeeded":
        operation["credential_revocation"] = "pending_retry"
        return operation
    result = member_store.transition_member(
        connection,
        member_id,
        target="active",
        reason=reason,
        actor_uid=actor_uid,
        now=now,
    )
    result["operation_id"] = operation_id
    result["credential_revocation"] = "done"
    return result


def revoke_member(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    actor_uid: int,
    reason: str,
    provisioning: ProvisioningClient,
    on_member_change: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    with _member_lock(member_id):
        return _revoke_member_locked(
            connection,
            member_id,
            actor_uid=actor_uid,
            reason=reason,
            provisioning=provisioning,
            on_member_change=on_member_change,
            now=now,
        )


def _revoke_member_locked(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    actor_uid: int,
    reason: str,
    provisioning: ProvisioningClient,
    on_member_change: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Revoke membership; workspace data is KEPT (§7.4, INV-07).

    Revocation is terminal but the administrative command is retryable: if a
    previous attempt already moved the member to ``revoked`` but failed while
    removing host credentials, calling it again finishes the cleanup instead
    of failing with "cannot transition revoked -> revoked".
    """
    existing = member_store.get_member(connection, member_id)["member"]
    if existing["status"] == "revoked":
        result = {
            "member_id": member_id,
            "status": "revoked",
            "access_version": int(existing["access_version"]),
            "valid_until": existing["valid_until"],
        }
    else:
        result = member_store.transition_member(
            connection,
            member_id,
            target="revoked",
            reason=reason,
            actor_uid=actor_uid,
            now=now,
        )
    if on_member_change is not None:
        try:
            on_member_change(member_id)
        except Exception:
            broker_store.record_event(
                connection,
                actor_uid=actor_uid,
                action="membership_connection_close_failed",
                detail={"member_id": member_id},
                now=now,
            )
            connection.commit()
    member = member_store.get_member(connection, member_id)
    username = str(member["member"]["linux_username"] or "")
    member_store.revoke_all_keys(
        connection,
        member_id,
        actor_uid=actor_uid,
        reason=reason,
        now=now,
    )
    connection.commit()
    if username:
        current = member_store.get_member(connection, member_id)["member"]
        operation = _disable_operation(
            connection,
            member_id=member_id,
            username=username,
            access_version=int(current["access_version"]),
            reason=reason,
            provisioning=provisioning,
            now=now,
        )
        result["operation_id"] = operation["operation_id"]
        result["credential_revocation"] = (
            "done" if operation["status"] == "succeeded" else "pending_retry"
        )
    else:
        result["credential_revocation"] = "not_applicable"
    return result


def _key_projection_failure(operation: dict[str, Any]) -> ProvisioningError:
    return ProvisioningError(
        f"stage={operation.get('stage', '')}: {operation.get('error', 'key projection failed')}"
    )


def add_member_key(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    public_key: str,
    actor_uid: int,
    provisioning: ProvisioningClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add a login key without holding a SQLite write lock during host I/O.

    Active members use a persisted ``set_keys`` operation: the prospective
    keyset is frozen and committed, the executor installs it, and only then a
    short compare-and-set transaction applies it to the ledger.  Suspended and
    not-yet-active members store the key for the next enable/activation.
    """
    from .models import parse_public_key

    normalized, fingerprint = parse_public_key(public_key)
    with _member_lock(member_id):
        member = member_store.get_member(connection, member_id)["member"]
        status = str(member["status"])
        if status == "revoked":
            raise Conflict("成员已撤销，不能再添加登录公钥")
        duplicate = connection.execute(
            "SELECT member_id FROM member_keys WHERE fingerprint = ? AND status = 'active'",
            (fingerprint,),
        ).fetchone()
        if duplicate is not None:
            raise Conflict("该公钥已登记；无需重复添加")
        if status != "active":
            own = _begin(connection)
            try:
                key_id = member_store.add_key(
                    connection,
                    member_id,
                    public_key=normalized,
                    fingerprint=fingerprint,
                    now=now,
                )
                if own:
                    connection.commit()
            except Exception:
                if own:
                    connection.rollback()
                raise
            return {"key_id": key_id, "fingerprint": fingerprint, "member_status": status}

        username = str(member["linux_username"] or "")
        if not username:
            raise Conflict("成员尚未完成开户，不能同步登录公钥")
        active_keys = [
            key["public_key"]
            for key in member_store.list_keys(connection, member_id)
            if key["status"] == "active"
        ]
        target_keys = [*active_keys, normalized]
        own = _begin(connection)
        try:
            operation_id, _created = member_store.create_operation(
                connection,
                action="set_keys",
                frozen_input={"username": username, "public_keys": target_keys},
                member_id=member_id,
                expected_access_version=int(member["access_version"]),
                now=now,
            )
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        operation = _execute_operation(
            connection,
            operation_id,
            provisioning=provisioning,
            intake_db="",
            now=now,
        )
        if operation["status"] != "succeeded":
            raise _key_projection_failure(operation)
        row = connection.execute(
            "SELECT key_id FROM member_keys "
            "WHERE member_id = ? AND fingerprint = ? AND status = 'active'",
            (member_id, fingerprint),
        ).fetchone()
        if row is None:
            raise Conflict("公钥已写入宿主机，但数据库投影未完成；请重试该操作")
        return {
            "key_id": str(row["key_id"]),
            "fingerprint": fingerprint,
            "member_status": status,
            "operation_id": operation_id,
        }


def revoke_member_key(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    key_id: str,
    actor_uid: int,
    provisioning: ProvisioningClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Revoke a login key with a short projection transaction (see add)."""
    with _member_lock(member_id):
        member = member_store.get_member(connection, member_id)["member"]
        status = str(member["status"])
        row = connection.execute(
            "SELECT * FROM member_keys WHERE key_id = ? AND member_id = ?",
            (key_id, member_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"key not found: {key_id}")
        if str(row["status"]) != "active":
            raise Conflict(f"key {key_id} is not active")
        active_keys = [
            key
            for key in member_store.list_keys(connection, member_id)
            if key["status"] == "active"
        ]
        if not any(key["key_id"] == key_id for key in active_keys):
            raise Conflict(f"key {key_id} is not active")
        remaining = [key["public_key"] for key in active_keys if key["key_id"] != key_id]
        if not remaining:
            raise Conflict(
                "这是最后一个有效公钥；移除后你将无法登录。请先添加新密钥，或联系管理员走恢复流程"
            )
        if status != "active":
            own = _begin(connection)
            try:
                member_store.revoke_key(
                    connection,
                    member_id,
                    key_id=key_id,
                    actor_uid=actor_uid,
                    now=now,
                )
                if own:
                    connection.commit()
            except Exception:
                if own:
                    connection.rollback()
                raise
            return {"key_id": key_id, "revoked": True, "member_status": status}

        username = str(member["linux_username"] or "")
        if not username:
            raise Conflict("成员尚未完成开户，不能同步登录公钥")
        own = _begin(connection)
        try:
            operation_id, _created = member_store.create_operation(
                connection,
                action="set_keys",
                frozen_input={"username": username, "public_keys": remaining},
                member_id=member_id,
                expected_access_version=int(member["access_version"]),
                now=now,
            )
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        operation = _execute_operation(
            connection,
            operation_id,
            provisioning=provisioning,
            intake_db="",
            now=now,
        )
        if operation["status"] != "succeeded":
            raise _key_projection_failure(operation)
        return {
            "key_id": key_id,
            "revoked": True,
            "member_status": status,
            "operation_id": operation_id,
        }


def import_legacy_member(
    connection: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    valid_until: str,
    quota_gb: int,
    public_keys: list[str],
    actor_uid: int,
    provisioning: ProvisioningClient,
    workspace_root: str,
    dry_run: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explicit, manifest-driven adoption of a pre-existing Linux account.

    Normal approvals fail closed if a same-name account already exists. This
    path is the ONLY way to adopt one, and it requires an administrator to
    provide a reviewed entry that names the exact account, keys, validity, and
    quota. Dry-run always inspects first and reports conflicts; apply refuses
    to proceed while any hard conflict remains. Nothing is ever adopted just
    because the name happens to exist.
    """
    from . import models as member_models

    username = username.strip().lower()
    if not member_models.USERNAME_RE.fullmatch(username):
        raise Conflict("legacy import username is invalid")
    normalized_keys: list[str] = []
    fingerprints: list[str] = []
    for raw in public_keys:
        try:
            key, fingerprint = member_models.parse_public_key(raw)
        except ValueError as exc:
            raise Conflict(f"legacy import public key is invalid: {exc}") from exc
        if key not in normalized_keys:
            normalized_keys.append(key)
            fingerprints.append(fingerprint)
    if not normalized_keys:
        raise Conflict("legacy import requires at least one reviewed public key")
    if len(normalized_keys) > 10:
        raise Conflict("legacy import accepts at most 10 public keys")
    if not 1 <= int(quota_gb) <= 10000:
        raise Conflict("legacy import quota_gb is out of range")
    if valid_until:
        try:
            parsed = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Conflict("legacy import valid_until is not ISO-8601") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if parsed <= (now or datetime.now(UTC)):
            raise Conflict("legacy import valid_until must be in the future")

    conflicts: list[str] = []
    warnings: list[str] = []
    existing_member = member_store.get_member_by_username(connection, username)
    if existing_member is not None:
        conflicts.append(
            f"member_record_exists:{existing_member.member_id}:{existing_member.status}"
        )
    try:
        approved = connection.execute(
            "SELECT request_id FROM membership_applications "
            "WHERE requested_username = ? AND status = 'approved'",
            (username,),
        ).fetchone()
        if approved is not None:
            conflicts.append(f"approved_application_exists:{approved['request_id']}")
    except sqlite3.Error:
        # Older/partial schema: the unique index still protects new approvals.
        warnings.append("application_conflict_check_unavailable")

    try:
        inspection = provisioning.execute("inspect_member", {"username": username})
    except ProvisioningError as exc:
        inspection = {"exists": None, "error": str(exc)}
        warnings.append(f"host_inspection_failed:{exc}")
    except Exception as exc:
        inspection = {"exists": None, "error": f"{type(exc).__name__}: {exc}"}
        warnings.append("host_inspection_failed:internal")

    is_dry_run = bool(dry_run)
    if inspection.get("exists") is False:
        conflicts.append("account_absent")
    uid = inspection.get("uid")
    if inspection.get("exists"):
        if not isinstance(uid, int) or uid < 1000:
            conflicts.append("system_or_root_account")
        else:
            occupied = member_store.get_member_by_uid(connection, uid)
            if occupied is not None and occupied.member_id != (
                existing_member.member_id if existing_member else ""
            ):
                conflicts.append(f"uid_already_bound:{occupied.member_id}")
        groups = set(inspection.get("groups") or [])
        privileged = groups & {
            "sudo",
            "wheel",
            "docker",
            "citespec-gpu",
            "video",
            "render",
            "citespec-admins",
            "citespec-portal",
            "root",
        }
        for group in sorted(privileged):
            conflicts.append(f"privileged_group:{group}")
        if inspection.get("home_is_symlink"):
            conflicts.append("home_is_symlink")
        if not inspection.get("home_exists"):
            conflicts.append("home_absent")
        if not inspection.get("managed"):
            warnings.append("account_will_be_converted_to_managed_member")
        if inspection.get("home_authorized_keys"):
            warnings.append("legacy_home_authorized_keys_will_be_scrubbed")
        if inspection.get("central_key_count"):
            warnings.append("central_keys_will_be_replaced")

    report: dict[str, Any] = {
        "username": username,
        "display_name": display_name,
        "valid_until": valid_until,
        "quota_gb": int(quota_gb),
        "key_fingerprints": fingerprints,
        "inspection": inspection,
        "conflicts": conflicts,
        "warnings": warnings,
        "dry_run": is_dry_run,
    }
    if is_dry_run:
        report["would_adopt_existing"] = bool(inspection.get("exists"))
        return report
    if conflicts:
        raise Conflict("legacy import refused due to conflicts: " + ", ".join(conflicts))
    if inspection.get("exists") is None:
        raise Conflict(
            "legacy import refused: host inspection failed; run the dry run on "
            "the target host with the provisioning executor available"
        )

    own = _begin(connection)
    try:
        if not isinstance(uid, int) or uid < 1000:
            raise Conflict("legacy import refused: inspected uid is invalid")
        member_id = member_store.create_member(
            connection,
            linux_uid=uid,
            linux_username=username,
            status="provisioning",
            valid_until=valid_until,
            display_name=display_name or username,
            now=now,
        )
        for key, fingerprint in zip(normalized_keys, fingerprints, strict=True):
            member_store.add_key(
                connection,
                member_id,
                public_key=key,
                fingerprint=fingerprint,
                now=now,
            )
        operation_id, _created = member_store.create_operation(
            connection,
            action="provision_member",
            frozen_input={
                "username": username,
                "display_name": display_name or username,
                "public_keys": normalized_keys,
                "workspace_root": workspace_root,
                "quota_gb": int(quota_gb),
                "valid_until": valid_until,
                "allow_existing": True,
            },
            member_id=member_id,
            request_id=None,
            idempotency_key=f"legacy-import:{username}:{uid}",
            now=now,
        )
        broker_store.record_event(
            connection,
            actor_uid=actor_uid,
            action="legacy_member_import_started",
            detail={
                "member_id": member_id,
                "operation_id": operation_id,
                "username": username,
                "uid": uid,
            },
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    operation = _execute_operation(
        connection,
        operation_id,
        provisioning=provisioning,
        intake_db="",
        now=now,
    )
    report.update(
        {
            "member_id": member_id,
            "operation_id": operation_id,
            "operation_status": operation["status"],
            "operation_error": operation.get("error", ""),
        }
    )
    return report
