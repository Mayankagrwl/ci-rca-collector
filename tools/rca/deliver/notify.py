"""Email + chat notifications (delivery spec v1.3 §10).

Gating (``plan_notification``), burst summaries (``summarize_burst``) and message
building are pure. Only ``ChatSender`` and ``SmtpSender`` do I/O, and both take
an injectable transport / SMTP factory. Neither ever sees the GitHub token.

The webhook URL and SMTP URL are credentials: they come from the environment
(``RCA_CHAT_WEBHOOK_URL`` / ``RCA_SMTP_URL``) or programmatically, never from
argv, and every recorded error is scrubbed of them (``Scrubber``).

Quiet window: one clock shared by email and chat — the sticky comment's
``update_quiet`` dedupe, else the fingerprint issue's ``updated_at`` from
*before* this run. A simplification of v1.3's "per channel" wording.
"""

from __future__ import annotations

import html
import re
import smtplib
import ssl
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from dateutil import parser as date_parser

from ..redact import redact_text
from .owners import split_owners
from .render_comment import card_headline, category_words, safe_text

CHAT_ENV = "RCA_CHAT_WEBHOOK_URL"
SMTP_ENV = "RCA_SMTP_URL"
PAYLOAD_FIELD_ENV = "RCA_CHAT_PAYLOAD_FIELD"
DEFAULT_PAYLOAD_FIELD = "text"
SEND_TIMEOUT_S = 10.0
SUBJECT_CAP = 200
LINE_CAP = 300
NOTIFY_SEVERITIES = frozenset({"high", "critical"})
# Triggers that notify at any severity. schedule: v1.3 §10. dispatch: v1.3 §4.1 /
# AC #14 — a manual run notifies the person who started it (only if configured).
ANY_SEVERITY_TRIGGERS = frozenset({"schedule", "dispatch"})
NEVER_NOTIFY = frozenset({"flaky", "draft", "delivery_error"})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---- configuration -------------------------------------------------------------------------


@dataclass(frozen=True)
class NotifyConfig:
    chat_url: str | None = None
    smtp_url: str | None = None
    payload_field: str = DEFAULT_PAYLOAD_FIELD

    @property
    def configured(self) -> bool:
        return bool(self.chat_url or self.smtp_url)

    def scrubber(self) -> Scrubber:
        return Scrubber([self.chat_url, self.smtp_url])


def notify_config(inputs: Any, env: Mapping[str, str]) -> NotifyConfig:
    """Channels from the environment first, then programmatic inputs. Never from argv."""
    chat = (env.get(CHAT_ENV) or "").strip() or (inputs.chat_webhook_url or "").strip() or None
    smtp = (env.get(SMTP_ENV) or "").strip() or (inputs.smtp_url or "").strip() or None
    field_name = (env.get(PAYLOAD_FIELD_ENV) or "").strip() or DEFAULT_PAYLOAD_FIELD
    return NotifyConfig(chat, smtp, field_name)


