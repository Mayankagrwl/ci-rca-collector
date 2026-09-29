"""Sticky RCA comments: find by marker, then create / edit / leave unchanged (v1.3 §7.3).

Shared by ``pr_comment`` (issue-comments API) and ``commit_comment`` (commit
comments API). The comment is matched by its first line — the marker — and
never by author (the bot identity differs between GITHUB_TOKEN and a PAT).

Nothing here raises: every lookup or write failure becomes a note or a
``WriteResult(action="failed", error=...)`` that the caller records.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from dateutil import parser as date_parser

from ..github_api import GitHubAPIError
from . import ExistingComment
from .render_comment import MARKER_PREFIX, marker_line

TargetKind = Literal["pr", "commit"]
WriteAction = Literal["created", "updated", "unchanged", "skipped", "failed"]
DELIVERY_BUDGET_SECONDS = 30.0  # v1.3 §14: whole delivery, wall-clock
BUDGET_EXCEEDED = "delivery budget exceeded"
# Prefix of the note find_existing returns when listing failed. Callers must not
# create in that case: an unseen existing comment would be duplicated.
LOOKUP_FAILED = "lookup failed"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Channel:
    """How one comment target maps onto ``GitHubClient`` methods."""

    kind: TargetKind
    label: str  # human name, used in notes and errors
    list_method: str
    create_method: str
    update_method: str
    scope: str  # the token permission a 403 means is missing
    delivered_as: str  # DeliveryReport.delivered_to entry


CHANNELS: dict[str, Channel] = {
    "pr": Channel(
        kind="pr",
        label="PR comment",
        list_method="list_issue_comments",
        create_method="create_issue_comment",
        update_method="update_issue_comment",
        scope="pull-requests: write",
        delivered_as="pr_comment",
    ),
    "commit": Channel(
        kind="commit",
        label="commit comment",
        list_method="list_commit_comments",
        create_method="create_commit_comment",
        update_method="update_commit_comment",
        scope="contents: write",
        delivered_as="commit_comment",
    ),
}


@dataclass
class DeliveryBudget:
    """Wall-clock budget for the whole delivery, on an injectable clock (seconds)."""

    started_at: float
    seconds: float = DELIVERY_BUDGET_SECONDS

    def exceeded(self, now: float) -> bool:
        return now - self.started_at >= self.seconds


@dataclass
class WriteResult:
    action: WriteAction
    url: str | None = None
    error: str | None = None
    comment_id: int | None = None


# ---- finding ---------------------------------------------------------------------------


def first_line(body: str | None) -> str:
    """The body's first line after stripping a BOM and surrounding whitespace."""
    text = (body or "").lstrip("﻿").strip()
    return text.split("\n", 1)[0].strip()


def is_ours(body: str | None, fingerprint_coarse: str) -> bool:
    """Exact marker on the first line only — a quoted ``> <!-- rca-bot…`` never matches."""
    return first_line(body) == marker_line(fingerprint_coarse)


def fingerprint_of(body: str | None) -> str | None:
    line = first_line(body)
    if line.startswith(MARKER_PREFIX) and line.endswith(" -->"):
        return line[len(MARKER_PREFIX) : -len(" -->")] or None
    return None


def find_existing(
    client: Any,
    repo: str,
    *,
    target_kind: TargetKind,
    target: int | str,
    fingerprint_coarse: str,
    branch: str,
) -> tuple[ExistingComment | None, list[str]]:
    """The newest RCA comment for this coarse fingerprint on the target. Never raises."""
    channel = CHANNELS[target_kind]
    try:
        comments = getattr(client, channel.list_method)(repo, target)
    except Exception as exc:  # noqa: BLE001 — a failed lookup is a note, not a crash
        return None, [f"{LOOKUP_FAILED}: {channel.label}s on {target}: {_describe(exc)}"]
    ours = [c for c in comments if isinstance(c, dict) and is_ours(c.get("body"), fingerprint_coarse)]
    if not ours:
        return None, []
    ours.sort(key=_updated_key, reverse=True)
    chosen = ours[0]
    notes: list[str] = []
    if len(ours) > 1:
        dupes = ", ".join(str(c.get("id")) for c in ours[1:])
        notes.append(
            f"{len(ours)} RCA {channel.label}s for this fingerprint on {target}; "
            f"editing the newest ({chosen.get('id')}), duplicates left untouched: {dupes}"
        )
    return _existing(chosen, fingerprint_coarse, branch), notes


