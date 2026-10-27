"""Tests for the VPN-only membership portal.

These tests exercise the local account/TOTP store, the narrow HTTP surface, and
the one-time activation flow.  They deliberately use a fake broker so no broker
socket or host service is required.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from citespec_resource.config import load_config
from citespec_resource.enrollment import store as intake_store
from citespec_resource.netutil import trusted_client_ip
from citespec_resource.portal import security
from citespec_resource.portal import server as portal_server
from citespec_resource.portal import store as portal_store
from citespec_resource.portal.broker import PortalBrokerError

ROOT = Path(__file__).resolve().parents[1]
SECRET = b"portal-test-secret-material-0123456789abcdef"
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "another strong portal password"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

VALID_APPLICATION = {
    "display_name": "Portal Applicant",
    "username": "newmember",
    "affiliation": "Lab A",
    "purpose": "training runs",
    "contact": "newmember@example.invalid",
    "nickname": "",
    "public_contact": "",
    "agree_terms": "1",
    "public_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI newmember@lab"],
}


class FakeBroker:
    """Minimal asynchronous stand-in for the portal broker bridge."""

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    async def call(
        self,
        command: str,
        *,
        actor: portal_server.PortalActor,
        **args: object,
    ) -> object:
        self.calls.append((command, actor.as_dict(), args))
        if command in self.responses:
            value = self.responses[command]
            if callable(value):
                return value(actor, args)
            if isinstance(value, Exception):
                raise value
            return value
        if command == "portal_status":
            return {
                "mode": "monitor",
                "active_allocations": 0,
                "queued_requests": 0,
                "gpu_count": 1,
                "generated_at": NOW.isoformat(),
                "gpus": [
                    {
                        "index": 0,
                        "name": "Test GPU",
                        "memory_mib": 81920,
                        "state": "idle",
                    }
                ],
            }
        if command == "portal_member_self":
            return {
                "member": {
                    "status": "active",
                    "valid_until": "2027-10-27",
                    "display_name": "Portal Applicant",
                    "linux_uid": 2000,
                },
                "keys": [],
                "operations": [],
            }
        if command == "portal_admin_attest":
            return {
                "attestation": "test-attestation-token-0123456789abcdef",
                "expires_at": (NOW + timedelta(minutes=2)).isoformat(),
                "action": args.get("action", ""),
                "target": args.get("target", ""),
            }
        if command == "portal_requests_list":
            return []
        if command == "portal_members_list":
            return {"members": []}
        if command == "portal_audit":
            return {"events": []}
        return {}


@pytest.fixture
def portal_config(monitor_config, tmp_path: Path):
    return replace(
        monitor_config,
        portal=replace(
            monitor_config.portal,
            enabled=True,
            db_path=str(tmp_path / "portal.db"),
            secret_file=str(tmp_path / "portal-secret.key"),
            base_url="http://portal.test",
            cookie_secure=False,
            registration_enabled=True,
        ),
        onboarding=replace(
            monitor_config.onboarding,
            intake_db=str(tmp_path / "intake.db"),
        ),
    )


@pytest.fixture
async def portal(portal_config):
    broker = FakeBroker()
    app = portal_server.create_app(
        portal_config,
        broker=broker,
        secret=SECRET,
        db_path=portal_config.portal.db_path,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, broker, portal_config
    finally:
        await client.close()


def _cookie(client: TestClient, name: str) -> str:
    for cookie in client.session.cookie_jar:
        if cookie.key == name:
            return str(cookie.value)
    return ""


def _seed_account(
    db: str,
    *,
    username: str,
    role: str = "member",
    status: str = "active",
    member_id: str = "m-1",
) -> tuple[str, str]:
    connection = portal_store.connect(db)
    try:
        totp_secret = security.new_totp_secret() if status == "active" else ""
        password_hash = (
            security.hash_password(PASSWORD, username=username) if status == "active" else ""
        )
        user_id = portal_store.create_user(
            connection,
            username=username,
            role=role,
            status=status,
            password_hash=password_hash,
            totp_secret_enc=(security.seal(SECRET, totp_secret) if totp_secret else ""),
            member_id=member_id,
            linux_username=username,
            display_name=username.title(),
            contact=f"{username}@example.invalid",
        )
    finally:
        connection.close()
    return user_id, totp_secret


def _reset_counter(db: str, user_id: str) -> None:
    # Test-only setup: production never rewinds a consumed TOTP counter.
    connection = portal_store.connect(db)
    try:
        connection.execute(
            "UPDATE portal_users SET totp_last_counter = 0 WHERE user_id = ?",
            (user_id,),
        )
        connection.commit()
    finally:
        connection.close()


async def _login(
    client: TestClient,
    username: str,
    password: str,
    totp_secret: str,
) -> None:
    response = await client.get("/login")
    assert response.status == 200
    csrf = _cookie(client, portal_server.CSRF_COOKIE)
    assert csrf
    response = await client.post(
        "/login",
        data={
            "_csrf": csrf,
            "username": username,
            "password": password,
            "totp": pyotp.TOTP(totp_secret).now(),
        },
        allow_redirects=False,
    )
    assert response.status == 302
    assert _cookie(client, portal_server.SESSION_COOKIE)


def test_totp_counter_can_only_be_consumed_once(tmp_path: Path):
    db = tmp_path / "portal.db"
    initialized = portal_store.init_portal_db(db)
    initialized.close()
    user_id, totp_secret = _seed_account(str(db), username="alice")
    code = pyotp.TOTP(totp_secret).now()
    counter = security.verify_totp(totp_secret, code)
    assert counter is not None
    connection = portal_store.connect(db)
    try:
        assert portal_store.set_totp_counter(connection, user_id, counter=counter)
        assert not portal_store.set_totp_counter(connection, user_id, counter=counter)
        assert not portal_store.set_totp_counter(connection, user_id, counter=counter - 1)
        assert portal_store.set_totp_counter(connection, user_id, counter=counter + 1)
    finally:
        connection.close()


def test_security_primitives_round_trip_and_reject_replay(tmp_path: Path):
    secret = security.load_secret(tmp_path / "portal.key", create=True)
    assert len(secret) >= 32
    assert security.load_secret(tmp_path / "portal.key") == secret

    password_hash = security.hash_password(PASSWORD, username="alice")
    assert security.verify_password(password_hash, PASSWORD)
    assert not security.verify_password(password_hash, "wrong password")
    with pytest.raises(security.PortalSecurityError):
        security.validate_password("short", username="alice")

    totp_secret = security.new_totp_secret()
    code = pyotp.TOTP(totp_secret).now()
    counter = security.verify_totp(totp_secret, code)
    assert counter is not None
    assert security.verify_totp(totp_secret, code, last_counter=counter) is None

    sealed = security.seal(secret, totp_secret)
    assert security.unseal(secret, sealed) == totp_secret
    token = security.sign_token(secret, {"purpose": "test"}, ttl_seconds=60)
    payload = security.verify_token(secret, token)
    assert payload is not None
    assert payload["purpose"] == "test"
    assert security.verify_token(secret, token + "x") is None


def test_store_activation_session_and_login_lock(tmp_path: Path):
    db = tmp_path / "portal.db"
    connection = portal_store.init_portal_db(db)
    try:
        user_id = portal_store.create_user(
            connection,
            username="alice",
            status="pending_activation",
            member_id="m-1",
            linux_username="alice",
        )
        code = portal_store.create_activation_code(
            connection,
            user_id,
            ttl_hours=24,
            now=NOW,
        )
        activation = portal_store.get_activation(
            connection,
            str(code["code"]),
            now=NOW,
        )
        assert activation is not None
        assert activation["user"]["user_id"] == user_id

        totp_secret = security.new_totp_secret()
        portal_store.activate_user(
            connection,
            user_id,
            password_hash=security.hash_password(PASSWORD, username="alice"),
            totp_secret_enc=security.seal(SECRET, totp_secret),
            totp_counter=123,
            now=NOW,
        )
        user = portal_store.get_user(connection, user_id)
        assert user is not None
        assert user["status"] == "active"
        assert security.verify_password(user["password_hash"], PASSWORD)
        assert security.unseal(SECRET, user["totp_secret_enc"]) == totp_secret
        assert portal_store.consume_activation(
            connection,
            str(activation["code_hash"]),
            now=NOW,
        )
        assert (
            portal_store.get_activation(
                connection,
                str(code["code"]),
                now=NOW,
            )
            is None
        )

        session = portal_store.create_session(
            connection,
            user_id=user_id,
            ttl_hours=1,
            csrf_token="csrf-test",
            now=NOW,
        )
        found = portal_store.get_session(
            connection,
            str(session["token"]),
            now=NOW + timedelta(seconds=1),
        )
        assert found is not None
        assert found["user"]["user_id"] == user_id
        assert (
            portal_store.get_session(
                connection,
                str(session["token"]),
                now=NOW + timedelta(hours=2),
            )
            is None
        )

        for _ in range(3):
            portal_store.record_login_failure(
                connection,
                user_id,
                lock_after=3,
                lock_seconds=60,
                now=NOW,
            )
        locked = portal_store.get_user(connection, user_id)
        assert locked is not None
        assert locked["locked_until"]
    finally:
        connection.close()


def test_portal_application_writes_intake_without_invitation(tmp_path: Path):
    db = tmp_path / "intake.db"
    connection = intake_store.init_intake_db(db)
    try:
        result = intake_store.submit_portal_application(
            connection,
            application=VALID_APPLICATION,
        )
        assert result["request_id"]
        assert result["receipt_secret"]
        requests = intake_store.list_requests(connection, status="pending_review")
        assert [item["username"] for item in requests] == ["newmember"]
        assert requests[0]["request_id"] == result["request_id"]
    finally:
        connection.close()


def test_portal_role_helpers_preserve_pending_admin(tmp_path: Path):
    db = tmp_path / "portal.db"
    connection = portal_store.init_portal_db(db)
    try:
        user_id = portal_store.create_user(
            connection,
            username="promoteme",
            status="pending_activation",
        )
        portal_store.update_user_role(connection, user_id, role="admin")
        assert portal_store.get_user(connection, user_id)["role"] == "admin"

        # Normal intake approval must not demote a pending administrator.
        portal_store.create_pending_account(
            connection,
            username="promoteme",
            display_name="Promoted",
            contact="promoted@example.invalid",
            member_id="m-1",
            linux_username="promoteme",
            activation_ttl_hours=24,
        )
        assert portal_store.get_user(connection, user_id)["role"] == "admin"

        new_id = portal_store.create_pending_account(
            connection,
            username="newadmin",
            display_name="New Admin",
            contact="admin@example.invalid",
            member_id="",
            linux_username="",
            activation_ttl_hours=24,
            role="admin",
        )["user_id"]
        assert portal_store.get_user(connection, new_id)["role"] == "admin"

        with pytest.raises(ValueError):
            portal_store.update_user_role(connection, user_id, role="owner")
    finally:
        connection.close()


def test_portal_reset_activation_invalidates_in_flight_pending_totp(tmp_path: Path):
    db = tmp_path / "portal.db"
    connection = portal_store.init_portal_db(db)
    try:
        user_id = portal_store.create_user(
            connection,
            username="pendinguser",
            status="pending_activation",
            member_id="m-1",
        )
        first = portal_store.create_activation_code(
            connection,
            user_id,
            ttl_hours=24,
            now=NOW,
        )
        assert first["code"]
        portal_store.set_pending_totp(
            connection,
            user_id,
            pending_totp_enc=security.seal(SECRET, security.new_totp_secret()),
            now=NOW,
        )
        assert portal_store.get_user(connection, user_id)["pending_totp_enc"]

        second = portal_store.create_activation_code(
            connection,
            user_id,
            ttl_hours=24,
            now=NOW,
        )
        assert second["code"] != first["code"]
        assert portal_store.get_user(connection, user_id)["pending_totp_enc"] == ""
        assert portal_store.get_activation(connection, first["code"], now=NOW) is None
    finally:
        connection.close()


def test_safe_next_rejects_non_local_redirect_targets():
    assert portal_server._safe_next("/status") == "/status"
    assert portal_server._safe_next("//evil.example") == "/"
    assert portal_server._safe_next("/\\evil.example") == "/"
    assert portal_server._safe_next("https://evil.example") == "/"
    assert portal_server._safe_next("/status\r\nLocation: https://evil.example") == "/"


def test_trusted_client_ip_ignores_spoofed_header_from_untrusted_peer():
    assert trusted_client_ip("127.0.0.1", "203.0.113.8") == "203.0.113.8"
    assert trusted_client_ip("::1", "203.0.113.8") == "203.0.113.8"
    assert trusted_client_ip("198.51.100.4", "203.0.113.8") == "198.51.100.4"
    assert trusted_client_ip("127.0.0.1", "not-an-ip") == "127.0.0.1"
    assert trusted_client_ip("127.0.0.1", "203.0.113.8, 198.51.100.9") == "127.0.0.1"
    assert trusted_client_ip(None, None) == "unknown"


def test_portal_config_rejects_public_bind_and_insecure_base_url(tmp_path: Path):
    source = (ROOT / "lab/resources.example.toml").read_text(encoding="utf-8")
    assert "enabled = false" in source
    assert "bind_port = 8770" in source

    public_bind = source.replace(
        'bind_host = "127.0.0.1"',
        'bind_host = "0.0.0.0"',
    )
    path = tmp_path / "public.toml"
    path.write_text(public_bind, encoding="utf-8")
    with pytest.raises(ValueError, match="loopback"):
        load_config(path)

    insecure_cookie = source.replace(
        'base_url = ""',
        'base_url = "http://portal.example.invalid"',
    )
    path = tmp_path / "insecure.toml"
    path.write_text(insecure_cookie, encoding="utf-8")
    with pytest.raises(ValueError, match="cookie_secure"):
        load_config(path)

    public_proxy = source.replace(
        'trusted_proxy_cidrs = ["127.0.0.1/32", "::1/128"]',
        'trusted_proxy_cidrs = ["0.0.0.0/0"]',
    )
    path = tmp_path / "public-proxy.toml"
    path.write_text(public_proxy, encoding="utf-8")
    with pytest.raises(ValueError, match="loopback"):
        load_config(path)


async def test_login_dashboard_status_and_no_dangerous_routes(portal):
    client, broker, config = portal
    _, totp_secret = _seed_account(config.portal.db_path, username="alice")
    await _login(client, "alice", PASSWORD, totp_secret)

    response = await client.get("/")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]

    response = await client.get("/status")
    assert response.status == 200
    assert "monitor" in await response.text()

    for path in ("/allocate", "/release", "/queue", "/shell", "/vscode", "/files"):
        assert (await client.get(path)).status == 404

    commands = [call[0] for call in broker.calls]
    assert "portal_status" in commands

    response = await client.post(
        "/logout",
        data={"_csrf": _cookie(client, portal_server.CSRF_COOKIE)},
        allow_redirects=False,
    )
    assert response.status == 302
    assert _cookie(client, portal_server.SESSION_COOKIE) == ""


async def test_post_requires_csrf_token(portal):
    client, _, _ = portal
    response = await client.get("/login")
    assert response.status == 200
    response = await client.post(
        "/login",
        data={"username": "alice", "password": PASSWORD, "totp": "000000"},
    )
    assert response.status == 403

    response = await client.post(
        "/login",
        data={
            "_csrf": "wrong",
            "username": "alice",
            "password": PASSWORD,
            "totp": "000000",
        },
    )
    assert response.status == 403


async def test_registration_submits_through_broker_and_links_local_row(portal):
    client, broker, config = portal
    broker.responses["portal_registration_submit"] = {
        "request_id": "request-1",
        "receipt_secret": "receipt-secret",
        "username": "newmember",
    }
    response = await client.get("/register")
    assert response.status == 200
    csrf = _cookie(client, portal_server.CSRF_COOKIE)
    response = await client.post(
        "/register",
        data={
            "_csrf": csrf,
            **VALID_APPLICATION,
            "public_keys": "\n".join(VALID_APPLICATION["public_keys"]),
        },
    )
    assert response.status == 200
    text = await response.text()
    assert "request-1" in text
    assert "receipt-secret" in text

    connection = portal_store.connect(config.portal.db_path)
    try:
        registration = portal_store.find_pending_registration(connection, "newmember")
        assert registration is not None
        assert registration["request_id"] == "request-1"
    finally:
        connection.close()

    command, actor, args = broker.calls[-1]
    assert command == "portal_registration_submit"
    assert actor["role"] == "applicant"
    assert args["application"]["username"] == "newmember"


async def test_activation_sets_password_and_totp(portal):
    client, broker, config = portal
    user_id, _ = _seed_account(
        config.portal.db_path,
        username="pendinguser",
        status="pending_activation",
        member_id="m-pending",
    )
    connection = portal_store.connect(config.portal.db_path)
    try:
        activation = portal_store.create_activation_code(
            connection,
            user_id,
            ttl_hours=24,
        )
    finally:
        connection.close()

    broker.responses["portal_member_self"] = {
        "member": {"status": "active", "linux_uid": 2000},
        "keys": [],
    }
    response = await client.get("/activate")
    assert response.status == 200
    csrf = _cookie(client, portal_server.CSRF_COOKIE)
    response = await client.post(
        "/activate/start",
        data={
            "_csrf": csrf,
            "username": "pendinguser",
            "activation_code": activation["code"],
        },
    )
    assert response.status == 200
    text = await response.text()
    secret_match = re.search(r'<code class="secret">([^<]+)</code>', text)
    token_match = re.search(r'name="activation_token" value="([^"]+)"', text)
    assert secret_match is not None
    assert token_match is not None
    totp_secret = secret_match.group(1).strip()

    response = await client.post(
        "/activate/complete",
        data={
            "_csrf": csrf,
            "activation_token": token_match.group(1),
            "password": NEW_PASSWORD,
            "password_confirm": NEW_PASSWORD,
            "totp": pyotp.TOTP(totp_secret).now(),
        },
        allow_redirects=False,
    )
    assert response.status == 302
    assert response.headers["Location"].startswith("/login")

    connection = portal_store.connect(config.portal.db_path)
    try:
        user = portal_store.get_user(connection, user_id)
        assert user is not None
        assert user["status"] == "active"
        assert security.verify_password(user["password_hash"], NEW_PASSWORD)
        assert security.unseal(SECRET, user["totp_secret_enc"]) == totp_secret
    finally:
        connection.close()


async def test_member_cannot_use_admin_routes(portal):
    client, _, config = portal
    _, totp_secret = _seed_account(config.portal.db_path, username="alice")
    await _login(client, "alice", PASSWORD, totp_secret)
    response = await client.get("/admin/registrations")
    assert response.status == 403
    csrf = _cookie(client, portal_server.CSRF_COOKIE)
    response = await client.post(
        "/admin/members/m-1/suspend",
        data={
            "_csrf": csrf,
            "password": PASSWORD,
            "totp": pyotp.TOTP(totp_secret).now(),
            "reason": "test",
        },
    )
    assert response.status == 403


async def test_admin_resume_does_not_activate_pending_portal_account(portal):
    client, broker, config = portal
    admin_id, admin_secret = _seed_account(
        config.portal.db_path,
        username="admin",
        role="admin",
        member_id="m-admin",
    )
    pending_id, _ = _seed_account(
        config.portal.db_path,
        username="pendinguser",
        status="pending_activation",
        member_id="m-pending",
    )
    broker.responses["portal_members_resume"] = {
        "member_id": "m-pending",
        "status": "active",
    }
    await _login(client, "admin", PASSWORD, admin_secret)
    _reset_counter(config.portal.db_path, admin_id)
    csrf = _cookie(client, portal_server.CSRF_COOKIE)
    response = await client.post(
        "/admin/members/m-pending/resume",
        data={
            "_csrf": csrf,
            "password": PASSWORD,
            "totp": pyotp.TOTP(admin_secret).now(),
            "reason": "restore Linux membership only",
        },
        allow_redirects=False,
    )
    assert response.status == 302, await response.text()
    connection = portal_store.connect(config.portal.db_path)
    try:
        pending = portal_store.get_user(connection, pending_id)
        assert pending is not None
        assert pending["status"] == "pending_activation"
    finally:
        connection.close()
    assert any(call[0] == "portal_members_resume" for call in broker.calls)


async def test_admin_mutation_requires_broker_attestation(portal):
    client, broker, config = portal
    admin_id, admin_secret = _seed_account(
        config.portal.db_path,
        username="admin",
        role="admin",
        member_id="m-admin",
    )
    broker.responses["portal_admin_attest"] = PortalBrokerError(
        "forbidden", "broker administrator verification failed"
    )
    await _login(client, "admin", PASSWORD, admin_secret)
    _reset_counter(config.portal.db_path, admin_id)
    csrf = _cookie(client, portal_server.CSRF_COOKIE)
    response = await client.post(
        "/admin/members/m-pending/resume",
        data={
            "_csrf": csrf,
            "password": PASSWORD,
            "totp": pyotp.TOTP(admin_secret).now(),
            "reason": "local credentials must not be enough",
        },
        allow_redirects=False,
    )
    assert response.status == 403, await response.text()
    assert not any(call[0] == "portal_members_resume" for call in broker.calls)


async def test_admin_approval_creates_pending_account_and_activation_code(portal):
    client, broker, config = portal
    admin_id, admin_secret = _seed_account(
        config.portal.db_path,
        username="admin",
        role="admin",
        member_id="m-admin",
    )
    connection = portal_store.connect(config.portal.db_path)
    try:
        portal_store.create_registration(
            connection,
            request_id="request-2",
            username="newmember",
            display_name="New Member",
            affiliation="Lab A",
            contact="newmember@example.invalid",
        )
    finally:
        connection.close()
    broker.responses["portal_requests_show"] = {
        "request_id": "request-2",
        "username": "newmember",
        "display_name": "New Member",
        "affiliation": "Lab A",
        "contact": "newmember@example.invalid",
        "status": "pending_review",
        "decision": None,
    }
    broker.responses["portal_requests_approve"] = {
        "request_id": "request-2",
        "member_id": "m-new",
        "status": "approved",
    }

    await _login(client, "admin", PASSWORD, admin_secret)
    _reset_counter(config.portal.db_path, admin_id)
    csrf = _cookie(client, portal_server.CSRF_COOKIE)
    response = await client.post(
        "/admin/registrations/request-2/approve",
        data={
            "_csrf": csrf,
            "password": PASSWORD,
            "totp": pyotp.TOTP(admin_secret).now(),
            "expected_version": "version-1",
            "valid_until": "",
            "quota_gb": "200",
            "review_note": "approved in test",
        },
    )
    text = await response.text()
    assert response.status == 200, text
    code_match = re.search(r'<code class="secret">([^<]+)</code>', text)
    assert code_match is not None
    activation_code = code_match.group(1).strip()

    connection = portal_store.connect(config.portal.db_path)
    try:
        account = portal_store.get_user_by_username(connection, "newmember")
        assert account is not None
        assert account["status"] == "pending_activation"
        assert account["member_id"] == "m-new"
        assert portal_store.get_activation(connection, activation_code) is not None
        registration = portal_store.get_registration_by_request(connection, "request-2")
        assert registration is not None
        assert registration["status"] == "approved"
        assert registration["portal_user_id"] == account["user_id"]
    finally:
        connection.close()
    assert any(call[0] == "portal_requests_approve" for call in broker.calls)
