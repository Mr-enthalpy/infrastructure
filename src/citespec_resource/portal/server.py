"""VPN-only membership portal HTTP service.

The portal is deliberately narrow.  It offers registration intake, local
account/TOTP activation, member information and read-only service status, plus
simple administrator approval and member-state management.  It never exposes
GPU allocation/release/queue operations, a web shell, VS Code, or file transfer;
those remain CLI/SSH capabilities.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web
from aiohttp.helpers import RequestKey
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import LabConfig
from ..enrollment import store as intake_store
from ..netutil import trusted_client_ip
from . import security, store
from .broker import PortalActor, PortalBroker, PortalBrokerError

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
TEMPLATE_DIR = Path(__file__).with_name("templates")
SESSION_COOKIE = "citespec_portal_session"
CSRF_COOKIE = "citespec_portal_csrf"
SESSION_KEY: RequestKey[dict[str, Any] | None] = RequestKey("portal_session")
USER_KEY: RequestKey[dict[str, Any] | None] = RequestKey("portal_user")
CSRF_KEY: RequestKey[str] = RequestKey("portal_csrf")
CONFIG_KEY: web.AppKey[LabConfig] = web.AppKey("portal_config", LabConfig)
DB_KEY: web.AppKey[str] = web.AppKey("portal_db", str)
SECRET_KEY: web.AppKey[bytes] = web.AppKey("portal_secret", bytes)
BROKER_KEY: web.AppKey[PortalBroker] = web.AppKey("portal_broker", PortalBroker)
TEMPLATES_KEY: web.AppKey[Environment] = web.AppKey("portal_templates", Environment)
CLOCK_KEY: web.AppKey[Callable[[], datetime]] = web.AppKey("portal_clock")

REQUEST_STATUSES = {
    "pending_review",
    "needs_info",
    "approved",
    "rejected",
    "cancelled",
    "withdrawn",
}
ADMIN_COMMANDS = {
    "approve": "portal_requests_approve",
    "reject": "portal_requests_reject",
    "suspend": "portal_members_suspend",
    "resume": "portal_members_resume",
    "revoke": "portal_members_revoke",
}

REGISTRATION_LIMIT = 5
REGISTRATION_WINDOW = 3600.0
LOGIN_IP_LIMIT = 20
ACTIVATION_LIMIT = 10

_SAFE_USER_FIELDS = {
    "user_id",
    "username",
    "role",
    "status",
    "member_id",
    "linux_username",
    "display_name",
    "contact",
    "created_at",
    "updated_at",
    "activated_at",
    "last_login_at",
}


class PortalFormError(ValueError):
    """A user-facing form/authorization error."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class _Limiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        events = self._events.setdefault(key, deque())
        while events and events[0] <= now - window:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        return True


LIMITER_KEY: web.AppKey[_Limiter] = web.AppKey("portal_limiter", _Limiter)


def _clock() -> datetime:
    return datetime.now(UTC)


def _safe_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {key: user[key] for key in _SAFE_USER_FIELDS if key in user}


def _actor(user: dict[str, Any]) -> PortalActor:
    return PortalActor(
        user_id=str(user["user_id"]),
        username=str(user["username"]),
        role=str(user["role"]),
        member_id=str(user.get("member_id", "")),
    )


def _anonymous_actor() -> PortalActor:
    return PortalActor(
        user_id="anonymous",
        username="anonymous",
        role="applicant",
    )


def _client_ip(request: web.Request) -> str:
    config = request.app[CONFIG_KEY]
    return trusted_client_ip(
        request.remote,
        request.headers.get("X-Citespec-Client-IP"),
        trusted_proxies=config.portal.trusted_proxy_cidrs,
    )


@contextmanager
def _db(request: web.Request):
    connection = store.connect(request.app[DB_KEY])
    try:
        yield connection
    finally:
        connection.close()


def _current_user(request: web.Request) -> dict[str, Any] | None:
    value = request.get(USER_KEY)
    return value if isinstance(value, dict) else None


def _require_user(request: web.Request) -> dict[str, Any]:
    user = _current_user(request)
    if user is None:
        target = quote(request.rel_url.path_qs, safe="/?=&")
        raise web.HTTPFound(f"/login?next={target}")
    return user


def _require_admin(request: web.Request) -> dict[str, Any]:
    user = _require_user(request)
    if user.get("role") != "admin":
        raise PortalFormError("需要 Portal 管理员权限", 403)
    return user


def _render(
    request: web.Request,
    template: str,
    *,
    status: int = 200,
    **context: Any,
) -> web.Response:
    config = request.app[CONFIG_KEY]
    user = _current_user(request)
    values: dict[str, Any] = {
        "app_name": "Citespec Portal",
        "base_url": config.portal.base_url,
        "support_label": config.onboarding.support_label,
        "support_contact": config.onboarding.support_contact,
        "user": _safe_user(user),
        "is_admin": bool(user and user.get("role") == "admin"),
        "csrf": request[CSRF_KEY],
        "notice": request.query.get("notice", ""),
        "error": request.query.get("error", ""),
        "activated": request.query.get("activated", ""),
        "registration_enabled": config.portal.registration_enabled,
        "now": request.app[CLOCK_KEY]().isoformat(),
    }
    values.update(context)
    html = request.app[TEMPLATES_KEY].get_template(template).render(**values)
    return web.Response(text=html, content_type="text/html", status=status)


def _form_error(request: web.Request, error: Exception, *, status: int = 400) -> web.Response:
    message = getattr(error, "message", None) or str(error)
    return _render(request, "error.html", status=status, error=message)


def _broker(request: web.Request) -> PortalBroker:
    return request.app[BROKER_KEY]


