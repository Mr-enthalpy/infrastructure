"""Low-privilege intake HTTP service (goal member-onboarding-and-ux.md §5/§9).

This service is the public application entry for people WITHOUT an SSH
account. It is deliberately narrow:

- binds loopback by default (production exposure is via the lab's reverse
  proxy, restricted to the lab network);
- owns ONLY its own SQLite store (invitations + applications + receipts);
  it has no access to the broker socket, member database, Docker, or GPUs;
- public operations: show guide info, validate an invitation and submit an
  application, query/cancel one application with the private receipt;
- no approval, provisioning, role change, or resource endpoints (§9.2);
- every response includes Cache-Control: no-store; secrets never appear in
  URLs or logs.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from . import store as intake_store

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 32 * 1024
REQUESTS_DB_KEY: web.AppKey[str] = web.AppKey("requests_db", str)
ONBOARDING_KEY: web.AppKey[dict[str, str]] = web.AppKey("onboarding", dict)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _json(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def create_app(
    *,
    requests_db: str,
    onboarding: dict[str, str] | None = None,
) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[REQUESTS_DB_KEY] = requests_db
    app[ONBOARDING_KEY] = onboarding or {}
    app.router.add_get("/api/guide", _api_guide)
    app.router.add_post("/api/requests", _api_submit)
    app.router.add_get("/api/requests/{request_id}", _api_status)
    app.router.add_post("/api/requests/{request_id}/cancel", _api_cancel)
    return app


def _onboarding(request: web.Request) -> dict[str, str]:
    return request.app[ONBOARDING_KEY]


async def _api_guide(request: web.Request) -> web.Response:
    onboarding = _onboarding(request)
    return _json(
        {
            "ok": True,
            "data": {
                "guide_url": onboarding.get("guide_url", ""),
                "apply_url": onboarding.get("apply_url", ""),
                "support_label": onboarding.get("support_label", ""),
                "support_contact": onboarding.get("support_contact", ""),
            },
        }
    )


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def _api_submit(request: web.Request) -> web.Response:
    body = await _read_json(request)
    if body is None:
        return _json(_error("bad_request", "申请内容必须是 JSON 对象"), 400)
    invite_id = str(body.get("invite_id", "") or "")
    invite_secret = str(body.get("invite_secret", "") or "")
    application = body.get("application")
    if not invite_id or not invite_secret or not isinstance(application, dict):
        return _json(_error("bad_request", "缺少邀请信息或申请内容"), 400)
    connection = intake_store.connect(request.app[REQUESTS_DB_KEY])
    try:
        result = intake_store.submit_application(
            connection,
            invite_id=invite_id,
            invite_secret=invite_secret,
            application=application,
        )
    except ValueError as exc:
        logger.info("application rejected: %s", exc)
        return _json(_error("rejected", str(exc)), 400)
    except Exception:
        logger.exception("intake submission failed")
        return _json(_error("internal", "提交失败，请稍后重试"), 500)
    finally:
        connection.close()
    # The receipt secret is shown once; it is never logged and never returned
    # again by any endpoint.
    return _json(
        {
            "ok": True,
            "data": {
                "request_id": result["request_id"],
                "receipt_secret": result["receipt_secret"],
            },
        }
    )


async def _api_status(request: web.Request) -> web.Response:
    request_id = request.match_info.get("request_id", "")
    receipt_secret = request.query.get("receipt", "")
    if not request_id or not receipt_secret:
        return _json(_error("bad_request", "需要申请编号与回执密钥"), 400)
    connection = intake_store.connect(request.app[REQUESTS_DB_KEY])
    try:
        status = intake_store.request_status(
            connection, request_id=request_id, receipt_secret=receipt_secret
        )
    except ValueError as exc:
        return _json(_error("not_found", str(exc)), 404)
    finally:
        connection.close()
    return _json({"ok": True, "data": status})


async def _api_cancel(request: web.Request) -> web.Response:
    request_id = request.match_info.get("request_id", "")
    body = await _read_json(request)
    receipt_secret = str((body or {}).get("receipt", "") or "")
    if not request_id or not receipt_secret:
        return _json(_error("bad_request", "需要申请编号与回执密钥"), 400)
    connection = intake_store.connect(request.app[REQUESTS_DB_KEY])
    try:
        result = intake_store.cancel_request(
            connection, request_id=request_id, receipt_secret=receipt_secret
        )
    except ValueError as exc:
        return _json(_error("not_found", str(exc)), 404)
    finally:
        connection.close()
    return _json({"ok": True, "data": result})


def main() -> None:
    """Local development entry: loopback-only HTTP (§9.2)."""
    import os

    from ..config import load_config

    config_path = os.environ.get("UDC_RESOURCE_CONFIG", "lab/resources.example.toml")
    config = load_config(config_path)
    onboarding = {
        "guide_url": config.onboarding.guide_url,
        "apply_url": config.onboarding.apply_url,
        "support_label": config.onboarding.support_label,
        "support_contact": config.onboarding.support_contact,
    }
    app = create_app(
        requests_db=config.onboarding.intake_db,
        onboarding=onboarding,
    )
    web.run_app(app, host="127.0.0.1", port=8765, print=None)


if __name__ == "__main__":
    main()
