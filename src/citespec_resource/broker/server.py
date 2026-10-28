"""Asyncio Unix-socket broker with SO_PEERCRED identity and SQLite transactions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from .. import store, version_info
from ..config import LabConfig, validate_active_mode_policy
from ..db import connect, init_db
from ..enrollment import store as intake_store
from ..errors import (
    BadRequest,
    CitespecError,
    Conflict,
    DockerError,
    Forbidden,
    GpuBusy,
    InsufficientResources,
    InternalError,
    MonitorOnly,
    NotFound,
)
from ..gpu_audit import GpuAuditResult, GpuAuditRunner, device_fingerprint
from ..identity import (
    Identity,
    IdentityResolver,
    IdentityResolverProtocol,
    peer_credentials,
    portal_account_issues,
    portal_group_extra_members,
    resolve_portal_account,
)
from ..legacy_audit import LegacyAuditResult, LegacyAuditRunner
from ..members import load_members, public_member_data
from ..membership import authorization as member_authz
from ..membership import service as member_service
from ..membership import store as member_store
from ..membership.provisioning import (
    ProvisioningClient,
    ProvisioningError,
    SpoolProvisioningClient,
)
from ..models import GPU, Allocation
from ..portal import store as portal_store
from ..stream import (
    FRAME_DATA,
    FRAME_EXIT,
    FRAME_RESIZE,
    exit_payload,
    parse_resize,
    read_frame,
    write_frame,
)
from ..workspace_quota import (
    QuotaCheckResult,
    check_workspace_quota,
    provision_member_quota,
    verify_member_quota,
)
from . import admin_auth, workspace_io
from .docker_driver import (
    ContainerInfo,
    DockerBackend,
    DockerDriver,
    GPUProcess,
    ShellSession,
    matches_managed_allocation,
)
from .expiry import expire_once as expire_allocations
from .reconcile import reconcile as reconcile_allocations

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024
CONNECTION_TIMEOUT = 30.0
ACTIVE_GPU_HOLDER_STATUSES = {"running", "restarting", "paused"}
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


@dataclass(frozen=True)
class LiveResources:
    gpus: list[GPU]
    containers: list[ContainerInfo]
    processes: list[GPUProcess]
    error: str | None = None


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


class Broker:
    def __init__(
        self,
        config: LabConfig,
        *,
        db_path: str | Path | None = None,
        socket_path: str | Path | None = None,
        docker: DockerBackend | None = None,
        identity_resolver: IdentityResolverProtocol | None = None,
        legacy_audit: Callable[[], LegacyAuditResult] | None = None,
        gpu_audit: Callable[[], GpuAuditResult] | None = None,
        quota_check: Callable[[], QuotaCheckResult] | None = None,
        provisioning: ProvisioningClient | None = None,
        member_quota_check: Callable[[str, int, int], Any] | None = None,
        member_quota_provision: Callable[[str, int, int], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        expiry_interval: float = 30.0,
        reconcile_interval: float = 300.0,
    ) -> None:
        self.config = config
        self.db_path = Path(db_path or config.server.state_db)
        self.socket_path = Path(socket_path or config.server.broker_socket)
        self.docker = docker or DockerDriver(config)
        if identity_resolver is None:
            portal_issues = portal_account_issues(config.portal.service_account)
            portal_account = (
                None if portal_issues else resolve_portal_account(config.portal.service_account)
            )
            if portal_account is None:
                logger.warning(
                    "portal service account %r is not a usable machine identity "
                    "(%s); portal broker commands will be denied",
                    config.portal.service_account,
                    "; ".join(portal_issues) or "account is not resolvable",
                )
                portal_uid: int | None = None
                portal_username: str | None = None
            else:
                portal_uid, portal_username = portal_account
                extra = portal_group_extra_members(
                    config.groups.portal, config.portal.service_account
                )
                if extra:
                    logger.warning(
                        "portal group %r contains non-service accounts: %s; "
                        "portal capability remains bound to uid %d only",
                        config.groups.portal,
                        ", ".join(extra),
                        portal_uid,
                    )
            identity_resolver = IdentityResolver(
                config.groups.admins,
                config.groups.portal,
                portal_uid=portal_uid,
                portal_username=portal_username,
            )
        self.identity_resolver = identity_resolver
        self.legacy_audit = legacy_audit or LegacyAuditRunner(
            config.policy.legacy_audit_script,
            timeout=config.policy.legacy_audit_timeout,
            allow_root_ssh_keys=config.policy.allow_root_ssh_keys,
        )
        self.gpu_audit = gpu_audit or GpuAuditRunner(
            config.policy.gpu_audit_script,
            timeout=config.policy.gpu_audit_timeout,
            expected_gpus=config.hardware.gpu_count,
            repair_script=str(
                Path(config.policy.gpu_audit_script).with_name("fix_gpu_permissions.sh")
            ),
        )
        self.provisioning = provisioning or SpoolProvisioningClient(
            config.onboarding.provisioning_spool
        )
        self.quota_check = quota_check or (
            lambda: check_workspace_quota(
                config.server.workspace_root,
                config.policy.max_workspace_gb,
            )
        )
        self.member_quota_check = member_quota_check or verify_member_quota
        self.member_quota_provision = member_quota_provision or provision_member_quota
        self.clock = clock or store.utcnow
        self.expiry_interval = expiry_interval
        self.reconcile_interval = reconcile_interval
        self._mode = config.policy.mode
        self._server: asyncio.AbstractServer | None = None
        self._expiry_task: asyncio.Task[None] | None = None
        self._gpu_audit_failure: str | None = None
        self._quota_failure: str | None = None
        self._storage_failure: str | None = None
        self._gpu_fingerprint = device_fingerprint()
        self._tunnels: dict[str, set[asyncio.Task[None]]] = {}
        self._tunnel_tasks: set[asyncio.Task[None]] = set()
        self._tunnel_uids: dict[asyncio.Task[None], int] = {}
        self._shell_tasks: set[asyncio.Task[None]] = set()
        self._shell_uids: dict[asyncio.Task[None], int] = {}
        # Long-lived shell/tunnel reads block in worker threads. Keep them off
        # the loop's default executor so idle terminals cannot starve release,
        # expiry, audit, or other control-plane work. Writes/resizes/closes use
        # a small separate lane so they never queue behind a blocking read.
        self._stream_read_executor: ThreadPoolExecutor | None = None
        self._stream_control_executor: ThreadPoolExecutor | None = None
        self._ensure_stream_executors()
        # Every authenticated broker connection, grouped by peer UID. Closing
        # these streams on suspend/revoke/failed-key-sync terminates already
        # authenticated shell, tunnel, and file-transfer sessions; the database
        # version gate alone cannot revoke an open stream.
        self._client_writers: dict[int, set[asyncio.StreamWriter]] = {}

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = connect(self.db_path)
        try:
            yield connection
        finally:
            connection.close()

    def _now(self) -> datetime:
        return self.clock()

    def _ensure_stream_executors(self) -> None:
        if self._stream_read_executor is None:
            # One blocking read may be outstanding per admitted stream. The
            # caps below therefore also bound how many read workers can be
            # occupied at once.
            workers = self.config.policy.max_shells_global + self.config.policy.max_tunnels_global
            self._stream_read_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="citespec-stream-read",
            )
        if self._stream_control_executor is None:
            self._stream_control_executor = ThreadPoolExecutor(
                max_workers=8,
                thread_name_prefix="citespec-stream-control",
            )

    async def _stream_read(self, func: Callable[..., Any], *args: Any) -> Any:
        self._ensure_stream_executors()
        assert self._stream_read_executor is not None
        return await asyncio.get_running_loop().run_in_executor(
            self._stream_read_executor, func, *args
        )

    async def _stream_control(self, func: Callable[..., Any], *args: Any) -> Any:
        self._ensure_stream_executors()
        assert self._stream_control_executor is not None
        return await asyncio.get_running_loop().run_in_executor(
            self._stream_control_executor, func, *args
        )

    def _reserve_shell(self, task: asyncio.Task[None] | None, uid: int) -> None:
        if task is None:
            return
        if len(self._shell_tasks) >= self.config.policy.max_shells_global:
            raise Conflict("too many open shell sessions on this server")
        per_user = sum(1 for item in self._shell_tasks if self._shell_uids.get(item) == uid)
        if per_user >= self.config.policy.max_shells_per_user:
            raise Conflict("too many open shell sessions for this member")
        self._shell_tasks.add(task)
        self._shell_uids[task] = uid

    def _release_shell(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        self._shell_tasks.discard(task)
        self._shell_uids.pop(task, None)

    def _reserve_tunnel(self, task: asyncio.Task[None] | None, uid: int) -> None:
        if task is None:
            return
        if len(self._tunnel_tasks) >= self.config.policy.max_tunnels_global:
            raise Conflict("too many open tunnels on this server")
        per_user = sum(1 for item in self._tunnel_tasks if self._tunnel_uids.get(item) == uid)
        if per_user >= self.config.policy.max_tunnels_per_user:
            raise Conflict("too many open tunnels for this member")
        self._tunnel_tasks.add(task)
        self._tunnel_uids[task] = uid

    def _release_tunnel(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        self._tunnel_tasks.discard(task)
        self._tunnel_uids.pop(task, None)

    def _allocation_map(self, connection: Any, now: datetime) -> dict[str, dict[str, Any]]:
        rows = store.list_allocations(
            connection,
            statuses=("pending", "active"),
            now=now,
        )
        return {str(row["id"]): row for row in rows}

    def _legacy_audit_result(self) -> LegacyAuditResult:
        if not self.config.policy.legacy_audit_required:
            return LegacyAuditResult(True, "legacy audit disabled by configuration")
        return self.legacy_audit()

    def _run_gpu_audit(self) -> str | None:
        if not self.config.policy.gpu_audit_required:
            self._gpu_audit_failure = None
            return None
        try:
            result = self.gpu_audit()
        except Exception as exc:
            self._gpu_audit_failure = f"audit runner crashed: {exc}"
            logger.exception("GPU permission audit crashed")
            return self._gpu_audit_failure
        self._gpu_fingerprint = result.fingerprint or self._gpu_fingerprint
        if not result.passed:
            self._gpu_audit_failure = result.failure_message()
            logger.error("GPU permission audit failed: %s", self._gpu_audit_failure)
            return self._gpu_audit_failure
        self._gpu_audit_failure = None
        return None

    def _run_quota_check(self) -> str | None:
        required = self.config.policy.workspace_quota_required or (
            self.config.policy.max_workspace_gb > 0
        )
        if not required:
            self._quota_failure = None
            return None
        try:
            result = self.quota_check()
        except Exception as exc:
            self._quota_failure = f"quota checker crashed: {exc}"
            logger.exception("workspace quota gate crashed")
            return self._quota_failure
        if not result.passed:
            self._quota_failure = result.failure_message()
            logger.error("workspace quota gate failed: %s", self._quota_failure)
            return self._quota_failure
        self._quota_failure = None
        return None

    def _container_storage_params(self) -> dict[str, int]:
        """Global writable-layer accounting parameters for capacity checks.

        Only the development profile has a per-container writable-layer quota,
        so the budget applies there; in the restricted profile every value is
        zero and the check is skipped.
        """

        if self.config.policy.container_profile != "development":
            return {"writable_layer_gb": 0, "docker_storage_budget_gb": 0}
        return {
            "writable_layer_gb": self.config.policy.writable_layer_gb,
            "docker_storage_budget_gb": self.config.policy.docker_storage_budget_gb,
        }

    def _membership_denial(
        self,
        connection: Any,
        identity: Any,
        command: str,
    ) -> dict[str, Any] | None:
        """Unified member authorization for one command (§6.2).

        The mode matrix is checked first so streaming handlers enforce the same
        maintenance/monitor restrictions as regular RPC commands.
        """
        mode_denial = member_authz.check_for_mode(
            mode=self._mode,
            command=command,
            is_admin=identity.is_admin,
        )
        if mode_denial is not None:
            return mode_denial
        return member_authz.check_for_command(
            connection,
            command=command,
            is_admin=identity.is_admin,
            uid=identity.uid,
            membership_required=self.config.policy.membership_required,
            now=self._now(),
        )

    def _require_member_access(self, identity: Any, command: str) -> None:
        """Defense-in-depth check immediately before a privileged side effect."""
        if identity.is_admin or not self.config.policy.membership_required:
            return
        with self._connection() as connection:
            denial = self._membership_denial(connection, identity, command)
        if denial is not None:
            raise Forbidden(denial["message"])

    def _provisioning_for_member(self, member_id: str) -> None:
        """Cancel queues and tunnels for one member (§8.4 steps 2-3).

        Called after the authoritative status/version changed so subsequent
        operations are already refused; this reclaims in-flight work.
        """
        owned_allocations: set[str] = set()
        with self._connection() as connection:
            member = member_store.get_member(connection, member_id)
            uid = member["member"].get("linux_uid")
            if uid is None:
                return
            uid = int(uid)
            queued = store.list_queue_requests(connection, owner_uid=uid, now=self._now())
            for request in queued:
                if request.get("status") == "queued":
                    try:
                        store.cancel_queue_request(
                            connection,
                            str(request["queue_id"]),
                            actor_uid=0,
                            reason="成员资格状态变更",
                            now=self._now(),
                        )
                    except Exception:
                        logger.exception("could not cancel queue request %s", request["queue_id"])
            owned_allocations = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM allocations WHERE owner_uid = ?", (uid,)
                ).fetchall()
            }
        # Only this member's streams and tunnels. Cancelling every tunnel or
        # every writer here would be a cross-member denial-of-service.
        self._close_member_connections(uid)
        for allocation_id in list(self._tunnels):
            if allocation_id in owned_allocations:
                self._cancel_tunnels(allocation_id)

    def _open_db(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _request_id(self, args: dict[str, Any]) -> str:
        request_id = args.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise BadRequest("request_id must be a non-empty string")
        return request_id

    def _parse_portal_actor(
        self,
        raw: Any,
        *,
        allow_applicant: bool = False,
    ) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise BadRequest("portal actor is required")
        extra = set(raw) - {"user_id", "username", "role", "member_id"}
        if extra:
            raise BadRequest("unexpected portal actor field(s)")
        user_id = raw.get("user_id", "")
        username = raw.get("username", "")
        role = raw.get("role", "")
        member_id = raw.get("member_id", "")
        if not isinstance(user_id, str) or not user_id:
            raise BadRequest("portal actor user_id must be a non-empty string")
        if not isinstance(username, str) or not username:
            raise BadRequest("portal actor username must be a non-empty string")
        if not isinstance(member_id, str):
            raise BadRequest("portal actor member_id must be a string")
        allowed_roles = {"member", "admin"}
        if allow_applicant:
            allowed_roles.add("applicant")
        if role not in allowed_roles:
            raise Forbidden("portal actor role is not authorized")
        return {
            "user_id": user_id,
            "username": username,
            "role": str(role),
            "member_id": member_id,
        }

    def _record_portal_event(
        self,
        identity: Identity,
        actor: dict[str, str],
        action: str,
        *,
        target: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "portal_user_id": actor["user_id"],
            "portal_username": actor["username"],
            "portal_role": actor["role"],
            "target": target,
            **(detail or {}),
        }
        with self._connection() as connection:
            store.record_event(
                connection,
                actor_uid=identity.uid,
                action=action,
                detail=payload,
                now=self._now(),
            )
            connection.commit()

    async def _handle_portal_command(
        self,
        command: str,
        identity: Identity,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Narrow portal bridge; never a general admin or resource API."""

        if not identity.is_portal:
            raise Forbidden("portal broker access required")
        actor = self._parse_portal_actor(
            args.get("actor"),
            allow_applicant=command == "portal_registration_submit",
        )
        if command == "portal_registration_submit":
            self._check_args(args, {"actor", "application"})
            application = args.get("application")
            if not isinstance(application, dict):
                raise BadRequest("application must be an object")
            intake_db = self.config.onboarding.intake_db
            result = await asyncio.to_thread(
                self._submit_portal_application,
                intake_db,
                application,
            )
            self._record_portal_event(
                identity,
                actor,
                "portal_registration_submitted",
                target=str(result.get("request_id", "")),
                detail={"username": str(result.get("username", ""))},
            )
            return result
        if actor["role"] not in {"member", "admin"}:
            raise Forbidden("applicant actors may only submit a registration")
        admin_only = {
            "portal_requests_list",
            "portal_requests_show",
            "portal_requests_approve",
            "portal_requests_reject",
            "portal_members_list",
            "portal_members_show",
            "portal_members_suspend",
            "portal_members_resume",
            "portal_members_revoke",
            "portal_audit",
        }
        if command in admin_only and actor["role"] != "admin":
            raise Forbidden("portal administrator permission required")
        if command == "portal_admin_attest":
            self._check_args(
                args,
                {"actor", "username", "password", "totp", "action", "target"},
            )
            if actor["role"] != "admin":
                raise Forbidden("portal administrator permission required")
            username = args.get("username")
            password = args.get("password")
            otp = args.get("totp")
            action = args.get("action")
            target = args.get("target")
            if (
                not isinstance(username, str)
                or not isinstance(password, str)
                or not isinstance(otp, str)
                or not isinstance(action, str)
                or not isinstance(target, str)
            ):
                raise BadRequest("administrator attestation fields must be strings")
            try:
                issued = await asyncio.to_thread(
                    self._issue_portal_admin_attestation,
                    username=username,
                    password=password,
                    otp=otp,
                    action=action,
                    target=target,
                    actor=actor,
                )
            except admin_auth.AdminAuthDenied as exc:
                self._record_portal_event(
                    identity,
                    actor,
                    "portal_admin_attestation_denied",
                    target=target,
                    detail={"attestation_action": action},
                )
                raise Forbidden(str(exc)) from exc
            except ValueError as exc:
                raise BadRequest(str(exc)) from exc
            self._record_portal_event(
                identity,
                actor,
                "portal_admin_attested",
                target=target,
                detail={
                    "attested_admin": issued["admin"],
                    "attestation_action": action,
                    "expires_at": issued["expires_at"],
                    "nonce": issued["nonce"],
                },
            )
            issued.pop("nonce", None)
            return issued
        if command == "portal_status":
            self._check_args(args, {"actor"})
            with self._connection() as connection:
                mode = store.get_mode(connection, self._mode)
                active = store.active_lease_count(connection)
                queued = store.queue_request_count(connection, now=self._now())
                gpus = store.list_gpus(connection, now=self._now())
            return {
                "mode": mode,
                "active_allocations": active,
                "queued_requests": queued,
                "gpu_count": len(gpus),
                "gpus": [
                    {
                        "index": int(gpu["index"]),
                        "name": str(gpu["name"]),
                        "memory_mib": int(gpu["memory_mib"]),
                        "state": str(gpu["state"]),
                    }
                    for gpu in gpus
                ],
                "generated_at": self._now().isoformat(),
            }
        if command in {"portal_member_self", "portal_members_show"}:
            self._check_args(args, {"actor", "member_id"})
            member_id = args.get("member_id")
            if not isinstance(member_id, str) or not member_id:
                raise BadRequest("member_id must be a non-empty string")
            if actor["role"] != "admin" and actor["member_id"] != member_id:
                raise Forbidden("members may only view their own membership")
            with self._connection() as connection:
                return member_store.get_member(connection, member_id)
        if command == "portal_members_list":
            self._check_args(args, {"actor"})
            with self._connection() as connection:
                return {"members": member_store.list_members(connection)}
        if command in {"portal_requests_list", "portal_requests_show"}:
            mapped = {
                "portal_requests_list": "admin_requests_list",
                "portal_requests_show": "admin_requests_show",
            }[command]
            return await self._handle_membership_command(
                mapped,
                identity,
                self._without_actor(args),
                allow_portal=True,
            )
        mapped_mutations = {
            "portal_requests_approve": "admin_requests_approve",
            "portal_requests_reject": "admin_requests_reject",
            "portal_members_suspend": "admin_members_suspend",
            "portal_members_resume": "admin_members_resume",
            "portal_members_revoke": "admin_members_revoke",
        }
        if command in mapped_mutations:
            target_key = "request_id" if command.startswith("portal_requests_") else "member_id"
            target = args.get(target_key)
            if not isinstance(target, str) or not target:
                raise BadRequest(f"{target_key} must be a non-empty string")
            token = args.get("attestation")
            if not isinstance(token, str) or not token:
                raise Forbidden("fresh administrator attestation required")
            try:
                attested = await asyncio.to_thread(
                    self._consume_portal_admin_attestation,
                    token=token,
                    action=command,
                    target=target,
                    actor=actor,
                )
            except admin_auth.AdminAuthDenied as exc:
                self._record_portal_event(
                    identity,
                    actor,
                    "portal_admin_attestation_denied",
                    target=target,
                    detail={"attestation_action": command},
                )
                raise Forbidden(str(exc)) from exc
            result = await self._handle_membership_command(
                mapped_mutations[command],
                identity,
                self._without_portal_auth(args),
                allow_portal=True,
            )
            detail: dict[str, Any] = {
                "attested_admin": attested["admin"],
                "attestation_nonce": attested["nonce"],
            }
            if isinstance(result, dict):
                for key in ("request_id", "member_id", "operation_id", "status"):
                    if key in result:
                        detail[key] = result[key]
            self._record_portal_event(
                identity,
                actor,
                f"portal_{command.removeprefix('portal_')}",
                target=target,
                detail=detail,
            )
            return result
        if command == "portal_audit":
            self._check_args(args, {"actor", "limit"})
            limit = self._parse_positive_int(args.get("limit", 100), "limit")
            if limit > 500:
                raise BadRequest("limit must not exceed 500")
            with self._connection() as connection:
                return {"events": store.list_events(connection, limit=limit)}
        raise BadRequest(f"unknown portal command: {command}")

    def _issue_portal_admin_attestation(
        self,
        *,
        username: str,
        password: str,
        otp: str,
        action: str,
        target: str,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        with self._connection() as connection:
            return admin_auth.issue_attestation(
                connection,
                username=username,
                password=password,
                otp=otp,
                action=action,
                target=target,
                portal_user_id=actor["user_id"],
                portal_username=actor["username"],
                key_path=self.config.server.admin_auth_key_file,
                ttl_seconds=self.config.server.admin_attestation_ttl_seconds,
                now=self._now(),
            )

    def _consume_portal_admin_attestation(
        self,
        *,
        token: str,
        action: str,
        target: str,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        with self._connection() as connection:
            return admin_auth.consume_attestation(
                connection,
                token=token,
                action=action,
                target=target,
                portal_user_id=actor["user_id"],
                portal_username=actor["username"],
                now=self._now(),
            )

    def _handle_portal_admin_auth_command(
        self,
        command: str,
        identity: Identity,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Broker-managed credentials for portal administrator attestations."""

        if command == "admin_portal_auth_list":
            self._check_args(args)
            with self._connection() as connection:
                return {"admins": admin_auth.list_admins(connection)}
        if command == "admin_portal_auth_enroll":
            self._check_args(args, {"username", "password"})
            username = self._portal_admin_string(args, "username")
            password = self._portal_admin_string(args, "password")
            try:
                with self._connection() as connection:
                    result = admin_auth.enroll_admin(
                        connection,
                        username=username,
                        password=password,
                        key_path=self.config.server.admin_auth_key_file,
                        now=self._now(),
                    )
                    store.record_event(
                        connection,
                        actor_uid=identity.uid,
                        action="admin_portal_auth_enrolled",
                        detail={"target": result["username"]},
                        now=self._now(),
                    )
                    connection.commit()
            except admin_auth.AdminAuthDenied as exc:
                raise InternalError("administrator authentication key is unavailable") from exc
            except ValueError as exc:
                raise BadRequest(str(exc)) from exc
            return result
        if command == "admin_portal_auth_remove":
            self._check_args(args, {"username"})
            username = self._portal_admin_string(args, "username")
            with self._connection() as connection:
                removed = admin_auth.remove_admin(connection, username=username)
            if not removed:
                raise NotFound(f"portal administrator credential not found: {username}")
            with self._connection() as connection:
                store.record_event(
                    connection,
                    actor_uid=identity.uid,
                    action="admin_portal_auth_removed",
                    detail={"target": username},
                    now=self._now(),
                )
                connection.commit()
            return {"username": username, "removed": True}
        raise BadRequest(f"unknown portal auth command: {command}")

    def _handle_portal_admin_command(
        self,
        command: str,
        identity: Identity,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """CLI-only portal role administration.

        Admin role changes intentionally never go through a browser route.  The
        root broker owns the portal database for this operation, restores the
        portal service's ownership of SQLite sidecars, and records the action in
        both the broker and portal audit logs.
        """

        path = Path(self.config.portal.db_path)
        try:
            owner = path.stat()
        except FileNotFoundError as exc:
            raise NotFound("portal database is not initialized") from exc
        connection = portal_store.connect(path)
        action = ""
        target = ""
        detail: dict[str, Any] = {}
        try:
            if command == "admin_portal_accounts_list":
                self._check_args(args)
                return {"accounts": portal_store.list_users(connection)}
            if command == "admin_portal_accounts_set_role":
                self._check_args(args, {"username", "role"})
                username = self._portal_admin_string(args, "username")
                role = self._portal_admin_string(args, "role")
                if role not in portal_store.PORTAL_ROLES:
                    raise BadRequest("role must be member or admin")
                current = portal_store.get_user_by_username(connection, username)
                if current is None:
                    raise NotFound(f"portal account not found: {username}")
                if current["role"] != role:
                    if role == "member" and current["role"] == "admin":
                        active_other_admins = [
                            user
                            for user in portal_store.list_users(connection)
                            if user["role"] == "admin"
                            and user["status"] == "active"
                            and user["user_id"] != current["user_id"]
                        ]
                        if not active_other_admins:
                            raise Conflict("cannot demote the last active portal administrator")
                    portal_store.update_user_role(
                        connection,
                        str(current["user_id"]),
                        role=role,
                        now=self._now(),
                    )
                    # A role change must not leave a browser session holding the
                    # old capability.  Reauthentication is required.
                    portal_store.delete_user_sessions(
                        connection,
                        str(current["user_id"]),
                    )
                action = "admin_portal_role_changed"
                target = username
                detail = {
                    "from_role": current["role"],
                    "to_role": role,
                    "portal_user_id": current["user_id"],
                }
                result = {
                    "user_id": current["user_id"],
                    "username": current["username"],
                    "role": role,
                    "status": current["status"],
                }
            elif command == "admin_portal_accounts_create":
                self._check_args(
                    args,
                    {
                        "username",
                        "role",
                        "member_id",
                        "linux_username",
                        "display_name",
                        "contact",
                    },
                )
                username = self._portal_admin_string(args, "username")
                if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
                    raise BadRequest("username must match [a-z_][a-z0-9_-]{0,31}")
                role = self._portal_admin_string(args, "role", default="member")
                if role not in portal_store.PORTAL_ROLES:
                    raise BadRequest("role must be member or admin")
                member_id = self._portal_admin_string(args, "member_id", default="")
                linux_username = self._portal_admin_string(args, "linux_username", default="")
                if linux_username and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", linux_username):
                    raise BadRequest("linux_username must match [a-z_][a-z0-9_-]{0,31}")
                display_name = self._portal_admin_string(args, "display_name", default="")
                contact = self._portal_admin_string(args, "contact", default="")
                try:
                    result = portal_store.create_pending_account(
                        connection,
                        username=username,
                        display_name=display_name[:200],
                        contact=contact[:300],
                        member_id=member_id,
                        linux_username=linux_username,
                        activation_ttl_hours=self.config.portal.activation_ttl_hours,
                        role=role,
                        created_by=f"cli:{identity.username}",
                        now=self._now(),
                    )
                except ValueError as exc:
                    raise Conflict(str(exc)) from exc
                action = "admin_portal_account_created"
                target = username
                detail = {
                    "role": role,
                    "portal_user_id": result["user_id"],
                    "member_id": member_id,
                    "linux_username": linux_username,
                }
            else:  # pragma: no cover - dispatch is exact
                raise BadRequest(f"unknown portal admin command: {command}")
            if action:
                portal_store.record_audit(
                    connection,
                    action=action,
                    actor_username=identity.username,
                    target=target,
                    detail=detail,
                    now=self._now(),
                )
        finally:
            connection.close()
            self._restore_portal_db_ownership(path, owner.st_uid, owner.st_gid)
        if action:
            with self._connection() as main_connection:
                store.record_event(
                    main_connection,
                    actor_uid=identity.uid,
                    action=action,
                    detail={"target": target, **detail},
                    now=self._now(),
                )
                main_connection.commit()
        return result

    @staticmethod
    def _portal_admin_string(
        args: dict[str, Any],
        key: str,
        *,
        default: str | None = None,
    ) -> str:
        value = args.get(key, default)
        if not isinstance(value, str):
            raise BadRequest(f"{key} must be a string")
        return value

    @staticmethod
    def _restore_portal_db_ownership(path: Path, uid: int, gid: int) -> None:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        chown = getattr(os, "chown", None)
        if chown is None:
            return
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                if candidate.exists():
                    chown(candidate, uid, gid)
                    if candidate != path:
                        candidate.chmod(0o600)
            except OSError:
                logger.warning("could not restore ownership of %s", candidate, exc_info=True)

    @staticmethod
    def _without_actor(args: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in args.items() if key != "actor"}

    @staticmethod
    def _without_portal_auth(args: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in args.items() if key not in {"actor", "attestation"}}

    @staticmethod
    def _submit_portal_application(
        intake_db: str,
        application: dict[str, Any],
    ) -> dict[str, Any]:
        path = Path(intake_db)
        # The broker runs as root while the intake service owns its database.
        # Preserve the directory owner on the database and any WAL/SHM files so
        # the low-privilege intake service can keep writing after this request.
        owner_uid = owner_gid = -1
        try:
            parent = path.parent.stat()
            owner_uid, owner_gid = parent.st_uid, parent.st_gid
        except OSError:
            pass
        connection = intake_store.init_intake_db(path)
        try:
            return intake_store.submit_portal_application(
                connection,
                application=application,
            )
        finally:
            connection.close()
            if hasattr(os, "geteuid") and os.geteuid() == 0 and owner_uid >= 0:
                for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                    try:
                        if candidate.exists():
                            os.chown(candidate, owner_uid, owner_gid)
                    except OSError:
                        logger.warning(
                            "could not restore intake database ownership: %s",
                            candidate,
                            exc_info=True,
                        )

    async def _handle_membership_command(
        self,
        command: str,
        identity: Any,
        args: dict[str, Any],
        *,
        allow_portal: bool = False,
    ) -> dict[str, Any]:
        """Admin membership management + member self-service keys (§7, §8)."""
        admin_commands = {
            "admin_requests_list",
            "admin_requests_show",
            "admin_requests_approve",
            "admin_requests_reject",
            "admin_requests_needs_info",
            "admin_members_list",
            "admin_members_show",
            "admin_members_keys_add",
            "admin_members_keys_revoke",
            "admin_members_import_legacy",
            "admin_members_suspend",
            "admin_members_resume",
            "admin_members_revoke",
            "admin_operations_show",
            "admin_operations_retry",
        }
        is_admin = command in admin_commands
        if is_admin:
            if not (identity.is_admin or (allow_portal and identity.is_portal)):
                raise Forbidden("admin permission required")
        else:
            # Member self-service (me_keys_*): requires an active member.
            if not self.config.policy.membership_required:
                raise Forbidden("成员自助公钥管理未启用")
            if identity.is_admin:
                raise Forbidden("管理员公钥不走成员自助管理")
        intake_db = self.config.onboarding.intake_db
        provisioning = self.provisioning

        if command == "admin_requests_list":
            self._check_args(args, {"status"})
            status = args.get("status")
            if status is not None and not isinstance(status, str):
                raise BadRequest("status must be a string")

            def _list_requests() -> Any:
                return member_service.list_requests(intake_db, status=status)

            return await asyncio.to_thread(_list_requests)
        if command == "admin_requests_show":
            self._check_args(args, {"request_id"})
            request_id = self._request_id(args)

            def _show_request() -> Any:
                decision = None
                with self._connection() as connection:
                    decision = member_store.get_application_decision(connection, request_id)
                    if decision is not None and decision.get("operation_id"):
                        operation = member_store.get_operation(
                            connection, str(decision["operation_id"])
                        )
                        decision["member_id"] = operation.get("member_id") or ""
                request = member_service.get_request(intake_db, request_id)
                request["decision"] = decision
                return request

            return await asyncio.to_thread(_show_request)
        if command == "admin_requests_approve":
            self._check_args(
                args,
                {
                    "request_id",
                    "valid_until",
                    "quota_gb",
                    "review_note",
                    "internal_note",
                    "expected_version",
                },
            )
            request_id = self._request_id(args)
            valid_until = args.get("valid_until", "") or ""
            if not isinstance(valid_until, str):
                raise BadRequest("valid_until must be a string")
            expected_version = args.get("expected_version", "") or ""
            if not isinstance(expected_version, str):
                raise BadRequest("expected_version must be a string")
            quota_gb = args.get("quota_gb", 200)
            if isinstance(quota_gb, bool) or not isinstance(quota_gb, int) or quota_gb <= 0:
                raise BadRequest("quota_gb must be a positive integer")
            review_note = args.get("review_note", "")
            internal_note = args.get("internal_note", "")
            if not isinstance(review_note, str) or not isinstance(internal_note, str):
                raise BadRequest("notes must be strings")

            def _approve() -> Any:
                with self._connection() as connection:
                    result = member_service.approve_request(
                        connection,
                        intake_db=intake_db,
                        request_id=request_id,
                        reviewer_uid=identity.uid,
                        valid_until=valid_until,
                        quota_gb=quota_gb,
                        review_note=review_note,
                        internal_note=internal_note,
                        workspace_root=self.config.server.workspace_root,
                        provisioning=provisioning,
                        expected_version=expected_version,
                        now=self._now(),
                    )
                    connection.commit()
                    return result

            return await asyncio.to_thread(_approve)
        if command == "admin_requests_reject":
            self._check_args(
                args, {"request_id", "review_note", "internal_note", "expected_version"}
            )
            request_id = self._request_id(args)
            review_note = args.get("review_note", "")
            internal_note = args.get("internal_note", "")
            expected_version = args.get("expected_version", "") or ""
            if not isinstance(review_note, str) or not review_note:
                raise BadRequest("拒绝时必须给出申请人可见的原因")
            if not isinstance(internal_note, str) or not isinstance(expected_version, str):
                raise BadRequest("notes and expected_version must be strings")

            def _reject() -> Any:
                with self._connection() as connection:
                    result = member_service.reject_request(
                        connection,
                        intake_db=intake_db,
                        request_id=request_id,
                        reviewer_uid=identity.uid,
                        review_note=review_note,
                        internal_note=internal_note,
                        expected_version=expected_version,
                        now=self._now(),
                    )
                    connection.commit()
                    return result

            return await asyncio.to_thread(_reject)
        if command == "admin_requests_needs_info":
            self._check_args(
                args,
                {"request_id", "review_note", "internal_note", "expected_version"},
            )
            request_id = self._request_id(args)
            review_note = args.get("review_note", "")
            internal_note = args.get("internal_note", "")
            expected_version = args.get("expected_version", "") or ""
            if not isinstance(review_note, str) or not review_note.strip():
                raise BadRequest("要求补充材料时必须给出申请人可见的说明")
            if not isinstance(internal_note, str) or not isinstance(expected_version, str):
                raise BadRequest("notes and expected_version must be strings")

            def _needs_info() -> Any:
                with self._connection() as connection:
                    result = member_service.request_info(
                        connection,
                        intake_db=intake_db,
                        request_id=request_id,
                        reviewer_uid=identity.uid,
                        public_note=review_note,
                        internal_note=internal_note,
                        expected_version=expected_version,
                        now=self._now(),
                    )
                    connection.commit()
                    return result

            return await asyncio.to_thread(_needs_info)
        if command == "admin_members_list":
            self._check_args(args)
            with self._connection() as connection:
                return {"members": member_store.list_members(connection)}
        if command == "admin_members_show":
            self._check_args(args, {"member_id"})
            member_id = args.get("member_id")
            if not isinstance(member_id, str) or not member_id:
                raise BadRequest("member_id must be a non-empty string")
            with self._connection() as connection:
                return member_store.get_member(connection, member_id)
        if command == "admin_members_import_legacy":
            self._check_args(
                args,
                {
                    "username",
                    "display_name",
                    "valid_until",
                    "quota_gb",
                    "public_keys",
                    "dry_run",
                },
            )
            username = args.get("username")
            public_keys = args.get("public_keys")
            dry_run = args.get("dry_run", True)
            display_name = args.get("display_name", "")
            valid_until = args.get("valid_until", "")
            quota_gb = args.get("quota_gb", 200)
            if not isinstance(username, str) or not username:
                raise BadRequest("username must be a non-empty string")
            if not isinstance(public_keys, list) or any(
                not isinstance(key, str) for key in public_keys
            ):
                raise BadRequest("public_keys must be a list of strings")
            if not isinstance(dry_run, bool):
                raise BadRequest("dry_run must be a boolean")
            if not isinstance(display_name, str) or not isinstance(valid_until, str):
                raise BadRequest("display_name and valid_until must be strings")
            if isinstance(quota_gb, bool) or not isinstance(quota_gb, int):
                raise BadRequest("quota_gb must be an integer")

            def _import_legacy() -> Any:
                with self._connection() as connection:
                    return member_service.import_legacy_member(
                        connection,
                        username=username,
                        display_name=display_name,
                        valid_until=valid_until,
                        quota_gb=quota_gb,
                        public_keys=public_keys,
                        actor_uid=identity.uid,
                        provisioning=provisioning,
                        workspace_root=self.config.server.workspace_root,
                        dry_run=dry_run,
                        now=self._now(),
                    )

            return await asyncio.to_thread(_import_legacy)
        if command == "admin_members_keys_add":
            self._check_args(args, {"member_id", "public_key"})
            raw_member_id = args.get("member_id")
            raw_public_key = args.get("public_key")
            if not isinstance(raw_member_id, str) or not raw_member_id:
                raise BadRequest("member_id must be a non-empty string")
            if not isinstance(raw_public_key, str) or not raw_public_key.strip():
                raise BadRequest("public_key must be a non-empty string")
            add_member_id = str(raw_member_id)
            add_public_key = str(raw_public_key)

            def _admin_add_key() -> Any:
                with self._connection() as connection:
                    try:
                        return member_service.add_member_key(
                            connection,
                            add_member_id,
                            public_key=add_public_key,
                            actor_uid=identity.uid,
                            provisioning=provisioning,
                            now=self._now(),
                        )
                    except ProvisioningError as exc:
                        raise Conflict("无法同步宿主机登录凭据；公钥未添加，请稍后重试") from exc

            return await asyncio.to_thread(_admin_add_key)
        if command == "admin_members_keys_revoke":
            self._check_args(args, {"member_id", "key_id"})
            raw_member_id = args.get("member_id")
            raw_key_id = args.get("key_id")
            if not isinstance(raw_member_id, str) or not raw_member_id:
                raise BadRequest("member_id must be a non-empty string")
            if not isinstance(raw_key_id, str) or not raw_key_id:
                raise BadRequest("key_id must be a non-empty string")
            revoke_member_id = str(raw_member_id)
            revoke_key_id = str(raw_key_id)

            def _admin_revoke_key() -> Any:
                with self._connection() as connection:
                    try:
                        result = member_service.revoke_member_key(
                            connection,
                            revoke_member_id,
                            key_id=revoke_key_id,
                            actor_uid=identity.uid,
                            provisioning=provisioning,
                            now=self._now(),
                        )
                    except ProvisioningError as exc:
                        raise Conflict("无法同步宿主机登录凭据；撤销未生效，请稍后重试") from exc
                if result.get("member_status") == "active":
                    with self._connection() as lookup:
                        uid = member_store.get_member(lookup, revoke_member_id)["member"].get(
                            "linux_uid"
                        )
                    if uid is not None:
                        self._close_member_connections(int(uid))
                return result

            return await asyncio.to_thread(_admin_revoke_key)
        if command in {
            "admin_members_suspend",
            "admin_members_resume",
            "admin_members_revoke",
        }:
            self._check_args(args, {"member_id", "reason"})
            member_id = args.get("member_id")
            reason = args.get("reason", "")
            if not isinstance(member_id, str) or not member_id:
                raise BadRequest("member_id must be a non-empty string")
            if not isinstance(reason, str) or not reason:
                raise BadRequest("reason is required for member status changes")
            target = {
                "admin_members_suspend": "suspended",
                "admin_members_resume": "active",
                "admin_members_revoke": "revoked",
            }[command]

            def _transition() -> Any:
                with self._connection() as connection:
                    if target == "suspended":
                        result = member_service.suspend_member(
                            connection,
                            member_id,
                            actor_uid=identity.uid,
                            reason=reason,
                            provisioning=provisioning,
                            on_member_change=self._provisioning_for_member,
                            now=self._now(),
                        )
                    elif target == "active":
                        result = member_service.resume_member(
                            connection,
                            member_id,
                            actor_uid=identity.uid,
                            reason=reason,
                            provisioning=provisioning,
                            now=self._now(),
                        )
                    else:
                        result = member_service.revoke_member(
                            connection,
                            member_id,
                            actor_uid=identity.uid,
                            reason=reason,
                            provisioning=provisioning,
                            on_member_change=self._provisioning_for_member,
                            now=self._now(),
                        )
                    connection.commit()
                    return result

            return await asyncio.to_thread(_transition)
        if command == "admin_operations_show":
            self._check_args(args, {"operation_id"})
            operation_id = args.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise BadRequest("operation_id must be a non-empty string")
            with self._connection() as connection:
                return member_store.get_operation(connection, operation_id)
        if command == "admin_operations_retry":
            self._check_args(args, {"operation_id"})
            operation_id = args.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise BadRequest("operation_id must be a non-empty string")

            def _retry() -> Any:
                with self._connection() as connection:
                    result = member_service.retry_operation(
                        connection,
                        operation_id,
                        provisioning=provisioning,
                        intake_db=intake_db,
                        actor_uid=identity.uid,
                        now=self._now(),
                    )
                    connection.commit()
                    return result

            return await asyncio.to_thread(_retry)
        if command == "me_keys_add":
            self._check_args(args, {"public_key"})
            self_public_key = args.get("public_key")
            if not isinstance(self_public_key, str) or not self_public_key.strip():
                raise BadRequest("public_key must be a non-empty string")

            def _add_key() -> Any:
                with self._connection() as connection:
                    decision = member_store.check_access(connection, identity.uid, now=self._now())
                    if not decision["allowed"]:
                        raise Forbidden("成员状态不允许管理公钥")
                    try:
                        result = member_service.add_member_key(
                            connection,
                            str(decision["member_id"]),
                            public_key=self_public_key,
                            actor_uid=identity.uid,
                            provisioning=provisioning,
                            now=self._now(),
                        )
                    except ProvisioningError as exc:
                        raise Conflict("无法同步宿主机登录凭据；公钥未添加，请稍后重试") from exc
                return {
                    "key_id": result["key_id"],
                    "fingerprint": result["fingerprint"],
                }

            return await asyncio.to_thread(_add_key)
        if command == "me_keys_revoke":
            self._check_args(args, {"key_id"})
            self_key_id = args.get("key_id")
            if not isinstance(self_key_id, str) or not self_key_id:
                raise BadRequest("key_id must be a non-empty string")

            def _revoke_key() -> Any:
                with self._connection() as connection:
                    decision = member_store.check_access(connection, identity.uid, now=self._now())
                    if not decision["allowed"]:
                        raise Forbidden("成员状态不允许管理公钥")
                    try:
                        member_service.revoke_member_key(
                            connection,
                            str(decision["member_id"]),
                            key_id=self_key_id,
                            actor_uid=identity.uid,
                            provisioning=provisioning,
                            now=self._now(),
                        )
                    except ProvisioningError as exc:
                        raise Conflict("无法同步宿主机登录凭据；撤销未生效，请稍后重试") from exc
                # A revoked key may still sit in an open ControlMaster; close
                # this member's established broker streams as well.
                self._close_member_connections(identity.uid)
                return {"key_id": self_key_id, "revoked": True}

            return await asyncio.to_thread(_revoke_key)
        raise BadRequest(f"unknown membership command: {command}")

    def _writable_layer_reservation_bytes(self, *, include_next_slot: bool) -> int:
        """Worst-case writable-layer growth promised to live allocations.

        Every pending/active/cleanup_pending allocation may still grow to its
        full quota, and a candidate allocation adds one more slot. Used to
        constrain admission by CURRENT free space, not filesystem capacity.
        A database failure fails closed with the full budget.
        """

        policy = self.config.policy
        if policy.container_profile != "development" or policy.writable_layer_gb <= 0:
            return 0
        try:
            with self._connection() as connection:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM allocations "
                        "WHERE status IN ('pending', 'active', 'cleanup_pending')"
                    ).fetchone()[0]
                )
        except Exception:
            logger.exception("could not count writable-layer reservations")
            return policy.docker_storage_budget_gb * 1024**3
        if include_next_slot:
            count += 1
        return count * policy.writable_layer_gb * 1024**3

    def _run_storage_check(self, *, include_next_slot: bool = False) -> str | None:
        try:
            passed, detail = self.docker.storage_preflight(
                writable_layer_entitlements_bytes=self._writable_layer_reservation_bytes(
                    include_next_slot=include_next_slot
                )
            )
        except Exception as exc:
            passed, detail = False, str(exc)
        if not passed:
            self._storage_failure = detail
            logger.error("Docker storage preflight failed: %s", detail)
            return detail
        self._storage_failure = None
        return None

    def _refresh_runtime_gates(self, *, include_storage: bool = True) -> str | None:
        failures: list[str] = []
        gpu_failure = self._run_gpu_audit()
        if gpu_failure:
            failures.append(f"GPU permission audit: {gpu_failure}")
        quota_failure = self._run_quota_check()
        if quota_failure:
            failures.append(f"workspace quota: {quota_failure}")
        if include_storage:
            storage_failure = self._run_storage_check()
            if storage_failure:
                failures.append(f"Docker storage: {storage_failure}")
        if failures:
            return "; ".join(failures)
        return None

    def _gate_failure(self) -> str | None:
        parts: list[str] = []
        if self._gpu_audit_failure:
            parts.append(f"GPU permission audit: {self._gpu_audit_failure}")
        if self._quota_failure:
            parts.append(f"workspace quota: {self._quota_failure}")
        if self._storage_failure:
            parts.append(f"Docker storage: {self._storage_failure}")
        return "; ".join(parts) if parts else None

    def _capabilities(self) -> dict[str, bool]:
        """Server-side feature flags; clients must defer to these instead of
        their own (possibly stale) local configuration."""

        return {
            "vscode": self.config.policy.vscode_enabled,
            "file_transfer": self.config.policy.file_transfer_enabled,
            "development_profile": self.config.policy.container_profile == "development",
        }

    def _live_resources(self) -> LiveResources:
        try:
            gpus = self.docker.list_gpus()
            processes = self.docker.list_compute_processes()
            containers = self.docker.list_containers()
        except Exception as exc:
            logger.warning("live resource inspection failed: %s", exc)
            return LiveResources([], [], [], str(exc))
        return LiveResources(gpus, containers, processes)

    @staticmethod
    def _resolve_gpu_refs(
        refs: tuple[str, ...],
        gpus: list[dict[str, Any]],
    ) -> tuple[set[str], bool]:
        by_uuid = {str(gpu["uuid"]): str(gpu["uuid"]) for gpu in gpus}
        by_index = {str(gpu["index"]): str(gpu["uuid"]) for gpu in gpus}
        resolved: set[str] = set()
        unknown = False
        for raw in refs:
            value = str(raw).strip()
            if value in {"", "none", "void"}:
                continue
            if value in {"all", "any"}:
                resolved.update(by_uuid.values())
                continue
            if value in by_uuid:
                resolved.add(by_uuid[value])
                continue
            if value in by_index:
                resolved.add(by_index[value])
                continue
            unknown = True
        return resolved, unknown

    def _managed_container_matches(
        self,
        container: ContainerInfo,
        allocations: dict[str, dict[str, Any]],
        gpus: list[dict[str, Any]],
    ) -> bool:
        if container.labels.get("citespec.managed") != "true":
            return False
        allocation_id = container.labels.get("citespec.allocation")
        if not allocation_id:
            return False
        allocation = allocations.get(allocation_id)
        if allocation is None:
            return False
        expected_container = str(allocation.get("container_id") or "")
        if not expected_container or container.id != expected_container:
            return False
        if container.labels.get("citespec.owner") != allocation.get("owner"):
            return False
        resolved, unknown = self._resolve_gpu_refs(container.gpu_refs, gpus)
        if unknown:
            return False
        expected_gpus = {str(item) for item in allocation.get("gpu_uuids", [])}
        return resolved == expected_gpus

    def _container_gpu_holders(
        self,
        containers: list[ContainerInfo],
        gpus: list[dict[str, Any]],
        allocations: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, list[ContainerInfo]], bool]:
        holders: dict[str, list[ContainerInfo]] = {}
        unknown = False
        for container in containers:
            if container.status not in ACTIVE_GPU_HOLDER_STATUSES:
                continue
            if self._managed_container_matches(container, allocations, gpus):
                continue
            resolved, bad_ref = self._resolve_gpu_refs(container.gpu_refs, gpus)
            unknown = unknown or bad_ref
            for uuid in resolved:
                holders.setdefault(uuid, []).append(container)
        return holders, unknown

    def _merge_gpus(
        self,
        db_gpus: list[dict[str, Any]],
        live: LiveResources,
        allocations: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        live_by_uuid = {gpu.uuid: gpu for gpu in live.gpus}
        processes_by_uuid: dict[str, list[GPUProcess]] = {}
        for process in live.processes:
            processes_by_uuid.setdefault(process.gpu_uuid, []).append(process)
        holders, unknown_refs = self._container_gpu_holders(
            live.containers,
            db_gpus,
            allocations,
        )
        result: list[dict[str, Any]] = []
        known_uuids: set[str] = set()
        for gpu in db_gpus:
            uuid = str(gpu["uuid"])
            known_uuids.add(uuid)
            live_gpu = live_by_uuid.get(uuid)
            processes = processes_by_uuid.get(uuid, [])
            pids = set(live_gpu.compute_pids if live_gpu else ())
            pids.update(process.pid for process in processes)
            used_mib = live_gpu.used_mib if live_gpu else 0
            if processes:
                used_mib = max(used_mib, max(process.used_mib for process in processes))
            item = dict(gpu)
            item["live_used_mib"] = used_mib if live_gpu or processes else None
            item["utilization"] = live_gpu.utilization if live_gpu else None
            item["compute_pids"] = sorted(pids)
            holder_ids = [container.id for container in holders.get(uuid, [])]
            item["unmanaged_container_ids"] = holder_ids
            if item.get("allocation_id"):
                state = "allocated"
                reason = "database claim"
            elif live.error:
                state = "unknown"
                reason = f"live detection failed: {live.error}"
            elif live_gpu is None:
                state = "unknown"
                reason = "GPU is absent from live nvidia-smi inventory"
            # The NVIDIA driver reports a tiny baseline (about 1 MiB on the
            # RTX 3090) even with no graphics/compute client.  Treat that
            # driver overhead as free; real compute processes are covered by
            # pids, and a graphics client such as Xorg reports >= 4 MiB per
            # GPU plus the baseline, which remains above this threshold.
            elif pids or used_mib > 8:
                state = "unmanaged"
                reason = "live compute process or significant used memory"
            elif holder_ids:
                state = "unmanaged"
                reason = "unmanaged container holds GPU access"
            elif unknown_refs:
                state = "unknown"
                reason = "unresolved unmanaged container GPU reference"
            else:
                state = "free"
                reason = "no claim, no live compute process, no container holder"
            item["state"] = state
            item["state_reason"] = reason
            result.append(item)
        for live_gpu in live.gpus:
            if live_gpu.uuid in known_uuids:
                continue
            result.append(
                {
                    "index": live_gpu.index,
                    "uuid": live_gpu.uuid,
                    "name": live_gpu.name,
                    "memory_mib": live_gpu.memory_mib,
                    "state": "unknown",
                    "state_reason": "live GPU is not in the database inventory",
                    "allocation_id": None,
                    "owner": None,
                    "status": None,
                    "expires_at": None,
                    "remaining_seconds": None,
                    "container_id": None,
                    "live_used_mib": live_gpu.used_mib,
                    "utilization": live_gpu.utilization,
                    "compute_pids": list(live_gpu.compute_pids),
                    "unmanaged_container_ids": [],
                }
            )
        return sorted(result, key=lambda item: int(item["index"]))

    def initialize(self) -> None:
        connection = init_db(self.db_path)
        try:
            self._mode = store.get_mode(connection, self.config.policy.mode)
            leases = store.active_lease_count(connection)
            queued = store.queue_request_count(connection, now=self._now())
            target = self._mode
            audit_failure = ""
            runtime_failure = ""
            policy_failure = ""
            if self._mode == "active":
                try:
                    validate_active_mode_policy(self.config.policy)
                except ValueError as exc:
                    # A persisted active mode must never outlive the policy
                    # gates it was approved with.
                    policy_failure = str(exc)
            if self._mode == "active" and self.config.policy.legacy_audit_required:
                audit = self._legacy_audit_result()
                if not audit.passed:
                    audit_failure = audit.failure_message()
            if self._mode == "active":
                runtime_failure = self._refresh_runtime_gates() or ""
            if self._mode == "monitor" and (leases or queued):
                target = "maintenance"
            elif self._mode == "active" and not self.config.policy.deployment_allowed:
                target = "maintenance" if (leases or queued) else "monitor"
            elif self._mode == "active" and (policy_failure or audit_failure or runtime_failure):
                target = "maintenance" if (leases or queued) else "monitor"
            if target != self._mode:
                logger.warning(
                    "adjusting persisted mode %s -> %s "
                    "(leases=%d queued=%d deployment_allowed=%s policy=%s audit=%s runtime=%s)",
                    self._mode,
                    target,
                    leases,
                    queued,
                    self.config.policy.deployment_allowed,
                    policy_failure or "passed",
                    audit_failure or "passed",
                    runtime_failure or "passed",
                )
                store.set_mode(
                    connection,
                    target,
                    actor_uid=0,
                    now=self._now(),
                )
                self._mode = target
        finally:
            connection.close()
        try:
            gpus = self.docker.list_gpus()
        except Exception:
            logger.exception("Docker GPU discovery failed")
            gpus = []
        if gpus:
            with self._connection() as connection:
                store.upsert_gpus(connection, gpus)

    async def start(self) -> None:
        await asyncio.to_thread(self.initialize)
        try:
            with self._connection() as connection:
                interrupted = member_service.recover_stale_operations(connection, now=self._now())
            if interrupted:
                logger.warning(
                    "membership operations interrupted before restart: %s",
                    ", ".join(interrupted),
                )
        except Exception:
            logger.exception("membership operation recovery failed")
        try:
            summary = await asyncio.to_thread(self.reconcile_once)
            logger.info("startup reconcile: %s", summary)
        except Exception:
            logger.exception("startup reconcile failed")
        try:
            admitted = await self._schedule_queue()
            if admitted:
                logger.info("startup queue admission: %s", ", ".join(admitted))
        except Exception:
            logger.exception("startup queue scheduling failed")
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise RuntimeError(f"refusing to replace non-socket path: {path}")
            path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(path),
            limit=MAX_MESSAGE_BYTES,
        )
        self._secure_socket(path)
        self._expiry_task = asyncio.create_task(self._expiry_loop())
        logger.info("broker listening on %s in %s mode", path, self._mode)

    def _secure_socket(self, path: Path) -> None:
        os.chmod(path, 0o660)
        if os.name != "posix" or os.geteuid() != 0:
            return
        try:
            import grp

            gid = grp.getgrnam(self.config.groups.cli).gr_gid
            os.chown(path, 0, gid)
        except (ImportError, KeyError):
            logger.warning("could not chown %s to group %s", path, self.config.groups.cli)

    def reconcile_once(self) -> dict[str, int]:
        if self._mode not in {"active", "maintenance"}:
            logger.info("reconcile skipped in %s mode", self._mode)
            return {"skipped": 1}
        with self._connection() as connection:
            return reconcile_allocations(connection, self.docker, now=self._now())

    async def expire_once(self) -> list[str]:
        if self._mode not in {"active", "maintenance"}:
            return []
        return await asyncio.to_thread(self._expire_once)

    def _expire_once(self) -> list[str]:
        with self._connection() as connection:
            expired = expire_allocations(connection, self.docker, now=self._now())
            store.expire_queue_requests(connection, now=self._now())
        for allocation_id in expired:
            self._cancel_tunnels(allocation_id)
        return expired

    async def _expiry_loop(self) -> None:
        elapsed = 0.0
        audit_elapsed = 0.0
        while True:
            await asyncio.sleep(self.expiry_interval)
            elapsed += self.expiry_interval
            audit_elapsed += self.expiry_interval
            try:
                fingerprint = await asyncio.to_thread(device_fingerprint)
            except Exception:
                logger.exception("could not fingerprint GPU device nodes")
                fingerprint = self._gpu_fingerprint
            due = self.config.policy.gpu_audit_required and (
                audit_elapsed >= self.config.policy.gpu_audit_interval
                or fingerprint != self._gpu_fingerprint
            )
            if due:
                audit_elapsed = 0.0
                try:
                    failure = await asyncio.to_thread(self._run_gpu_audit)
                    if failure:
                        logger.error(
                            "continuous GPU audit failed; new allocations are blocked: %s",
                            failure,
                        )
                    else:
                        logger.info("continuous GPU audit passed")
                except Exception:
                    self._gpu_audit_failure = "continuous GPU audit crashed"
                    logger.exception("continuous GPU audit crashed")
            if elapsed >= self.reconcile_interval:
                elapsed = 0.0
                try:
                    summary = await asyncio.to_thread(self.reconcile_once)
                    self._cancel_inactive_tunnels()
                    logger.info("periodic reconcile: %s", summary)
                except Exception:
                    logger.exception("periodic reconcile failed")
            try:
                expired = await self.expire_once()
                if expired:
                    logger.info("expired allocations: %s", ", ".join(expired))
            except Exception:
                logger.exception("expiry tick failed")
            if self._mode == "active":
                try:
                    admitted = await self._schedule_queue()
                    if admitted:
                        logger.info("queue admissions: %s", ", ".join(admitted))
                except Exception:
                    logger.exception("queue scheduling failed")

    async def stop(self) -> None:
        for allocation_id in list(self._tunnels):
            self._cancel_tunnels(allocation_id)
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            self._expiry_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.socket_path.exists():
            self.socket_path.unlink()
        for executor in (self._stream_read_executor, self._stream_control_executor):
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        self._stream_read_executor = None
        self._stream_control_executor = None

    async def run(self) -> None:
        await self.start()
        assert self._server is not None
        try:
            await self._server.serve_forever()
        finally:
            await self.stop()

    async def _send(self, writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
        payload = json.dumps(response, separators=(",", ":")) + "\n"
        writer.write(payload.encode("utf-8"))
        await writer.drain()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            sock = writer.get_extra_info("socket")
            pid, uid, gid = peer_credentials(sock)
        except Exception:
            logger.exception("could not read peer credentials")
            await self._send(writer, _error("forbidden", "peer identity is unavailable"))
            writer.close()
            await writer.wait_closed()
            return
        self._client_writers.setdefault(uid, set()).add(writer)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=CONNECTION_TIMEOUT)
                except TimeoutError:
                    break
                except ValueError:
                    await self._send(
                        writer,
                        _error("bad_request", "message is too large"),
                    )
                    break
                if not line:
                    break
                if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                    await self._send(
                        writer,
                        _error("bad_request", "message is too large or lacks a newline"),
                    )
                    break
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    await self._send(writer, _error("bad_request", "invalid JSON"))
                    continue
                if isinstance(payload, dict) and payload.get("cmd") == "shell":
                    await self._handle_shell(
                        payload,
                        reader=reader,
                        writer=writer,
                        uid=uid,
                        gid=gid,
                    )
                    break
                if isinstance(payload, dict) and payload.get("cmd") in {
                    "file_put",
                    "file_get",
                }:
                    await self._handle_file(
                        payload,
                        reader=reader,
                        writer=writer,
                        uid=uid,
                        gid=gid,
                    )
                    break
                if isinstance(payload, dict) and payload.get("cmd") == "tunnel":
                    await self._handle_tunnel(
                        payload,
                        reader=reader,
                        writer=writer,
                        uid=uid,
                        gid=gid,
                    )
                    break
                response = await self.handle_request(payload, uid=uid, gid=gid, pid=pid)
                await self._send(writer, response)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writers = self._client_writers.get(uid)
            if writers is not None:
                writers.discard(writer)
                if not writers:
                    self._client_writers.pop(uid, None)
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _close_member_connections(self, uid: int) -> int:
        writers = list(self._client_writers.get(uid, set()))
        for writer in writers:
            try:
                writer.close()
            except Exception:
                logger.exception("could not close broker stream for uid %s", uid)
        return len(writers)

    async def _handle_file(
        self,
        payload: Any,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid: int,
        gid: int,
    ) -> None:
        upload: workspace_io.UploadHandle | None = None
        download_fd: int | None = None
        committed = False
        try:
            identity = self.identity_resolver.resolve(uid, gid)
            with self._connection() as connection:
                store.ensure_user(
                    connection,
                    uid=identity.uid,
                    username=identity.username,
                    role=identity.role,
                    now=self._now(),
                )
            command, args = self._parse_request(payload)
            if command not in {"file_put", "file_get"}:
                raise BadRequest("expected file transfer command")
            self._check_args(args, {"id", "path"})
            if not identity.is_admin:
                with self._connection() as connection:
                    denial = self._membership_denial(connection, identity, command)
                if denial is not None:
                    raise Forbidden(denial["message"])
            if not self.config.policy.file_transfer_enabled:
                raise Forbidden("file transfer is disabled by policy")
            allocation_id = args.get("id")
            if not isinstance(allocation_id, str) or not allocation_id:
                raise BadRequest("id must be a non-empty string")
            with self._connection() as connection:
                allocation = store.get_allocation(connection, allocation_id, now=self._now())
            if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may transfer files")
            # Released workspaces remain on disk. Their owner may still fetch
            # non-log artifacts (checkpoints, metrics) without holding the GPU,
            # but uploads and every other state keep requiring a live
            # allocation.
            released_read = command == "file_get" and allocation["status"] == "released"
            if allocation["status"] != "active" and not released_read:
                raise Conflict(f"allocation is {allocation['status']}, not active")
            parts = workspace_io.relative_parts(args.get("path"))
            relative = PurePosixPath(*parts).as_posix()
            workspace = str(allocation["workspace"])
            if not workspace_io.supported():
                raise InternalError("descriptor-relative file access is unavailable")
            if command == "file_get":
                download_fd = await asyncio.to_thread(
                    workspace_io.open_read_file,
                    workspace,
                    parts,
                )
            else:
                owner_uid, owner_gid = self._allocation_owner_ids(allocation)
                upload = await asyncio.to_thread(
                    workspace_io.open_upload,
                    workspace,
                    parts,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
        except (KeyError, RuntimeError) as exc:
            if upload is not None:
                upload.abort()
            if download_fd is not None:
                os.close(download_fd)
            await self._send(writer, _error("forbidden", f"peer identity failed: {exc}"))
            return
        except CitespecError as exc:
            if upload is not None:
                upload.abort()
            if download_fd is not None:
                os.close(download_fd)
            await self._send(writer, _error(exc.code, exc.message))
            return
        except Exception:
            if upload is not None:
                upload.abort()
            if download_fd is not None:
                os.close(download_fd)
            logger.exception("could not prepare file transfer")
            await self._send(writer, _error("internal", "could not prepare file transfer"))
            return

        try:
            await self._send(
                writer,
                _ok({"id": allocation_id, "path": relative}),
            )
        except Exception:
            if upload is not None:
                upload.abort()
            if download_fd is not None:
                os.close(download_fd)
            raise
        if command == "file_get":
            try:
                assert download_fd is not None
                with os.fdopen(download_fd, "rb") as handle:
                    download_fd = None
                    while chunk := handle.read(65536):
                        await write_frame(writer, FRAME_DATA, chunk)
                await write_frame(
                    writer,
                    FRAME_EXIT,
                    json.dumps(
                        {"ok": True, "data": {"path": relative}},
                        sort_keys=True,
                    ).encode("utf-8"),
                )
                with self._connection() as connection:
                    store.record_event(
                        connection,
                        actor_uid=identity.uid,
                        action="file_get",
                        allocation_id=allocation_id,
                        detail={"path": relative},
                        now=self._now(),
                    )
                    connection.commit()
            except Exception:
                logger.exception("file get failed")
                try:
                    await write_frame(
                        writer,
                        FRAME_EXIT,
                        json.dumps(
                            {
                                "ok": False,
                                "error": {
                                    "code": "internal",
                                    "message": "file download failed",
                                },
                            },
                            sort_keys=True,
                        ).encode("utf-8"),
                    )
                except Exception:
                    pass
            finally:
                if download_fd is not None:
                    os.close(download_fd)
            return

        written = 0
        assert upload is not None
        try:
            while True:
                frame = await read_frame(reader)
                if frame is None:
                    raise BadRequest("file upload ended without an exit frame")
                kind, data = frame
                if kind == FRAME_DATA:
                    if written + len(data) > self.config.policy.max_upload_bytes:
                        raise BadRequest(
                            "upload exceeds policy.max_upload_bytes "
                            f"({self.config.policy.max_upload_bytes})"
                        )
                    view = memoryview(data)
                    while view:
                        piece = view[:65536]
                        count = os.write(upload.fd, piece)
                        if count <= 0:
                            raise InternalError("short write while uploading")
                        written += count
                        view = view[count:]
                elif kind == FRAME_EXIT:
                    break
                else:
                    raise BadRequest("unexpected file transfer frame")
            upload.commit()
            committed = True
            with self._connection() as connection:
                store.record_event(
                    connection,
                    actor_uid=identity.uid,
                    action="file_put",
                    allocation_id=allocation_id,
                    detail={"path": relative, "bytes": written},
                    now=self._now(),
                )
                connection.commit()
            await write_frame(
                writer,
                FRAME_EXIT,
                json.dumps(
                    {
                        "ok": True,
                        "data": {"path": relative, "bytes": written},
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            )
        except Exception as exc:
            if not committed:
                upload.abort()
            logger.exception("file put failed")
            error = exc if isinstance(exc, CitespecError) else InternalError(str(exc))
            try:
                await write_frame(
                    writer,
                    FRAME_EXIT,
                    json.dumps(
                        {
                            "ok": False,
                            "error": {"code": error.code, "message": error.message},
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
            except Exception:
                pass

    async def _handle_shell(
        self,
        payload: Any,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid: int,
        gid: int,
    ) -> None:
        task = asyncio.current_task()
        try:
            await self._handle_shell_inner(
                payload,
                reader=reader,
                writer=writer,
                uid=uid,
                gid=gid,
            )
        finally:
            self._release_shell(task)

    async def _handle_shell_inner(
        self,
        payload: Any,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid: int,
        gid: int,
    ) -> None:
        try:
            identity = self.identity_resolver.resolve(uid, gid)
            with self._connection() as connection:
                store.ensure_user(
                    connection,
                    uid=identity.uid,
                    username=identity.username,
                    role=identity.role,
                    now=self._now(),
                )
            command, args = self._parse_request(payload)
            if command != "shell":
                raise BadRequest("expected shell command")
            self._check_args(args, {"id", "cols", "rows", "job"})
            if not identity.is_admin:
                with self._connection() as connection:
                    denial = self._membership_denial(connection, identity, "shell")
                if denial is not None:
                    raise Forbidden(denial["message"])
            allocation_id = args.get("id")
            if not isinstance(allocation_id, str) or not allocation_id:
                raise BadRequest("id must be a non-empty string")
            cols = self._parse_terminal_dimension(args.get("cols", 80), "cols")
            rows = self._parse_terminal_dimension(args.get("rows", 24), "rows")
            with self._connection() as connection:
                allocation = store.get_allocation(
                    connection,
                    allocation_id,
                    now=self._now(),
                )
            if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may open this shell")
            if allocation["status"] != "active":
                raise Conflict(f"allocation is {allocation['status']}, not active")
            container_id = str(allocation["container_id"])
            if not container_id:
                raise NotFound("allocation has no container")
            task = asyncio.current_task()
            self._reserve_shell(task, identity.uid)
            shell_command: list[str] | None = None
            job = args.get("job")
            if job is not None:
                job_name = self._job_name(job)
                jobs = await self._stream_control(self.docker.list_jobs, container_id)
                if not any(item.get("name") == job_name for item in jobs):
                    raise NotFound(f"job not found: {job_name}")
                shell_command = [
                    "env",
                    "TERM=xterm-256color",
                    "tmux",
                    "attach-session",
                    "-t",
                    f"citespec-{job_name}",
                ]
            session = await self._stream_control(
                self.docker.open_shell,
                container_id,
                cols,
                rows,
                shell_command,
            )
        except (KeyError, RuntimeError) as exc:
            await self._send(writer, _error("forbidden", f"peer identity failed: {exc}"))
            return
        except CitespecError as exc:
            await self._send(writer, _error(exc.code, exc.message))
            return
        except Exception:
            logger.exception("could not open shell")
            await self._send(writer, _error("internal", "could not open shell"))
            return
        await self._send(
            writer,
            _ok({"id": allocation_id, "cols": cols, "rows": rows}),
        )
        await self._relay_shell(reader, writer, session)

    async def _relay_shell(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: ShellSession,
    ) -> None:
        async def pump_output() -> None:
            try:
                while True:
                    data = await self._stream_read(session.read, 65536)
                    if not data:
                        break
                    await write_frame(writer, FRAME_DATA, data)
                await write_frame(writer, FRAME_EXIT, exit_payload("shell exited"))
            except Exception:
                logger.exception("shell output pump failed")

        async def pump_input() -> None:
            while True:
                frame = await read_frame(reader)
                if frame is None:
                    return
                kind, payload = frame
                if kind == FRAME_DATA:
                    await self._stream_control(session.write, payload)
                elif kind == FRAME_RESIZE:
                    cols, rows = parse_resize(payload)
                    await self._stream_control(session.resize, cols, rows)
                elif kind == FRAME_EXIT:
                    return

        tasks = [
            asyncio.create_task(pump_output()),
            asyncio.create_task(pump_input()),
        ]
        try:
            _done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
        finally:
            try:
                await self._stream_control(session.close)
            except Exception:
                logger.warning("could not close shell session", exc_info=True)
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _parse_terminal_dimension(raw: Any, name: str) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise BadRequest(f"{name} must be an integer")
        if not 1 <= raw <= 1000:
            raise BadRequest(f"{name} is out of range")
        return raw

    def _container_home(self, username: str, uid: int) -> str:
        configured = self.config.policy.container_home.strip()
        if configured:
            home = configured
        else:
            safe = re.sub(r"[^a-z0-9_-]", "-", username.lower())
            home = f"/home/{safe or uid}"
        if ".." in PurePosixPath(home).parts or not home.startswith("/home/") or home == "/home/":
            raise Conflict("development container HOME must be under /home")
        return home

    @staticmethod
    def _member_public_keys(username: str) -> list[str]:
        if os.name != "posix":
            return []
        try:
            import pwd

            record = pwd.getpwnam(username)
            home = record.pw_dir
        except (ImportError, KeyError):
            return []
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = os.O_RDONLY | os.O_DIRECTORY | nofollow
        home_fd = ssh_fd = key_fd = -1
        try:
            home_fd = os.open(home, directory)
            ssh_fd = os.open(".ssh", directory, dir_fd=home_fd)
            key_fd = os.open("authorized_keys", os.O_RDONLY | nofollow, dir_fd=ssh_fd)
            info = os.fstat(key_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
                return []
            if info.st_uid not in {record.pw_uid, 0}:
                return []
            with os.fdopen(key_fd, "r", encoding="utf-8", errors="replace") as handle:
                key_fd = -1
                text = handle.read()
        except OSError:
            return []
        finally:
            for fd in (key_fd, ssh_fd, home_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        keys: list[str] = []
        key_types = ("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-")
        for line in text.splitlines():
            parts = line.strip().split()
            if not parts or parts[0].startswith("#"):
                continue
            for index, token in enumerate(parts):
                if token.startswith(key_types) and index + 1 < len(parts):
                    key = f"{token} {parts[index + 1]}"
                    if key not in keys:
                        keys.append(key)
                    break
        return keys[:20]

    def _cancel_tunnels(self, allocation_id: str) -> None:
        for task in list(self._tunnels.pop(allocation_id, set())):
            task.cancel()

    def _cancel_inactive_tunnels(self) -> None:
        """Close tunnels whose allocation is no longer active after reconcile."""

        if not self._tunnels:
            return
        try:
            with self._connection() as connection:
                active = {
                    str(row["id"])
                    for row in store.list_allocations(
                        connection,
                        statuses=("active",),
                        now=self._now(),
                    )
                }
        except Exception:
            logger.exception("could not check active allocations for tunnel cleanup")
            return
        for allocation_id in list(self._tunnels):
            if allocation_id not in active:
                logger.info("closing tunnels for inactive allocation %s", allocation_id)
                self._cancel_tunnels(allocation_id)

    async def _handle_tunnel(
        self,
        payload: Any,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid: int,
        gid: int,
    ) -> None:
        task = asyncio.current_task()
        allocation_id = ""
        session: ShellSession | None = None
        try:
            try:
                if not self.config.policy.vscode_enabled:
                    raise Forbidden("VS Code tunnels are disabled by policy")
                if self.config.policy.container_profile != "development":
                    raise Conflict("VS Code tunnels require the development container profile")
                try:
                    identity = self.identity_resolver.resolve(uid, gid)
                except (KeyError, RuntimeError) as exc:
                    raise Forbidden(f"peer identity failed: {exc}") from exc
                if not identity.is_admin:
                    with self._connection() as connection:
                        denial = self._membership_denial(connection, identity, "tunnel")
                    if denial is not None:
                        raise Forbidden(denial["message"])
                with self._connection() as connection:
                    store.ensure_user(
                        connection,
                        uid=identity.uid,
                        username=identity.username,
                        role=identity.role,
                        now=self._now(),
                    )
                command, args = self._parse_request(payload)
                if command != "tunnel":
                    raise BadRequest("expected tunnel command")
                self._check_args(args, {"id"})
                raw_id = args.get("id")
                if not isinstance(raw_id, str) or not raw_id:
                    raise BadRequest("id must be a non-empty string")
                allocation_id = raw_id
                with self._connection() as connection:
                    allocation = store.get_allocation(
                        connection,
                        allocation_id,
                        now=self._now(),
                    )
                if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                    raise Forbidden("only the owner or an admin may open this tunnel")
                if allocation["status"] != "active":
                    raise Conflict(f"allocation is {allocation['status']}, not active")
                container_id = str(allocation["container_id"])
                if not container_id:
                    raise NotFound("allocation has no container")

                # Reserve the slot before the first blocking call. The check and
                # insert are synchronous in the event loop, so concurrent
                # connections cannot all observe the same free count.
                active = self._tunnels.setdefault(allocation_id, set())
                if task is not None:
                    active.discard(task)
                if len(active) >= self.config.policy.vscode_max_tunnels:
                    raise Conflict("too many open VS Code tunnels for this allocation")
                self._reserve_tunnel(task, identity.uid)
                if task is not None:
                    active.add(task)

                owner_uid, owner_gid = self._allocation_owner_ids(allocation)
                # Use the allocation owner for the container passwd entry and
                # HOME. The CLI connects as this user; an administrator opening
                # the tunnel acts as the owner and only contributes a key.
                container_user = str(allocation["owner"])
                if not container_user:
                    raise NotFound("allocation owner is missing")
                if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", container_user):
                    raise Conflict(
                        "allocation owner name is not supported by the container SSH profile"
                    )
                # Authorize the allocation owner plus the current connecter:
                # an administrator opening a member's allocation must not
                # revoke the member's key material, and vice versa.
                # Authoritative key projection (goal §8.3): when the member
                # record exists, its current active key set is authoritative —
                # revoked historical keys are no longer projected. The host
                # authorized_keys fallback covers pre-membership accounts.
                with self._connection() as connection:
                    owner_member_keys = (
                        member_store.active_keys(connection, int(owner_uid))
                        if self.config.policy.membership_required and int(owner_uid) > 0
                        else []
                    )
                    connecter_member_keys = (
                        member_store.active_keys(connection, identity.uid)
                        if self.config.policy.membership_required
                        and identity.uid != int(owner_uid)
                        and not identity.is_admin
                        else []
                    )
                connecter_keys = connecter_member_keys or (
                    await self._stream_control(self._member_public_keys, identity.username)
                )
                owner_keys = owner_member_keys or await self._stream_control(
                    self._member_public_keys,
                    container_user,
                )
                keys: list[str] = []
                for key in [*owner_keys, *connecter_keys]:
                    key = key.strip()
                    if key and key not in keys:
                        keys.append(key)
                if not keys:
                    raise Conflict("no SSH public keys found for this allocation on the host")
                home = self._container_home(container_user, owner_uid)
                await self._stream_control(
                    self.docker.prepare_container_ssh,
                    container_id,
                    owner_uid,
                    owner_gid,
                    container_user,
                    keys,
                    home,
                )
                session = await self._stream_control(self.docker.open_tunnel, container_id)
            except CitespecError as exc:
                await self._send(writer, _error(exc.code, exc.message))
                return
            except Exception:
                logger.exception("could not open VS Code tunnel")
                await self._send(writer, _error("internal", "could not open VS Code tunnel"))
                return

            active_session = session
            assert active_session is not None
            try:
                await self._send(writer, _ok({"id": allocation_id, "protocol": "ssh"}))
                await self._relay_tunnel(reader, writer, active_session)
            finally:
                try:
                    await self._stream_control(active_session.close)
                except Exception:
                    logger.warning("could not close VS Code tunnel session", exc_info=True)
        finally:
            self._release_tunnel(task)
            if task is not None:
                remaining = self._tunnels.get(allocation_id)
                if remaining is not None:
                    remaining.discard(task)
                    if not remaining:
                        self._tunnels.pop(allocation_id, None)

    async def _relay_tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: ShellSession,
    ) -> None:
        async def pump_input() -> None:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                await self._stream_control(session.write, data)

        async def pump_output() -> None:
            while True:
                data = await self._stream_read(session.read, 65536)
                if not data:
                    return
                writer.write(data)
                await writer.drain()

        tasks = [
            asyncio.create_task(pump_input()),
            asyncio.create_task(pump_output()),
        ]
        try:
            _done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in tasks:
                task.cancel()
            # A blocking recv() cannot be cancelled directly. Closing the
            # session socket unblocks the worker before we join it.
            try:
                await self._stream_control(session.close)
            except Exception:
                logger.warning("could not close tunnel session", exc_info=True)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle_request(
        self,
        payload: Any,
        *,
        uid: int,
        gid: int,
        pid: int = 0,
    ) -> dict[str, Any]:
        try:
            identity = self.identity_resolver.resolve(uid, gid)
        except (KeyError, RuntimeError) as exc:
            return _error("forbidden", f"peer identity could not be resolved: {exc}")
        try:
            with self._connection() as connection:
                store.ensure_user(
                    connection,
                    uid=identity.uid,
                    username=identity.username,
                    role=identity.role,
                    now=self._now(),
                )
            command, args = self._parse_request(payload)
            if command == "ping":
                self._check_args(args)
                return _ok(
                    {
                        "uid": identity.uid,
                        "username": identity.username,
                        "role": identity.role,
                        "admin": identity.is_admin,
                        "pid": pid,
                        "mode": self._mode,
                    }
                )
            if command == "me":
                self._check_args(args)
                with self._connection() as connection:
                    decision = member_store.check_access(connection, identity.uid, now=self._now())
                    keys = (
                        member_store.list_keys(connection, str(decision["member_id"]))
                        if decision["member_id"]
                        else []
                    )
                return _ok(
                    {
                        "uid": identity.uid,
                        "username": identity.username,
                        "admin": identity.is_admin,
                        "membership": {
                            "status": ("admin" if identity.is_admin else decision["status"]),
                            "reason": decision["reason"],
                            "valid_until": decision["valid_until"],
                            "access_version": decision["access_version"],
                        },
                        "keys": [
                            {
                                "key_id": key["key_id"],
                                "fingerprint": key["fingerprint"],
                                "status": key["status"],
                            }
                            for key in keys
                        ],
                    }
                )
            if command.startswith("portal_"):
                return _ok(await self._handle_portal_command(command, identity, args))
            # Mode and membership are independent gates.  Maintenance/monitor
            # never bypasses membership, and membership never overrides the
            # mode matrix that blocks new work.  Admin operations keep their
            # trusted path and are checked by _require_admin below.
            if not identity.is_admin:
                with self._connection() as connection:
                    denial = self._membership_denial(connection, identity, command)
                if denial is not None:
                    return _error(denial["code"], denial["message"])
            if command == "list":
                self._check_args(args)
                return _ok(await asyncio.to_thread(self._list))
            if command == "status":
                self._check_args(args, {"id"})
                return _ok(self._status(identity, args))
            if command == "apply":
                self._check_args(
                    args,
                    {
                        "gpus",
                        "gpu_count",
                        "cpus",
                        "memory_gb",
                        "hours",
                        "image",
                        "note",
                        "queue",
                        "idempotency_key",
                    },
                )
                return _ok(await self._apply(identity, args))
            if command == "queue_list":
                self._check_args(args, {"all", "include_finished"})
                return _ok(self._queue_list(identity, args))
            if command == "queue_get":
                self._check_args(args, {"id"})
                return _ok(self._queue_get(identity, args))
            if command == "queue_cancel":
                self._check_args(args, {"id", "reason"})
                return _ok(self._queue_cancel(identity, args))
            if command == "release":
                self._check_args(args, {"id"})
                return _ok(await self._release(identity, args))
            if command == "logs":
                self._check_args(args, {"id", "tail"})
                return _ok(await self._logs(identity, args))
            if command == "job_start":
                self._check_args(args, {"id", "name", "command"})
                return _ok(await self._job_start(identity, args))
            if command == "job_list":
                self._check_args(args, {"id"})
                return _ok(await self._job_list(identity, args))
            if command == "job_files":
                self._check_args(args, {"id"})
                return _ok(await self._job_files(identity, args))
            if command == "job_logs":
                self._check_args(args, {"id", "name", "tail"})
                return _ok(await self._job_logs(identity, args))
            if command == "job_stop":
                self._check_args(args, {"id", "name"})
                return _ok(await self._job_stop(identity, args))
            if command == "admin_users":
                self._check_args(args)
                self._require_admin(identity)
                return _ok(self._admin_users())
            if command == "admin_resources":
                self._check_args(args)
                self._require_admin(identity)
                return _ok(await asyncio.to_thread(self._admin_resources))
            if command == "admin_audit":
                self._check_args(args, {"limit"})
                self._require_admin(identity)
                return _ok(self._admin_audit(args))
            if command == "admin_mode":
                self._check_args(args, {"mode"})
                self._require_admin(identity)
                return _ok(await self._admin_mode(identity, args))
            if command == "admin_release":
                self._check_args(args, {"id", "reason"})
                self._require_admin(identity)
                return _ok(await self._release(identity, args, admin=True))
            if command == "admin_reconcile":
                self._check_args(args)
                self._require_admin(identity)
                summary = await asyncio.to_thread(self.reconcile_once)
                self._cancel_inactive_tunnels()
                return _ok(summary)
            if command in {
                "admin_portal_accounts_list",
                "admin_portal_accounts_create",
                "admin_portal_accounts_set_role",
            }:
                self._require_admin(identity)
                return _ok(
                    await asyncio.to_thread(
                        self._handle_portal_admin_command,
                        command,
                        identity,
                        args,
                    )
                )
            if command in {
                "admin_portal_auth_list",
                "admin_portal_auth_enroll",
                "admin_portal_auth_remove",
            }:
                self._require_admin(identity)
                return _ok(
                    await asyncio.to_thread(
                        self._handle_portal_admin_auth_command,
                        command,
                        identity,
                        args,
                    )
                )
            if command in {
                "admin_requests_list",
                "admin_requests_show",
                "admin_requests_approve",
                "admin_requests_reject",
                "admin_requests_needs_info",
                "admin_members_list",
                "admin_members_show",
                "admin_members_keys_add",
                "admin_members_keys_revoke",
                "admin_members_import_legacy",
                "admin_members_suspend",
                "admin_members_resume",
                "admin_members_revoke",
                "admin_operations_show",
                "admin_operations_retry",
                "me_keys_add",
                "me_keys_revoke",
            }:
                return _ok(await self._handle_membership_command(command, identity, args))
            raise BadRequest(f"unknown command: {command}")
        except CitespecError as exc:
            return _error(exc.code, exc.message)
        except Exception:
            logger.exception("unhandled broker request failure")
            return _error("internal", "internal broker error")

    def _parse_request(self, payload: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise BadRequest("request must be a JSON object")
        unknown = set(payload) - {"cmd", "args"}
        if unknown:
            raise BadRequest(f"unexpected request field(s): {', '.join(sorted(unknown))}")
        command = payload.get("cmd")
        if not isinstance(command, str) or not command:
            raise BadRequest("request cmd must be a non-empty string")
        args = payload.get("args", {})
        if not isinstance(args, dict):
            raise BadRequest("request args must be a JSON object")
        return command, args

    @staticmethod
    def _check_args(args: dict[str, Any], allowed: set[str] | None = None) -> None:
        extra = set(args) - (allowed or set())
        if extra:
            raise BadRequest(f"unexpected argument(s): {', '.join(sorted(extra))}")

    def _list(self) -> dict[str, Any]:
        now = self._now()
        live = self._live_resources()
        with self._connection() as connection:
            persisted = store.get_mode(connection, self._mode)
            if persisted == "monitor" and (
                store.active_lease_count(connection)
                or store.queue_request_count(connection, now=now)
            ):
                logger.warning(
                    "promoting monitor to maintenance while active/pending/queued work exists"
                )
                store.set_mode(connection, "maintenance", actor_uid=0, now=now)
                persisted = "maintenance"
            self._mode = persisted
            db_gpus = store.list_gpus(connection, now=now)
            allocations = store.list_allocations(connection, now=now)
            queue = store.list_queue_requests(connection, now=now)
            active_allocations = self._allocation_map(connection, now)
        data: dict[str, Any] = {
            "server": self.config.server.name,
            "mode": self._mode,
            "now": now.isoformat(),
            "detection_error": live.error,
            "gpus": self._merge_gpus(db_gpus, live, active_allocations),
            "allocations": allocations,
            "queue": queue,
            "unmanaged": self._unmanaged(db_gpus, active_allocations, live),
            "gpu_audit": {
                "required": self.config.policy.gpu_audit_required,
                "ok": self._gpu_audit_failure is None,
                "message": self._gpu_audit_failure or "",
            },
            "workspace_quota": {
                "required": self.config.policy.workspace_quota_required,
                "ok": self._quota_failure is None,
                "message": self._quota_failure or "",
            },
            "storage": {
                "required": (
                    self.config.policy.container_profile == "development"
                    or self.config.policy.min_free_docker_gb > 0
                ),
                "ok": self._storage_failure is None,
                "message": self._storage_failure or "",
            },
            "capabilities": self._capabilities(),
            **version_info(),
        }
        self._add_member_identity(data)
        return data

    def _load_member_directory(self) -> dict[str, Any]:
        try:
            return load_members(self.config.server.members_file)
        except (OSError, ValueError) as exc:
            logger.warning(
                "could not load member directory %s: %s",
                self.config.server.members_file,
                exc,
            )
            return {}

    def _add_member_identity(self, data: dict[str, Any]) -> None:
        members = self._load_member_directory()
        if not members:
            return
        public = {username: public_member_data(members, username) for username in members}
        data["members"] = public

        def enrich(
            item: dict[str, Any],
            username: Any,
            *,
            prefix: str = "owner",
        ) -> None:
            if not isinstance(username, str):
                return
            profile = public.get(username)
            if not profile:
                return
            if profile.get("display_name"):
                item[f"{prefix}_display"] = profile["display_name"]
            if profile.get("nickname"):
                item[f"{prefix}_nickname"] = profile["nickname"]
            if profile.get("public_contact"):
                item[f"{prefix}_public_contact"] = profile["public_contact"]

        for allocation in data.get("allocations", []):
            enrich(allocation, allocation.get("owner"))
        for request in data.get("queue", []):
            enrich(request, request.get("owner"))
        for gpu in data.get("gpus", []):
            enrich(gpu, gpu.get("owner"))
        unmanaged = data.get("unmanaged") or {}
        for container in unmanaged.get("containers", []):
            enrich(container, container.get("owner"))
        for process in unmanaged.get("processes", []):
            enrich(process, process.get("username"))
            enrich(process, process.get("container_owner"), prefix="container_owner")

    def _unmanaged(
        self,
        gpus: list[dict[str, Any]],
        allocations: dict[str, dict[str, Any]],
        live: LiveResources,
    ) -> dict[str, list[dict[str, Any]]]:
        container_by_uuid = {
            str(gpu["uuid"]): str(gpu["container_id"]) for gpu in gpus if gpu["container_id"]
        }
        index_by_uuid = {str(gpu["uuid"]): int(gpu["index"]) for gpu in gpus}
        unmanaged_containers: list[dict[str, Any]] = []
        for container in live.containers:
            allocation_id = container.labels.get("citespec.allocation")
            if self._managed_container_matches(container, allocations, gpus):
                continue
            resolved, unknown_ref = self._resolve_gpu_refs(container.gpu_refs, gpus)
            indices = sorted({int(gpu["index"]) for gpu in gpus if str(gpu["uuid"]) in resolved})
            active_gpu_access = (
                bool(indices) or unknown_ref
            ) and container.status in ACTIVE_GPU_HOLDER_STATUSES
            unmanaged_containers.append(
                {
                    "id": container.id,
                    "name": container.name,
                    "image": container.image,
                    "status": container.status,
                    "allocation_id": allocation_id,
                    "owner": container.labels.get("citespec.owner"),
                    "gpu_refs": list(container.gpu_refs),
                    "gpus": indices,
                    "gpu_unknown": unknown_ref,
                    "active_gpu_access": active_gpu_access,
                }
            )
        unmanaged_processes: list[dict[str, Any]] = []
        for process in live.processes:
            container_id = container_by_uuid.get(process.gpu_uuid)
            if container_id and self._pid_in_container(process.pid, container_id):
                continue
            process_container = self._pid_container(process.pid, live.containers)
            unmanaged_processes.append(
                {
                    "gpu_uuid": process.gpu_uuid,
                    "gpu_index": index_by_uuid.get(process.gpu_uuid),
                    "pid": process.pid,
                    "used_mib": process.used_mib,
                    "process_name": process.process_name,
                    "uid": process.uid,
                    "username": process.username,
                    "container_id": (process_container.id if process_container else None),
                    "container_name": (process_container.name if process_container else None),
                    "container_owner": (
                        process_container.labels.get("citespec.owner")
                        if process_container
                        else None
                    ),
                }
            )
        return {
            "containers": unmanaged_containers,
            "processes": unmanaged_processes,
        }

    @staticmethod
    def _pid_container(pid: int, containers: list[ContainerInfo]) -> ContainerInfo | None:
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None
        for container in containers:
            if container.id and container.id in cgroup:
                return container
        return None

    @staticmethod
    def _pid_in_container(pid: int, container_id: str) -> bool:
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return False
        return container_id in cgroup

    def _queue_list(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        all_users = args.get("all", False)
        include_finished = args.get("include_finished", False)
        if not isinstance(all_users, bool) or not isinstance(include_finished, bool):
            raise BadRequest("all and include_finished must be booleans")
        if all_users:
            self._require_admin(identity)
        with self._connection() as connection:
            queue = store.list_queue_requests(
                connection,
                owner_uid=None if all_users else identity.uid,
                include_finished=include_finished,
                now=self._now(),
            )
        return {"queue": queue}

    def _queue_get(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        queue_id = args.get("id")
        if not isinstance(queue_id, str) or not queue_id:
            raise BadRequest("id must be a non-empty string")
        with self._connection() as connection:
            queue = store.get_queue_request(connection, queue_id, now=self._now())
            if queue["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may inspect this queue request")
            result: dict[str, Any] = {"queue": queue}
            allocation_id = queue.get("allocation_id")
            if isinstance(allocation_id, str) and allocation_id:
                try:
                    result["allocation"] = store.get_allocation(
                        connection,
                        allocation_id,
                        now=self._now(),
                    )
                    result["events"] = store.list_allocation_events(connection, allocation_id)
                except NotFound:
                    pass
        return result

    def _queue_cancel(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        queue_id = args.get("id")
        if not isinstance(queue_id, str) or not queue_id:
            raise BadRequest("id must be a non-empty string")
        reason = args.get("reason", "")
        if not isinstance(reason, str):
            raise BadRequest("reason must be a string")
        with self._connection() as connection:
            queue = store.get_queue_request(connection, queue_id, now=self._now())
            if queue["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may cancel this queue request")
            store.cancel_queue_request(
                connection,
                queue_id,
                actor_uid=identity.uid,
                reason=reason,
                now=self._now(),
            )
            return store.get_queue_request(connection, queue_id, now=self._now())

    def _status(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation_id = args.get("id")
        if allocation_id is not None and not isinstance(allocation_id, str):
            raise BadRequest("id must be a string")
        with self._connection() as connection:
            if allocation_id is None:
                return {
                    "allocations": store.list_allocations(
                        connection, owner_uid=identity.uid, now=self._now()
                    ),
                    "capabilities": self._capabilities(),
                    **version_info(),
                }
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
            if allocation["owner_uid"] != identity.uid and not identity.is_admin:
                raise Forbidden("only the owner or an admin may inspect this allocation")
            return {
                "allocation": allocation,
                "events": store.list_allocation_events(connection, allocation_id),
                "capabilities": self._capabilities(),
                **version_info(),
            }

    async def _apply(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        self._require_active()
        if self.config.policy.gpu_audit_required:
            gpu_failure = await asyncio.to_thread(self._run_gpu_audit)
            if gpu_failure:
                raise Conflict(f"new allocations blocked by GPU permission audit: {gpu_failure}")
        if self.config.policy.workspace_quota_required or self.config.policy.max_workspace_gb > 0:
            quota_failure = await asyncio.to_thread(self._run_quota_check)
            if quota_failure:
                raise Conflict(f"new allocations blocked by workspace quota: {quota_failure}")
        storage_failure = await asyncio.to_thread(
            self._run_storage_check,
            include_next_slot=True,
        )
        if storage_failure:
            raise Conflict(f"new allocations blocked by Docker storage: {storage_failure}")
        gate_failure = self._gate_failure()
        if gate_failure:
            raise Conflict(f"new allocations blocked by host safety gate: {gate_failure}")
        gpus = self._parse_gpus(args.get("gpus", []))
        gpu_count = args.get("gpu_count")
        if gpu_count is not None:
            if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 0:
                raise BadRequest("gpu_count must be a non-negative integer")
            if gpu_count > 8:
                raise BadRequest("gpu_count must be at most 8")
            if gpus:
                raise BadRequest("gpus 与 gpu_count 只能二选一")
        auto_request = gpu_count is not None
        requested_gpu_count = int(gpu_count) if gpu_count is not None else 0
        cpus = self._parse_positive_float(args.get("cpus"), "cpus")
        memory_gb = self._parse_positive_int(args.get("memory_gb"), "memory_gb")
        hours = self._parse_positive_int(
            args.get("hours", self.config.policy.default_hours), "hours"
        )
        if not 1 <= hours <= self.config.policy.max_hours:
            raise BadRequest(f"hours must be between 1 and {self.config.policy.max_hours}")
        if cpus > self.config.policy.max_cpus_per_allocation:
            raise InsufficientResources(
                "requested cpus exceed policy.max_cpus_per_allocation "
                f"({self.config.policy.max_cpus_per_allocation:g})"
            )
        if memory_gb > self.config.policy.max_memory_gb_per_allocation:
            raise InsufficientResources(
                "requested memory exceeds policy.max_memory_gb_per_allocation "
                f"({self.config.policy.max_memory_gb_per_allocation})"
            )
        queue_requested = args.get("queue", self.config.policy.queue_enabled)
        if not isinstance(queue_requested, bool):
            raise BadRequest("queue must be a boolean")
        image = args.get("image", self.config.image.base)
        if not isinstance(image, str) or not image:
            raise BadRequest("image must be a non-empty string")
        if not identity.is_admin and not self.config.image.image_allowed(image):
            raise Forbidden("members may only use images on the configured allowlist")
        note = args.get("note", "")
        if not isinstance(note, str):
            raise BadRequest("note must be a string")
        workspace = self._workspace(identity.username)
        starts_at = self._now()
        expires_at = starts_at + timedelta(hours=hours)
        idempotency_key = args.get("idempotency_key", "")
        if not isinstance(idempotency_key, str) or len(idempotency_key) > 128:
            raise BadRequest("idempotency_key must be a string of at most 128 characters")
        with self._connection() as connection:
            membership_valid_until = (
                member_store.member_valid_until(connection, identity.uid)
                if self.config.policy.membership_required and not identity.is_admin
                else ""
            )
        busy_reason: str | None = None
        if gpus:
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                gpu_rows = store.gpus_for_indices(connection, gpus)
                active_allocations = self._allocation_map(connection, now)
            live = await asyncio.to_thread(self._live_resources)
            merged = self._merge_gpus(db_gpus, live, active_allocations)
            by_index = {int(gpu["index"]): gpu for gpu in merged}
            for index in gpus:
                gpu = by_index.get(index)
                if gpu is None:
                    raise NotFound(f"unknown GPU index: {index}")
                state = str(gpu["state"])
                if state == "unknown":
                    raise DockerError(f"cannot determine GPU {index} state: {gpu['state_reason']}")
                if state == "unmanaged":
                    busy_reason = f"GPU {index} is unmanaged: {gpu['state_reason']}"
                elif state not in {"free", "allocated"}:
                    busy_reason = f"GPU {index} is {state}: {gpu['state_reason']}"
            gpu_uuids = [str(row["uuid"]) for row in gpu_rows]
        elif gpu_count:
            # Server-side auto-selection (§3.2): the broker picks free GPUs
            # during admission, never the client from a stale snapshot.
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                active_allocations = self._allocation_map(connection, now)
            live = await asyncio.to_thread(self._live_resources)
            if live.error:
                raise DockerError(f"cannot determine GPU state for selection: {live.error}")
            merged = self._merge_gpus(db_gpus, live, active_allocations)
            free_indices = sorted(int(gpu["index"]) for gpu in merged if gpu["state"] == "free")
            if len(free_indices) < gpu_count:
                busy_reason = (
                    f"当前空闲 GPU 数量不足：需要 {gpu_count} 张，当前空闲 {len(free_indices)} 张"
                )
            gpus = free_indices[:gpu_count]
            with self._connection() as connection:
                gpu_rows = store.gpus_for_indices(connection, gpus) if gpus else []
            gpu_uuids = [str(row["uuid"]) for row in gpu_rows]
        else:
            gpu_uuids = []
        if self.config.policy.min_free_gb:
            disk_path = Path(self.config.server.workspace_root)
            while not disk_path.exists() and disk_path != disk_path.parent:
                disk_path = disk_path.parent
            try:
                usage = shutil.disk_usage(disk_path)
            except OSError as exc:
                raise InternalError(f"could not inspect workspace disk: {exc}") from exc
            free_gb = usage.free // (1024**3)
            if free_gb < self.config.policy.min_free_gb:
                raise InsufficientResources(
                    f"workspace disk has only {free_gb} GiB free; "
                    f"policy.min_free_gb is {self.config.policy.min_free_gb}"
                )
        with self._connection() as connection:
            pending_before = store.queue_request_count(connection, now=self._now())
        if pending_before:
            if not self.config.policy.queue_enabled:
                raise Conflict(
                    "queued requests are pending while queue_enabled is false; "
                    "an administrator must resolve the queue"
                )
            await self._schedule_queue_safely("direct_apply")
            with self._connection() as connection:
                pending_after = store.queue_request_count(connection, now=self._now())
            if pending_after:
                reason = busy_reason or "queued requests are pending"
                if not queue_requested:
                    raise GpuBusy(f"{reason}; --no-queue cannot bypass queued requests")
                return await self._enqueue_allocation(
                    identity,
                    gpus=[] if auto_request else gpus,
                    requested_gpu_count=requested_gpu_count,
                    idempotency_key=idempotency_key,
                    cpus=cpus,
                    memory_gb=memory_gb,
                    hours=hours,
                    image=image,
                    workspace=workspace,
                    note=note,
                    reason=reason,
                )
        if busy_reason is not None:
            if not (self.config.policy.queue_enabled and queue_requested):
                raise GpuBusy(busy_reason)
            return await self._enqueue_allocation(
                identity,
                gpus=[] if auto_request else gpus,
                requested_gpu_count=requested_gpu_count,
                idempotency_key=idempotency_key,
                cpus=cpus,
                memory_gb=memory_gb,
                hours=hours,
                image=image,
                workspace=workspace,
                note=note,
                reason=busy_reason,
            )
        try:
            with self._connection() as connection:
                allocation_id = store.create_allocation(
                    connection,
                    owner_uid=identity.uid,
                    owner_name=identity.username,
                    gpu_uuids=gpu_uuids,
                    cpus=cpus,
                    memory_gb=memory_gb,
                    image=image,
                    workspace=workspace,
                    note=note,
                    starts_at=starts_at,
                    expires_at=expires_at,
                    max_cpus=self.config.policy.global_max_cpus,
                    max_memory_gb=self.config.policy.global_max_memory_gb,
                    max_cpus_per_user=self.config.policy.max_cpus_per_user,
                    max_memory_gb_per_user=self.config.policy.max_memory_gb_per_user,
                    max_gpus_per_user=self.config.policy.max_gpus_per_user,
                    max_active_allocations_per_user=(
                        self.config.policy.max_active_allocations_per_user
                    ),
                    idempotency_key=idempotency_key,
                    membership_valid_until=membership_valid_until,
                    now=self._now(),
                    **self._container_storage_params(),
                )
                existing = store.get_allocation(connection, allocation_id, now=self._now())
        except (GpuBusy, InsufficientResources) as exc:
            if not (self.config.policy.queue_enabled and queue_requested):
                raise
            return await self._enqueue_allocation(
                identity,
                gpus=[] if auto_request else gpus,
                requested_gpu_count=requested_gpu_count,
                idempotency_key=idempotency_key,
                cpus=cpus,
                memory_gb=memory_gb,
                hours=hours,
                image=image,
                workspace=workspace,
                note=note,
                reason=str(exc),
            )
        # Idempotent replay (§3.3): a retried apply returns the original
        # allocation without re-materializing anything.
        if existing["status"] != "pending":
            if idempotency_key:
                existing["idempotency_key"] = idempotency_key
            return existing
        materialized = await self._materialize_allocation(allocation_id)
        if idempotency_key:
            materialized["idempotency_key"] = idempotency_key
        return materialized

    async def _enqueue_allocation(
        self,
        identity: Identity,
        *,
        gpus: list[int],
        cpus: float,
        memory_gb: int,
        hours: int,
        image: str,
        workspace: str,
        note: str,
        reason: str,
        requested_gpu_count: int = 0,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        with self._connection() as connection:
            queue_id = store.create_queue_request(
                connection,
                owner_uid=identity.uid,
                owner_name=identity.username,
                requested_gpus=gpus,
                requested_gpu_count=requested_gpu_count,
                idempotency_key=idempotency_key,
                cpus=cpus,
                memory_gb=memory_gb,
                hours=hours,
                image=image,
                workspace=workspace,
                note=note,
                max_queue_length=self.config.policy.max_queue_length,
                max_queued_allocations_per_user=(
                    self.config.policy.max_queued_allocations_per_user
                ),
                max_queue_hours=self.config.policy.max_queue_hours,
                now=self._now(),
            )
        logger.info(
            "queued allocation request %s for %s after: %s",
            queue_id,
            identity.username,
            reason,
        )
        await self._schedule_queue_safely("apply")
        with self._connection() as connection:
            queued = store.get_queue_request(connection, queue_id, now=self._now())
        if idempotency_key:
            queued["idempotency_key"] = idempotency_key
        return queued

    def _verify_member_workspace_quota(self, workspace: str, owner_uid: int) -> None:
        """Per-allocation gate: the member directory must really be bounded.

        ``check_workspace_quota`` only proves the filesystem *supports* project
        quotas. This verifies the member's own project ID, project inheritance,
        and hard limit, provisioning them on first use so a workspace created
        after ``configure_quotas.sh`` ran can never slip through unbounded.
        """

        if (
            not self.config.policy.workspace_quota_required
            or self.config.policy.max_workspace_gb <= 0
        ):
            return
        if owner_uid <= 0:
            logger.warning("skipping member workspace quota verification for UID %d", owner_uid)
            return
        limit_gb = self.config.policy.max_workspace_gb
        result = self.member_quota_check(workspace, owner_uid, limit_gb)
        if result.passed:
            return
        logger.warning(
            "member workspace %s is not quota-bounded (%s); reprovisioning",
            workspace,
            result.detail,
        )
        provisioned = self.member_quota_provision(workspace, owner_uid, limit_gb)
        if not provisioned.passed:
            raise Conflict(f"workspace quota could not be enforced: {provisioned.detail}")

    async def _materialize_allocation(self, allocation_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            allocation_data = store.get_allocation(
                connection,
                allocation_id,
                now=self._now(),
            )
            starts_at = datetime.fromisoformat(str(allocation_data["starts_at"]))
            expires_at = datetime.fromisoformat(str(allocation_data["expires_at"]))
            owner_uid = int(allocation_data["owner_uid"])
            owner_gid = owner_uid
        if os.name == "posix":
            try:
                import pwd

                owner_gid = pwd.getpwuid(owner_uid).pw_gid
            except (ImportError, KeyError):
                pass
        try:
            await asyncio.to_thread(
                workspace_io.ensure_workspace,
                str(allocation_data["workspace"]),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            await asyncio.to_thread(
                self._verify_member_workspace_quota,
                str(allocation_data["workspace"]),
                owner_uid,
            )
        except Exception as exc:
            with self._connection() as connection:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason=f"workspace preparation failed: {exc}",
                    now=self._now(),
                )
            if isinstance(exc, CitespecError):
                raise
            raise InternalError(f"workspace preparation failed: {exc}") from exc
        allocation = self._allocation_model(allocation_data, starts_at, expires_at)
        try:
            container = await asyncio.to_thread(self.docker.create_container, allocation)
        except Exception as exc:
            logger.exception("container creation failed for %s", allocation_id)
            with self._connection() as connection:
                store.fail_allocation(
                    connection,
                    allocation_id,
                    reason=f"docker create failed: {exc}",
                    now=self._now(),
                )
            raise DockerError(f"container creation failed: {exc}") from exc
        # Record the container reference before any later step can fail, so a
        # restarted broker can still find and clean up the orphan.
        try:
            with self._connection() as connection:
                store.record_container(
                    connection,
                    allocation_id,
                    container_id=container.id,
                    container_name=container.name,
                    now=self._now(),
                )
        except Exception:
            logger.exception("could not record container %s", container.id)

        async def _fail_activated_container(step: str, exc: Exception) -> NoReturn:
            logger.exception("%s failed for %s", step, allocation_id)
            cleanup_failed = False
            try:
                await asyncio.to_thread(self.docker.remove_container, container.id)
            except Exception:
                cleanup_failed = True
                logger.exception(
                    "could not remove container %s after %s failure",
                    container.id,
                    step,
                )
            with self._connection() as connection:
                if cleanup_failed:
                    # The orphan container may still be running: keep the GPU
                    # claims and CPU/memory/storage reservations (non-terminal
                    # state) until reconciliation confirms the removal.
                    store.mark_cleanup_pending(
                        connection,
                        allocation_id,
                        reason=f"{step} failed: {exc}",
                        now=self._now(),
                    )
                else:
                    store.fail_allocation(
                        connection,
                        allocation_id,
                        reason=f"{step} failed: {exc}",
                        now=self._now(),
                    )
            if cleanup_failed:
                raise InternalError(
                    f"{step} failed and container cleanup is pending; resources remain reserved"
                ) from exc
            raise InternalError("allocation could not be activated") from exc

        # The container only becomes usable once its member identity exists:
        # without this gate, `sudo` inside the container fails with "you do
        # not exist in the passwd database" on first use for base images that
        # lack the member UID.
        try:
            await asyncio.to_thread(
                self.docker.initialize_container_identity,
                container.id,
                allocation,
            )
        except Exception as exc:
            await _fail_activated_container("container identity initialization", exc)
        try:
            with self._connection() as connection:
                store.activate_allocation(
                    connection,
                    allocation_id,
                    container_id=container.id,
                    container_name=container.name,
                    now=self._now(),
                )
                return store.get_allocation(connection, allocation_id, now=self._now())
        except Exception as exc:
            await _fail_activated_container("allocation activation", exc)

    async def _schedule_queue_safely(self, trigger: str) -> None:
        try:
            admitted = await self._schedule_queue()
        except Exception:
            logger.exception("queue scheduling failed during %s", trigger)
            return
        if admitted:
            logger.info("%s queue admissions: %s", trigger, ", ".join(admitted))

    async def _schedule_queue(self) -> list[str]:
        if self._mode != "active" or not self.config.policy.queue_enabled:
            return []
        with self._connection() as connection:
            if store.queue_request_count(connection, now=self._now()) == 0:
                return []
        # Admission must be as fresh as a direct apply: re-run the audit gates
        # here instead of trusting results cached by an earlier check, so a
        # host permission or quota change between periodic audits pauses
        # automatic admission too.
        gpu_failure = await asyncio.to_thread(self._run_gpu_audit)
        if gpu_failure:
            logger.warning("queue scheduling paused by GPU audit gate: %s", gpu_failure)
            return []
        quota_failure = await asyncio.to_thread(self._run_quota_check)
        if quota_failure:
            logger.warning("queue scheduling paused by workspace quota gate: %s", quota_failure)
            return []
        # Re-check the nosuid/quota storage gate at admission time instead of
        # relying only on the value cached at startup or the last direct apply.
        storage_failure = await asyncio.to_thread(
            self._run_storage_check,
            include_next_slot=True,
        )
        if storage_failure:
            logger.warning("queue scheduling paused by Docker storage gate: %s", storage_failure)
            return []
        gate_failure = self._gate_failure()
        if gate_failure:
            logger.warning("queue scheduling paused by host safety gate: %s", gate_failure)
            return []
        admitted: list[str] = []
        while True:
            live = await asyncio.to_thread(self._live_resources)
            if live.error:
                logger.warning("queue scheduling paused: live GPU detection failed: %s", live.error)
                break
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                active_allocations = self._allocation_map(connection, now)
                merged = self._merge_gpus(db_gpus, live, active_allocations)
                available_gpu_uuids = {str(gpu["uuid"]) for gpu in merged if gpu["state"] == "free"}
                allocation_id = store.admit_next_queued(
                    connection,
                    max_cpus=self.config.policy.global_max_cpus,
                    max_memory_gb=self.config.policy.global_max_memory_gb,
                    max_cpus_per_user=self.config.policy.max_cpus_per_user,
                    max_memory_gb_per_user=self.config.policy.max_memory_gb_per_user,
                    max_gpus_per_user=self.config.policy.max_gpus_per_user,
                    max_active_allocations_per_user=(
                        self.config.policy.max_active_allocations_per_user
                    ),
                    available_gpu_uuids=available_gpu_uuids,
                    backfill_enabled=self.config.policy.queue_backfill_enabled,
                    backfill_reserve_minutes=self.config.policy.queue_backfill_reserve_minutes,
                    membership_required=self.config.policy.membership_required,
                    now=now,
                    **self._container_storage_params(),
                )
            if allocation_id is None:
                break
            logger.info("admitting queued request as allocation %s", allocation_id)
            try:
                await self._materialize_allocation(allocation_id)
            except Exception:
                logger.exception("queued allocation %s could not be materialized", allocation_id)
            admitted.append(allocation_id)
        return admitted

    async def _release(
        self,
        identity: Identity,
        args: dict[str, Any],
        *,
        admin: bool = False,
    ) -> dict[str, Any]:
        allocation_id = args.get("id")
        if not isinstance(allocation_id, str) or not allocation_id:
            raise BadRequest("id must be a non-empty string")
        reason = args.get("reason", "")
        if not isinstance(reason, str):
            raise BadRequest("reason must be a string")
        with self._connection() as connection:
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
        if not admin and allocation["owner_uid"] != identity.uid:
            raise Forbidden("only the owner or an admin may release this allocation")
        if not admin:
            self._require_member_access(identity, "release")
        try:
            container_id = str(allocation["container_id"])
            if container_id:
                container = await asyncio.to_thread(self.docker.get_container, container_id)
                if container is not None and not matches_managed_allocation(
                    container,
                    allocation_id,
                    container_id,
                ):
                    raise Conflict(
                        "refusing to remove a container that does not match this allocation"
                    )
                await asyncio.to_thread(self.docker.remove_container, container_id)
            else:
                containers = await asyncio.to_thread(self.docker.list_containers)
                for container in containers:
                    if matches_managed_allocation(container, allocation_id):
                        await asyncio.to_thread(self.docker.remove_container, container.id)
        except Conflict:
            raise
        except Exception as exc:
            raise DockerError(f"cannot remove allocation container: {exc}") from exc
        with self._connection() as connection:
            store.release_allocation(
                connection,
                allocation_id,
                actor_uid=identity.uid,
                reason=reason,
                now=self._now(),
            )
            released = store.get_allocation(connection, allocation_id, now=self._now())
        self._cancel_tunnels(allocation_id)
        await self._schedule_queue_safely("release")
        return released

    @staticmethod
    def _job_name(raw: Any) -> str:
        if not isinstance(raw, str) or not JOB_NAME_RE.fullmatch(raw):
            raise BadRequest("job name must match [A-Za-z0-9_.-]{1,32}")
        if raw in {".", ".."}:
            raise BadRequest("invalid job name")
        return raw

    def _job_allocation(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation_id = args.get("id")
        if not isinstance(allocation_id, str) or not allocation_id:
            raise BadRequest("id must be a non-empty string")
        with self._connection() as connection:
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
        if allocation["owner_uid"] != identity.uid and not identity.is_admin:
            raise Forbidden("only the owner or an admin may manage jobs")
        self._require_member_access(identity, "job_start")
        if allocation["status"] != "active":
            raise Conflict(f"allocation is {allocation['status']}, not active")
        if not allocation["container_id"]:
            raise NotFound("allocation has no container")
        return allocation

    @staticmethod
    def _job_log_path(allocation: dict[str, Any], name: str) -> Path:
        # Display path only.  Reads and writes use workspace_io so this path
        # cannot become a check/use race.
        return Path(str(allocation["workspace"])) / ".citespec-logs" / f"{name}.log"

    @staticmethod
    def _allocation_owner_ids(allocation: dict[str, Any]) -> tuple[int, int]:
        owner_uid = int(allocation["owner_uid"])
        owner_gid = owner_uid
        if os.name == "posix":
            try:
                import pwd

                owner_gid = pwd.getpwuid(owner_uid).pw_gid
            except (ImportError, KeyError):
                pass
        return owner_uid, owner_gid

    @staticmethod
    def _ensure_job_log(allocation: dict[str, Any], name: str) -> None:
        if not workspace_io.supported():
            raise InternalError("descriptor-relative job-log access is unavailable")
        owner_uid, owner_gid = Broker._allocation_owner_ids(allocation)
        workspace_io.ensure_job_log(
            str(allocation["workspace"]),
            name,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

    async def _job_start(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._job_allocation(identity, args)
        name = self._job_name(args.get("name"))
        command = args.get("command")
        if isinstance(command, str):
            # Copy-pasteable shell command. It runs inside the member's own
            # container as `bash -lc` with a fixed argv; the broker never
            # interprets it and it grants nothing the member does not already
            # have through `shell`.
            shell_text = command.strip()
            if not shell_text:
                raise BadRequest("shell command must not be empty")
            if "\x00" in shell_text:
                raise BadRequest("shell command must not contain NUL bytes")
            if len(shell_text) > 4096:
                raise BadRequest("shell command must not exceed 4096 characters")
            command_argv = ["bash", "-lc", shell_text]
        elif (
            isinstance(command, list)
            and command
            and len(command) <= 64
            and all(isinstance(item, str) and item and len(item) <= 512 for item in command)
        ):
            command_argv = command
        else:
            raise BadRequest(
                "command must be a non-empty shell string or a list of argument strings"
            )
        container_id = str(allocation["container_id"])
        log_path = self._job_log_path(allocation, name)
        await asyncio.to_thread(self._ensure_job_log, allocation, name)
        await asyncio.to_thread(self.docker.start_job, container_id, name, command_argv)
        with self._connection() as connection:
            store.record_event(
                connection,
                actor_uid=identity.uid,
                action="job_started",
                allocation_id=str(allocation["id"]),
                detail={"name": name, "command": command_argv},
                now=self._now(),
            )
            connection.commit()
        return {
            "id": allocation["id"],
            "name": name,
            "session": f"citespec-{name}",
            "log": str(log_path),
        }

    async def _job_list(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._job_allocation(identity, args)
        container_id = str(allocation["container_id"])
        jobs = await asyncio.to_thread(self.docker.list_jobs, container_id)
        enriched: list[dict[str, Any]] = []
        for job in jobs:
            name = str(job["name"])
            path = self._job_log_path(allocation, name)
            item: dict[str, Any] = dict(job)
            item["log"] = str(path)
            if not workspace_io.supported():
                raise InternalError("descriptor-relative job-log access is unavailable")
            size = await asyncio.to_thread(
                workspace_io.regular_file_size,
                str(allocation["workspace"]),
                (".citespec-logs", f"{name}.log"),
            )
            item["log_bytes"] = size or 0
            enriched.append(item)
        return {"id": allocation["id"], "jobs": enriched}

    def _log_allocation(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        """Fetch an allocation for log access: owner or admin, any status.

        Persistent job logs outlive the container (the workspace survives
        release and expiry), so unlike container operations this may target
        released or expired allocations.
        """

        allocation_id = args.get("id")
        if not isinstance(allocation_id, str) or not allocation_id:
            raise BadRequest("id must be a non-empty string")
        with self._connection() as connection:
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
        if allocation["owner_uid"] != identity.uid and not identity.is_admin:
            raise Forbidden("only the owner or an admin may read these logs")
        return allocation

    async def _job_files(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._log_allocation(identity, args)
        files = await asyncio.to_thread(
            workspace_io.list_workspace_logs,
            str(allocation["workspace"]),
        )
        return {"id": allocation["id"], "files": files}

    async def _job_logs(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._log_allocation(identity, args)
        name = self._job_name(args.get("name"))
        tail = self._parse_positive_int(args.get("tail", 200), "tail")
        if tail > 10000:
            raise BadRequest("tail must not exceed 10000")
        path = self._job_log_path(allocation, name)
        if not workspace_io.supported():
            raise InternalError("descriptor-relative job-log access is unavailable")
        content = await asyncio.to_thread(
            workspace_io.tail_file,
            str(allocation["workspace"]),
            (".citespec-logs", f"{name}.log"),
            tail,
        )
        return {
            "id": allocation["id"],
            "name": name,
            "log": str(path),
            "logs": content,
        }

    async def _job_stop(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation = self._job_allocation(identity, args)
        name = self._job_name(args.get("name"))
        container_id = str(allocation["container_id"])
        await asyncio.to_thread(self.docker.stop_job, container_id, name)
        with self._connection() as connection:
            store.record_event(
                connection,
                actor_uid=identity.uid,
                action="job_stopped",
                allocation_id=str(allocation["id"]),
                detail={"name": name},
                now=self._now(),
            )
            connection.commit()
        return {"id": allocation["id"], "name": name, "stopped": True}

    async def _logs(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        allocation_id = args.get("id")
        if not isinstance(allocation_id, str) or not allocation_id:
            raise BadRequest("id must be a non-empty string")
        tail = self._parse_positive_int(args.get("tail", 200), "tail")
        if tail > 1000:
            raise BadRequest("tail must not exceed 1000")
        with self._connection() as connection:
            allocation = store.get_allocation(connection, allocation_id, now=self._now())
        if allocation["owner_uid"] != identity.uid and not identity.is_admin:
            raise Forbidden("only the owner or an admin may read these logs")
        container_id = str(allocation["container_id"])
        if not container_id:
            raise NotFound("allocation has no container")
        content = await asyncio.to_thread(self.docker.logs, container_id, tail)
        return {
            "id": allocation_id,
            "container_id": container_id,
            "logs": content,
        }

    def _admin_users(self) -> dict[str, Any]:
        with self._connection() as connection:
            return {"users": store.list_users(connection)}

    def _admin_resources(self) -> dict[str, Any]:
        data = self._list()
        with self._connection() as connection:
            data["users"] = store.list_users(connection)
        return data

    def _admin_audit(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = self._parse_positive_int(args.get("limit", 100), "limit")
        if limit > 1000:
            raise BadRequest("limit must not exceed 1000")
        with self._connection() as connection:
            return {"events": store.list_events(connection, limit=limit)}

    async def _admin_mode(
        self,
        identity: Identity,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        mode = args.get("mode")
        if mode not in {"monitor", "maintenance", "active"}:
            raise BadRequest("mode must be monitor, maintenance, or active")
        if mode == "monitor":
            with self._connection() as connection:
                leases = store.active_lease_count(connection)
                queued = store.queue_request_count(connection, now=self._now())
            if leases or queued:
                raise Conflict(
                    "cannot switch to monitor while active/pending leases or queued "
                    "requests exist; use maintenance mode"
                )
            try:
                managed = [
                    container
                    for container in self.docker.list_containers()
                    if container.labels.get("citespec.managed") == "true"
                ]
            except Exception as exc:
                raise Conflict(f"cannot verify managed containers: {exc}") from exc
            if managed:
                raise Conflict(
                    "cannot switch to monitor while managed containers exist; use maintenance mode"
                )
        if mode == "active":
            try:
                validate_active_mode_policy(self.config.policy)
            except ValueError as exc:
                raise Forbidden(f"active mode blocked by policy configuration: {exc}") from exc
        if mode == "active":
            audit = await asyncio.to_thread(self._legacy_audit_result)
            if not audit.passed:
                raise Conflict(
                    "active mode blocked by legacy access audit: " + audit.failure_message()
                )
            runtime_failure = await asyncio.to_thread(self._refresh_runtime_gates)
            if runtime_failure:
                raise Conflict(f"active mode blocked by host safety gate: {runtime_failure}")
            now = self._now()
            with self._connection() as connection:
                db_gpus = store.list_gpus(connection, now=now)
                active_allocations = self._allocation_map(connection, now)
            live = await asyncio.to_thread(self._live_resources)
            if live.error:
                raise Conflict(f"active mode blocked: live GPU detection failed: {live.error}")
            merged = self._merge_gpus(db_gpus, live, active_allocations)
            unmanaged = self._unmanaged(db_gpus, active_allocations, live)
            blocking_gpus = [
                f"GPU {gpu['index']} ({gpu['state']}: {gpu['state_reason']})"
                for gpu in merged
                if gpu["state"] in {"unmanaged", "unknown"}
            ]
            blocking_containers = [
                str(container["name"])
                for container in unmanaged["containers"]
                if container.get("active_gpu_access")
            ]
            blocking_processes = [
                f"pid {process['pid']} on GPU {process.get('gpu_index')}"
                for process in unmanaged["processes"]
            ]
            blockers = blocking_gpus + blocking_containers + blocking_processes
            if blockers:
                raise Conflict(
                    "active mode blocked by unmanaged GPU access: " + "; ".join(blockers[:10])
                )
        with self._connection() as connection:
            store.set_mode(connection, str(mode), actor_uid=identity.uid, now=self._now())
        self._mode = str(mode)
        if self._mode == "active":
            await self._schedule_queue_safely("mode_activation")
        return {"mode": self._mode}

    @staticmethod
    def _require_admin(identity: Identity) -> None:
        if not identity.is_admin:
            raise Forbidden("admin permission required")

    def _require_active(self) -> None:
        if self._mode == "maintenance":
            raise MonitorOnly(
                "broker is in maintenance mode; new allocations are disabled until an "
                "administrator finishes maintenance"
            )
        if self._mode != "active":
            raise MonitorOnly(
                "broker is in monitor-only mode; an administrator must run "
                "'citespec-resource admin mode active' (and allow deployment in the server "
                "configuration) before new allocations are accepted"
            )

    @staticmethod
    def _parse_gpus(raw: Any) -> list[int]:
        if not isinstance(raw, list):
            raise BadRequest("gpus must be a list of integer indices")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in raw):
            raise BadRequest("gpus must be a list of integer indices")
        if any(item < 0 for item in raw):
            raise BadRequest("gpu indices must not be negative")
        if len(set(raw)) != len(raw):
            raise BadRequest("gpus must not contain duplicates")
        return raw

    @staticmethod
    def _parse_positive_float(raw: Any, name: str) -> float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BadRequest(f"{name} must be a number")
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise BadRequest(f"{name} must be a positive finite number")
        return value

    @staticmethod
    def _parse_positive_int(raw: Any, name: str) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise BadRequest(f"{name} must be an integer")
        if raw <= 0:
            raise BadRequest(f"{name} must be positive")
        return raw

    def _workspace(self, username: str) -> str:
        if not username or username in {".", ".."} or "/" in username or "\\" in username:
            raise Forbidden("invalid username for workspace path")
        return f"{self.config.server.workspace_root.rstrip('/')}/{username}"

    @staticmethod
    def _allocation_model(
        data: dict[str, Any],
        starts_at: datetime,
        expires_at: datetime,
    ) -> Allocation:
        return Allocation(
            id=str(data["id"]),
            owner_uid=int(data["owner_uid"]),
            owner_name=str(data["owner"]),
            gpus=tuple(str(gpu) for gpu in data["gpu_uuids"]),
            cpus=float(data["cpus"]),
            memory_gb=int(data["memory_gb"]),
            image=str(data["image"]),
            workspace=str(data["workspace"]),
            note=str(data["note"]),
            starts_at=starts_at,
            expires_at=expires_at,
            status="pending",
        )
