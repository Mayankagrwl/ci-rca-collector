"""Step 13 AC4 — DefaultBranchState from the workflow's run list (v1.3 §4.2)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.rca.deliver._helpers import NOW, REPO, FakeClient, make_summary, run_row
from tools.rca.deliver.targets import default_branch_state
from tools.rca.github_api import GitHubAPIError
from tools.rca.models import HistoryContext


def _state(client, summary=None):
    return default_branch_state(
        summary or make_summary(run_id=100),
        client=client,
        repo=REPO,
        default_branch="main",
        now=NOW,
    )


def test_streak_of_three_failures() -> None:
    client = FakeClient(
        runs=[
            run_row(100, "failure", 5),
            run_row(99, "failure", 30),
            run_row(98, "cancelled", 60),
            run_row(97, "success", 120, sha="green"),
            run_row(96, "failure", 180),
        ]
    )
    state = _state(client)
    assert state.consecutive_failures == 3
    assert state.last_green_sha == "green"
    assert state.source == "run_list"
    listed = [kw for name, kw in client.calls if name == "list_runs"][0]
    assert listed["workflow_id"] == 7
    assert listed["branch"] == "main"
    assert listed["status"] == "completed"


def test_red_for_90_minutes() -> None:  # delivery AC #6
    client = FakeClient(
        runs=[run_row(100, "failure", 10), run_row(99, "failure", 90), run_row(98, "success", 95)]
    )
    state = _state(client)
    assert state.red_duration_hours == pytest.approx(1.5)
    assert state.red_since == NOW - timedelta(minutes=90)


def test_red_since_is_oldest_failing_run_in_streak() -> None:
    client = FakeClient(
        runs=[
            run_row(99, "failure", 60),  # listed out of order on purpose
            run_row(100, "failure", 5),
            run_row(98, "failure", 200),
            run_row(97, "success", 300),
        ]
    )
    state = _state(client)
    assert state.red_since == NOW - timedelta(minutes=200)
    assert state.consecutive_failures == 3


def test_other_workflows_are_excluded() -> None:  # delivery AC #32
    client = FakeClient(
        runs=[
            run_row(100, "failure", 5),
            run_row(55, "success", 10, workflow_id=8, name="Docs"),  # another workflow
            run_row(99, "failure", 20),
            run_row(98, "success", 30, sha="green"),
        ]
    )
    state = _state(client)
    assert state.consecutive_failures == 2
    assert state.last_green_sha == "green"


def test_name_filter_when_workflow_id_unavailable() -> None:
    client = FakeClient(
        run_error=GitHubAPIError("nope", status_code=404),
        runs=[
            run_row(100, "failure", 5),
            run_row(55, "success", 10, name="Docs"),
            run_row(99, "failure", 20),
            run_row(98, "success", 30, sha="green"),
        ],
    )
    state = _state(client)
    assert state.consecutive_failures == 2
    listed = [kw for name, kw in client.calls if name == "list_runs"][0]
    assert listed.get("workflow_id") is None


def test_current_run_counts_even_if_not_listed_yet() -> None:
    client = FakeClient(runs=[run_row(99, "failure", 20), run_row(98, "success", 30)])
    state = _state(client)
    assert state.consecutive_failures == 2
    assert state.red_since == NOW - timedelta(minutes=20)


def test_newer_runs_than_current_are_ignored() -> None:
    client = FakeClient(
        runs=[
            run_row(101, "success", 1),  # a later run; does not describe this failure
            run_row(100, "failure", 5),
            run_row(99, "success", 20, sha="green"),
        ]
    )
    state = _state(client)
    assert state.consecutive_failures == 1
    assert state.last_green_sha == "green"


def test_streak_capped_at_50_uses_oldest_seen() -> None:
    runs = [run_row(100 - i, "failure", 5 + i * 10) for i in range(60)]
    state = _state(FakeClient(runs=runs))
    assert state.consecutive_failures == 50
    assert state.last_green_sha is None
    assert state.red_since == NOW - timedelta(minutes=5 + 49 * 10)


def test_run_list_failure_falls_back_to_history() -> None:
    history = HistoryContext(
        last_success_at=NOW - timedelta(hours=3), last_success_sha="hist-green"
    )
    client = FakeClient(runs_error=GitHubAPIError("rate limited", status_code=403))
    state = _state(client, make_summary(run_id=100, history=history))
    assert state.source == "history"
    assert state.consecutive_failures is None
    assert state.last_green_sha == "hist-green"
    assert state.red_duration_hours == pytest.approx(3.0)


def test_no_run_list_and_no_history_is_none() -> None:
    summary = make_summary(run_id=100)
    summary.history = None
    state = _state(FakeClient(runs_error=RuntimeError("down")), summary)
    assert state.source == "none"
    assert state.consecutive_failures is None
    assert state.red_duration_hours is None
