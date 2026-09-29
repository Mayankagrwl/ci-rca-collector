"""Step 13 AC1 — trigger precedence and default-branch fallbacks (v1.3 §4)."""

from __future__ import annotations

import pytest

from tests.rca.deliver._helpers import REPO, FakeClient, make_summary
from tools.rca.deliver.targets import resolve_context
from tools.rca.github_api import GitHubAPIError


def _ctx(summary, client=None, env=None):
    return resolve_context(summary, client=client or FakeClient(), repo=REPO, env=env or {})


@pytest.mark.parametrize(
    ("kwargs", "trigger"),
    [
        ({"event": "pull_request", "branch": "feature", "pr_number": 5}, "pull_request"),
        ({"event": "merge_group", "branch": "gh-readonly-queue/main/pr-5"}, "merge_group"),
        ({"event": "push", "branch": "main"}, "push_default"),
        ({"event": "push", "branch": "feature"}, "push_branch"),
        ({"event": "schedule", "branch": "main"}, "schedule"),
        ({"event": "workflow_dispatch", "branch": "main"}, "dispatch"),
        ({"event": "repository_dispatch", "branch": "main"}, "unknown"),
    ],
)
def test_each_trigger(kwargs, trigger) -> None:
    context, _notes = _ctx(make_summary(**kwargs))
    assert context.trigger == trigger


def test_pr_number_wins_over_event() -> None:
    # A workflow_run whose collector resolved a PR (incl. fork SHA fallback) is a PR,
    # even though the triggering event was a push to the default branch.
    context, _ = _ctx(make_summary(event="push", branch="main", pr_number=9))
    assert context.trigger == "pull_request"
    assert context.pr_number == 9


def test_default_branch_master_routes_push_default() -> None:  # delivery AC #4
    client = FakeClient(default_branch="master")
    context, notes = _ctx(make_summary(event="push", branch="master"), client)
    assert context.trigger == "push_default"
    assert context.default_branch == "master"
    assert context.is_default_branch is True
    assert notes == []
    # ...and a push to "main" in that repo is only a branch push.
    context, _ = _ctx(make_summary(event="push", branch="main"), client)
    assert context.trigger == "push_branch"


def test_tag_push_routes_as_tag() -> None:
    client = FakeClient(tags={"v1.2.0"})
    context, _ = _ctx(make_summary(event="push", branch="v1.2.0"), client)
    assert context.trigger == "tag"
    assert ("ref_is_tag", "v1.2.0") in client.calls


def test_tag_lookup_only_for_push() -> None:
    client = FakeClient(tags={"main"})
    context, _ = _ctx(make_summary(event="schedule", branch="main"), client)
    assert context.trigger == "schedule"
    assert not any(call[0] == "ref_is_tag" for call in client.calls)


def test_tag_lookup_failure_routes_push_branch_with_note() -> None:  # delivery AC #31
    client = FakeClient(tag_error=GitHubAPIError("boom", status_code=500))
    context, notes = _ctx(make_summary(event="push", branch="v1.2.0"), client)
    assert context.trigger == "push_branch"
    assert any("tag lookup" in note and "not a tag" in note for note in notes)


def test_get_repo_failure_falls_back_to_env_then_main() -> None:
    client = FakeClient(repo_error=GitHubAPIError("forbidden", status_code=403))
    summary = make_summary(event="push", branch="trunk")

    context, notes = _ctx(summary, client, env={"RCA_DEFAULT_BRANCH": "trunk"})
    assert context.default_branch == "trunk"
    assert context.trigger == "push_default"
    assert any("get_repo failed" in n for n in notes)
    assert any("RCA_DEFAULT_BRANCH" in n for n in notes)

    context, notes = _ctx(summary, client, env={})
    assert context.default_branch == "main"
    assert context.trigger == "push_branch"
    assert any("fell back to 'main'" in n for n in notes)


def test_get_repo_without_default_branch_is_a_fallback() -> None:
    context, notes = _ctx(make_summary(), FakeClient(default_branch=None))
    assert context.default_branch == "main"
    assert any("no default_branch" in n for n in notes)


def test_context_fields_come_from_summary() -> None:
    summary = make_summary(
        event="pull_request", branch="feature", pr_number=3, is_fork=True, draft=True
    )
    context, _ = _ctx(summary)
    assert context.pr_is_draft is True
    assert context.is_fork is True
    assert context.branch == "feature"
    assert context.is_default_branch is False
    assert context.actor == "octocat"
    assert context.commit_sha == summary.run.head_sha


def test_missing_changes_means_not_draft() -> None:
    summary = make_summary(event="pull_request", pr_number=3)
    summary.changes = None
    context, _ = _ctx(summary)
    assert context.pr_is_draft is False


def test_resolve_context_never_raises_on_unexpected_errors() -> None:
    client = FakeClient(
        repo_error=RuntimeError("unexpected"), tag_error=ValueError("also unexpected")
    )
    context, notes = _ctx(make_summary(event="push", branch="x"), client)
    assert context.trigger == "push_branch"
    assert len(notes) >= 2
    assert all("unexpected" not in n for n in notes)  # type names only, never messages
