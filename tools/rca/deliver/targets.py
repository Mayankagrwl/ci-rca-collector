"""Delivery context, routing and default-branch state (delivery spec v1.3 §4, §4.1, §4.2, §8.2).

The only module in ``deliver/`` that touches a client, and only through the
read-only ``ReadClient`` Protocol (three lookups: ``get_repo``, ``ref_is_tag``,
``get_run`` / ``list_runs``). Performs no writes. ``resolve_context`` and
``default_branch_state`` never raise; failures become notes / fallbacks.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from dateutil import parser as date_parser

from ..models import Summary
from . import DefaultBranchState, DeliveryContext, DeliveryInputs, RoutePlan

DEFAULT_BRANCH_ENV = "RCA_DEFAULT_BRANCH"
DEFAULT_BRANCH_FALLBACK = "main"
RUN_WALK_CAP = 50


class ReadClient(Protocol):
    """The read-only slice of ``GitHubClient`` delivery decisions may use."""

    def get_repo(self, repo: str) -> dict[str, Any]: ...

    def get_run(self, repo: str, run_id: int) -> dict[str, Any]: ...

    def list_runs(
        self,
        repo: str,
        *,
        head_sha: str | None = None,
        branch: str | None = None,
        status: str | None = None,
        per_page: int = 50,
        workflow_id: int | str | None = None,
    ) -> list[dict[str, Any]]: ...

    def ref_is_tag(self, repo: str, name: str) -> bool: ...


# ---- context ---------------------------------------------------------------


def _resolve_default_branch(
    client: ReadClient, repo: str, env: Mapping[str, str], notes: list[str]
) -> str:
    try:
        value = client.get_repo(repo).get("default_branch")
        if isinstance(value, str) and value.strip():
            return value.strip()
        notes.append("get_repo returned no default_branch")
    except Exception as exc:  # noqa: BLE001 — never crash delivery on a lookup
        notes.append(f"get_repo failed: {_describe(exc)}")
    from_env = (env.get(DEFAULT_BRANCH_ENV) or "").strip()
    if from_env:
        notes.append(f"default_branch from {DEFAULT_BRANCH_ENV}={from_env}")
        return from_env
    notes.append(f"default_branch fell back to {DEFAULT_BRANCH_FALLBACK!r}")
    return DEFAULT_BRANCH_FALLBACK


def _is_tag(client: ReadClient, repo: str, name: str, notes: list[str]) -> bool:
    if not name:
        return False
    try:
        return bool(client.ref_is_tag(repo, name))
    except Exception as exc:  # noqa: BLE001 — lookup failure → not a tag
        notes.append(f"tag lookup for {name!r} failed; treating as not a tag: {_describe(exc)}")
        return False


def _trigger(
    summary: Summary,
    *,
    client: ReadClient,
    repo: str,
    default_branch: str,
    notes: list[str],
) -> str:
    """First match wins (v1.3 §4)."""
    run = summary.run
    event = (run.event or "").strip()
    if run.pr_number is not None:
        return "pull_request"
    if event == "merge_group":
        return "merge_group"
    if event == "push":
        if _is_tag(client, repo, run.head_branch, notes):
            return "tag"
        if run.head_branch == default_branch:
            return "push_default"
        return "push_branch"
    if event == "schedule":
        return "schedule"
    if event == "workflow_dispatch":
        return "dispatch"
    return "unknown"


def resolve_context(
    summary: Summary,
    *,
    client: ReadClient,
    repo: str,
    env: Mapping[str, str] = os.environ,
    now: datetime | None = None,
) -> tuple[DeliveryContext, list[str]]:
    """Summary → DeliveryContext. Never raises; returns (context, notes)."""
    _ = now  # accepted for signature symmetry; context has no time component
    notes: list[str] = []
    run = summary.run
    try:
        default_branch = _resolve_default_branch(client, repo, env, notes)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"default_branch resolution error: {_describe(exc)}")
        default_branch = DEFAULT_BRANCH_FALLBACK
    try:
        trigger = _trigger(
            summary, client=client, repo=repo, default_branch=default_branch, notes=notes
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"trigger resolution error: {_describe(exc)}")
        trigger = "unknown"
    branch = run.head_branch or ""
    context = DeliveryContext(
        trigger=trigger,  # type: ignore[arg-type]
        pr_number=run.pr_number,
        pr_is_draft=bool(summary.changes.pr_is_draft) if summary.changes is not None else False,
        is_fork=bool(run.is_fork),
        branch=branch,
        default_branch=default_branch,
        is_default_branch=branch == default_branch,
        actor=run.actor or "",
        commit_sha=run.head_sha or "",
    )
    return context, notes


# ---- routing (v1.3 §4.1) ------------------------------------------------------


@dataclass(frozen=True)
class _Route:
    comment: Literal["pr", "commit"] | None
    comment_requires: tuple[str, ...]  # DeliveryInputs switches that must all be true
    issue: Literal["always", "on_recurrence"] | None
    issue_requires: tuple[str, ...]
    notify: tuple[str, ...]


# One row per trigger/variant — the routing matrix as data, not an if-chain.
ROUTING_TABLE: dict[str, _Route] = {
    "pr_ready": _Route("pr", ("comment_on_pr",), "on_recurrence", (), ()),
    "pr_draft": _Route(None, (), None, (), ()),
    "pr_fork": _Route("pr", ("comment_on_pr",), "on_recurrence", ("allow_fork_issues",), ()),
    "push_default": _Route("commit", ("comment_on_commit",), "always", (), ("team", "actor")),
    "push_branch": _Route(
        "commit", ("comment_on_commit", "comment_on_branch_push"), "on_recurrence", (), ()
    ),
    "merge_group": _Route(None, (), "on_recurrence", (), ("actor",)),
    "tag": _Route("commit", ("comment_on_commit",), "always", (), ("team",)),
    "schedule": _Route(None, (), "always", (), ("owning_team",)),
    "dispatch": _Route(None, (), None, (), ("actor",)),
    "unknown": _Route(None, (), None, (), ()),
}


def route_variant(context: DeliveryContext) -> str:
    """Routing-table row key: the trigger, with pull_request split into ready/draft/fork."""
    if context.trigger != "pull_request":
        return context.trigger
    if context.pr_is_draft:
        return "pr_draft"
    if context.is_fork:
        return "pr_fork"
    return "pr_ready"


def route(context: DeliveryContext, inputs: DeliveryInputs) -> RoutePlan:
    variant = route_variant(context)
    row = ROUTING_TABLE[variant]
    notes: list[str] = [f"route: {variant}"]

    comment = row.comment
    off = [name for name in row.comment_requires if not getattr(inputs, name)]
    if comment is not None and off:
        notes.append(f"{comment} comment disabled by {', '.join(f'{n}=false' for n in off)}")
        comment = None

    issue = row.issue
    if issue is not None and not inputs.create_issues:
        notes.append("issue disabled by create_issues=false")
        issue = None
    off = [name for name in row.issue_requires if not getattr(inputs, name)]
    if issue is not None and off:
        notes.append(f"issue disabled by {', '.join(f'{n}=false' for n in off)}")
        issue = None

    return RoutePlan(
        job_summary=True,
        comment=comment,
        issue=issue,
        notify=list(row.notify),
        notes=notes,
    )


def should_open_issue(
    context: DeliveryContext, summary: Summary, inputs: DeliveryInputs
) -> bool:
    """v1.3 §8.2, via the routing table's issue policy."""
    plan = route(context, inputs)
    if plan.issue == "always":
        return True
    if plan.issue == "on_recurrence":
        seen = summary.history.seen_count if summary.history is not None else 0
        return seen >= inputs.issue_threshold
    return False


