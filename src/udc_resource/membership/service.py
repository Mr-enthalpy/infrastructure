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
import sqlite3
from datetime import datetime
from typing import Any

from .. import store as broker_store
from ..enrollment import store as intake_store
from ..errors import Conflict, NotFound
from . import store as member_store
from .provisioning import ProvisioningClient, ProvisioningError


def _digest(snapshot: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def list_requests(intake_db: str) -> list[dict[str, Any]]:
    """Broker pulls applications from the intake store (§9.2, pull-only)."""
    intake = intake_store.connect(intake_db)
    try:
        return intake_store.list_requests(intake)
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
    workspace_root: str = "/data1/udc-users",
    provisioning: ProvisioningClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Approve one application and provision the member (§7.2, §8.1).

    Concurrency/idempotency (§7.3): the frozen decision record is created
    under BEGIN IMMEDIATE keyed by request_id; a second approver gets a
    conflict; a retried approval reuses the same member and operation.
    """
    intake = intake_store.connect(intake_db)
    try:
        request = intake_store.get_request(intake, request_id)
        if request is None:
            raise NotFound(f"request not found: {request_id}")
        # Convergence first (§7.3): a retried/concurrent approval returns the
        # original member and operation instead of conflicting on the intake
        # projection, which the first approval already moved to approved.
        decision_check = member_store.get_application_decision(connection, request_id)
        if decision_check is not None and decision_check["status"] == "approved":
            operation = (
                member_store.get_operation(connection, str(decision_check["operation_id"] or ""))
                if decision_check.get("operation_id")
                else None
            )
            if operation is not None:
                return {
                    "request_id": request_id,
                    "status": "approved",
                    "member_id": operation.get("member_id"),
                    "operation_id": operation["operation_id"],
                    "operation_status": operation["status"],
                }
            raise Conflict("申请已批准但缺少开户操作记录；请联系管理员检查")
        if request["status"] not in ("pending_review", "needs_info"):
            raise Conflict(f"申请当前状态为 {request['status']}，已被其他管理员处理或不可批准")
        snapshot = json.loads(
            json.dumps(
                {
                    "display_name": request["display_name"],
                    "username": request["username"],
                    "affiliation": request["affiliation"],
                    "purpose": request["purpose"],
                    "contact": request["contact"],
                    "nickname": request["nickname"],
                    "public_contact": request["public_contact"],
                    "public_keys": request["keys"],
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        version = _digest(snapshot)
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

    own = not connection.in_transaction
    try:
        if own:
            connection.execute("BEGIN IMMEDIATE")
        member_id = member_store.create_member(
            connection,
            status="provisioning",
            display_name=snapshot["display_name"],
            nickname=snapshot.get("nickname", ""),
            private_contact=snapshot.get("contact", ""),
            public_contact=(snapshot.get("contact", "") if snapshot.get("public_contact") else ""),
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

    return _execute_provision_operation(
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
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reject one application: no account, no key, no member, no quota (AC-15)."""
    intake = intake_store.connect(intake_db)
    try:
        request = intake_store.get_request(intake, request_id)
        if request is None:
            raise NotFound(f"request not found: {request_id}")
        if request["status"] not in ("pending_review", "needs_info"):
            raise Conflict(f"申请当前状态为 {request['status']}，已被其他管理员处理")
        snapshot = {
            "display_name": request["display_name"],
            "username": request["username"],
            "public_keys": request["keys"],
        }
        version = _digest(snapshot)
    finally:
        intake.close()
    try:
        connection.execute("BEGIN IMMEDIATE")
        decision = member_store.get_application_decision(connection, request_id)
        if decision is not None and decision["status"] == "rejected":
            connection.rollback()
            return {"request_id": request_id, "status": "rejected"}
        member_store.record_application_decision(
            connection,
            request_id=request_id,
            version=version,
            snapshot=snapshot,
            status="rejected",
            requested_username=request["username"],
            display_name=request["display_name"],
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
            detail={"request_id": request_id},
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    intake = intake_store.connect(intake_db)
    try:
        intake_store.set_decision(intake, request_id, status="rejected", public_note=review_note)
    finally:
        intake.close()
    return {"request_id": request_id, "status": "rejected"}


def _execute_provision_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    provisioning: ProvisioningClient,
    intake_db: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one provisioning operation and converge all projections (§8.1)."""
    operation = member_store.get_operation(connection, operation_id)
    member_id = str(operation["member_id"] or "")
    frozen = operation["frozen_input"]
    try:
        connection.execute("BEGIN IMMEDIATE")
        member_store.update_operation(
            connection, operation_id, status="running", stage="submit", now=now
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    try:
        result = provisioning.execute(str(operation["action"]), frozen)
    except ProvisioningError as exc:
        member_store.update_operation(
            connection,
            operation_id,
            status="failed",
            stage=str(exc).split("stage=")[-1].split(":")[0] or "unknown",
            error=str(exc),
            now=now,
        )
        connection.commit()
        if member_id:
            member_store.transition_member(
                connection,
                member_id,
                target="provision_failed",
                reason=str(exc),
                actor_uid=None,
                now=now,
            )
        broker_store.record_event(
            connection,
            actor_uid=None,
            action="membership_provision_failed",
            detail={"operation_id": operation_id, "error": str(exc)},
            now=now,
        )
        connection.commit()
        return {
            "operation_id": operation_id,
            "status": "failed",
            "stage": str(exc),
            "member_id": member_id,
        }
    # Re-provisioning re-entry (§6.1): provision_failed -> provisioning -> active.
    row = connection.execute(
        "SELECT status FROM members WHERE member_id = ?", (member_id,)
    ).fetchone()
    if row is not None and str(row["status"]) == "provision_failed":
        member_store.transition_member(
            connection,
            member_id,
            target="provisioning",
            reason="provisioning retry",
            actor_uid=None,
            now=now,
        )
    # Success: keys projection from the authoritative set, member activation.
    member_store.update_operation(
        connection,
        operation_id,
        status="succeeded",
        stage=",".join(result.get("stages", [])),
        now=now,
    )
    connection.commit()
    username = frozen["username"]
    uid = result.get("uid")
    if uid is None:
        uid = _uid_of_member(connection, member_id)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE members SET linux_uid = ?, linux_username = ? WHERE member_id = ?",
        (uid, username, member_id),
    )
    connection.commit()
    member_store.transition_member(
        connection,
        member_id,
        target="active",
        reason="provisioning complete",
        actor_uid=None,
        now=now,
    )
    intake = intake_store.connect(intake_db)
    try:
        intake_store.set_decision(
            intake,
            str(operation["request_id"] or ""),
            status="approved",
            public_note="已开通",
        )
    finally:
        intake.close()
    return {
        "operation_id": operation_id,
        "status": "succeeded",
        "member_id": member_id,
        "stages": result.get("stages", []),
    }


def _uid_of_member(connection: sqlite3.Connection, member_id: str) -> int:
    """Resolve the provisioned account's UID from the real system state."""
    import pwd

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
    """Retry a failed provisioning operation (§7.3, AC-14)."""
    operation = member_store.get_operation(connection, operation_id)
    if operation["status"] not in ("failed", "pending"):
        raise Conflict(f"操作当前状态 {operation['status']} 不可重试")
    broker_store.record_event(
        connection,
        actor_uid=actor_uid,
        action="membership_operation_retry",
        detail={"operation_id": operation_id},
        now=now,
    )
    connection.commit()
    return _execute_provision_operation(
        connection,
        operation_id,
        provisioning=provisioning,
        intake_db=intake_db,
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
    """Suspend interactive access; running jobs keep their lease (§7.4)."""
    result = member_store.transition_member(
        connection,
        member_id,
        target="suspended",
        reason=reason,
        actor_uid=actor_uid,
        now=now,
    )
    member = member_store.get_member(connection, member_id)
    username = str(member["member"]["linux_username"] or "")
    if username:
        try:
            provisioning.execute(
                "disable_member",
                {"username": username, "reason": reason},
            )
        except ProvisioningError as exc:
            # Access is already blocked by the status/version change; the
            # credential sync failure is reported for retry (§8.4).
            broker_store.record_event(
                connection,
                actor_uid=actor_uid,
                action="membership_disable_failed",
                detail={"member_id": member_id, "error": str(exc)},
                now=now,
            )
            connection.commit()
            result["credential_revocation"] = "pending_retry"
    if on_member_change is not None:
        on_member_change(member_id)
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
    """Resume a suspended member: verify the account still matches (§7.4)."""
    member = member_store.get_member(connection, member_id)
    username = str(member["member"]["linux_username"] or "")
    if username:
        # Re-assert the central key projection before reopening access.
        active = [key for key in member["keys"] if key["status"] == "active"]
        key_rows = member_store.list_keys(connection, member_id)
        keys = [key["public_key"] for key in key_rows if key["status"] == "active"]
        provisioning.execute("set_keys", {"username": username, "public_keys": keys})
        if not active and not keys:
            raise Conflict("该成员没有有效公钥；恢复前请先安装登录密钥")
    return member_store.transition_member(
        connection,
        member_id,
        target="active",
        reason=reason,
        actor_uid=actor_uid,
        now=now,
    )


def revoke_member(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    actor_uid: int,
    reason: str,
    provisioning: ProvisioningClient,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Revoke membership; workspace data is KEPT (§7.4, INV-07)."""
    member = member_store.get_member(connection, member_id)
    username = str(member["member"]["linux_username"] or "")
    if username:
        try:
            provisioning.execute(
                "disable_member",
                {"username": username, "reason": reason},
            )
        except ProvisioningError as exc:
            broker_store.record_event(
                connection,
                actor_uid=actor_uid,
                action="membership_disable_failed",
                detail={"member_id": member_id, "error": str(exc)},
                now=now,
            )
            connection.commit()
    return member_store.transition_member(
        connection,
        member_id,
        target="revoked",
        reason=reason,
        actor_uid=actor_uid,
        now=now,
    )
