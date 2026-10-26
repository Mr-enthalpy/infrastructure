"""Intake HTTP service tests (goal §9, AC-08/09/10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from citespec_resource.enrollment import server as intake_server
from citespec_resource.enrollment import store as intake_store

ONBOARDING = {
    "guide_url": "https://lab.example.invalid/compute",
    "apply_url": "https://lab.example.invalid/compute/join",
    "support_label": "计算资源管理员",
    "support_contact": "admin@lab.example.invalid",
}

VALID_APPLICATION = {
    "display_name": "Test Member",
    "username": "testmember",
    "affiliation": "Lab A",
    "purpose": "training runs",
    "contact": "member@example.invalid",
    "nickname": "",
    "public_contact": "",
    "agree_terms": "1",
    "public_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@lab"],
}


@pytest.fixture
def intake_db(tmp_path: Path) -> str:
    path = tmp_path / "intake.db"
    connection = intake_store.init_intake_db(path)
    connection.close()
    return str(path)


@pytest.fixture
async def client(intake_db: str):
    app = intake_server.create_app(requests_db=intake_db, onboarding=ONBOARDING)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


async def test_guide_endpoint_is_public(client):
    response = await client.get("/api/guide")
    assert response.status == 200
    payload = await response.json()
    assert payload["data"]["guide_url"] == ONBOARDING["guide_url"]


async def test_submit_requires_valid_invitation(client, intake_db):
    response = await client.post(
        "/api/requests",
        json={"invite_id": "nope", "invite_secret": "nope", "application": VALID_APPLICATION},
    )
    assert response.status == 400
    payload = await response.json()
    assert payload["error"]["code"] == "rejected"


async def test_submit_with_valid_invitation_returns_receipt_once(client, intake_db):
    connection = intake_store.connect(intake_db)
    invitation = intake_store.create_invitation(connection, created_by="admin")
    connection.close()
    response = await client.post(
        "/api/requests",
        json={
            "invite_id": invitation["invite_id"],
            "invite_secret": invitation["invite_secret"],
            "application": VALID_APPLICATION,
        },
    )
    assert response.status == 200
    payload = await response.json()
    request_id = payload["data"]["request_id"]
    receipt = payload["data"]["receipt_secret"]
    assert request_id and receipt

    # Status requires the receipt in a POST body; guessing ids yields 404
    # (AC-09) and a receipt in the URL is not a supported route at all.
    ok = await client.post(
        "/api/requests/status", json={"request_id": request_id, "receipt": receipt}
    )
    assert ok.status == 200
    assert (await ok.json())["data"]["status"] == "pending_review"
    bad = await client.post(
        "/api/requests/status", json={"request_id": request_id, "receipt": "wrong"}
    )
    assert bad.status == 404
    guessed = await client.post(
        "/api/requests/status", json={"request_id": "000000", "receipt": "x"}
    )
    assert guessed.status == 404
    url_leak = await client.get(f"/api/requests/{request_id}?receipt={receipt}")
    assert url_leak.status == 404


async def test_private_key_submission_is_rejected_cleanly(client, intake_db):
    connection = intake_store.connect(intake_db)
    invitation = intake_store.create_invitation(connection, created_by="admin")
    connection.close()
    bad_application = dict(VALID_APPLICATION)
    bad_application["public_keys"] = ["-----BEGIN OPENSSH PRIVATE KEY-----"]
    response = await client.post(
        "/api/requests",
        json={
            "invite_id": invitation["invite_id"],
            "invite_secret": invitation["invite_secret"],
            "application": bad_application,
        },
    )
    assert response.status == 400
    payload = await response.json()
    assert "私钥" in payload["error"]["message"]


async def test_oversized_body_is_rejected(client):
    response = await client.post(
        "/api/requests",
        data=b"x" * (40 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert response.status in (400, 413)


async def test_public_html_intake_page_is_served_with_security_headers(client):
    response = await client.get("/")
    assert response.status == 200
    assert "text/html" in response.headers["Content-Type"]
    body = await response.text()
    assert "申请使用共享 GPU 资源" in body
    assert 'id="apply-form"' in body
    assert 'id="status-form"' in body
    assert response.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_submit_rate_limit_is_enforced(intake_db):
    app = intake_server.create_app(
        requests_db=intake_db,
        onboarding=ONBOARDING,
        rate_limits={"submit": (1, 60.0), "status": (1, 60.0), "cancel": (1, 60.0)},
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        first = await client.post(
            "/api/requests",
            json={"invite_id": "x", "invite_secret": "y", "application": {}},
        )
        assert first.status == 400
        second = await client.post(
            "/api/requests",
            json={"invite_id": "x", "invite_secret": "y", "application": {}},
        )
        assert second.status == 429
        assert second.headers["Retry-After"] == "60"
    finally:
        await client.close()


async def test_cancel_with_receipt(client, intake_db):
    connection = intake_store.connect(intake_db)
    invitation = intake_store.create_invitation(connection, created_by="admin")
    connection.close()
    submitted = await client.post(
        "/api/requests",
        json={
            "invite_id": invitation["invite_id"],
            "invite_secret": invitation["invite_secret"],
            "application": VALID_APPLICATION,
        },
    )
    payload = await submitted.json()
    request_id = payload["data"]["request_id"]
    receipt = payload["data"]["receipt_secret"]
    cancelled = await client.post(f"/api/requests/{request_id}/cancel", json={"receipt": receipt})
    assert cancelled.status == 200
    assert (await cancelled.json())["data"]["status"] == "cancelled"


def test_secrets_never_written_to_store(intake_db, tmp_path: Path):
    """Invitation/receipt plaintexts exist only in the return values."""
    connection = intake_store.init_intake_db(Path(intake_db))
    invitation = intake_store.create_invitation(connection, created_by="admin")
    submitted = intake_store.submit_application(
        connection,
        invite_id=invitation["invite_id"],
        invite_secret=invitation["invite_secret"],
        application=VALID_APPLICATION,
    )
    connection.close()
    raw = Path(intake_db).read_bytes()
    assert invitation["invite_secret"].encode() not in raw
    assert submitted["receipt_secret"].encode() not in raw
