"""Membership service + provisioning integration tests (goal §7/§8, AC-11..16)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from udc_resource.enrollment import store as intake_store
from udc_resource.membership import service as member_service
from udc_resource.membership import store as member_store
from udc_resource.membership.provisioning import (
    CommandExecutorClient,
    ProvisioningError,
    validate_request,
)

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOkmYS2WMJDCO8mXcSuUCLjC0Y0pSCfTOcyUNVox member"


@dataclass
class FakeExecutor:
    """Records requests; optionally fails at a named stage (AC-14)."""

    fail_stage: str | None = None

    def __post_init__(self) -> None:
        self.calls: list = []

    def execute(self, action: str, frozen_input: dict) -> dict:
        from udc_resource.membership.provisioning import validate_request

        validate_request({"action": action, "frozen_input": frozen_input})
        self.calls.append((action, frozen_input))
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
    from udc_resource.db import init_db

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
