"""Membership service + provisioning integration tests (goal §7/§8, AC-11..16)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from citespec_resource.enrollment import store as intake_store
from citespec_resource.membership import service as member_service
from citespec_resource.membership import store as member_store
from citespec_resource.membership.provisioning import (
    CommandExecutorClient,
    ProvisioningError,
    validate_request,
)

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOkmYS2WMJDCO8mXcSuUCLjC0Y0pSCfTOcyUNVox member"


@dataclass
class FakeExecutor:
    """Records requests; optionally fails at a named stage (AC-14)."""

    fail_stage: str | None = None
    inspect_result: dict | None = None

    def __post_init__(self) -> None:
        self.calls: list = []

    def execute(
        self,
        action: str,
        frozen_input: dict,
        *,
        operation_id: str | None = None,
    ) -> dict:
        from citespec_resource.membership.provisioning import validate_request

        validate_request({"action": action, "frozen_input": frozen_input})
        self.calls.append((action, frozen_input))
        if operation_id is not None:
            self.operation_ids: list[str] = getattr(self, "operation_ids", [])
            self.operation_ids.append(operation_id)
        if action == "inspect_member":
            return {
                "status": "succeeded",
                "stages": ["inspected"],
                "username": frozen_input.get("username"),
                **(self.inspect_result or {"exists": False}),
            }
        if self.fail_stage:
            raise ProvisioningError(f"stage={self.fail_stage}: simulated failure")
        return {
            "status": "succeeded",
            "stages": [f"{action}:ok"],
            "uid": 42424,
        }


@pytest.fixture
def dbs(tmp_path):
    broker_db = tmp_path / "state.db"
    intake_db = tmp_path / "intake.db"
    from citespec_resource.db import init_db

    connection = init_db(broker_db)
    intake = intake_store.init_intake_db(intake_db)
    intake.close()
    yield connection, str(intake_db)
    connection.close()


def _submit(broker_connection, intake_db, *, username: str = "newmember") -> dict:
    intake = intake_store.connect(intake_db)
    invitation = intake_store.create_invitation(intake, created_by="admin", validity_hours=24)
    result = intake_store.submit_application(
        intake,
        invite_id=invitation["invite_id"],
        invite_secret=invitation["invite_secret"],
        application={
            "display_name": "New Member",
            "username": username,
            "affiliation": "Lab A",
            "purpose": "training",
            "contact": "newmember@example.invalid",
            "nickname": "",
            "public_contact": "",
            "agree_terms": "1",
            "public_keys": [KEY],
        },
    )
    intake.close()
    return result


def test_approve_provisions_and_activates(dbs):
    connection, intake_db = dbs
    submitted = _submit(connection, intake_db)
    executor = FakeExecutor()
    result = member_service.approve_request(
        connection,
        intake_db=intake_db,
        request_id=submitted["request_id"],
        reviewer_uid=0,
        valid_until="",
        quota_gb=200,
        review_note="",
        internal_note="",
        provisioning=executor,
    )
    assert result["status"] == "succeeded"
    member = member_store.get_member(connection, str(result["member_id"]))
    assert member["member"]["status"] == "active"
    assert member["member"]["linux_username"] == "newmember"
    assert any(key["fingerprint"].startswith("SHA256:") for key in member["keys"])
    decision = member_store.get_application_decision(connection, submitted["request_id"])
    assert decision is not None and decision["status"] == "approved"
    assert executor.calls[0][0] == "provision_member"


def test_double_approval_converges(dbs):
    """AC-11: a second approval returns the same member and operation."""
    connection, intake_db = dbs
    submitted = _submit(connection, intake_db)
    executor = FakeExecutor()
    first = member_service.approve_request(
        connection,
        intake_db=intake_db,
        request_id=submitted["request_id"],
        reviewer_uid=0,
        valid_until="",
        quota_gb=200,
        review_note="",
        internal_note="",
        provisioning=executor,
    )
    second = member_service.approve_request(
        connection,
        intake_db=intake_db,
        request_id=submitted["request_id"],
        reviewer_uid=0,
        valid_until="",
        quota_gb=200,
        review_note="",
        internal_note="",
        provisioning=executor,
    )
    assert second["member_id"] == first["member_id"]
    assert second["operation_id"] == first["operation_id"]
    members = member_store.list_members(connection)
    assert len([m for m in members if m["linux_username"] == "newmember"]) == 1


def test_reject_creates_nothing(dbs):
    """AC-15: rejection creates no member, key, or operation."""
    connection, intake_db = dbs
    submitted = _submit(connection, intake_db)
    result = member_service.reject_request(
        connection,
        intake_db=intake_db,
        request_id=submitted["request_id"],
        reviewer_uid=0,
        review_note="身份尚无法核验",
    )
    assert result["status"] == "rejected"
    assert member_store.list_members(connection) == []
    intake = intake_store.connect(intake_db)
    try:
        request = intake_store.get_request(intake, submitted["request_id"])
        assert request is not None and request["status"] == "rejected"
    finally:
        intake.close()


def test_provision_failure_is_retryable(dbs):
    """AC-14: a failed stage leaves provision_failed with a retryable op."""
    connection, intake_db = dbs
    submitted = _submit(connection, intake_db)
    executor = FakeExecutor(fail_stage="create_account")
    result = member_service.approve_request(
        connection,
        intake_db=intake_db,
        request_id=submitted["request_id"],
        reviewer_uid=0,
        valid_until="",
        quota_gb=200,
        review_note="",
        internal_note="",
        provisioning=executor,
    )
    assert result["status"] == "failed"
    member = member_store.get_member(connection, str(result["member_id"]))
    assert member["member"]["status"] == "provision_failed"
    operation = member_store.get_operation(connection, result["operation_id"])
    assert operation["status"] == "failed"
    assert operation["attempts"] == 1

    # Retry with a working executor converges to active.
    result = member_service.retry_operation(
        connection,
        result["operation_id"],
        provisioning=FakeExecutor(),
        intake_db=intake_db,
        actor_uid=0,
    )
    assert result["status"] == "succeeded"
    member = member_store.get_member(connection, str(result["member_id"]))
    assert member["member"]["status"] == "active"


def test_suspended_member_cannot_be_reactivated_by_late_provision(dbs):
    """§13: a late provisioning task must not re-enable a revoked member."""
    connection, intake_db = dbs
    submitted = _submit(connection, intake_db)
    executor = FakeExecutor(fail_stage="create_account")
    result = member_service.approve_request(
        connection,
        intake_db=intake_db,
        request_id=submitted["request_id"],
        reviewer_uid=0,
        valid_until="",
        quota_gb=200,
        review_note="",
        internal_note="",
        provisioning=executor,
    )
    member_id = str(result["member_id"])
    member_store.transition_member(
        connection, member_id, target="revoked", reason="left lab", actor_uid=0
    )
    with pytest.raises(Exception, match="cannot transition"):
        member_service.retry_operation(
            connection,
            result["operation_id"],
            provisioning=FakeExecutor(),
            intake_db=intake_db,
            actor_uid=0,
        )
    assert member_store.get_member(connection, member_id)["member"]["status"] == "revoked"


def test_intake_privacy_and_validation(dbs, tmp_path: Path):
    """AC-09/AC-10: guessed ids yield nothing; private keys rejected."""
    connection, intake_db = dbs
    submitted = _submit(connection, intake_db)
    intake = intake_store.connect(intake_db)
    try:
        with pytest.raises(ValueError):
            intake_store.request_status(
                intake,
                request_id=submitted["request_id"],
                receipt_secret="wrong-secret",
            )
        with pytest.raises(ValueError):
            intake_store.request_status(intake, request_id="guessed-id", receipt_secret="x")
        with pytest.raises(ValueError, match="私钥"):
            invitation = intake_store.create_invitation(
                intake, created_by="admin", validity_hours=1
            )
            intake_store.submit_application(
                intake,
                invite_id=invitation["invite_id"],
                invite_secret=invitation["invite_secret"],
                application={
                    "display_name": "X",
                    "username": "abc",
                    "affiliation": "x",
                    "purpose": "x",
                    "contact": "x",
                    "agree_terms": "1",
                    "public_keys": ["-----BEGIN OPENSSH PRIVATE KEY-----"],
                },
            )
    finally:
        intake.close()


def test_executor_request_validation():
    assert validate_request({"action": "inspect_member", "frozen_input": {"username": "legacy"}})
    with pytest.raises(ValueError):
        validate_request({"action": "run_shell", "frozen_input": {}})
    with pytest.raises(ValueError):
        validate_request(
            {
                "action": "provision_member",
                "frozen_input": {"username": "x", "extra": "nope"},
            }
        )
    with pytest.raises(ValueError):
        validate_request({"action": "disable_member", "frozen_input": {"username": "root"}})
    with pytest.raises(ValueError):
        validate_request(
            {"action": "inspect_member", "frozen_input": {"username": "x", "extra": "no"}}
        )


def test_command_executor_client(tmp_path):
    """The fixed-command client protocol works against a real subprocess."""
    executor_script = tmp_path / "fake_executor.py"
    executor_script.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "json.dump({'status': 'succeeded', 'stages': ['ok']}, sys.stdout)\n",
        encoding="utf-8",
    )
    import sys as sys_module

    client = CommandExecutorClient([sys_module.executable, str(executor_script)])
    result = client.execute("set_keys", {"username": "alice", "public_keys": [KEY]})
    assert result["status"] == "succeeded"

    failing = tmp_path / "fail_executor.py"
    failing.write_text(
        "import json, sys\n"
        "json.dump({'status': 'failed', 'stage': 'keys', 'error': 'boom'}, sys.stdout)\n",
        encoding="utf-8",
    )
    client = CommandExecutorClient([sys_module.executable, str(failing)])
    with pytest.raises(ProvisioningError, match="boom"):
        client.execute("set_keys", {"username": "alice", "public_keys": []})


def _approve(connection, intake_db, submit, executor) -> dict:
    return member_service.approve_request(
        connection,
        intake_db=intake_db,
        request_id=submit["request_id"],
        reviewer_uid=0,
        valid_until="",
        quota_gb=200,
        review_note="",
        internal_note="",
        provisioning=executor,
    )


INSPECTION = {
    "exists": True,
    "uid": 42424,
    "gid": 42424,
    "home": "/home/legacy",
    "home_exists": True,
    "home_is_symlink": False,
    "shell": "/bin/bash",
    "groups": ["citespec-members"],
    "managed": False,
    "home_authorized_keys": ["/home/legacy/.ssh/authorized_keys"],
    "central_key_count": 0,
}


def test_legacy_import_dry_run_then_explicit_apply(dbs):
    """M6: dry-run inspects and reports; apply adopts only the reviewed entry."""
    connection, intake_db = dbs
    executor = FakeExecutor(inspect_result=INSPECTION)
    dry = member_service.import_legacy_member(
        connection,
        username="legacy",
        display_name="Legacy Person",
        valid_until="2099-01-01T00:00:00+00:00",
        quota_gb=50,
        public_keys=[KEY],
        actor_uid=0,
        provisioning=executor,
        workspace_root="/data1/citespec-users",
        dry_run=True,
    )
    assert dry["dry_run"] is True
    assert dry["inspection"]["uid"] == 42424
    assert dry["would_adopt_existing"] is True
    assert "account_will_be_converted_to_managed_member" in dry["warnings"]
    assert dry["conflicts"] == []
    assert member_store.list_members(connection) == []

    applied = member_service.import_legacy_member(
        connection,
        username="legacy",
        display_name="Legacy Person",
        valid_until="2099-01-01T00:00:00+00:00",
        quota_gb=50,
        public_keys=[KEY],
        actor_uid=0,
        provisioning=executor,
        workspace_root="/data1/citespec-users",
        dry_run=False,
    )
    assert applied["operation_status"] == "succeeded"
    member = member_store.get_member(connection, applied["member_id"])
    assert member["member"]["status"] == "active"
    assert member["member"]["linux_uid"] == 42424
    provision = [call for call in executor.calls if call[0] == "provision_member"]
    assert provision and provision[0][1]["allow_existing"] is True


def test_legacy_import_refuses_conflicts_and_unknown_accounts(dbs):
    connection, intake_db = dbs
    privileged = dict(INSPECTION, groups=["citespec-members", "sudo"])
    report = member_service.import_legacy_member(
        connection,
        username="legacy",
        display_name="Legacy",
        valid_until="",
        quota_gb=50,
        public_keys=[KEY],
        actor_uid=0,
        provisioning=FakeExecutor(inspect_result=privileged),
        workspace_root="/data1/citespec-users",
        dry_run=True,
    )
    assert "privileged_group:sudo" in report["conflicts"]
    with pytest.raises(Exception, match="conflicts"):
        member_service.import_legacy_member(
            connection,
            username="legacy",
            display_name="Legacy",
            valid_until="",
            quota_gb=50,
            public_keys=[KEY],
            actor_uid=0,
            provisioning=FakeExecutor(inspect_result=privileged),
            workspace_root="/data1/citespec-users",
            dry_run=False,
        )
    assert member_store.list_members(connection) == []

    absent = member_service.import_legacy_member(
        connection,
        username="ghost",
        display_name="Ghost",
        valid_until="",
        quota_gb=50,
        public_keys=[KEY],
        actor_uid=0,
        provisioning=FakeExecutor(inspect_result={"exists": False}),
        workspace_root="/data1/citespec-users",
        dry_run=True,
    )
    assert "account_absent" in absent["conflicts"]


def test_legacy_import_refuses_portal_service_group(dbs):
    connection, intake_db = dbs
    portal_member = dict(INSPECTION, groups=["citespec-members", "citespec-portal"])
    report = member_service.import_legacy_member(
        connection,
        username="legacy",
        display_name="Legacy",
        valid_until="",
        quota_gb=50,
        public_keys=[KEY],
        actor_uid=0,
        provisioning=FakeExecutor(inspect_result=portal_member),
        workspace_root="/data1/citespec-users",
        dry_run=True,
    )
    assert "privileged_group:citespec-portal" in report["conflicts"]
    assert member_store.list_members(connection) == []


def test_reconcile_restored_operations_fails_closed(dbs):
    connection, _ = dbs
    member_id = member_store.create_member(
        connection,
        linux_uid=42424,
        linux_username="legacy",
        status="provisioning",
        display_name="Legacy",
    )
    operation_id, _ = member_store.create_operation(
        connection,
        action="provision_member",
        frozen_input={"username": "legacy"},
        member_id=member_id,
    )
    member_store.update_operation(connection, operation_id, status="running")
    connection.commit()

    reconciled = member_service.reconcile_restored_operations(connection)

    assert reconciled == [
        {
            "operation_id": operation_id,
            "action": "provision_member",
            "previous_status": "running",
            "status": "failed",
        }
    ]
    operation = member_store.get_operation(connection, operation_id)
    assert operation["status"] == "failed"
    assert operation["stage"] == "restore"
    member = member_store.get_member(connection, member_id)
    assert member["member"]["status"] == "provision_failed"
    assert member_service.reconcile_restored_operations(connection) == []


def test_admin_key_add_and_revoke_project_to_host(dbs):
    connection, intake_db = dbs
    submitted = _submit(connection, intake_db)
    executor = FakeExecutor()
    result = _approve(connection, intake_db, submitted, executor)
    member_id = str(result["member_id"])
    second_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILegacyKeyForTestsOnly0000000000000000000 b"
    added = member_service.add_member_key(
        connection,
        member_id,
        public_key=second_key,
        actor_uid=0,
        provisioning=executor,
    )
    assert added["key_id"]
    assert executor.calls[-1][0] == "set_keys"
    keys = member_store.get_member(connection, member_id)["keys"]
    assert len([key for key in keys if key["status"] == "active"]) == 2

    member_service.revoke_member_key(
        connection,
        member_id,
        key_id=added["key_id"],
        actor_uid=0,
        provisioning=executor,
    )
    assert executor.calls[-1][0] == "set_keys"
    active = member_store.get_member(connection, member_id)["keys"]
    assert len([key for key in active if key["status"] == "active"]) == 1


def test_resume_member_reinstalls_active_keys(dbs):
    connection, intake_db = dbs
    result = _approve(connection, intake_db, _submit(connection, intake_db), FakeExecutor())
    member_id = str(result["member_id"])
    suspended = member_service.suspend_member(
        connection,
        member_id,
        actor_uid=0,
        reason="temporary pause",
        provisioning=FakeExecutor(),
    )
    assert suspended["credential_revocation"] == "done"

    executor = FakeExecutor()
    resumed = member_service.resume_member(
        connection,
        member_id,
        actor_uid=0,
        reason="pause over",
        provisioning=executor,
    )
    assert resumed["status"] == "active"
    assert resumed["credential_revocation"] == "done"
    assert executor.calls[-1][0] == "enable_member"
    enabled_keys = executor.calls[-1][1]["public_keys"]
    assert len(enabled_keys) == 1
    assert enabled_keys[0].startswith("ssh-ed25519 ")
    assert member_store.get_member(connection, member_id)["member"]["status"] == "active"


class _ConcurrentWriterExecutor(FakeExecutor):
    def __init__(self, db_path: str) -> None:
        super().__init__()
        self.db_path = db_path

    def execute(
        self,
        action: str,
        frozen_input: dict,
        *,
        operation_id: str | None = None,
    ) -> dict:
        if action == "set_keys":
            from citespec_resource.db import connect

            other = connect(self.db_path)
            try:
                other.execute("PRAGMA busy_timeout=0")
                other.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES ('probe', '1', 'now') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
                other.commit()
            finally:
                other.close()
        return super().execute(action, frozen_input, operation_id=operation_id)


class _VersionRaceExecutor(FakeExecutor):
    def __init__(self, db_path: str, member_id: str) -> None:
        super().__init__()
        self.db_path = db_path
        self.member_id = member_id

    def execute(
        self,
        action: str,
        frozen_input: dict,
        *,
        operation_id: str | None = None,
    ) -> dict:
        if action == "set_keys":
            from citespec_resource.db import connect

            other = connect(self.db_path)
            try:
                other.execute(
                    "UPDATE members SET access_version = access_version + 1 WHERE member_id = ?",
                    (self.member_id,),
                )
                other.commit()
            finally:
                other.close()
        return super().execute(action, frozen_input, operation_id=operation_id)


def test_key_projection_does_not_hold_sqlite_write_lock(dbs):
    connection, intake_db = dbs
    db_path = str(connection.execute("PRAGMA database_list").fetchone()["file"])
    result = _approve(connection, intake_db, _submit(connection, intake_db), FakeExecutor())
    member_id = str(result["member_id"])
    second_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIConcurrentWriterKeyForTests00000000000000 w"
    executor = _ConcurrentWriterExecutor(db_path)
    added = member_service.add_member_key(
        connection,
        member_id,
        public_key=second_key,
        actor_uid=0,
        provisioning=executor,
    )
    assert added["key_id"]
    probe = connection.execute("SELECT value FROM settings WHERE key = 'probe'").fetchone()
    assert probe is not None and probe["value"] == "1"
    operations = member_store.get_member(connection, member_id)["operations"]
    assert operations[0]["action"] == "set_keys"
    assert operations[0]["status"] == "succeeded"
    assert executor.operation_ids == [operations[0]["operation_id"]]


def test_key_projection_rejects_stale_member_version(dbs):
    connection, intake_db = dbs
    db_path = str(connection.execute("PRAGMA database_list").fetchone()["file"])
    result = _approve(connection, intake_db, _submit(connection, intake_db), FakeExecutor())
    member_id = str(result["member_id"])
    before = member_store.get_member(connection, member_id)["keys"]
    second_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIVersionRaceKeyForTests00000000000000000 v"
    with pytest.raises(ProvisioningError) as error:
        member_service.add_member_key(
            connection,
            member_id,
            public_key=second_key,
            actor_uid=0,
            provisioning=_VersionRaceExecutor(db_path, member_id),
        )
    assert "stale_key_operation" in str(error.value)
    after = member_store.get_member(connection, member_id)["keys"]
    assert len(after) == len(before)
    operations = member_store.get_member(connection, member_id)["operations"]
    assert operations[0]["action"] == "set_keys"
    assert operations[0]["status"] == "failed"
    assert operations[0]["stage"] == "stale_key_operation"


def test_legacy_key_projection_without_version_fails_closed(dbs):
    connection, intake_db = dbs
    result = _approve(connection, intake_db, _submit(connection, intake_db), FakeExecutor())
    member_id = str(result["member_id"])
    member = member_store.get_member(connection, member_id)["member"]
    operation_id, _created = member_store.create_operation(
        connection,
        action="set_keys",
        frozen_input={
            "username": member["linux_username"],
            "public_keys": [KEY],
        },
        member_id=member_id,
        expected_access_version=0,
    )
    connection.commit()

    retried = member_service.retry_operation(
        connection,
        operation_id,
        provisioning=FakeExecutor(),
        intake_db=intake_db,
        actor_uid=0,
    )

    assert retried["status"] == "failed"
    assert retried["stage"] == "stale_key_operation"


def test_admin_key_sync_failure_rolls_back(dbs):
    connection, intake_db = dbs
    result = _approve(connection, intake_db, _submit(connection, intake_db), FakeExecutor())
    member_id = str(result["member_id"])
    before = member_store.get_member(connection, member_id)
    second_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFailureKeyForTestsOnly0000000000000000 c"
    with pytest.raises(ProvisioningError):
        member_service.add_member_key(
            connection,
            member_id,
            public_key=second_key,
            actor_uid=0,
            provisioning=FakeExecutor(fail_stage="set_keys"),
        )
    after = member_store.get_member(connection, member_id)
    assert len(after["keys"]) == len(before["keys"])


def test_revoke_is_retryable_after_disable_failure(dbs):
    connection, intake_db = dbs
    result = _approve(connection, intake_db, _submit(connection, intake_db), FakeExecutor())
    member_id = str(result["member_id"])
    first = member_service.revoke_member(
        connection,
        member_id,
        actor_uid=0,
        reason="left lab",
        provisioning=FakeExecutor(fail_stage="disable_member"),
    )
    assert first["credential_revocation"] == "pending_retry"
    second = member_service.revoke_member(
        connection,
        member_id,
        actor_uid=0,
        reason="left lab",
        provisioning=FakeExecutor(),
    )
    assert second["credential_revocation"] == "done"
    assert member_store.get_member(connection, member_id)["member"]["status"] == "revoked"
    operations = [
        operation
        for operation in member_store.get_member(connection, member_id)["operations"]
        if operation["action"] == "disable_member"
    ]
    assert len(operations) == 1
    assert operations[0]["status"] == "succeeded"