def _existing(comment: dict[str, Any], fingerprint_coarse: str, branch: str) -> ExistingComment:
    comment_id = comment.get("id")
    return ExistingComment(
        fingerprint_coarse=fingerprint_coarse,
        branch=branch,
        updated_at=_updated_key(comment),
        comment_id=comment_id if isinstance(comment_id, int) else None,
        html_url=comment.get("html_url"),
        body=comment.get("body") or "",
    )


def _updated_key(comment: dict[str, Any]) -> datetime:
    for key in ("updated_at", "created_at"):
        value = comment.get(key)
        if isinstance(value, str) and value.strip():
            try:
                parsed = date_parser.isoparse(value)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                continue
    return _EPOCH


# ---- writing -----------------------------------------------------------------------------


def post_or_update(
    client: Any,
    repo: str,
    target: int | str,
    body: str,
    existing: ExistingComment | None,
    *,
    target_kind: TargetKind,
    clock: Callable[[], float],
    budget: DeliveryBudget,
) -> WriteResult:
    """Leave unchanged, edit, or create exactly once. Never raises; never deletes."""
    channel = CHANNELS[target_kind]
    if existing is not None and existing.body == body:
        return WriteResult("unchanged", url=existing.html_url, comment_id=existing.comment_id)
    if budget.exceeded(clock()):
        return WriteResult("skipped", error=BUDGET_EXCEEDED)

    if existing is not None and existing.comment_id is not None:
        try:
            payload = getattr(client, channel.update_method)(repo, existing.comment_id, body)
        except Exception as exc:  # noqa: BLE001
            return WriteResult("failed", error=_write_error(channel, "edit", exc))
        return WriteResult(
            "updated",
            url=payload.get("html_url") or existing.html_url,
            comment_id=existing.comment_id,
        )

    try:
        payload = getattr(client, channel.create_method)(repo, target, body)
    except GitHubAPIError as exc:
        if exc.status_code is None:
            # Timeout / dropped connection: the comment may have landed. Look once
            # more; never POST twice in one run (create_* do not retry on timeout).
            return _confirm_create(client, repo, target, body, channel, exc)
        return WriteResult("failed", error=_write_error(channel, "create", exc))
    except Exception as exc:  # noqa: BLE001
        return WriteResult("failed", error=_write_error(channel, "create", exc))
    return WriteResult("created", url=payload.get("html_url"), comment_id=payload.get("id"))


def _confirm_create(
    client: Any,
    repo: str,
    target: int | str,
    body: str,
    channel: Channel,
    original: GitHubAPIError,
) -> WriteResult:
    fingerprint = fingerprint_of(body)
    try:
        comments = getattr(client, channel.list_method)(repo, target)
    except Exception:  # noqa: BLE001
        comments = []
    for comment in comments:
        if (
            isinstance(comment, dict)
            and fingerprint is not None
            and is_ours(comment.get("body"), fingerprint)
            and comment.get("body") == body
        ):
            return WriteResult(
                "created", url=comment.get("html_url"), comment_id=comment.get("id")
            )
    return WriteResult(
        "failed",
        error=f"{_write_error(channel, 'create', original)}; not found on re-check, not retried",
    )


def _write_error(channel: Channel, verb: str, exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if status == 403:
        return (
            f"{channel.label} {verb} forbidden (403): the token needs "
            f"`{channel.scope}` (or the org restricts it)"
        )
    return f"{channel.label} {verb} failed: {_describe(exc)}"


def _describe(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if isinstance(exc, GitHubAPIError):
        # Our own messages: method + path only, never a token.
        return f"{exc} (status {status})" if status is not None else str(exc)
    return type(exc).__name__


__all__ = [
    "BUDGET_EXCEEDED",
    "CHANNELS",
    "DELIVERY_BUDGET_SECONDS",
    "DeliveryBudget",
    "LOOKUP_FAILED",
    "WriteResult",
    "find_existing",
    "first_line",
    "fingerprint_of",
    "is_ours",
    "post_or_update",
]
