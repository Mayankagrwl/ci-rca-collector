"""Fake notification channels (no network): a webhook over MockTransport + an SMTP factory."""

from __future__ import annotations

import json
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import httpx

CHAT_URL = "https://hooks.example.invalid/services/T0/B0/SECRETPATHxyz?token=QUERYSECRET123"
SMTP_PASSWORD = "p4ss-w0rd-SECRET"
SMTP_URL = f"smtp://mailer:{SMTP_PASSWORD}@smtp.example.invalid:587?from=ci-bot@example.invalid"


@dataclass
class FakeWebhook:
    status: int = 200
    raise_exc: Exception | None = None
    posts: list[dict[str, Any]] = field(default_factory=list)
    raw: list[bytes] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.raw.append(request.content)
            self.headers.append(dict(request.headers))
            self.posts.append(json.loads(request.content.decode("utf-8")))
            if self.raise_exc is not None:
                raise self.raise_exc
            return httpx.Response(self.status, json={"ok": self.status < 300})

        return httpx.MockTransport(handler)


@dataclass
class FakeSmtp:
    """Stands in for smtplib.SMTP / SMTP_SSL. One instance per connection."""

    offer_starttls: bool = True
    fail_on_send: Exception | None = None
    connections: list[dict[str, Any]] = field(default_factory=list)
    messages: list[EmailMessage] = field(default_factory=list)

    def factory(self, host: str, port: int, timeout: float, implicit_tls: bool) -> Any:
        record: dict[str, Any] = {
            "host": host, "port": port, "timeout": timeout, "implicit_tls": implicit_tls,
            "starttls": False, "login": None, "sent": False,
        }
        self.connections.append(record)
        owner = self

        class _Server:
            def ehlo(self) -> None:
                return None

            def has_extn(self, name: str) -> bool:
                return name.lower() == "starttls" and owner.offer_starttls

            def starttls(self, context: Any = None) -> None:
                record["starttls"] = True

            def login(self, user: str, password: str) -> None:
                record["login"] = (user, password)

            def send_message(self, message: EmailMessage) -> None:
                if owner.fail_on_send is not None:
                    raise owner.fail_on_send
                record["sent"] = True
                owner.messages.append(message)

            def quit(self) -> None:
                return None

        return _Server()


def smtp_error_with_secret() -> Exception:
    return smtplib.SMTPAuthenticationError(535, f"auth failed for mailer:{SMTP_PASSWORD} at {SMTP_URL}".encode())
