"""Three-stage delivery suppression (delivery spec v1.3 §5). Pure: no client, no clock, no HTTP.

Stage A — hard suppressions, first match wins, sets ``suppressed_by`` and stops B.
Stage B — dedupe against an already-posted comment; never sets ``suppressed_by``.
Stage C — presentation modifiers; always evaluated, cumulative, never suppress.

Reads the post-validate ``AnalysisRecord``: grounding is never recomputed and
citations are never re-stripped here (v1.3 §5).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..models import AnalysisRecord, Summary
from . import (
    CONFIDENCE_LEVELS,
    DeliveryContext,
    DeliveryInputs,
    ExistingComment,
    SuppressionDecision,
)
from .severity import is_flaky

SUPPRESSED_FLAKY = "flaky"  # A1
SUPPRESSED_INFRA_WIDESPREAD = "infra_widespread"  # A2
SUPPRESSED_INFRA_RUNNER = "infra_runner"  # A3
SUPPRESSED_DRAFT = "draft"  # A4
# A5: a delivery error for (run_id, channel) after one retry. Enforced by the writer (Step 15).
SUPPRESSED_DELIVERY_ERROR = "delivery_error"

_CONFIDENCE_RANK = {level: rank for rank, level in enumerate(CONFIDENCE_LEVELS)}


def within_quiet_window(updated_at: datetime, now: datetime, minutes: int) -> bool:
    """True when ``updated_at`` is no older than ``minutes`` before ``now``."""
    updated_at = _aware(updated_at)
    now = _aware(now)
    return timedelta(0) <= now - updated_at <= timedelta(minutes=minutes)


def evaluate(
    summary: Summary,
    record: AnalysisRecord | None,
    context: DeliveryContext,
    inputs: DeliveryInputs,
    *,
    existing: ExistingComment | tuple | None = None,
    now: datetime | None = None,
) -> SuppressionDecision:
    decision = SuppressionDecision()
    _stage_a(summary, context, decision)
    if decision.suppressed_by is None:
        _stage_b(summary, inputs, existing, now, decision)
    _stage_c(summary, record, inputs, decision)
    return decision


def _stage_a(summary: Summary, context: DeliveryContext, decision: SuppressionDecision) -> None:
    short = summary.verdict.short_circuit
    if is_flaky(summary):
        decision.suppressed_by = SUPPRESSED_FLAKY
        decision.add_flaky_label = context.pr_number is not None
        decision.reasons.append("A1 flaky: no comment")
    elif short == "infra_widespread":
        decision.suppressed_by = SUPPRESSED_INFRA_WIDESPREAD
        decision.notify_platform_once = True
        decision.reasons.append("A2 infra_widespread: one platform notification, no per-PR comments")
    elif short == "infra_runner":
        decision.suppressed_by = SUPPRESSED_INFRA_RUNNER
        decision.reasons.append("A3 infra_runner: job summary only, no author comment")
    elif context.trigger == "pull_request" and context.pr_is_draft:
        decision.suppressed_by = SUPPRESSED_DRAFT
        decision.reasons.append("A4 draft PR: job summary only")


def _stage_b(
    summary: Summary,
    inputs: DeliveryInputs,
    existing: ExistingComment | tuple | None,
    now: datetime | None,
    decision: SuppressionDecision,
) -> None:
    if existing is None:
        return
    prior = ExistingComment(*existing)
    if prior.fingerprint_coarse != summary.fingerprint_coarse:
        return
    same_branch = prior.branch == summary.run.head_branch
    # B2 is the more specific case (also no new chat/email), so it is checked first.
    if (
        same_branch
        and now is not None
        and within_quiet_window(prior.updated_at, now, inputs.quiet_window_minutes)
    ):
        decision.dedupe = "update_quiet"
        decision.reasons.append(
            f"B2 same fingerprint on {prior.branch} inside "
            f"{inputs.quiet_window_minutes}m quiet window: update in place, no new notify"
        )
        return
    decision.dedupe = "edit"
    decision.reasons.append("B1 same coarse fingerprint already commented: edit in place")


def _stage_c(
    summary: Summary,
    record: AnalysisRecord | None,
    inputs: DeliveryInputs,
    decision: SuppressionDecision,
) -> None:
    if record is not None:
        grounding = record.grounding
        if grounding is not None and (
            grounding.grounding_rate < 1.0 or grounding.grounded is False
        ):
            decision.unverified_banner = True
            decision.reasons.append(
                f"C1 grounding_rate={grounding.grounding_rate:.2f}: unverified banner"
            )
        elif _display_status(record) == "needs-review":
            decision.unverified_banner = True
            decision.reasons.append("C1 display_status=needs-review: unverified banner")

    if record is not None and record.result is not None:
        confidence, origin = record.result.confidence, "analysis"
    else:
        confidence, origin = summary.classification.confidence, "deterministic card"
    rank = _CONFIDENCE_RANK.get(confidence or "low", 0)
    if rank < _CONFIDENCE_RANK[inputs.confidence_threshold]:
        decision.omit_root_cause = True
        decision.reasons.append(
            f"C2 {origin} confidence {confidence} below threshold "
            f"{inputs.confidence_threshold}: omit root-cause claims"
        )


def _display_status(record: AnalysisRecord) -> str:
    # Imported lazily: analyze pulls in the STGPT HTTP client, and this module
    # must stay importable without any HTTP stack.
    from ..analyze import display_status

    return display_status(record)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


__all__ = [
    "SUPPRESSED_DELIVERY_ERROR",
    "SUPPRESSED_DRAFT",
    "SUPPRESSED_FLAKY",
    "SUPPRESSED_INFRA_RUNNER",
    "SUPPRESSED_INFRA_WIDESPREAD",
    "evaluate",
    "within_quiet_window",
]
