"""Step 13 AC5 — severity table (v1.3 §6)."""

from __future__ import annotations

import pytest

from tests.rca.deliver._helpers import make_summary
from tools.rca.deliver import DefaultBranchState, DeliveryContext
from tools.rca.deliver.severity import severity


def _ctx(trigger: str, *, draft: bool = False, fork: bool = False) -> DeliveryContext:
    return DeliveryContext(
        trigger=trigger,  # type: ignore[arg-type]
        pr_number=7 if trigger == "pull_request" else None,
        pr_is_draft=draft,
        is_fork=fork,
        branch="main",
        default_branch="main",
        is_default_branch=True,
        actor="octocat",
        commit_sha="f" * 40,
    )


_CALM = DefaultBranchState(red_duration_hours=0.2, consecutive_failures=1, source="run_list")


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (_ctx("push_default"), "high"),
        (_ctx("tag"), "high"),
        (_ctx("merge_group"), "high"),
        (_ctx("pull_request"), "normal"),
        (_ctx("pull_request", fork=True), "normal"),
        (_ctx("schedule"), "normal"),
        (_ctx("push_branch"), "low"),
        (_ctx("pull_request", draft=True), "low"),
        (_ctx("dispatch"), "low"),
        (_ctx("unknown"), "low"),
    ],
    ids=[
        "push_default", "tag", "merge_group", "pr_ready", "pr_fork",
        "schedule", "push_branch", "pr_draft", "dispatch", "unknown",
    ],
)
def test_base_severity(context, expected) -> None:
    assert severity(context, _CALM, make_summary()) == expected


@pytest.mark.parametrize("trigger", ["push_default", "tag"])
def test_critical_via_duration(trigger) -> None:
    state = DefaultBranchState(red_duration_hours=1.5, consecutive_failures=1)
    assert severity(_ctx(trigger), state, make_summary()) == "critical"


@pytest.mark.parametrize("trigger", ["push_default", "tag"])
def test_critical_via_count(trigger) -> None:
    state = DefaultBranchState(red_duration_hours=0.1, consecutive_failures=3)
    assert severity(_ctx(trigger), state, make_summary()) == "critical"


def test_boundaries_are_strict_hour_and_inclusive_count() -> None:
    at_one_hour = DefaultBranchState(red_duration_hours=1.0, consecutive_failures=2)
    assert severity(_ctx("push_default"), at_one_hour, make_summary()) == "high"


def test_none_values_never_critical() -> None:
    unknown = DefaultBranchState(red_duration_hours=None, consecutive_failures=None)
    assert severity(_ctx("push_default"), unknown, make_summary()) == "high"
    assert severity(_ctx("tag"), None, make_summary()) == "high"
    history_only = DefaultBranchState(red_duration_hours=2.0, consecutive_failures=None)
    assert severity(_ctx("push_default"), history_only, make_summary()) == "critical"


def test_critical_only_for_default_branch_and_tag() -> None:
    red = DefaultBranchState(red_duration_hours=5, consecutive_failures=9)
    assert severity(_ctx("merge_group"), red, make_summary()) == "high"
    assert severity(_ctx("schedule"), red, make_summary()) == "normal"


def test_info_for_flaky_and_notified_widespread() -> None:
    red = DefaultBranchState(red_duration_hours=5, consecutive_failures=9)
    assert severity(_ctx("push_default"), red, make_summary(is_flaky=True)) == "info"
    flake = make_summary(short_circuit="flake_same_sha_passed")
    assert severity(_ctx("pull_request"), None, flake) == "info"
    widespread = make_summary(short_circuit="infra_widespread")
    assert severity(_ctx("pull_request"), None, widespread) == "normal"
    assert (
        severity(_ctx("pull_request"), None, widespread, widespread_already_notified=True)
        == "info"
    )
