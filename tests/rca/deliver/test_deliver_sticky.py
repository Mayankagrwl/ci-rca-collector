"""Step 15 — sticky finder + writer against the in-memory fake GitHub (no network)."""

from __future__ import annotations

import pytest

from tests.rca.deliver._fake_github import Fault, FakeGitHub
from tools.rca.deliver import ExistingComment
from tools.rca.deliver import commit_comment, pr_comment
from tools.rca.deliver.render_comment import marker_line
from tools.rca.deliver.sticky import (
    BUDGET_EXCEEDED,
    LOOKUP_FAILED,
    DeliveryBudget,
    find_existing,
    first_line,
    is_ours,
    post_or_update,
)

REPO = "acme/widgets"
FP = "a" * 16


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RCA_GITHUB_API_URL", "GITHUB_API_URL", "GITHUB_SERVER_URL", "GH_HOST",
                 "RCA_GITHUB_HOST", "RCA_SSL_VERIFY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


def _body(text: str = "details", fp: str = FP) -> str:
    return f"{marker_line(fp)}\n### CI failure — test failure\n\n**{text}**\n"


def _budget() -> DeliveryBudget:
    return DeliveryBudget(started_at=0.0)


def _clock(value: float = 1.0):
    return lambda: value


# ---- marker matching ------------------------------------------------------------


def test_marker_must_be_the_exact_first_line() -> None:
    assert is_ours(_body(), FP)
    assert is_ours("﻿  \n" + _body(), FP)  # BOM + surrounding whitespace stripped
    assert not is_ours("> " + _body(), FP)  # quoted in a blockquote
    assert not is_ours("Thanks!\n" + _body(), FP)  # marker not on the first line
    assert not is_ours(_body(fp="b" * 16), FP)  # other fingerprint
    assert first_line("﻿\n  x  \ny") == "x"


def test_find_ignores_quotes_and_author() -> None:
    fake = FakeGitHub()
    fake.seed_issue_comment(5, "> " + _body() + "\n\nI think this is wrong", login="human")
    with fake.client() as client:
        assert find_existing(
            client, REPO, target_kind="pr", target=5, fingerprint_coarse=FP, branch="f"
        ) == (None, [])
    ours = fake.seed_issue_comment(5, _body(), login="someone-else-entirely")
    with fake.client() as client:
        existing, notes = pr_comment.find_existing(client, REPO, 5, fingerprint_coarse=FP, branch="f")
    assert existing is not None and existing.comment_id == ours["id"]  # matched by marker, not author
    assert existing.body == ours["body"] and existing.html_url == ours["html_url"]
    assert notes == []


def test_find_paginates() -> None:
    fake = FakeGitHub(page_size=2)
    for i in range(5):
        fake.seed_issue_comment(5, f"chatter {i}")
    ours = fake.seed_issue_comment(5, _body())
    with fake.client() as client:
        existing, _ = find_existing(client, REPO, target_kind="pr", target=5, fingerprint_coarse=FP, branch="f")
    assert existing is not None and existing.comment_id == ours["id"]
    assert sum(1 for r in fake.requests if r.method == "GET") == 3


def test_duplicates_choose_newest_and_note_them() -> None:
    fake = FakeGitHub()
    old = fake.seed_issue_comment(5, _body("old"))
    new = fake.seed_issue_comment(5, _body("new"))
    with fake.client() as client:
        existing, notes = find_existing(client, REPO, target_kind="pr", target=5, fingerprint_coarse=FP, branch="f")
    assert existing.comment_id == new["id"]
    assert len(notes) == 1 and str(old["id"]) in notes[0] and "untouched" in notes[0]
    assert fake.writes() == []  # nothing deleted, nothing edited by a lookup


def test_lookup_error_is_a_note() -> None:
    fake = FakeGitHub()
    fake.faults.append(Fault("GET", r"/comments$", status=500, times=3))
    with fake.client() as client:
        existing, notes = find_existing(client, REPO, target_kind="commit", target="abc", fingerprint_coarse=FP, branch="main")
    assert existing is None
    assert notes and notes[0].startswith(LOOKUP_FAILED)


# ---- writes ------------------------------------------------------------------------


def test_create_edit_unchanged() -> None:
    fake = FakeGitHub()
    with fake.client() as client:
        first = pr_comment.post_or_update(client, REPO, 5, _body("v1"), None, clock=_clock(), budget=_budget())
        existing, _ = pr_comment.find_existing(client, REPO, 5, fingerprint_coarse=FP, branch="f")
        second = pr_comment.post_or_update(client, REPO, 5, _body("v2"), existing, clock=_clock(), budget=_budget())
        existing, _ = pr_comment.find_existing(client, REPO, 5, fingerprint_coarse=FP, branch="f")
        third = pr_comment.post_or_update(client, REPO, 5, _body("v2"), existing, clock=_clock(), budget=_budget())
    assert (first.action, second.action, third.action) == ("created", "updated", "unchanged")
    assert first.url == second.url == third.url
    assert [r.method for r in fake.writes()] == ["POST", "PATCH"]
    assert fake.issue_comments[5][0]["body"] == _body("v2")


