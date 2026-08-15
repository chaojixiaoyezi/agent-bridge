from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge import email_delivery as email_module
from agent_bridge.email_delivery import EmailConfigurationError, SMTPEmailDelivery


EMAIL_ENV_NAMES = (
    "AGENT_BRIDGE_SMTP_HOST",
    "AGENT_BRIDGE_SMTP_PORT",
    "AGENT_BRIDGE_SMTP_SECURITY",
    "AGENT_BRIDGE_SMTP_USERNAME",
    "AGENT_BRIDGE_SMTP_PASSWORD",
    "AGENT_BRIDGE_SMTP_PASSWORD_FILE",
    "AGENT_BRIDGE_EMAIL_FROM",
    "AGENT_BRIDGE_PUBLIC_BASE_URL",
)


def clear_email_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in EMAIL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_smtp_configuration_is_disabled_by_default_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_email_env(monkeypatch)
    assert SMTPEmailDelivery.from_env(public_mode=False) is None

    monkeypatch.setenv("AGENT_BRIDGE_EMAIL_FROM", "bridge@example.com")
    with pytest.raises(EmailConfigurationError, match="SMTP_HOST"):
        SMTPEmailDelivery.from_env(public_mode=False)

    clear_email_env(monkeypatch)
    password_file = tmp_path / "smtp-password"
    password_file.write_text("secret-password\n", encoding="utf-8")
    password_file.chmod(0o644)
    monkeypatch.setenv("AGENT_BRIDGE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AGENT_BRIDGE_SMTP_USERNAME", "bridge")
    monkeypatch.setenv("AGENT_BRIDGE_SMTP_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("AGENT_BRIDGE_EMAIL_FROM", "bridge@example.com")
    monkeypatch.setenv("AGENT_BRIDGE_PUBLIC_BASE_URL", "http://localhost:8765")
    with pytest.raises(EmailConfigurationError, match="0600"):
        SMTPEmailDelivery.from_env(public_mode=False)

    password_file.chmod(0o600)
    configured = SMTPEmailDelivery.from_env(public_mode=False)
    assert configured is not None
    assert configured.password == "secret-password"
    with pytest.raises(EmailConfigurationError, match="https"):
        SMTPEmailDelivery.from_env(public_mode=True)


def test_starttls_delivery_uses_tls_and_fragment_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def ehlo(self) -> None:
            calls.append("ehlo")

        def starttls(self, *, context) -> None:
            calls.append(("starttls", context is not None))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def send_message(self, message) -> None:
            calls.append(("message", message))

    monkeypatch.setattr(email_module.smtplib, "SMTP", FakeSMTP)
    delivery = SMTPEmailDelivery(
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="bridge",
        password="private",
        sender="bridge@example.com",
        public_base_url="https://bridge.example",
    )
    delivery.send_password_reset("user@example.com", "secret_token")

    assert calls[:5] == [
        ("connect", "smtp.example.com", 587, 10.0),
        "ehlo",
        ("starttls", True),
        "ehlo",
        ("login", "bridge", "private"),
    ]
    message = calls[5][1]
    assert message["To"] == "user@example.com"
    assert "https://bridge.example/#reset-password=secret_token" in message.get_content()
    assert "?" not in message.get_content().splitlines()[2]