async def _broker_call(
    request: web.Request,
    user: dict[str, Any],
    command: str,
    **args: Any,
) -> Any:
    try:
        return await _broker(request).call(command, actor=_actor(user), **args)
    except PortalBrokerError as exc:
        raise PortalFormError(f"服务暂不可用：{exc.message}", 503) from exc


async def _public_broker_call(
    request: web.Request,
    command: str,
    **args: Any,
) -> Any:
    try:
        return await _broker(request).call(command, actor=_anonymous_actor(), **args)
    except PortalBrokerError as exc:
        raise PortalFormError(f"服务暂不可用：{exc.message}", 503) from exc


def _form_text(post: Any, key: str, *, strip: bool = True) -> str:
    value = post.get(key, "")
    if not isinstance(value, str):
        return ""
    return value.strip() if strip else value


def _form_keys(post: Any) -> list[str]:
    raw = _form_text(post, "public_keys", strip=False)
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _audit(
    request: web.Request,
    action: str,
    *,
    user: dict[str, Any] | None = None,
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    actor = user if user is not None else _current_user(request)
    with _db(request) as connection:
        store.record_audit(
            connection,
            action=action,
            actor_user_id=str(actor.get("user_id", "")) if actor else "",
            actor_username=str(actor.get("username", "")) if actor else "",
            target=target,
            detail=detail,
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
            now=request.app[CLOCK_KEY](),
        )


def _set_session_cookie(response: web.Response, token: str, config: LabConfig) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=config.portal.session_ttl_hours * 3600,
        httponly=True,
        secure=config.portal.cookie_secure,
        samesite="Lax",
        path="/",
    )


def _clear_session_cookie(response: web.Response, config: LabConfig) -> None:
    response.del_cookie(
        SESSION_COOKIE,
        path="/",
        secure=config.portal.cookie_secure,
        httponly=True,
        samesite="Lax",
    )


def _set_csrf_cookie(response: web.Response, token: str, config: LabConfig) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=max(3600, config.portal.session_ttl_hours * 3600),
        httponly=True,
        secure=config.portal.cookie_secure,
        samesite="Lax",
        path="/",
    )


def _security_headers(response: web.Response, config: LabConfig) -> None:
    headers = response.headers
    headers["Cache-Control"] = "no-store"
    headers["Pragma"] = "no-cache"
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "connect-src 'self'"
    )
    if config.portal.cookie_secure:
        headers["Strict-Transport-Security"] = "max-age=31536000"


@web.middleware
async def _portal_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Any],
) -> web.StreamResponse:
    config = request.app[CONFIG_KEY]
    token = request.cookies.get(SESSION_COOKIE, "")
    session: dict[str, Any] | None = None
    user: dict[str, Any] | None = None
    if token:
        with _db(request) as connection:
            session = store.get_session(
                connection,
                token,
                now=request.app[CLOCK_KEY](),
            )
        if session is not None:
            user = session["user"]
            with _db(request) as connection:
                store.touch_session(
                    connection,
                    token,
                    ttl_hours=config.portal.session_ttl_hours,
                    now=request.app[CLOCK_KEY](),
                )
    request[SESSION_KEY] = session
    request[USER_KEY] = user
    cookie_csrf = request.cookies.get(CSRF_COOKIE, "")
    csrf = str(session["csrf_token"]) if session is not None else cookie_csrf
    if not csrf:
        csrf = security.new_csrf_token()
    request[CSRF_KEY] = csrf

    try:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.path.startswith("/static/"):
                return web.Response(status=405)
            try:
                post = await request.post()
            except (ValueError, web.HTTPException) as exc:
                raise PortalFormError("表单格式不正确", 400) from exc
            form_token = post.get("_csrf", "")
            if not isinstance(form_token, str) or not cookie_csrf:
                raise PortalFormError("表单已过期，请刷新页面后重试", 403)
            if not security.constant_time_equal(form_token, cookie_csrf):
                raise PortalFormError("表单校验失败，请刷新页面后重试", 403)
            if session is not None and not security.constant_time_equal(
                form_token, str(session["csrf_token"])
            ):
                raise PortalFormError("会话校验失败，请重新登录", 403)
        response = await handler(request)
    except PortalFormError as exc:
        response = _render(request, "error.html", status=exc.status, error=exc.message)
    except web.HTTPException as exc:
        # Raise rather than returning the exception object: aiohttp 3.14
        # deprecates returning HTTPException from a handler/middleware.  Apply
        # the same cookie and security-header policy first.
        _security_headers(exc, config)
        if session is not None:
            _set_csrf_cookie(exc, str(session["csrf_token"]), config)
        elif not cookie_csrf:
            _set_csrf_cookie(exc, request[CSRF_KEY], config)
        if token and session is None:
            _clear_session_cookie(exc, config)
        raise
    _security_headers(response, config)
    if session is not None:
        _set_csrf_cookie(response, str(session["csrf_token"]), config)
    elif not cookie_csrf:
        _set_csrf_cookie(response, request[CSRF_KEY], config)
    if token and session is None:
        _clear_session_cookie(response, config)
    return response


def _safe_next(value: str) -> str:
    """Only allow a local, header-safe post-login redirect target."""

    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(character) < 32 for character in value):
        return "/"
    return value


def _parse_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _login_error(
    request: web.Request,
    message: str,
    status: int = 401,
    *,
    next_url: str = "",
) -> web.Response:
    return _render(
        request,
        "login.html",
        status=status,
        error=message,
        next_url=next_url or request.query.get("next", ""),
    )


