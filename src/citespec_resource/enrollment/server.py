"""Low-privilege intake HTTP service (goal member-onboarding-and-ux.md §5/§9).

This service is the public application entry for people WITHOUT an SSH
account. It is deliberately narrow:

- binds loopback by default (production exposure is via the lab's reverse
  proxy, restricted to the lab network);
- owns ONLY its own SQLite store (invitations + applications + receipts);
  it has no access to the broker socket, member database, Docker, or GPUs;
- public operations: show guide info and a real HTML form, validate an
  invitation and submit an application, query/cancel one application with the
  private receipt (POST body only), no approval/provisioning endpoints (§9.2);
- every response includes Cache-Control: no-store; receipt secrets never
  appear in URLs, logs, analytics, or the HTML page.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from ..netutil import trusted_client_ip
from . import store as intake_store

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 32 * 1024
STATIC_DIR = Path(__file__).with_name("static")
REQUESTS_DB_KEY: web.AppKey[str] = web.AppKey("requests_db", str)
ONBOARDING_KEY: web.AppKey[dict[str, str]] = web.AppKey("onboarding", dict)
RATE_LIMIT_KEY: web.AppKey[dict[tuple[str, str], list[float]]] = web.AppKey("rate_limit", dict)
RATE_LIMITS_KEY: web.AppKey[dict[str, tuple[int, float]]] = web.AppKey("rate_limits", dict)

# Per-IP sliding-window limits. Deliberately generous for a lab and low enough
# that a single client cannot enumerate invitation/receipt secrets at speed.
RATE_LIMITS = {
    "submit": (10, 60.0),
    "status": (40, 60.0),
    "cancel": (10, 60.0),
}

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; form-action 'self'; "
        "connect-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'"
    ),
}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _json(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=SECURITY_HEADERS)


@web.middleware
async def _security_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        for header, value in SECURITY_HEADERS.items():
            exc.headers.setdefault(header, value)
        raise
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


def create_app(
    *,
    requests_db: str,
    onboarding: dict[str, str] | None = None,
    rate_limits: dict[str, tuple[int, float]] | None = None,
) -> web.Application:
    # The intake service owns its schema.  Initializing here makes a fresh
    # deployment usable without a separate privileged migration step and keeps
    # the admin CLI's direct invitation writer working immediately.
    intake_store.init_intake_db(requests_db).close()
    app = web.Application(
        client_max_size=MAX_BODY_BYTES,
        middlewares=[_security_middleware],
    )
    app[REQUESTS_DB_KEY] = requests_db
    app[ONBOARDING_KEY] = onboarding or {}
    app[RATE_LIMIT_KEY] = {}
    app[RATE_LIMITS_KEY] = rate_limits or RATE_LIMITS
    app.router.add_get("/", _index)
    app.router.add_get("/api/guide", _api_guide)
    app.router.add_post("/api/requests", _api_submit)
    # Receipts are NEVER accepted in a URL or query string. Status and cancel
    # both require a POST body so the secret does not land in proxy logs or
    # browser history.
    app.router.add_post("/api/requests/status", _api_status)
    app.router.add_post("/api/requests/{request_id}/cancel", _api_cancel)
    return app


def _onboarding(request: web.Request) -> dict[str, str]:
    return request.app[ONBOARDING_KEY]


async def _index(request: web.Request) -> web.StreamResponse:
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        return _json(_error("unavailable", "申请页面尚未安装"), 503)
    return web.FileResponse(path, headers=SECURITY_HEADERS)


def _rate_limited(request: web.Request, bucket: str) -> bool:
    limit, window = request.app[RATE_LIMITS_KEY].get(bucket, (60, 60.0))
    if limit <= 0:
        return False
    client_ip = trusted_client_ip(
        request.remote,
        request.headers.get("X-Citespec-Client-IP"),
    )
    key = (client_ip, bucket)
    now = time.monotonic()
    state = request.app[RATE_LIMIT_KEY]
    stamps = [stamp for stamp in state.get(key, []) if now - stamp < window]
    state[key] = stamps
    if len(stamps) >= limit:
        return True
    stamps.append(now)
    return False


def _too_many() -> web.Response:
    response = _json(_error("rate_limited", "请求过于频繁，请稍后再试"), 429)
    response.headers["Retry-After"] = "60"
    return response


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
    if _rate_limited(request, "submit"):
        return _too_many()
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
    if _rate_limited(request, "status"):
        return _too_many()
    body = await _read_json(request)
    request_id = str((body or {}).get("request_id", "") or "").strip()
    receipt_secret = str((body or {}).get("receipt", "") or "")
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
    labels = {
        "pending_review": "待审核",
        "needs_info": "需要补充材料",
        "approved": "已批准",
        "rejected": "未通过",
        "cancelled": "已撤回",
    }
    current = str(status.get("status", ""))
    status["status_label"] = labels.get(current, current)
    if status.get("status") in {"approved", "rejected", "cancelled"}:
        status["decision_at"] = status.get("updated_at", "")
    return _json({"ok": True, "data": status})


async def _api_cancel(request: web.Request) -> web.Response:
    if _rate_limited(request, "cancel"):
        return _too_many()
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

    config_path = os.environ.get("CITESPEC_RESOURCE_CONFIG", "lab/resources.example.toml")
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