def test_commit_comment_create_and_edit_paths() -> None:
    fake = FakeGitHub()
    sha = "f" * 40
    with fake.client() as client:
        created = commit_comment.post_or_update(client, REPO, sha, _body("a"), None, clock=_clock(), budget=_budget())
        existing, _ = commit_comment.find_existing(client, REPO, sha, fingerprint_coarse=FP, branch="main")
        updated = commit_comment.post_or_update(client, REPO, sha, _body("b"), existing, clock=_clock(), budget=_budget())
    assert (created.action, updated.action) == ("created", "updated")
    writes = fake.writes()
    assert (writes[0].method, writes[0].path) == ("POST", f"/repos/{REPO}/commits/{sha}/comments")
    assert (writes[1].method, writes[1].path) == ("PATCH", f"/repos/{REPO}/comments/{created.comment_id}")


def test_timeout_that_landed_is_created_with_one_post() -> None:  # AC 7
    fake = FakeGitHub()
    fake.faults.append(Fault("POST", r"/issues/5/comments$", lost_response=True))
    with fake.client() as client:
        result = post_or_update(client, REPO, 5, _body(), None, target_kind="pr", clock=_clock(), budget=_budget())
    assert result.action == "created"
    assert result.url and result.comment_id == fake.issue_comments[5][0]["id"]
    assert sum(1 for r in fake.requests if r.method == "POST") == 1


def test_timeout_that_did_not_land_fails_with_one_post() -> None:  # AC 7
    fake = FakeGitHub()
    fake.faults.append(Fault("POST", r"/issues/5/comments$", timeout=True))
    with fake.client() as client:
        result = post_or_update(client, REPO, 5, _body(), None, target_kind="pr", clock=_clock(), budget=_budget())
    assert result.action == "failed"
    assert "not retried" in (result.error or "")
    assert sum(1 for r in fake.requests if r.method == "POST") == 1
    assert fake.issue_comments.get(5, []) == []


def test_relist_ignores_a_different_body() -> None:
    fake = FakeGitHub()
    fake.seed_issue_comment(5, _body("someone else's earlier version"))
    fake.faults.append(Fault("POST", r"/issues/5/comments$", timeout=True))
    with fake.client() as client:
        result = post_or_update(client, REPO, 5, _body("ours"), None, target_kind="pr", clock=_clock(), budget=_budget())
    assert result.action == "failed"


@pytest.mark.parametrize(
    ("kind", "target", "path", "scope"),
    [
        ("pr", 5, r"/issues/5/comments$", "pull-requests: write"),
        ("commit", "abc", r"/commits/abc/comments$", "contents: write"),
    ],
)
def test_403_names_the_missing_scope(kind, target, path, scope) -> None:
    fake = FakeGitHub()
    fake.faults.append(Fault("POST", path, status=403))
    with fake.client() as client:
        result = post_or_update(client, REPO, target, _body(), None, target_kind=kind, clock=_clock(), budget=_budget())
    assert result.action == "failed"
    assert scope in (result.error or "")
    assert "test-token" not in (result.error or "")


def test_edit_failure_is_recorded() -> None:
    fake = FakeGitHub()
    seeded = fake.seed_issue_comment(5, _body("old"))
    fake.faults.append(Fault("PATCH", r"/issues/comments/\d+$", status=403))
    existing = ExistingComment(FP, "f", fake.start, seeded["id"], seeded["html_url"], seeded["body"])
    with fake.client() as client:
        result = post_or_update(client, REPO, 5, _body("new"), existing, target_kind="pr", clock=_clock(), budget=_budget())
    assert result.action == "failed" and "pull-requests: write" in result.error


def test_budget_exceeded_skips_the_write() -> None:  # AC 9
    fake = FakeGitHub()
    with fake.client() as client:
        result = post_or_update(
            client, REPO, 5, _body(), None, target_kind="pr",
            clock=_clock(31.0), budget=DeliveryBudget(started_at=0.0),
        )
    assert result.action == "skipped" and result.error == BUDGET_EXCEEDED
    assert fake.writes() == []


def test_unchanged_needs_no_budget() -> None:
    existing = ExistingComment(FP, "f", FakeGitHub().start, 1, "https://x/1", _body())
    result = post_or_update(None, REPO, 5, _body(), existing, target_kind="pr", clock=_clock(99.0), budget=_budget())
    assert (result.action, result.url) == ("unchanged", "https://x/1")


def test_step13_positional_existing_still_works() -> None:
    legacy = ExistingComment(FP, "main", FakeGitHub().start)
    assert (legacy.comment_id, legacy.html_url, legacy.body) == (None, None, "")