# ---- default-branch state (v1.3 §4.2) ----------------------------------------


def default_branch_state(
    summary: Summary,
    *,
    client: ReadClient,
    repo: str,
    default_branch: str,
    now: datetime | None = None,
) -> DefaultBranchState:
    """Streak of non-success runs on the default branch. Only for push_default / tag.

    Never raises: a run-list failure falls back to ``Summary.history``.
    """
    now = _aware(now or datetime.now(timezone.utc))
    try:
        runs = _workflow_runs(summary, client=client, repo=repo, default_branch=default_branch)
        return _state_from_runs(runs, summary, now)
    except Exception:  # noqa: BLE001 — fall back, never crash
        return _state_from_history(summary, now)


def _workflow_runs(
    summary: Summary, *, client: ReadClient, repo: str, default_branch: str
) -> list[dict[str, Any]]:
    workflow_id: Any = None
    try:
        workflow_id = client.get_run(repo, summary.run.run_id).get("workflow_id")
    except Exception:  # noqa: BLE001 — fall back to name filtering below
        workflow_id = None
    if workflow_id is not None:
        return list(
            client.list_runs(
                repo,
                branch=default_branch,
                status="completed",
                per_page=RUN_WALK_CAP,
                workflow_id=workflow_id,
            )
        )
    runs = client.list_runs(
        repo, branch=default_branch, status="completed", per_page=RUN_WALK_CAP
    )
    name = summary.run.workflow_name
    return [run for run in runs if run.get("name") == name]


def _state_from_runs(
    runs: list[dict[str, Any]], summary: Summary, now: datetime
) -> DefaultBranchState:
    ordered = sorted(runs, key=_created_key, reverse=True)
    # Walk from the current run: runs created after it do not describe this failure.
    current_id = summary.run.run_id
    for index, run in enumerate(ordered):
        if run.get("id") == current_id:
            ordered = ordered[index:]
            break
    else:
        # Not listed yet: the current (failed) run still counts as the first.
        ordered = [{"id": current_id, "conclusion": "failure", "created_at": None}] + ordered

    streak: list[dict[str, Any]] = []
    last_green_sha: str | None = None
    for run in ordered[:RUN_WALK_CAP]:
        if run.get("conclusion") == "success":
            last_green_sha = run.get("head_sha")
            break
        streak.append(run)

    red_since: datetime | None = None
    for run in reversed(streak):  # oldest failing run in the streak first
        red_since = _parse_time(run.get("created_at"))
        if red_since is not None:
            break
    return DefaultBranchState(
        red_since=red_since,
        red_duration_hours=_hours_between(red_since, now),
        consecutive_failures=len(streak),
        last_green_sha=last_green_sha,
        source="run_list",
    )


def _state_from_history(summary: Summary, now: datetime) -> DefaultBranchState:
    history = summary.history
    if history is None or (history.last_success_at is None and not history.last_success_sha):
        return DefaultBranchState(source="none")
    red_since = _aware(history.last_success_at) if history.last_success_at else None
    return DefaultBranchState(
        red_since=red_since,
        red_duration_hours=_hours_between(red_since, now),
        consecutive_failures=None,
        last_green_sha=history.last_success_sha,
        source="history",
    )


# ---- helpers -----------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    return f"{name} (status {status})" if status is not None else name


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


def _created_key(run: dict[str, Any]) -> datetime:
    return _parse_time(run.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)


def _hours_between(start: datetime | None, end: datetime) -> float | None:
    if start is None:
        return None
    return max(0.0, (end - start).total_seconds() / 3600.0)


__all__ = [
    "DEFAULT_BRANCH_ENV",
    "ROUTING_TABLE",
    "ReadClient",
    "default_branch_state",
    "resolve_context",
    "route",
    "route_variant",
    "should_open_issue",
]
