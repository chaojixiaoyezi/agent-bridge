from __future__ import annotations

import asyncio

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .email_delivery import EmailDelivery
from .security import ViewerSecurityPolicy
from .viewer_http import (
    _json_body,
    _json_error,
    _public_web_identity,
)
from .web_auth import (
    WebAuthorizationError,
    WebAuthStore,
    password_policy_payload,
)


def build_auth_routes(
    *,
    web_auth: WebAuthStore,
    policy: ViewerSecurityPolicy,
    web_session_cookie: str,
    resolved_email_delivery: EmailDelivery | None,
    authenticated_web_user,
    require_web_intent,
    enforce_rate,
) -> list[Route]:
    def require_email_delivery() -> EmailDelivery:
        if resolved_email_delivery is None:
            raise WebAuthorizationError("当前部署未启用邮箱验证和密码找回")
        return resolved_email_delivery

    def deliver_verification(result: dict[str, object] | None) -> None:
        if result is None or resolved_email_delivery is None:
            return
        try:
            resolved_email_delivery.send_verification(
                str(result["email"]),
                str(result["token"]),
            )
        except Exception:
            # Never leak recipient addresses or one-time tokens into logs. A
            # user can safely request a replacement token if delivery fails.
            return

    def deliver_password_reset(result: dict[str, object] | None) -> None:
        if result is None or resolved_email_delivery is None:
            return
        try:
            resolved_email_delivery.send_password_reset(
                str(result["email"]),
                str(result["token"]),
            )
        except Exception:
            return

    def deliver_password_changed(result: dict[str, object] | None) -> None:
        if result is None or resolved_email_delivery is None:
            return
        try:
            resolved_email_delivery.send_password_changed(str(result["email"]))
        except Exception:
            return

    def login_response(
        request: Request,
        *,
        identity: dict[str, object],
        session_token: str,
        status_code: int = 200,
        background: BackgroundTask | None = None,
    ) -> JSONResponse:
        response = JSONResponse(
            {
                "user": _public_web_identity(identity),
                "password_policy": password_policy_payload(),
            },
            status_code=status_code,
            background=background,
        )
        response.set_cookie(
            web_session_cookie,
            session_token,
            max_age=policy.web_session_ttl_seconds,
            path="/",
            secure=policy.secure_cookies or request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    async def auth_captcha(request: Request) -> Response:
        try:
            enforce_rate(
                request,
                "auth-captcha-ip",
                limit=20,
                window_seconds=60,
            )
            challenge = await asyncio.to_thread(web_auth.create_captcha)
            return JSONResponse({"captcha": challenge})
        except Exception as exc:
            return _json_error(exc)

    async def auth_register(request: Request) -> Response:
        try:
            require_web_intent(request, intent="register")
            if policy.web_registration_mode == "closed":
                raise WebAuthorizationError("公开注册已关闭，请联系管理员")
            payload = await _json_body(
                request,
                required={"username", "password", "captcha_id", "captcha_answer"},
                allowed={
                    "username",
                    "password",
                    "captcha_id",
                    "captcha_answer",
                    "registration_code",
                    "email",
                },
            )
            if payload.get("email") is not None:
                require_email_delivery()
            enforce_rate(
                request,
                "auth-register-ip",
                limit=6,
                window_seconds=60 * 60,
            )
            enforce_rate(
                request,
                "auth-register-account",
                subject=str(payload["username"]).strip().casefold(),
                limit=3,
                window_seconds=60 * 60,
            )
            supplied_registration_code = payload.get("registration_code")
            legacy_code_matches = policy.registration_code_matches(
                supplied_registration_code
            )
            identity, session_token = await asyncio.to_thread(
                web_auth.register,
                username=payload["username"],
                password=payload["password"],
                captcha_id=payload["captcha_id"],
                captcha_answer=payload["captcha_answer"],
                registration_code=supplied_registration_code,
                registration_code_required=(
                    policy.web_registration_mode == "access_code"
                    and not legacy_code_matches
                ),
                email=payload.get("email"),
            )
            verification = identity.pop("_email_verification", None)
            return login_response(
                request,
                identity=identity,
                session_token=session_token,
                status_code=201,
                background=(
                    BackgroundTask(deliver_verification, verification)
                    if verification is not None
                    else None
                ),
            )
        except Exception as exc:
            return _json_error(exc)

    async def auth_login(request: Request) -> Response:
        try:
            require_web_intent(request, intent="login")
            payload = await _json_body(
                request,
                required={"username", "password", "captcha_id", "captcha_answer"},
                allowed={"username", "password", "captcha_id", "captcha_answer"},
            )
            enforce_rate(
                request,
                "auth-login-ip",
                limit=30,
                window_seconds=5 * 60,
            )
            enforce_rate(
                request,
                "auth-login-account",
                subject=str(payload["username"]).strip().casefold(),
                limit=12,
                window_seconds=5 * 60,
            )
            identity, session_token = await asyncio.to_thread(
                web_auth.login,
                username=payload["username"],
                password=payload["password"],
                captcha_id=payload["captcha_id"],
                captcha_answer=payload["captcha_answer"],
            )
            return login_response(
                request,
                identity=identity,
                session_token=session_token,
            )
        except Exception as exc:
            return _json_error(exc)

    async def auth_email_request(request: Request) -> Response:
        try:
            require_web_intent(request, intent="request-email-verification")
            require_email_delivery()
            identity = authenticated_web_user(request)
            payload = await _json_body(
                request,
                required={"email", "current_password"},
                allowed={"email", "current_password"},
            )
            enforce_rate(
                request,
                "auth-email-session",
                subject=identity["session_id"],
                limit=5,
                window_seconds=60 * 60,
            )
            verification = await asyncio.to_thread(
                web_auth.request_email_verification,
                user_id=str(identity["user_id"]),
                session_id=str(identity["session_id"]),
                current_password=payload["current_password"],
                email=payload["email"],
            )
            refreshed = await asyncio.to_thread(
                web_auth.authenticate,
                request.cookies.get(web_session_cookie),
            )
            return JSONResponse(
                {
                    "accepted": True,
                    "user": _public_web_identity(refreshed),
                    "message": "验证邮件已提交发送，请在 24 小时内完成验证。",
                },
                background=BackgroundTask(deliver_verification, verification),
            )
        except Exception as exc:
            return _json_error(exc)

    async def auth_email_verify(request: Request) -> Response:
        try:
            require_web_intent(request, intent="verify-email")
            payload = await _json_body(
                request,
                required={"token"},
                allowed={"token"},
            )
            enforce_rate(
                request,
                "auth-email-verify-ip",
                limit=20,
                window_seconds=60 * 60,
            )
            await asyncio.to_thread(web_auth.verify_email, payload["token"])
            return JSONResponse(
                {"verified": True, "message": "邮箱验证成功，请继续使用。"}
            )
        except Exception as exc:
            return _json_error(exc)

    async def auth_password_reset_request(request: Request) -> Response:
        try:
            require_web_intent(request, intent="request-password-reset")
            require_email_delivery()
            payload = await _json_body(
                request,
                required={"identifier", "captcha_id", "captcha_answer"},
                allowed={"identifier", "captcha_id", "captcha_answer"},
            )
            identifier = str(payload["identifier"] or "").strip().casefold()
            enforce_rate(
                request,
                "auth-password-reset-ip",
                limit=8,
                window_seconds=60 * 60,
            )
            enforce_rate(
                request,
                "auth-password-reset-account",
                subject=identifier,
                limit=4,
                window_seconds=60 * 60,
            )
            reset = await asyncio.to_thread(
                web_auth.create_password_reset,
                identifier=identifier,
                captcha_id=payload["captcha_id"],
                captcha_answer=payload["captcha_answer"],
            )
            return JSONResponse(
                {
                    "accepted": True,
                    "message": (
                        "如果账户存在且已验证邮箱，重置邮件会很快送达。"
                    ),
                },
                background=BackgroundTask(deliver_password_reset, reset),
            )
        except Exception as exc:
            return _json_error(exc)

    async def auth_password_reset_confirm(request: Request) -> Response:
        try:
            require_web_intent(request, intent="confirm-password-reset")
            payload = await _json_body(
                request,
                required={"token", "new_password"},
                allowed={"token", "new_password"},
            )
            enforce_rate(
                request,
                "auth-password-reset-confirm-ip",
                limit=12,
                window_seconds=60 * 60,
            )
            result = await asyncio.to_thread(
                web_auth.reset_password,
                token_value=payload["token"],
                new_password=payload["new_password"],
            )
            response = JSONResponse(
                {
                    "reset": True,
                    "message": "密码已更新，请使用新密码重新登录。",
                    "password_policy": password_policy_payload(),
                },
                background=BackgroundTask(deliver_password_changed, result),
            )
            response.delete_cookie(
                web_session_cookie,
                path="/",
                secure=policy.secure_cookies or request.url.scheme == "https",
                httponly=True,
                samesite="strict",
            )
            if policy.public_mode:
                response.headers["Clear-Site-Data"] = '"cookies"'
            return response
        except Exception as exc:
            return _json_error(exc)

    async def auth_me(request: Request) -> Response:
        try:
            identity = authenticated_web_user(
                request,
                allow_password_change=True,
            )
            return JSONResponse(
                {
                    "user": _public_web_identity(identity),
                    "password_policy": password_policy_payload(),
                }
            )
        except Exception as exc:
            return _json_error(exc)

    async def auth_logout(request: Request) -> Response:
        try:
            require_web_intent(request, intent="logout")
            web_auth.logout(request.cookies.get(web_session_cookie))
            response = JSONResponse({"logged_out": True})
            response.delete_cookie(
                web_session_cookie,
                path="/",
                secure=policy.secure_cookies or request.url.scheme == "https",
                httponly=True,
                samesite="strict",
            )
            if policy.public_mode:
                response.headers["Clear-Site-Data"] = '"cache", "cookies"'
            return response
        except Exception as exc:
            return _json_error(exc)

    async def auth_password(request: Request) -> Response:
        try:
            require_web_intent(request, intent="change-password")
            identity = authenticated_web_user(
                request,
                allow_password_change=True,
            )
            payload = await _json_body(
                request,
                required={"current_password", "new_password"},
                allowed={"current_password", "new_password"},
            )
            enforce_rate(
                request,
                "auth-password-session",
                subject=identity["session_id"],
                limit=10,
                window_seconds=10 * 60,
            )
            updated = await asyncio.to_thread(
                web_auth.change_password,
                user_id=str(identity["user_id"]),
                session_id=str(identity["session_id"]),
                current_password=payload["current_password"],
                new_password=payload["new_password"],
            )
            notification_email = updated.pop("_verified_email", None)
            return JSONResponse(
                {
                    "user": _public_web_identity(updated),
                    "password_policy": password_policy_payload(),
                },
                background=(
                    BackgroundTask(
                        deliver_password_changed,
                        {"email": notification_email},
                    )
                    if notification_email is not None
                    else None
                ),
            )
        except Exception as exc:
            return _json_error(exc)

    async def auth_profile(request: Request) -> Response:
        try:
            require_web_intent(request, intent="update-profile")
            identity = authenticated_web_user(request)
            payload = await _json_body(
                request,
                required={"display_name", "signature"},
                allowed={"display_name", "signature", "avatar_key"},
            )
            updated = web_auth.update_profile(
                user_id=str(identity["user_id"]),
                session_id=str(identity["session_id"]),
                display_name=payload["display_name"],
                signature=payload["signature"],
                avatar_key=payload.get("avatar_key"),
            )
            return JSONResponse({"user": _public_web_identity(updated)})
        except Exception as exc:
            return _json_error(exc)

    return [
            Route("/api/auth/captcha", auth_captcha, methods=["GET"]),
            Route("/api/auth/register", auth_register, methods=["POST"]),
            Route("/api/auth/login", auth_login, methods=["POST"]),
            Route(
                "/api/auth/email/request",
                auth_email_request,
                methods=["POST"],
            ),
            Route(
                "/api/auth/email/verify",
                auth_email_verify,
                methods=["POST"],
            ),
            Route(
                "/api/auth/password-reset/request",
                auth_password_reset_request,
                methods=["POST"],
            ),
            Route(
                "/api/auth/password-reset/confirm",
                auth_password_reset_confirm,
                methods=["POST"],
            ),
            Route("/api/auth/me", auth_me, methods=["GET"]),
            Route("/api/auth/logout", auth_logout, methods=["POST"]),
            Route("/api/auth/password", auth_password, methods=["POST"]),
            Route("/api/auth/profile", auth_profile, methods=["PATCH"]),
    ]
