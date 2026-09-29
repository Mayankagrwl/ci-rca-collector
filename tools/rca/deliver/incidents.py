"""Platform-incident issues: one notification per infra burst (v1.3 §5 A2/A3, §10).

Replaces run-list leader election, which could elect a run that never takes the
platform path (an ordinary failure in the same window) — then nobody sent. Here
the coordination point is a GitHub-held marker issue:

- an open ``rca-platform-incident`` issue of the same kind updated inside the
  quiet window → join it with a short comment (bumping its ``updated_at``) and
  do not send: one continuous outage, one message;
- otherwise open one, re-list, and let the lowest open issue number created in
  the window win (concurrent creators close theirs as ``Duplicate of #N``).

Stateless on our side, duplicate-safe, and fail-open: when issues can't be
listed or created, the caller sends anyway (a duplicate beats a missed outage).
Incident issues are never labelled ``rca-fingerprint``, so the fingerprint
index and stale sweep ignore them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as date_parser

from .render_comment import category_words, safe_text
from .sticky import BUDGET_EXCEEDED, DeliveryBudget

LABEL_INCIDENT = "rca-platform-incident"
INCIDENT_MARKER_PREFIX = "<!-- rca-incident:kind="
INCIDENT_KINDS = ("infra_widespread", "infra_runner")
SUPERSEDE_MAX = 5
CLOCK_SKEW = timedelta(minutes=5)  # GitHub vs runner clocks: a stamp slightly "ahead" still counts
MAINTAINED_NOTE = "_Maintained by the RCA bot: one platform notification per burst of failures._"


@dataclass
class IncidentOutcome:
    send: bool
    reason: str
    number: int | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def incident_marker(kind: str) -> str:
    return f"{INCIDENT_MARKER_PREFIX}{kind} -->"


def incident_kind_of(body: str | None) -> str | None:
    first = (body or "").lstrip("﻿").strip().split("\n", 1)[0].strip()
    if first.startswith(INCIDENT_MARKER_PREFIX) and first.endswith(" -->"):
        return first[len(INCIDENT_MARKER_PREFIX) : -len(" -->")] or None
    return None


def incident_title(kind: str) -> str:
    return f"[RCA] platform incident: {category_words(kind)}"


def incident_body(kind: str, *, workflows: list[str], branches: list[str], window_minutes: int) -> str:
    seen = (
        f"Failed runs in the last {window_minutes} minutes — workflows: "
        f"{safe_text(', '.join(workflows) or 'unknown', 300)}; "
        f"branches: {safe_text(', '.join(branches) or 'unknown', 300)}."
    )
    return "\n".join(
        [incident_marker(kind), f"### Platform incident: {category_words(kind)}", "", seen, "", MAINTAINED_NOTE]
    ) + "\n"


def also_failed_comment(summary: Any) -> str:
    run = summary.run
    return (
        f"Also failed: [run {run.run_id}]({run.html_url}) · "
        f"{safe_text(run.workflow_name or 'workflow', 120)} · {safe_text(run.head_branch or '', 120)}"
    )


def coordinate_incident(
    client: Any,
    repo: str,
    *,
    kind: str,
    summary: Any,
    workflows: list[str],
    branches: list[str],
    now: datetime,
    window_minutes: int,
    live: bool,
    enabled: bool,
    budget: DeliveryBudget,
    clock: Callable[[], float],
) -> IncidentOutcome:
    """Decide whether this platform-path run sends. Never raises; fails open."""
    out = IncidentOutcome(True, "ok")
    if not enabled:
        out.notes.append("burst dedupe unavailable (create-issues is off); sending")
        return out
    window = timedelta(minutes=window_minutes)
    try:
        incidents = _open_incidents(client, repo, kind)
    except Exception as exc:  # noqa: BLE001 — fail open
        out.notes.append(f"burst dedupe unavailable ({_why(exc)}); sending")
        return out

    recent = [i for i in incidents if _within(i.get("updated_at"), now, window)]
    if recent:
        target = max(recent, key=lambda i: _time(i.get("updated_at")) or now)
        number = int(target["number"])
        out.send, out.number = False, number
        out.reason = f"platform notified via #{number}"
        if not live:
            out.notes.append(f"would join platform incident #{number}")
            return out
        if budget.exceeded(clock()):
            out.errors.append(f"incident #{number} comment skipped: {BUDGET_EXCEEDED}")
            return out
        try:
            client.create_issue_comment(repo, number, also_failed_comment(summary))
        except Exception as exc:  # noqa: BLE001 — already notified; only the bump is lost
            out.errors.append(f"incident #{number} comment failed ({_why(exc)})")
        out.notes.append(f"platform notified via #{number}")
        return out

    if not live:
        out.notes.append("would open a platform incident issue")
        return out
    if budget.exceeded(clock()):
        out.notes.append(f"burst dedupe unavailable ({BUDGET_EXCEEDED}); sending")
        return out
    try:
        created = client.create_issue(
            repo,
            incident_title(kind),
            incident_body(kind, workflows=workflows, branches=branches, window_minutes=window_minutes),
            labels=[LABEL_INCIDENT],
        )
        ours = int(created["number"])
    except Exception as exc:  # noqa: BLE001 — fail open
        out.notes.append(f"burst dedupe unavailable ({_why(exc)}); sending")
        return out
    out.number = ours

    try:
        incidents = _open_incidents(client, repo, kind)
    except Exception as exc:  # noqa: BLE001
        out.notes.append(f"incident re-check failed ({_why(exc)}); sending")
        return out
    contenders = {ours} | {
        int(i["number"]) for i in incidents if _within(i.get("created_at"), now, window)
    }
    lowest = min(contenders)
    if lowest != ours:
        out.send, out.number = False, lowest
        out.reason = f"platform notified via #{lowest}"
        out.notes.append(f"platform notified via #{lowest}; closed #{ours} as a duplicate")
        _close(client, repo, ours, f"Duplicate of #{lowest}", out)
        return out

    out.notes.append(f"opened platform incident #{ours}")
    older = sorted(
        int(i["number"])
        for i in incidents
        if int(i["number"]) != ours and not _within(i.get("updated_at"), now, window)
    )
    for number in older[:SUPERSEDE_MAX]:
        if budget.exceeded(clock()):
            out.errors.append(f"incident supersede skipped: {BUDGET_EXCEEDED}")
            break
        _close(client, repo, number, f"Superseded by #{ours}", out)
    return out


def _open_incidents(client: Any, repo: str, kind: str) -> list[dict[str, Any]]:
    items = client.list_issues(repo, labels=[LABEL_INCIDENT], state="open")
    return [
        i
        for i in items
        if isinstance(i, dict) and "pull_request" not in i and incident_kind_of(i.get("body")) == kind
    ]


def _close(client: Any, repo: str, number: int, comment: str, out: IncidentOutcome) -> None:
    try:
        client.create_issue_comment(repo, number, comment)
        client.update_issue(repo, number, state="closed", state_reason="not_planned")
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"incident #{number} close failed ({_why(exc)})")


def _why(exc: BaseException) -> str:
    if getattr(exc, "status_code", None) == 403:
        return "403: the token needs `issues: write`"
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" {status}" if status else "")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = date_parser.isoparse(value)
    except (ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _within(value: Any, now: datetime, window: timedelta) -> bool:
    stamp = _time(value)
    return stamp is not None and -CLOCK_SKEW <= now - stamp <= window


__all__ = [
    "INCIDENT_KINDS",
    "LABEL_INCIDENT",
    "IncidentOutcome",
    "also_failed_comment",
    "coordinate_incident",
    "incident_body",
    "incident_kind_of",
    "incident_marker",
    "incident_title",
]
