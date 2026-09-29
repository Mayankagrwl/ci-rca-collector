"""Step 13 AC6 — three-stage suppression (v1.3 §5)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.rca.deliver._helpers import NOW, make_summary
from tools.rca.deliver import DeliveryContext, DeliveryInputs, ExistingComment
from tools.rca.deliver.suppress import (
    SUPPRESSED_DELIVERY_ERROR,
    evaluate,
    within_quiet_window,
)
from tools.rca.models import AnalysisRecord, AnalysisResult, GroundingResult


def _ctx(trigger: str = "pull_request", *, draft: bool = False, pr: int | None = 7):
    return DeliveryContext(
        trigger=trigger,  # type: ignore[arg-type]
        pr_number=pr if trigger == "pull_request" else None,
        pr_is_draft=draft,
        branch="main",
        default_branch="main",
        is_default_branch=True,
        actor="octocat",
        commit_sha="f" * 40,
    )


def _record(
    *,
    confidence: str = "high",
    rate: float | None = None,
    grounded: bool = True,
    status: str = "ok",
) -> AnalysisRecord:
    return AnalysisRecord(
        status=status,  # type: ignore[arg-type]
        result=AnalysisResult(
            root_cause="the cause", suggested_fix="the fix", confidence=confidence  # type: ignore[arg-type]
        ),
        grounding=None if rate is None else GroundingResult(grounded=grounded, grounding_rate=rate),
    )


def _existing(summary, *, minutes_ago: float, branch: str | None = None) -> ExistingComment:
    return ExistingComment(
        summary.fingerprint_coarse,
        branch or summary.run.head_branch,
        NOW - timedelta(minutes=minutes_ago),
    )


# ---- Stage A -----------------------------------------------------------------


def test_flaky_suppresses_and_labels_pr() -> None:  # delivery AC #8
    decision = evaluate(make_summary(is_flaky=True), _record(), _ctx(), DeliveryInputs())
    assert decision.suppressed_by == "flaky"
    assert decision.add_flaky_label is True


def test_flaky_without_pr_has_no_label() -> None:
    summary = make_summary(short_circuit="flake_same_sha_passed")
    decision = evaluate(summary, _record(), _ctx("push_default"), DeliveryInputs())
    assert decision.suppressed_by == "flaky"
    assert decision.add_flaky_label is False


def test_widespread_notifies_platform_once_and_no_comment() -> None:  # delivery AC #9
    summary = make_summary(short_circuit="infra_widespread")
    decision = evaluate(summary, _record(), _ctx(), DeliveryInputs())
    assert decision.suppressed_by == "infra_widespread"
    assert decision.notify_platform_once is True


def test_infra_runner() -> None:
    summary = make_summary(short_circuit="infra_runner")
    decision = evaluate(summary, _record(), _ctx(), DeliveryInputs())
    assert decision.suppressed_by == "infra_runner"
    assert decision.notify_platform_once is False


def test_draft() -> None:
    decision = evaluate(make_summary(), _record(), _ctx(draft=True), DeliveryInputs())
    assert decision.suppressed_by == "draft"


@pytest.mark.parametrize(
    ("summary_kwargs", "ctx_kwargs", "expected"),
    [
        ({"is_flaky": True}, {}, "flaky"),
        ({"short_circuit": "infra_widespread"}, {}, "infra_widespread"),
        ({"short_circuit": "infra_runner"}, {}, "infra_runner"),
        ({}, {"draft": True}, "draft"),
        ({"is_flaky": True, "short_circuit": "infra_runner"}, {"draft": True}, "flaky"),
        ({"short_circuit": "infra_runner"}, {"draft": True}, "infra_runner"),
    ],
)
def test_every_stage_a_outcome_sets_suppressed_by(summary_kwargs, ctx_kwargs, expected) -> None:
    # delivery AC #12; also: first match wins, and Stage A stops Stage B.
    summary = make_summary(**summary_kwargs)
    decision = evaluate(
        summary,
        _record(),
        _ctx(**ctx_kwargs),
        DeliveryInputs(),
        existing=_existing(summary, minutes_ago=5),
        now=NOW,
    )
    assert decision.suppressed_by == expected
    assert decision.dedupe is None


def test_delivery_error_is_reserved() -> None:
    assert SUPPRESSED_DELIVERY_ERROR == "delivery_error"


def test_clean_failure_is_not_suppressed() -> None:
    decision = evaluate(make_summary(), _record(), _ctx(), DeliveryInputs(), now=NOW)
    assert decision.suppressed_by is None
    assert decision.dedupe is None
    assert not decision.unverified_banner
    assert not decision.omit_root_cause


# ---- Stage B -----------------------------------------------------------------


def test_b1_same_fingerprint_edits() -> None:
    summary = make_summary(branch="main")
    existing = _existing(summary, minutes_ago=600)  # outside the quiet window
    decision = evaluate(summary, _record(), _ctx(), DeliveryInputs(), existing=existing, now=NOW)
    assert decision.dedupe == "edit"
    assert decision.suppressed_by is None


def test_b2_same_branch_inside_quiet_window() -> None:
    summary = make_summary(branch="main")
    existing = _existing(summary, minutes_ago=30)
    decision = evaluate(summary, _record(), _ctx(), DeliveryInputs(), existing=existing, now=NOW)
    assert decision.dedupe == "update_quiet"
    assert decision.suppressed_by is None


def test_b2_other_branch_is_b1() -> None:
    summary = make_summary(branch="main")
    existing = _existing(summary, minutes_ago=5, branch="release")
    decision = evaluate(summary, _record(), _ctx(), DeliveryInputs(), existing=existing, now=NOW)
    assert decision.dedupe == "edit"


def test_b2_needs_explicit_now() -> None:
    summary = make_summary()
    existing = _existing(summary, minutes_ago=5)
    decision = evaluate(summary, _record(), _ctx(), DeliveryInputs(), existing=existing)
    assert decision.dedupe == "edit"  # no wall-clock read in a pure function


def test_different_fingerprint_is_not_deduped() -> None:
    summary = make_summary()
    existing = ExistingComment("other-fingerprint", "main", NOW)
    decision = evaluate(summary, _record(), _ctx(), DeliveryInputs(), existing=existing, now=NOW)
    assert decision.dedupe is None


def test_existing_accepts_plain_tuple() -> None:
    summary = make_summary()
    existing = (summary.fingerprint_coarse, "main", NOW - timedelta(minutes=1))
    decision = evaluate(summary, _record(), _ctx(), DeliveryInputs(), existing=existing, now=NOW)
    assert decision.dedupe == "update_quiet"


def test_within_quiet_window() -> None:
    assert within_quiet_window(NOW - timedelta(minutes=60), NOW, 60)
    assert not within_quiet_window(NOW - timedelta(minutes=61), NOW, 60)
    assert not within_quiet_window(NOW + timedelta(minutes=1), NOW, 60)  # future stamp
    assert within_quiet_window((NOW - timedelta(minutes=1)).replace(tzinfo=None), NOW, 60)


# ---- Stage C -----------------------------------------------------------------


def test_low_confidence_below_medium_omits_root_cause() -> None:  # delivery AC #10
    decision = evaluate(make_summary(), _record(confidence="low"), _ctx(), DeliveryInputs())
    assert decision.omit_root_cause is True
    assert decision.suppressed_by is None


@pytest.mark.parametrize(
    ("confidence", "threshold", "omit"),
    [
        ("low", "low", False),
        ("medium", "medium", False),
        ("medium", "high", True),
        ("high", "high", False),
    ],
)
def test_threshold_table(confidence, threshold, omit) -> None:
    decision = evaluate(
        make_summary(),
        _record(confidence=confidence),
        _ctx(),
        DeliveryInputs(confidence_threshold=threshold),
    )
    assert decision.omit_root_cause is omit


def test_deterministic_card_confidence_when_no_analysis_result() -> None:
    high_card = make_summary(confidence="high")
    assert not evaluate(high_card, None, _ctx(), DeliveryInputs()).omit_root_cause
    low_card = make_summary(confidence="low")
    assert evaluate(low_card, None, _ctx(), DeliveryInputs()).omit_root_cause
    skipped = AnalysisRecord(status="skipped")  # model skipped, no result
    assert evaluate(low_card, skipped, _ctx(), DeliveryInputs()).omit_root_cause


def test_partial_grounding_adds_banner() -> None:  # delivery AC #11
    decision = evaluate(make_summary(), _record(rate=0.8), _ctx(), DeliveryInputs())
    assert decision.unverified_banner is True
    assert decision.suppressed_by is None


def test_ungrounded_flag_and_needs_review_add_banner() -> None:
    ungrounded = _record(rate=1.0, grounded=False)
    assert evaluate(make_summary(), ungrounded, _ctx(), DeliveryInputs()).unverified_banner
    needs_review = _record(status="unvalidated")
    assert evaluate(make_summary(), needs_review, _ctx(), DeliveryInputs()).unverified_banner
    fully = _record(rate=1.0, grounded=True)
    assert not evaluate(make_summary(), fully, _ctx(), DeliveryInputs()).unverified_banner


def test_c1_c2_and_b1_combine_on_one_decision() -> None:  # delivery AC #33
    summary = make_summary(branch="main")
    decision = evaluate(
        summary,
        _record(confidence="low", rate=0.6),
        _ctx(),
        DeliveryInputs(),
        existing=_existing(summary, minutes_ago=600),
        now=NOW,
    )
    assert decision.suppressed_by is None
    assert decision.dedupe == "edit"
    assert decision.unverified_banner is True
    assert decision.omit_root_cause is True
    assert [r[:2] for r in decision.reasons] == ["B1", "C1", "C2"]