async def login_form(request: web.Request) -> web.Response:
    if _current_user(request) is not None:
        raise web.HTTPFound("/")
    return _login_error(request, "", 200)


async def login_submit(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    limiter = request.app[LIMITER_KEY]
    ip = _client_ip(request)
    post = await request.post()
    username = _form_text(post, "username")
    password = _form_text(post, "password", strip=False)
    otp = _form_text(post, "totp")
    next_url = _form_text(post, "next")
    if not limiter.allow(f"login-ip:{ip}", LOGIN_IP_LIMIT, config.portal.login_window_seconds):
        return _login_error(
            request,
            "登录尝试过于频繁，请稍后再试",
            429,
            next_url=next_url,
        )
    if not username or not password or not otp:
        return _login_error(request, "请输入用户名、密码和 TOTP", 400, next_url=next_url)
    with _db(request) as connection:
        user = store.get_user_by_username(connection, username)
        if user is None:
            return _login_error(request, "用户名或凭据不正确", next_url=next_url)
        locked_until = _parse_time(user["locked_until"])
        now = request.app[CLOCK_KEY]()
        if locked_until is not None and locked_until > now:
            return _login_error(
                request,
                "账号暂时锁定，请稍后再试或联系管理员",
                429,
                next_url=next_url,
            )
        if locked_until is not None:
            store.reset_login_lock(connection, user["user_id"])
        if user["status"] == "pending_activation":
            return _login_error(
                request,
                "账号尚未激活，请先使用激活码完成激活",
                403,
                next_url=next_url,
            )
        if user["status"] != "active":
            return _login_error(
                request,
                "账号当前不可登录，请联系管理员",
                403,
                next_url=next_url,
            )
        if not security.verify_password(user["password_hash"], password):
            store.record_login_failure(
                connection,
                user["user_id"],
                lock_after=config.portal.login_max_attempts,
                lock_seconds=config.portal.login_window_seconds,
                now=now,
            )
            _audit(request, "login_failed", user=user, detail={"reason": "password"})
            return _login_error(request, "用户名或凭据不正确", next_url=next_url)
        if not user["totp_secret_enc"]:
            return _login_error(
                request,
                "账号未绑定 TOTP，请联系管理员",
                403,
                next_url=next_url,
            )
        try:
            totp_secret = security.unseal(request.app[SECRET_KEY], user["totp_secret_enc"])
        except security.PortalSecurityError:
            logger.exception("portal TOTP secret could not be decrypted")
            return _login_error(
                request,
                "账号凭据损坏，请联系管理员",
                500,
                next_url=next_url,
            )
        counter = security.verify_totp(
            totp_secret,
            otp,
            last_counter=int(user["totp_last_counter"]),
            now=now.timestamp(),
        )
        if counter is None:
            store.record_login_failure(
                connection,
                user["user_id"],
                lock_after=config.portal.login_max_attempts,
                lock_seconds=config.portal.login_window_seconds,
                now=now,
            )
            _audit(request, "login_failed", user=user, detail={"reason": "totp"})
            return _login_error(request, "用户名或凭据不正确", next_url=next_url)
        csrf = security.new_csrf_token()
        session = store.create_session(
            connection,
            user_id=user["user_id"],
            ttl_hours=config.portal.session_ttl_hours,
            csrf_token=csrf,
            ip=ip,
            user_agent=request.headers.get("User-Agent", ""),
            now=now,
        )
        store.record_login_success(
            connection,
            user["user_id"],
            totp_counter=counter,
            now=now,
        )
        _audit(request, "login_success", user=user)
    target = _safe_next(next_url)
    response = web.HTTPFound(target)
    _set_session_cookie(response, session["token"], config)
    # Keep the double-submit cookie in sync with the new session immediately;
    # otherwise a client that does not follow the redirect would keep the
    # pre-login token and every subsequent POST would fail CSRF validation.
    _set_csrf_cookie(response, csrf, config)
    raise response


async def logout(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    user = _current_user(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        with _db(request) as connection:
            store.delete_session(connection, token)
    if user is not None:
        _audit(request, "logout", user=user)
    response = web.HTTPFound("/login")
    _clear_session_cookie(response, config)
    raise response


async def index(request: web.Request) -> web.Response:
    user = _require_user(request)
    member: dict[str, Any] | None = None
    member_error = ""
    service: dict[str, Any] | None = None
    service_error = ""
    admin_pending: list[dict[str, Any]] = []
    admin_member_count: int | None = None
    try:
        service = await _broker_call(request, user, "portal_status")
    except PortalFormError as exc:
        service_error = exc.message
    if user.get("member_id"):
        try:
            member = await _broker_call(
                request,
                user,
                "portal_member_self",
                member_id=str(user["member_id"]),
            )
        except PortalFormError as exc:
            member_error = exc.message
    if user.get("role") == "admin":
        try:
            pending = await _broker_call(
                request,
                user,
                "portal_requests_list",
                status="pending_review",
            )
            admin_pending = pending if isinstance(pending, list) else []
            members = await _broker_call(request, user, "portal_members_list")
            admin_member_count = len(members.get("members", []))
        except PortalFormError as exc:
            service_error = service_error or exc.message
    return _render(
        request,
        "dashboard.html",
        member=member,
        member_error=member_error,
        service=service,
        service_error=service_error,
        admin_pending=admin_pending,
        admin_member_count=admin_member_count,
    )


async def status_page(request: web.Request) -> web.Response:
    user = _require_user(request)
    service: dict[str, Any] | None = None
    service_error = ""
    try:
        service = await _broker_call(request, user, "portal_status")
    except PortalFormError as exc:
        service_error = exc.message
    return _render(
        request,
        "status.html",
        service=service,
        service_error=service_error,
    )


async def register_form(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    if not config.portal.registration_enabled:
        raise web.HTTPNotFound()
    if _current_user(request) is not None:
        raise web.HTTPFound("/")
    return _render(request, "register.html")


async def register_submit(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    if not config.portal.registration_enabled:
        raise web.HTTPNotFound()
    limiter = request.app[LIMITER_KEY]
    ip = _client_ip(request)
    if not limiter.allow(f"register:{ip}", REGISTRATION_LIMIT, REGISTRATION_WINDOW):
        raise PortalFormError("申请提交过于频繁，请稍后再试", 429)
    post = await request.post()
    raw_application = {
        "display_name": _form_text(post, "display_name"),
        "username": _form_text(post, "username"),
        "affiliation": _form_text(post, "affiliation"),
        "purpose": _form_text(post, "purpose"),
        "contact": _form_text(post, "contact"),
        "nickname": _form_text(post, "nickname"),
        "public_contact": _form_text(post, "public_contact"),
        "agree_terms": _form_text(post, "agree_terms"),
        "public_keys": _form_keys(post),
    }
    try:
        application = intake_store.normalize_application(raw_application)
        security.validate_username(str(application["username"]))
    except (ValueError, security.PortalSecurityError) as exc:
        return _render(
            request,
            "register.html",
            status=400,
            error=str(exc),
            form=raw_application,
        )
    with _db(request) as connection:
        if store.get_user_by_username(connection, str(application["username"])) is not None:
            return _render(
                request,
                "register.html",
                status=409,
                error="该用户名已有 Portal 账号，请直接登录",
                form=raw_application,
            )
        if store.find_pending_registration(connection, str(application["username"])) is not None:
            return _render(
                request,
                "register.html",
                status=409,
                error="该用户名已有待审申请，请等待管理员处理",
                form=raw_application,
            )
    try:
        result = await _public_broker_call(
            request,
            "portal_registration_submit",
            application=application,
        )
    except PortalFormError as exc:
        return _render(
            request,
            "register.html",
            status=503,
            error=exc.message,
            form=raw_application,
        )
    request_id = str(result.get("request_id", ""))
    receipt = str(result.get("receipt_secret", ""))
    if not request_id:
        return _render(
            request,
            "register.html",
            status=502,
            error="提交未返回申请编号，请稍后重试或联系管理员",
            form=raw_application,
        )
    try:
        with _db(request) as connection:
            store.create_registration(
                connection,
                request_id=request_id,
                username=str(application["username"]),
                display_name=str(application["display_name"]),
                affiliation=str(application["affiliation"]),
                contact=str(application["contact"]),
                now=request.app[CLOCK_KEY](),
            )
    except (sqlite3.Error, ValueError):
        logger.exception("could not link portal registration %s", request_id)
    _audit(
        request,
        "registration_submitted",
        target=request_id,
        detail={"username": str(application["username"])},
    )
    return _render(
        request,
        "register_success.html",
        request_id=request_id,
        receipt_secret=receipt,
        username=str(application["username"]),
    )


async def activate_form(request: web.Request) -> web.Response:
    return _render(request, "activate_start.html")


async def activate_start(request: web.Request) -> web.Response:
    limiter = request.app[LIMITER_KEY]
    if not limiter.allow(f"activate:{_client_ip(request)}", ACTIVATION_LIMIT, 900.0):
        raise PortalFormError("激活尝试过于频繁，请稍后再试", 429)
    post = await request.post()
    username = _form_text(post, "username")
    code = _form_text(post, "activation_code")
    if not username or not code:
        return _render(
            request,
            "activate_start.html",
            status=400,
            error="请输入用户名和激活码",
            form={"username": username},
        )
    with _db(request) as connection:
        activation = store.get_activation(
            connection,
            code,
            now=request.app[CLOCK_KEY](),
        )
        if activation is None:
            return _render(
                request,
                "activate_start.html",
                status=400,
                error="激活码无效、已使用或已过期",
                form={"username": username},
            )
        activation_user = activation["user"]
        if str(activation_user["username"]).casefold() != username.casefold():
            return _render(
                request,
                "activate_start.html",
                status=400,
                error="激活码无效、已使用或已过期",
                form={"username": username},
            )
        if activation_user["status"] != "pending_activation":
            return _render(
                request,
                "activate_start.html",
                status=409,
                error="该账号已激活，请直接登录",
                form={"username": username},
            )
        member_id = str(activation_user.get("member_id", ""))
        if member_id:
            try:
                member = await _broker_call(
                    request,
                    activation_user,
                    "portal_member_self",
                    member_id=member_id,
                )
            except PortalFormError as exc:
                return _render(
                    request,
                    "activate_start.html",
                    status=503,
                    error=exc.message,
                    form={"username": username},
                )
            status = str(member.get("member", {}).get("status", ""))
            if status != "active":
                return _render(
                    request,
                    "activate_start.html",
                    status=409,
                    error=f"成员账号当前状态为 {status or '未知'}，请等开通完成后再激活",
                    form={"username": username},
                )
        totp_secret = security.new_totp_secret()
        store.set_pending_totp(
            connection,
            activation_user["user_id"],
            pending_totp_enc=security.seal(request.app[SECRET_KEY], totp_secret),
            now=request.app[CLOCK_KEY](),
        )
    token = security.sign_token(
        request.app[SECRET_KEY],
        {
            "purpose": "activate",
            "user_id": activation_user["user_id"],
            "code_hash": activation["code_hash"],
        },
        ttl_seconds=900,
    )
    _audit(
        request,
        "activation_started",
        target=str(activation_user["user_id"]),
        detail={"username": str(activation_user["username"])},
    )
    return _render(
        request,
        "activate_complete.html",
        activation_token=token,
        username=str(activation_user["username"]),
        totp_secret=totp_secret,
        otpauth_uri=security.totp_uri(
            totp_secret,
            username=str(activation_user["username"]),
            issuer=request.app[CONFIG_KEY].portal.totp_issuer,
        ),
    )


async def activate_complete(request: web.Request) -> web.Response:
    post = await request.post()
    token = _form_text(post, "activation_token")
    password = _form_text(post, "password", strip=False)
    confirm = _form_text(post, "password_confirm", strip=False)
    otp = _form_text(post, "totp")
    payload = security.verify_token(request.app[SECRET_KEY], token)
    if payload is None or payload.get("purpose") != "activate":
        raise PortalFormError("激活流程已过期，请重新输入激活码", 400)
    if not password or password != confirm:
        raise PortalFormError("两次输入的密码不一致", 400)
    user_id = str(payload.get("user_id", ""))
    code_hash = str(payload.get("code_hash", ""))
    with _db(request) as connection:
        activation_user = store.get_user(connection, user_id)
        if activation_user is None or activation_user["status"] != "pending_activation":
            raise PortalFormError("该账号不再等待激活，请直接登录或联系管理员", 409)
        if not activation_user["pending_totp_enc"]:
            raise PortalFormError("激活流程已过期，请重新输入激活码", 400)
        try:
            totp_secret = security.unseal(
                request.app[SECRET_KEY],
                activation_user["pending_totp_enc"],
            )
        except security.PortalSecurityError as exc:
            raise PortalFormError("激活信息损坏，请联系管理员", 500) from exc
        counter = security.verify_totp(
            totp_secret,
            otp,
            now=request.app[CLOCK_KEY]().timestamp(),
        )
        if counter is None:
            raise PortalFormError("TOTP 验证码不正确，请重新输入", 400)
        try:
            password_hash = security.hash_password(
                password, username=str(activation_user["username"])
            )
        except security.PortalSecurityError as exc:
            raise PortalFormError(str(exc), 400) from exc
        try:
            store.activate_user(
                connection,
                user_id,
                password_hash=password_hash,
                totp_secret_enc=security.seal(request.app[SECRET_KEY], totp_secret),
                totp_counter=counter,
                now=request.app[CLOCK_KEY](),
            )
        except ValueError as exc:
            raise PortalFormError(
                "该账号不再等待激活，请直接登录或联系管理员",
                409,
            ) from exc
        store.consume_activation(
            connection,
            code_hash,
            now=request.app[CLOCK_KEY](),
        )
    _audit(
        request,
        "account_activated",
        target=user_id,
        detail={"username": str(activation_user["username"])},
    )
    raise web.HTTPFound("/login?activated=1")


async def account_page(request: web.Request) -> web.Response:
    user = _require_user(request)
    member: dict[str, Any] | None = None
    member_error = ""
    if user.get("member_id"):
        try:
            member = await _broker_call(
                request,
                user,
                "portal_member_self",
                member_id=str(user["member_id"]),
            )
        except PortalFormError as exc:
            member_error = exc.message
    return _render(
        request,
        "account.html",
        member=member,
        member_error=member_error,
    )


async def account_password(request: web.Request) -> web.Response:
    user = _require_user(request)
    post = await request.post()
    current = _form_text(post, "current_password", strip=False)
    otp = _form_text(post, "totp")
    new_password = _form_text(post, "new_password", strip=False)
    confirm = _form_text(post, "new_password_confirm", strip=False)
    if new_password != confirm:
        return _render(
            request,
            "account.html",
            status=400,
            error="两次输入的新密码不一致",
            member=None,
            member_error="",
        )
    with _db(request) as connection:
        _verify_reauth(
            request,
            connection,
            user,
            password=current,
            otp=otp,
            action="password_change",
        )
        try:
            password_hash = security.hash_password(new_password, username=str(user["username"]))
        except security.PortalSecurityError as exc:
            return _render(
                request,
                "account.html",
                status=400,
                error=str(exc),
                member=None,
                member_error="",
            )
        store.set_password(
            connection,
            user["user_id"],
            password_hash=password_hash,
            now=request.app[CLOCK_KEY](),
        )
        store.delete_user_sessions(
            connection,
            user["user_id"],
            except_token=request.cookies.get(SESSION_COOKIE, ""),
        )
        _audit(request, "password_changed", user=user)
    raise web.HTTPFound("/account?notice=密码已更新")


def _verify_reauth(
    request: web.Request,
    connection: sqlite3.Connection,
    user: dict[str, Any],
    *,
    password: str,
    otp: str,
    action: str,
) -> None:
    if not security.verify_password(user["password_hash"], password):
        _audit(request, "reauth_failed", user=user, detail={"action": action, "reason": "password"})
        raise PortalFormError("当前密码或 TOTP 不正确", 403)
    if not user["totp_secret_enc"]:
        raise PortalFormError("账号未绑定 TOTP，请联系管理员", 403)
    try:
        totp_secret = security.unseal(request.app[SECRET_KEY], user["totp_secret_enc"])
    except security.PortalSecurityError as exc:
        raise PortalFormError("账号凭据损坏，请联系管理员", 500) from exc
    counter = security.verify_totp(
        totp_secret,
        otp,
        last_counter=int(user["totp_last_counter"]),
        now=request.app[CLOCK_KEY]().timestamp(),
    )
    if counter is None:
        _audit(request, "reauth_failed", user=user, detail={"action": action, "reason": "totp"})
        raise PortalFormError("当前密码或 TOTP 不正确", 403)
    store.set_totp_counter(
        connection,
        user["user_id"],
        counter=counter,
        now=request.app[CLOCK_KEY](),
    )


async def admin_index(request: web.Request) -> web.Response:
    _require_admin(request)
    raise web.HTTPFound("/admin/registrations")


async def admin_registrations(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    selected = request.query.get("status", "pending_review")
    if selected == "all":
        status_filter: str | None = None
    elif selected in REQUEST_STATUSES:
        status_filter = selected
    else:
        selected = "pending_review"
        status_filter = "pending_review"
    requests: list[dict[str, Any]] = []
    error = ""
    try:
        result = await _broker_call(
            request,
            admin,
            "portal_requests_list",
            status=status_filter,
        )
        requests = result if isinstance(result, list) else []
    except PortalFormError as exc:
        error = exc.message
    registrations: dict[str, dict[str, Any]] = {}
    with _db(request) as connection:
        for registration in store.list_registrations(connection):
            registrations[str(registration["request_id"])] = registration
    return _render(
        request,
        "admin_registrations.html",
        requests=requests,
        registrations=registrations,
        selected_status=selected,
        error=error,
    )


async def admin_registration_detail(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    request_id = request.match_info["request_id"]
    try:
        detail = await _broker_call(
            request,
            admin,
            "portal_requests_show",
            request_id=request_id,
        )
    except PortalFormError as exc:
        return _render(request, "error.html", status=404, error=exc.message)
    registration: dict[str, Any] | None = None
    portal_user: dict[str, Any] | None = None
    with _db(request) as connection:
        registration = store.get_registration_by_request(connection, request_id)
        if registration is not None and registration.get("portal_user_id"):
            portal_user = store.get_user(connection, str(registration["portal_user_id"]))
    return _render(
        request,
        "admin_registration_detail.html",
        request_row=detail,
        registration=registration,
        portal_user=portal_user,
    )


async def _request_detail(
    request: web.Request,
    admin: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    detail = await _broker_call(
        request,
        admin,
        "portal_requests_show",
        request_id=request_id,
    )
    return detail


async def _ensure_registration(
    request: web.Request,
    connection: sqlite3.Connection,
    request_id: str,
    detail: dict[str, Any],
) -> dict[str, Any] | None:
    registration = store.get_registration_by_request(connection, request_id)
    if registration is not None:
        return registration
    try:
        return store.create_registration(
            connection,
            request_id=request_id,
            username=str(detail.get("username", "")),
            display_name=str(detail.get("display_name", "")),
            affiliation=str(detail.get("affiliation", "")),
            contact=str(detail.get("contact", "")),
            now=request.app[CLOCK_KEY](),
        )
    except sqlite3.Error:
        logger.exception("could not create portal registration for %s", request_id)
        return None


async def admin_approve(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    request_id = request.match_info["request_id"]
    post = await request.post()
    password = _form_text(post, "password", strip=False)
    otp = _form_text(post, "totp")
    expected_version = _form_text(post, "expected_version")
    valid_until = _form_text(post, "valid_until")
    quota_raw = _form_text(post, "quota_gb")
    review_note = _form_text(post, "review_note")
    try:
        quota_gb = int(quota_raw or "200")
    except ValueError:
        return _render(request, "error.html", status=400, error="配额必须是整数")
    if quota_gb <= 0:
        return _render(request, "error.html", status=400, error="配额必须为正整数")
    with _db(request) as connection:
        _verify_reauth(
            request,
            connection,
            admin,
            password=password,
            otp=otp,
            action="request_approve",
        )
    try:
        detail = await _request_detail(request, admin, request_id)
        result = await _broker_call(
            request,
            admin,
            "portal_requests_approve",
            request_id=request_id,
            valid_until=valid_until,
            quota_gb=quota_gb,
            review_note=review_note,
            internal_note="",
            expected_version=expected_version,
        )
    except PortalFormError as exc:
        return _render(
            request,
            "error.html",
            status=409,
            error=f"批准失败：{exc.message}",
        )
    member_id = str(result.get("member_id", ""))
    if not member_id:
        return _render(
            request,
            "error.html",
            status=502,
            error="成员已批准，但未返回成员编号，请到成员列表核对",
        )
    try:
        with _db(request) as connection:
            registration = await _ensure_registration(request, connection, request_id, detail)
            account = store.create_pending_account(
                connection,
                username=str(detail.get("username", "")),
                display_name=str(detail.get("display_name", "")),
                contact=str(detail.get("contact", "")),
                member_id=member_id,
                linux_username=str(detail.get("username", "")),
                registration_id=str(registration["registration_id"]) if registration else "",
                activation_ttl_hours=request.app[CONFIG_KEY].portal.activation_ttl_hours,
                created_by=str(admin["user_id"]),
                now=request.app[CLOCK_KEY](),
            )
    except (sqlite3.Error, ValueError) as exc:
        logger.exception("could not create pending portal account for %s", request_id)
        return _render(
            request,
            "error.html",
            status=500,
            error=f"成员已批准，但 Portal 账号创建失败：{exc}",
        )
    _audit(
        request,
        "request_approved",
        user=admin,
        target=request_id,
        detail={"member_id": member_id, "username": account["username"]},
    )
    return _render(
        request,
        "activation_code.html",
        username=str(account["username"]),
        activation_code=str(account["activation_code"]),
        expires_at=str(account["expires_at"]),
        member_id=member_id,
        request_id=request_id,
    )


async def admin_reject(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    request_id = request.match_info["request_id"]
    post = await request.post()
    password = _form_text(post, "password", strip=False)
    otp = _form_text(post, "totp")
    expected_version = _form_text(post, "expected_version")
    review_note = _form_text(post, "review_note")
    if not review_note:
        return _render(request, "error.html", status=400, error="拒绝时必须填写申请人可见的原因")
    with _db(request) as connection:
        _verify_reauth(
            request,
            connection,
            admin,
            password=password,
            otp=otp,
            action="request_reject",
        )
    try:
        await _broker_call(
            request,
            admin,
            "portal_requests_reject",
            request_id=request_id,
            review_note=review_note,
            internal_note="",
            expected_version=expected_version,
        )
    except PortalFormError as exc:
        return _render(request, "error.html", status=409, error=f"拒绝失败：{exc.message}")
    with _db(request) as connection:
        registration = store.get_registration_by_request(connection, request_id)
        if registration is not None:
            store.update_registration(
                connection,
                str(registration["registration_id"]),
                status="rejected",
                reviewed_by=str(admin["user_id"]),
                review_note=review_note,
                now=request.app[CLOCK_KEY](),
            )
    _audit(request, "request_rejected", user=admin, target=request_id)
    raise web.HTTPFound(f"/admin/registrations/{quote(request_id)}?notice=申请已拒绝")


async def admin_create_portal_account(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    request_id = request.match_info["request_id"]
    post = await request.post()
    password = _form_text(post, "password", strip=False)
    otp = _form_text(post, "totp")
    with _db(request) as connection:
        _verify_reauth(
            request,
            connection,
            admin,
            password=password,
            otp=otp,
            action="portal_account_create",
        )
    try:
        detail = await _request_detail(request, admin, request_id)
    except PortalFormError as exc:
        return _render(request, "error.html", status=404, error=exc.message)
    decision = detail.get("decision")
    if not isinstance(decision, dict) or decision.get("status") != "approved":
        return _render(request, "error.html", status=409, error="申请尚未批准")
    member_id = str(decision.get("member_id", ""))
    if not member_id:
        return _render(
            request,
            "error.html",
            status=409,
            error="批准记录缺少成员编号，请到成员列表处理",
        )
    try:
        with _db(request) as connection:
            registration = await _ensure_registration(request, connection, request_id, detail)
            account = store.create_pending_account(
                connection,
                username=str(detail.get("username", "")),
                display_name=str(detail.get("display_name", "")),
                contact=str(detail.get("contact", "")),
                member_id=member_id,
                linux_username=str(detail.get("username", "")),
                registration_id=str(registration["registration_id"]) if registration else "",
                activation_ttl_hours=request.app[CONFIG_KEY].portal.activation_ttl_hours,
                created_by=str(admin["user_id"]),
                now=request.app[CLOCK_KEY](),
            )
    except (sqlite3.Error, ValueError) as exc:
        return _render(request, "error.html", status=409, error=f"Portal 账号创建失败：{exc}")
    _audit(
        request,
        "portal_account_created",
        user=admin,
        target=request_id,
        detail={"member_id": member_id, "username": account["username"]},
    )
    return _render(
        request,
        "activation_code.html",
        username=str(account["username"]),
        activation_code=str(account["activation_code"]),
        expires_at=str(account["expires_at"]),
        member_id=member_id,
        request_id=request_id,
    )


async def admin_members(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    members: list[dict[str, Any]] = []
    error = ""
    try:
        result = await _broker_call(request, admin, "portal_members_list")
        members = result.get("members", []) if isinstance(result, dict) else []
    except PortalFormError as exc:
        error = exc.message
    portal_users: dict[str, dict[str, Any]] = {}
    with _db(request) as connection:
        for user in store.list_users(connection):
            member_id = str(user.get("member_id", ""))
            if member_id:
                portal_users[member_id] = user
    return _render(
        request,
        "admin_members.html",
        members=members,
        portal_users=portal_users,
        error=error,
    )


async def admin_member_detail(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    member_id = request.match_info["member_id"]
    try:
        detail = await _broker_call(
            request,
            admin,
            "portal_members_show",
            member_id=member_id,
        )
    except PortalFormError as exc:
        return _render(request, "error.html", status=404, error=exc.message)
    portal_user: dict[str, Any] | None = None
    with _db(request) as connection:
        portal_user = store.get_user_by_member_id(connection, member_id)
    return _render(
        request,
        "admin_member_detail.html",
        detail=detail,
        portal_user=portal_user,
    )


async def admin_member_action(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    member_id = request.match_info["member_id"]
    action = request.match_info["action"]
    command = ADMIN_COMMANDS.get(action)
    if command is None or action not in {"suspend", "resume", "revoke"}:
        raise web.HTTPNotFound()
    post = await request.post()
    password = _form_text(post, "password", strip=False)
    otp = _form_text(post, "totp")
    reason = _form_text(post, "reason")
    if not reason:
        return _render(request, "error.html", status=400, error="状态变更必须填写原因")
    with _db(request) as connection:
        _verify_reauth(
            request,
            connection,
            admin,
            password=password,
            otp=otp,
            action=f"member_{action}",
        )
    try:
        await _broker_call(
            request,
            admin,
            command,
            member_id=member_id,
            reason=reason,
        )
    except PortalFormError as exc:
        return _render(request, "error.html", status=409, error=f"操作失败：{exc.message}")
    target_status = {
        "suspend": "suspended",
        "resume": "active",
        "revoke": "revoked",
    }[action]
    with _db(request) as connection:
        portal_user = store.get_user_by_member_id(connection, member_id)
        if portal_user is not None:
            account_status = str(portal_user["status"])
            new_status = target_status
            if action == "resume" and account_status != "suspended":
                # Never turn a pending or revoked Portal account into an
                # active one as a side effect of resuming Linux membership.
                new_status = account_status
            elif action == "suspend" and account_status != "active":
                new_status = account_status
            store.update_user_status(
                connection,
                str(portal_user["user_id"]),
                status=new_status,
                now=request.app[CLOCK_KEY](),
            )
            if new_status in {"suspended", "revoked"}:
                store.delete_user_sessions(connection, str(portal_user["user_id"]))
    _audit(
        request,
        f"member_{action}",
        user=admin,
        target=member_id,
        detail={"reason": reason},
    )
    raise web.HTTPFound(f"/admin/members/{quote(member_id)}?notice=成员状态已更新")


async def admin_reset_activation(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    member_id = request.match_info["member_id"]
    post = await request.post()
    password = _form_text(post, "password", strip=False)
    otp = _form_text(post, "totp")
    with _db(request) as connection:
        _verify_reauth(
            request,
            connection,
            admin,
            password=password,
            otp=otp,
            action="activation_code_reset",
        )
        portal_user = store.get_user_by_member_id(connection, member_id)
        if portal_user is None:
            return _render(request, "error.html", status=404, error="该成员没有 Portal 账号")
        if portal_user["status"] != "pending_activation":
            return _render(
                request,
                "error.html",
                status=409,
                error="只有待激活账号可以重新生成激活码",
            )
        code = store.create_activation_code(
            connection,
            str(portal_user["user_id"]),
            ttl_hours=request.app[CONFIG_KEY].portal.activation_ttl_hours,
            created_by=str(admin["user_id"]),
            now=request.app[CLOCK_KEY](),
        )
    _audit(
        request,
        "activation_code_reset",
        user=admin,
        target=member_id,
        detail={"username": str(portal_user["username"])},
    )
    return _render(
        request,
        "activation_code.html",
        username=str(portal_user["username"]),
        activation_code=str(code["code"]),
        expires_at=str(code["expires_at"]),
        member_id=member_id,
        request_id="",
    )


async def admin_audit(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    with _db(request) as connection:
        events = store.list_audit(connection, limit=200)
    broker_events: list[dict[str, Any]] = []
    broker_error = ""
    try:
        result = await _broker_call(request, admin, "portal_audit", limit=100)
        broker_events = result.get("events", []) if isinstance(result, dict) else []
    except PortalFormError as exc:
        broker_error = exc.message
    return _render(
        request,
        "admin_audit.html",
        events=events,
        broker_events=broker_events,
        broker_error=broker_error,
    )


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "citespec-portal"})


def create_app(
    config: LabConfig,
    *,
    broker: PortalBroker | None = None,
    secret: bytes | None = None,
    db_path: str | Path | None = None,
    templates_dir: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
    create_secret: bool = False,
) -> web.Application:
    path = str(db_path or config.portal.db_path)
    connection = store.init_portal_db(path)
    connection.close()
    if secret is None:
        secret = security.load_secret(config.portal.secret_file, create=create_secret)
    if templates_dir is None:
        environment = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(("html", "xml")),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    else:
        environment = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(("html", "xml")),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    app = web.Application(
        client_max_size=config.portal.max_body_bytes,
        middlewares=[_portal_middleware],
    )
    app[CONFIG_KEY] = config
    app[DB_KEY] = path
    app[SECRET_KEY] = secret
    app[BROKER_KEY] = broker or PortalBroker(config.server.broker_socket)
    app[TEMPLATES_KEY] = environment
    app[CLOCK_KEY] = clock or _clock
    app[LIMITER_KEY] = _Limiter()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    app.router.add_get("/login", login_form)
    app.router.add_post("/login", login_submit)
    app.router.add_post("/logout", logout)
    app.router.add_get("/register", register_form)
    app.router.add_post("/register", register_submit)
    app.router.add_get("/activate", activate_form)
    app.router.add_post("/activate/start", activate_start)
    app.router.add_post("/activate/complete", activate_complete)
    app.router.add_get("/account", account_page)
    app.router.add_post("/account/password", account_password)
    app.router.add_get("/status", status_page)
    app.router.add_get("/admin", admin_index)
    app.router.add_get("/admin/registrations", admin_registrations)
    app.router.add_get(
        "/admin/registrations/{request_id}",
        admin_registration_detail,
    )
    app.router.add_post(
        "/admin/registrations/{request_id}/approve",
        admin_approve,
    )
    app.router.add_post(
        "/admin/registrations/{request_id}/reject",
        admin_reject,
    )
    app.router.add_post(
        "/admin/registrations/{request_id}/portal-account",
        admin_create_portal_account,
    )
    app.router.add_get("/admin/members", admin_members)
    app.router.add_get("/admin/members/{member_id}", admin_member_detail)
    app.router.add_post(
        "/admin/members/{member_id}/{action:suspend|resume|revoke}",
        admin_member_action,
    )
    app.router.add_post(
        "/admin/members/{member_id}/activation-code",
        admin_reset_activation,
    )
    app.router.add_get("/admin/audit", admin_audit)
    app.router.add_static("/static/", STATIC_DIR, name="static")
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from ..config import load_config

    config_path = Path(
        os.environ.get("CITESPEC_RESOURCE_CONFIG", "/etc/citespec-resource/config.toml")
    )
    config = load_config(config_path)
    if not config.portal.enabled:
        logger.info("portal is disabled by config; exiting")
        return
    app = create_app(config)
    web.run_app(
        app,
        host=config.portal.bind_host,
        port=config.portal.bind_port,
        print=None,
    )


if __name__ == "__main__":
    main()
