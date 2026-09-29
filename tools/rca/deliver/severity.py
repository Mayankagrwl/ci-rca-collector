"""Delivery severity (delivery spec v1.3 §6). Pure: no client, no clock, no HTTP."""

from __future__ import annotations

from ..models import Summary
from . import DefaultBranchState, DeliveryContext, Severity

CRITICAL_RED_HOURS = 1.0
CRITICAL_CONSECUTIVE_FAILURES = 3

# Base severity per routing variant (the pull_request trigger split by draft).
_BASE_SEVERITY: dict[str, Severity] = {
    "push_default": "high",
    "tag": "high",
    "merge_group": "high",
    "pr_ready": "normal",  # includes fork PRs
    "schedule": "normal",
    "push_branch": "low",
    "pr_draft": "low",
    "dispatch": "low",
    "unknown": "low",
}
_CRITICAL_TRIGGERS = frozenset({"push_default", "tag"})


def _variant(context: DeliveryContext) -> str:
    if context.trigger == "pull_request":
        return "pr_draft" if context.pr_is_draft else "pr_ready"
    return context.trigger


def is_flaky(summary: Summary) -> bool:
    return bool(summary.classification.is_flaky) or (
        summary.verdict.short_circuit == "flake_same_sha_passed"
    )


def _red_too_long(state: DefaultBranchState | None) -> bool:
    if state is None:
        return False
    hours = state.red_duration_hours
    count = state.consecutive_failures
    # None never triggers critical: an unknown streak is not evidence of one.
    return (hours is not None and hours > CRITICAL_RED_HOURS) or (
        count is not None and count >= CRITICAL_CONSECUTIVE_FAILURES
    )


def severity(
    context: DeliveryContext,
    state: DefaultBranchState | None,
    summary: Summary,
    *,
    widespread_already_notified: bool = False,
) -> Severity:
    if is_flaky(summary):
        return "info"
    if summary.verdict.short_circuit == "infra_widespread" and widespread_already_notified:
        return "info"
    if context.trigger in _CRITICAL_TRIGGERS and _red_too_long(state):
        return "critical"
    return _BASE_SEVERITY.get(_variant(context), "low")


__all__ = ["CRITICAL_CONSECUTIVE_FAILURES", "CRITICAL_RED_HOURS", "is_flaky", "severity"]