class Scrubber:
    """Removes credential material (URLs, userinfo, query strings) from any text."""

    def __init__(self, urls: Iterable[str | None]) -> None:
        secrets: set[str] = set()
        for url in urls:
            if not url:
                continue
            secrets.add(url)
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            for piece in (parts.password, parts.username, parts.query, parts.fragment):
                if piece:
                    secrets.add(piece)
                    secrets.add(unquote(piece))
            if parts.netloc and "@" in parts.netloc:
                secrets.add(parts.netloc.rsplit("@", 1)[0])
            secrets.add(f"{parts.scheme}://{parts.netloc}{parts.path}")
        self._secrets = sorted((s for s in secrets if len(s) >= 3), key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return redact_text(text)[0]


# ---- burst summary (A2 message content; informational only) ------------------------------------
# Who sends for a burst is decided by the platform-incident issue (deliver.incidents),
# never by guessing from the run list.


@dataclass
class Burst:
    run_count: int
    workflows: list[str]
    branches: list[str]


def summarize_burst(
    runs: list[dict[str, Any]],
    *,
    run_id: int,
    workflow: str,
    branch: str,
    now: datetime,
    window_minutes: int,
) -> Burst:
    """Failed runs created in the window (this one included): count, workflows, branches."""
    cutoff = now - timedelta(minutes=window_minutes)
    burst: dict[int, dict[str, Any]] = {}
    for run in runs:
        created = _parse_time(run.get("created_at"))
        rid = run.get("id")
        if isinstance(rid, int) and created is not None and cutoff <= created <= now + timedelta(minutes=5):
            burst[rid] = run
    burst.setdefault(run_id, {"id": run_id, "name": workflow, "head_branch": branch})
    workflows = sorted({str(r.get("name") or "") for r in burst.values()} - {""})
    branches = sorted({str(r.get("head_branch") or "") for r in burst.values()} - {""})
    return Burst(len(burst), workflows, branches)


# ---- gating + content (pure) -------------------------------------------------------------------


@dataclass
class NotifyPlan:
    send: bool
    reason: str  # "ok" or why nothing is sent
    platform: str | None = None  # "A2" | "A3" | None
    audience: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    subject: str = ""
    email_body: str = ""
    chat_text: str = ""
    notes: list[str] = field(default_factory=list)


def platform_path(decision: Any) -> str | None:
    if decision.notify_platform_once:
        return "A2"
    if decision.suppressed_by == "infra_runner":
        return "A3"
    return None


def in_quiet_window(
    decision: Any, previous_updated_at: datetime | None, now: datetime, minutes: int
) -> bool:
    if decision.dedupe == "update_quiet":
        return True
    if previous_updated_at is None:
        return False
    age = _aware(now) - _aware(previous_updated_at)
    return timedelta(0) <= age <= timedelta(minutes=minutes)


def plan_notification(
    config: NotifyConfig,
    *,
    summary: Any,
    record: Any,
    decision: Any,
    context: Any,
    route_notify: list[str],
    severity: str,
    inputs: Any,
    owners: list[str],
    owner_resolved_by: str | None,
    issue_url: str | None,
    previous_updated_at: datetime | None,
    now: datetime,
    burst: Burst | None = None,
) -> NotifyPlan:
    """Whether to notify, whom, and the exact texts. Pure; the caller sends.

    For the platform paths the caller then coordinates through a platform-incident
    issue (``deliver.incidents``), which may turn ``send`` off for this run.
    """
    if not config.configured:
        return NotifyPlan(False, "none configured")
    platform = platform_path(decision)
    notes: list[str] = []
    if decision.suppressed_by in NEVER_NOTIFY:
        return NotifyPlan(False, f"never notifies ({decision.suppressed_by})", platform, notes=notes)
    if decision.suppressed_by is not None and platform is None:
        return NotifyPlan(False, f"suppressed by {decision.suppressed_by}", notes=notes)
    severity_ok = severity in NOTIFY_SEVERITIES or context.trigger in ANY_SEVERITY_TRIGGERS
    if platform != "A2" and not severity_ok:
        return NotifyPlan(False, f"severity {severity} below high", platform, notes=notes)
    if in_quiet_window(decision, previous_updated_at, now, inputs.quiet_window_minutes):
        notes.append(f"inside the {inputs.quiet_window_minutes}m quiet window for this fingerprint")
        return NotifyPlan(False, "quiet window", platform, notes=notes)

    audience = _audience(platform, route_notify, context, inputs, owners)
    emails = [a for a in audience if _EMAIL_RE.match(a)]
    for handle in audience:
        if handle not in emails:
            notes.append(f"no email address for {handle}")
    if config.smtp_url and not emails:
        notes.append("no email recipients; email not sent")

    facts = _facts(
        summary, record, decision, severity, platform, burst, audience, owner_resolved_by, issue_url
    )
    return NotifyPlan(
        True,
        "ok",
        platform,
        audience,
        emails,
        subject=facts["subject"],
        email_body=_email_body(facts),
        chat_text=_chat_text(facts),
        notes=notes,
    )


def _audience(platform, route_notify, context, inputs, owners) -> list[str]:
    """Owners (or the platform team) first, then default_notify — in that order, deduped."""
    fallback = split_owners(inputs.default_notify)
    if platform is not None:
        return _dedupe(split_owners(inputs.platform_team) + fallback)
    audience: list[str] = []
    for intent in route_notify:
        if intent in {"team", "owning_team"}:
            audience += list(owners) + fallback
        elif intent == "actor":
            actor = (context.actor or "").strip()
            if actor and not actor.endswith("[bot]"):
                audience.append(actor if actor.startswith("@") else f"@{actor}")
    return _dedupe(audience)


def _facts(summary, record, decision, severity, platform, burst, audience, resolved_by, issue_url):
    run = summary.run
    workflow = header_text(safe_text(run.workflow_name or "workflow", 80))
    branch = header_text(safe_text(run.head_branch or "unknown branch", 80))
    if platform == "A2":
        runs = burst.run_count if burst is not None else 1
        workflows = ", ".join(burst.workflows) if burst is not None else run.workflow_name
        branches = ", ".join(burst.branches) if burst is not None else run.head_branch
        subject = f"[CI] {severity}: widespread CI failures ({runs} runs)"
        cause = f"Widespread infrastructure failure: {runs} failed run(s) in the quiet window."
        extra = [
            f"Workflows: {safe_text(workflows, LINE_CAP)} · Branches: {safe_text(branches, LINE_CAP)}"
        ]
        fix = None
    else:
        subject = f"[CI] {severity}: {workflow} failed on {branch}"
        headline, fix = card_headline(summary, record, decision)
        words = category_words(summary.classification.category)
        # C2: no root-cause claim — category words only.
        cause = safe_text(headline, LINE_CAP) if headline else f"CI failure — {words}"
        fix = safe_text(fix, LINE_CAP) if (headline and fix) else None
        extra = []
    owners_text = ", ".join(safe_text(a, 100) for a in audience)
    return {
        "subject": _clip(header_text(subject), SUBJECT_CAP),
        "severity": severity,
        "cause": cause,
        "fix": fix,
        "extra": extra,
        "unverified": bool(decision.unverified_banner) and platform != "A2",
        "run": f"run {run.run_id}: {run.html_url}",
        "issue": issue_url,
        "owners": owners_text,
        "resolved_by": resolved_by or "none",
    }


def _email_body(f: dict[str, Any]) -> str:
    lines = [f"Severity: {f['severity']}"]
    if f["unverified"]:
        lines.append("Unverified: this diagnosis is not fully grounded in the failed step's log.")
    lines.append(f"Cause: {f['cause']}")
    if f["fix"]:
        lines.append(f"Fix: {f['fix']}")
    lines += f["extra"]
    lines.append(f"Run: {f['run']}")
    if f["issue"]:
        lines.append(f"Issue: {f['issue']}")
    lines.append(f"Owners: {f['owners'] or 'none'} (resolved by {f['resolved_by']})")
    return "\n".join(redact_text(line)[0] for line in lines) + "\n"


def _chat_text(f: dict[str, Any]) -> str:
    esc = chat_escape
    head = f"{esc(f['subject'])}" + (" — Unverified" if f["unverified"] else "")
    lines = [head, esc(f["cause"])]
    if f["fix"]:
        lines.append(f"Fix: {esc(f['fix'])}")
    lines += [esc(line) for line in f["extra"]]
    links = f"Run: {esc(f['run'])}" + (f" · Issue: {esc(f['issue'])}" if f["issue"] else "")
    lines.append(links)
    lines.append(f"Owners: {esc(f['owners'] or 'none')} (resolved by {esc(f['resolved_by'])})")
    return "\n".join(redact_text(line)[0] for line in lines)


def chat_escape(text: str) -> str:
    """&, <, > escaped: neutralises <!channel> / <@U…> broadcast syntax across providers."""
    return html.escape(text or "", quote=False)


def header_text(text: str) -> str:
    """One line, no control characters (CR/LF header injection)."""
    return " ".join(_CONTROL_RE.sub(" ", text or "").split())


# ---- senders (the only I/O) ----------------------------------------------------------------------


@dataclass
class SendResult:
    ok: bool
    detail: str = ""


class ChatSender:
    """POST {<field>: text} to an https webhook. No redirects, one attempt, no GitHub token."""

    def __init__(
        self,
        url: str,
        *,
        payload_field: str = DEFAULT_PAYLOAD_FIELD,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.payload_field = payload_field or DEFAULT_PAYLOAD_FIELD
        self.transport = transport

    def config_error(self) -> str | None:
        return None if self.url.lower().startswith("https://") else "chat webhook must be https://; not sent"

    def payload(self, text: str) -> dict[str, str]:
        return {self.payload_field: text}

    def send(self, text: str, *, timeout: float) -> SendResult:
        problem = self.config_error()
        if problem:
            return SendResult(False, problem)
        kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            with httpx.Client(**kwargs) as client:
                response = client.post(self.url, json=self.payload(text))
        except Exception as exc:  # noqa: BLE001 — exception text carries the URL: type only
            return SendResult(False, f"chat webhook failed: {type(exc).__name__}")
        if 200 <= response.status_code < 300:
            return SendResult(True)
        return SendResult(False, f"chat webhook failed (status {response.status_code})")


SmtpFactory = Callable[[str, int, float, bool], Any]


class SmtpSender:
    """smtp:// (STARTTLS required) or smtps:// (implicit TLS); plaintext only with ?tls=none."""

    def __init__(self, url: str, *, smtp_factory: SmtpFactory | None = None) -> None:
        self.url = url
        self.factory = smtp_factory or _default_smtp_factory
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        self.scheme = parts.scheme.lower()
        self.host = parts.hostname or ""
        self.implicit_tls = self.scheme == "smtps"
        self.plaintext = (query.get("tls", [""])[0].lower() == "none") and not self.implicit_tls
        self.port = parts.port or (465 if self.implicit_tls else 587)
        self.user = unquote(parts.username) if parts.username else None
        self.password = unquote(parts.password) if parts.password else None
        self.sender = header_text(query.get("from", [""])[0])

    def config_error(self) -> str | None:
        if self.scheme not in {"smtp", "smtps"}:
            return "SMTP URL must be smtp:// or smtps://; not sent"
        if not self.host:
            return "SMTP URL has no host; not sent"
        if not self.sender:
            return "SMTP URL has no ?from= address; not sent"
        return None

    def config_notes(self) -> list[str]:
        return ["SMTP tls=none: sending in clear text (insecure; internal relays only)"] if self.plaintext else []

    def message(self, subject: str, body: str, recipients: list[str]) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = header_text(subject)
        msg["From"] = self.sender
        msg["To"] = ", ".join(header_text(r) for r in recipients)
        msg.set_content(body, charset="utf-8")
        return msg

    def send(self, message: EmailMessage, *, timeout: float) -> SendResult:
        problem = self.config_error()
        if problem:
            return SendResult(False, problem)
        try:
            server = self.factory(self.host, self.port, timeout, self.implicit_tls)
            try:
                if not self.implicit_tls and not self.plaintext:
                    server.ehlo()
                    if not server.has_extn("starttls"):
                        return SendResult(False, "SMTP server does not offer STARTTLS; not sent")
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if self.user:
                    server.login(self.user, self.password or "")
                server.send_message(message)
            finally:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001 — the caller scrubs this text
            return SendResult(False, f"SMTP send failed: {type(exc).__name__}: {exc}")
        return SendResult(True)


def _default_smtp_factory(host: str, port: int, timeout: float, implicit_tls: bool) -> Any:
    if implicit_tls:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


# ---- helpers ------------------------------------------------------------------------------------


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [i for i in items if i and not (i in seen or seen.add(i))]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _aware(date_parser.isoparse(value))
    except (ValueError, OverflowError):
        return None


__all__ = [
    "ChatSender",
    "Burst",
    "NotifyConfig",
    "NotifyPlan",
    "Scrubber",
    "SendResult",
    "SmtpSender",
    "chat_escape",
    "header_text",
    "in_quiet_window",
    "notify_config",
    "plan_notification",
    "platform_path",
    "summarize_burst",
]
