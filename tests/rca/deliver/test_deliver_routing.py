"""Step 13 AC2/AC3 — routing matrix (v1.3 §4.1) and issue policy (v1.3 §8.2)."""

from __future__ import annotations

import pytest

from tests.rca.deliver._helpers import make_summary
from tools.rca.deliver import DeliveryContext, DeliveryInputs
from tools.rca.deliver.targets import ROUTING_TABLE, route, route_variant, should_open_issue


def _context(trigger: str, *, draft: bool = False, fork: bool = False) -> DeliveryContext:
    pr = 7 if trigger == "pull_request" else None
    branch = "main" if trigger == "push_default" else "feature"
    return DeliveryContext(
        trigger=trigger,  # type: ignore[arg-type]
        pr_number=pr,
        pr_is_draft=draft,
        is_fork=fork,
        branch=branch,
        default_branch="main",
        is_default_branch=branch == "main",
        actor="octocat",
        commit_sha="f" * 40,
    )


# (row, context, comment, issue, notify) — v1.3 §4.1 with default inputs.
MATRIX = [
    ("pr_ready", _context("pull_request"), "pr", "on_recurrence", []),
    ("pr_draft", _context("pull_request", draft=True), None, None, []),
    ("pr_fork", _context("pull_request", fork=True), "pr", None, []),
    ("push_default", _context("push_default"), "commit", "always", ["team", "actor"]),
    ("push_branch", _context("push_branch"), None, "on_recurrence", []),
    ("merge_group", _context("merge_group"), None, "on_recurrence", ["actor"]),
    ("tag", _context("tag"), "commit", "always", ["team"]),
    ("schedule", _context("schedule"), None, "always", ["owning_team"]),
    ("dispatch", _context("dispatch"), None, None, ["actor"]),
    ("unknown", _context("unknown"), None, None, []),
]


def test_table_has_exactly_the_ten_rows() -> None:
    assert set(ROUTING_TABLE) == {row for row, *_ in MATRIX}
    assert len(ROUTING_TABLE) == 10


@pytest.mark.parametrize(
    ("row", "context", "comment", "issue", "notify"), MATRIX, ids=[m[0] for m in MATRIX]
)
def test_routing_matrix_defaults(row, context, comment, issue, notify) -> None:
    assert route_variant(context) == row
    plan = route(context, DeliveryInputs())
    assert plan.job_summary is True
    assert plan.comment == comment
    assert plan.issue == issue
    assert plan.notify == notify


def test_draft_fork_pr_is_draft() -> None:
    assert route_variant(_context("pull_request", draft=True, fork=True)) == "pr_draft"


def test_comment_on_branch_push_switch() -> None:
    ctx = _context("push_branch")
    assert route(ctx, DeliveryInputs(comment_on_branch_push=True)).comment == "commit"
    off = route(ctx, DeliveryInputs(comment_on_branch_push=True, comment_on_commit=False))
    assert off.comment is None


@pytest.mark.parametrize("row", ["pr_ready", "pr_fork"])
def test_comment_on_pr_switch(row) -> None:
    ctx = dict((m[0], m[1]) for m in MATRIX)[row]
    plan = route(ctx, DeliveryInputs(comment_on_pr=False))
    assert plan.comment is None
    assert any("comment_on_pr=false" in n for n in plan.notes)


@pytest.mark.parametrize("row", ["push_default", "tag"])
def test_comment_on_commit_switch(row) -> None:
    ctx = dict((m[0], m[1]) for m in MATRIX)[row]
    plan = route(ctx, DeliveryInputs(comment_on_commit=False))
    assert plan.comment is None
    assert plan.issue == "always"  # the switch only affects the comment


def test_fork_issue_needs_allow_fork_issues() -> None:
    plan = route(_context("pull_request", fork=True), DeliveryInputs(allow_fork_issues=True))
    assert plan.issue == "on_recurrence"


# ---- should_open_issue (v1.3 §8.2) ------------------------------------------


@pytest.mark.parametrize("trigger", ["push_default", "tag", "schedule"])
def test_issue_always_for_default_tag_schedule(trigger) -> None:
    assert should_open_issue(_context(trigger), make_summary(seen_count=0), DeliveryInputs())


def test_pr_issue_on_recurrence_threshold() -> None:
    ctx = _context("pull_request")
    inputs = DeliveryInputs(issue_threshold=3)
    assert not should_open_issue(ctx, make_summary(seen_count=2), inputs)
    assert should_open_issue(ctx, make_summary(seen_count=3), inputs)


def test_fork_pr_issue_only_with_allow_fork_issues() -> None:  # delivery AC #17
    ctx = _context("pull_request", fork=True)
    summary = make_summary(seen_count=10)
    assert not should_open_issue(ctx, summary, DeliveryInputs())
    assert should_open_issue(ctx, summary, DeliveryInputs(allow_fork_issues=True))


@pytest.mark.parametrize(
    "ctx",
    [_context("dispatch"), _context("unknown"), _context("pull_request", draft=True)],
    ids=["dispatch", "unknown", "draft"],
)
def test_no_issue_for_dispatch_unknown_draft(ctx) -> None:  # delivery AC #14 (dispatch)
    assert not should_open_issue(ctx, make_summary(seen_count=99), DeliveryInputs())


def test_create_issues_false_disables_all() -> None:
    inputs = DeliveryInputs(create_issues=False)
    for _row, ctx, *_ in MATRIX:
        assert not should_open_issue(ctx, make_summary(seen_count=99), inputs)
        assert route(ctx, inputs).issue is None


def test_missing_history_counts_as_zero() -> None:
    summary = make_summary()
    summary.history = None
    assert not should_open_issue(_context("push_branch"), summary, DeliveryInputs())


def test_inputs_validate_confidence_threshold() -> None:
    assert DeliveryInputs(confidence_threshold="HIGH").confidence_threshold == "high"
    with pytest.raises(ValueError):
        DeliveryInputs(confidence_threshold="certain")
    defaults = DeliveryInputs()
    assert defaults.deliver is False
    assert (defaults.issue_threshold, defaults.quiet_window_minutes) == (3, 60)
