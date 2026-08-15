from __future__ import annotations

import os
import smtplib
import ssl
import stat
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit


class EmailConfigurationError(RuntimeError):
    pass


class EmailDelivery(Protocol):
    def send_verification(self, recipient: str, token: str) -> None: ...

    def send_password_reset(self, recipient: str, token: str) -> None: ...

    def send_password_changed(self, recipient: str) -> None: ...


@dataclass(frozen=True)
class SMTPEmailDelivery:
    host: str
    port: int
    security: str
    username: str | None
    password: str | None
    sender: str
    public_base_url: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, *, public_mode: bool) -> SMTPEmailDelivery | None:
        host = os.environ.get("AGENT_BRIDGE_SMTP_HOST", "").strip()
        configured_names = (
            "AGENT_BRIDGE_SMTP_PORT",
            "AGENT_BRIDGE_SMTP_SECURITY",
            "AGENT_BRIDGE_SMTP_USERNAME",
            "AGENT_BRIDGE_SMTP_PASSWORD",
            "AGENT_BRIDGE_SMTP_PASSWORD_FILE",
            "AGENT_BRIDGE_EMAIL_FROM",
            "AGENT_BRIDGE_PUBLIC_BASE_URL",
        )
        if not host:
            if any(os.environ.get(name, "").strip() for name in configured_names):
                raise EmailConfigurationError(
                    "email settings require AGENT_BRIDGE_SMTP_HOST"
                )
            return None

        security = os.environ.get(
            "AGENT_BRIDGE_SMTP_SECURITY",
            "starttls",
        ).strip().casefold()
        if security not in {"starttls", "ssl"}:
            raise EmailConfigurationError(
                "AGENT_BRIDGE_SMTP_SECURITY must be starttls or ssl"
            )
        default_port = 465 if security == "ssl" else 587
        try:
            port = int(os.environ.get("AGENT_BRIDGE_SMTP_PORT", str(default_port)))
        except ValueError as exc:
            raise EmailConfigurationError(
                "AGENT_BRIDGE_SMTP_PORT must be an integer"
            ) from exc
        if not 1 <= port <= 65535:
            raise EmailConfigurationError(
                "AGENT_BRIDGE_SMTP_PORT must be between 1 and 65535"
            )

        username = os.environ.get("AGENT_BRIDGE_SMTP_USERNAME", "").strip() or None
        password = _read_secret(
            direct_name="AGENT_BRIDGE_SMTP_PASSWORD",
            file_name="AGENT_BRIDGE_SMTP_PASSWORD_FILE",
        )
        if bool(username) != bool(password):
            raise EmailConfigurationError(
                "SMTP username and password must be configured together"
            )

        sender = os.environ.get("AGENT_BRIDGE_EMAIL_FROM", "").strip()
        if not sender or "\n" in sender or "\r" in sender:
            raise EmailConfigurationError(
                "AGENT_BRIDGE_EMAIL_FROM is required and must be one line"
            )
        public_base_url = _validate_public_base_url(
            os.environ.get("AGENT_BRIDGE_PUBLIC_BASE_URL", ""),
            public_mode=public_mode,
        )
        return cls(
            host=host,
            port=port,
            security=security,
            username=username,
            password=password,
            sender=sender,
            public_base_url=public_base_url,
        )

    def send_verification(self, recipient: str, token: str) -> None:
        url = self._fragment_url("verify-email", token)
        self._send(
            recipient=recipient,
            subject="验证你的 Agent Bridge 邮箱",
            body=(
                "请在 24 小时内打开下面的链接完成邮箱验证。\n\n"
                f"{url}\n\n"
                "如果不是你发起的操作，请忽略本邮件。"
            ),
        )

    def send_password_reset(self, recipient: str, token: str) -> None:
        url = self._fragment_url("reset-password", token)
        self._send(
            recipient=recipient,
            subject="重置你的 Agent Bridge 密码",
            body=(
                "请在 30 分钟内打开下面的链接设置新密码。链接只能使用一次。\n\n"
                f"{url}\n\n"
                "如果不是你发起的操作，请忽略本邮件；账户不会因此改变。"
            ),
        )

    def send_password_changed(self, recipient: str) -> None:
        self._send(
            recipient=recipient,
            subject="Agent Bridge 密码已更新",
            body=(
                "你的 Agent Bridge 密码刚刚被更新，所有其他登录会话已失效。\n\n"
                "如果不是你本人操作，请立即联系管理员。"
            ),
        )

    def _fragment_url(self, action: str, token: str) -> str:
        return f"{self.public_base_url}/#{action}={quote(str(token), safe='')}"

    def _send(self, *, recipient: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        context = ssl.create_default_context()
        if self.security == "ssl":
            with smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
                context=context,
            ) as client:
                self._authenticate(client)
                client.send_message(message)
            return
        with smtplib.SMTP(
            self.host,
            self.port,
            timeout=self.timeout_seconds,
        ) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            self._authenticate(client)
            client.send_message(message)

    def _authenticate(self, client: smtplib.SMTP) -> None:
        if self.username is not None and self.password is not None:
            client.login(self.username, self.password)


def _validate_public_base_url(value: str, *, public_mode: bool) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        raise EmailConfigurationError(
            "AGENT_BRIDGE_PUBLIC_BASE_URL is required when email is enabled"
        )
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EmailConfigurationError(
            "AGENT_BRIDGE_PUBLIC_BASE_URL must be an absolute http(s) URL"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EmailConfigurationError(
            "AGENT_BRIDGE_PUBLIC_BASE_URL cannot contain credentials, query, or fragment"
        )
    if public_mode and parsed.scheme != "https":
        raise EmailConfigurationError(
            "public email links require an https AGENT_BRIDGE_PUBLIC_BASE_URL"
        )
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise EmailConfigurationError(
            "plain-http email links are allowed only for localhost"
        )
    return normalized


def _read_secret(*, direct_name: str, file_name: str) -> str | None:
    direct = os.environ.get(direct_name, "").strip()
    raw_path = os.environ.get(file_name, "").strip()
    if direct and raw_path:
        raise EmailConfigurationError(
            f"configure only one of {direct_name} and {file_name}"
        )
    if direct:
        return direct
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise EmailConfigurationError(
                f"{file_name} must point to a private 0600 regular file"
            )
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise EmailConfigurationError(f"cannot read {file_name}") from exc
    if not value:
        raise EmailConfigurationError(f"{file_name} is empty")
    return value
