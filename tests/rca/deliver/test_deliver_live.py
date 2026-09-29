"""Step 15 — `deliver --live` end to end against the in-memory fake GitHub (no network)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.rca.deliver._fake_github import Fault, FakeGitHub
from tools.rca import cli
from tools.rca.cli import COMMENT_BEGIN, COMMENT_END, main
from tools.rca.models import HistoryContext, Summary
from tools.rca.redact import redact_text

_ROOT = Path(__file__).resolve().parents[3]
_GOLDENS = _ROOT / "tools" / "eval" / "goldens"
REPO = "acme/widgets"
SHA = "c0ffee" + "0" * 34
_RULE_ID = re.compile(r"\bR\d+\b")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_OUTPUT", "GITHUB_REPOSITORY", "RCA_DEFAULT_BRANCH", "RCA_GITHUB_API_URL",
                 "GITHUB_API_URL", "GITHUB_SERVER_URL", "GH_HOST", "RCA_GITHUB_HOST", "RCA_SSL_VERIFY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    server = FakeGitHub()
    monkeypatch.setattr(cli, "_delivery_client", lambda _args: server.client())
    return server


def _stage(
    tmp_path: Path,
    *,
    golden: str = "artifactory-version-exists",
    event: str = "pull_request",
    branch: str = "feature/login",
    pr: int | None = 5,
    fp: str = "a" * 16,
    seen: int = 0,
    primary: str | None = None,
    name: str = "rca",
) -> Path:
    summary = Summary.model_validate_json((_GOLDENS / golden / "summary.json").read_text(encoding="utf-8"))
    summary.run.event = event
    summary.run.head_branch = branch
    summary.run.head_sha = SHA
    summary.run.pr_number = pr
    summary.fingerprint_coarse = fp
    summary.history = HistoryContext(seen_count=seen)
    if primary is not None:
        summary.failed_jobs[0].primary_failure_line = primary
    out = tmp_path / name
    out.mkdir(exist_ok=True)
    path = out / "summary.json"
    path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    return path


def _run(summary: Path, *flags: str) -> int:
    return main(["deliver", "--summary", str(summary), "--out", str(summary.parent), "--repo", REPO, *flags])


def _delivery(summary: Path) -> dict[str, Any]:
    return json.loads(summary.read_text(encoding="utf-8"))["delivery"]


def _preview(summary: Path) -> str:
    return (summary.parent / "delivery-preview.md").read_text(encoding="utf-8")


def _preview_body(summary: Path) -> str:
    return _preview(summary).split(COMMENT_BEGIN + "\n", 1)[1].split(COMMENT_END, 1)[0]


def _posts(fake: FakeGitHub) -> list:
    return [r for r in fake.requests if r.method == "POST"]


# ---- AC1 / AC2: sticky PR comment ---------------------------------------------------------


def test_pr_create_edit_unchanged(tmp_path, fake) -> None:  # delivery AC #1
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    first = _delivery(summary)
    assert first["dry_run"] is False
    assert first["delivered_to"] == ["pr_comment"]
    assert first["comment_url"] == fake.issue_comments[5][0]["html_url"]
    assert "LIVE" in _preview(summary) and "→ created" in _preview(summary)

    summary = _stage(tmp_path, seen=1)  # changed text: "Seen 2×"
    assert _run(summary, "--live") == 0
    assert "→ updated" in _preview(summary)

    assert _run(summary, "--live") == 0
    assert "→ unchanged" in _preview(summary)

    assert [(r.method, r.path) for r in fake.writes()] == [
        ("POST", f"/repos/{REPO}/issues/5/comments"),
        ("PATCH", f"/repos/{REPO}/issues/comments/{fake.issue_comments[5][0]['id']}"),
    ]
    assert len(fake.issue_comments[5]) == 1
    assert fake.issue_comments[5][0]["body"] == _preview_body(summary)
    assert _delivery(summary)["delivered_to"] == ["pr_comment"]


def test_different_fingerprint_creates_a_second_comment(tmp_path, fake) -> None:  # AC #2
    assert _run(_stage(tmp_path, fp="a" * 16), "--live") == 0
    first = dict(fake.issue_comments[5][0])
    assert _run(_stage(tmp_path, fp="b" * 16), "--live") == 0
    assert len(fake.issue_comments[5]) == 2
    assert fake.issue_comments[5][0] == first  # the older fingerprint's comment is untouched
    assert [r.method for r in fake.writes()] == ["POST", "POST"]


# ---- AC3 / AC4: commit comments -----------------------------------------------------------


def test_push_default_commit_comment_and_edit(tmp_path, fake) -> None:  # AC #3
    # Step 16: push_default now also opens its issue first (was "pending Step 16"),
    # so only the commit-comment writes are asserted here; issues are in test_deliver_issues.
    summary = _stage(tmp_path, event="push", branch="main", pr=None)
    assert _run(summary, "--live") == 0
    # Step 17 may add "issue:assigned" (the actor owns it); this test is about the comment.
    delivered = _delivery(summary)["delivered_to"]
    assert delivered[0] == "issue:created" and delivered[-1] == "commit_comment"
    assert len(fake.commit_comments[SHA]) == 1
    assert "would open an issue: yes" in _preview(summary)
    summary = _stage(tmp_path, event="push", branch="main", pr=None, seen=3)
    assert _run(summary, "--live") == 0
    commit_writes = [r for r in fake.writes() if "/commits/" in r.path or re.search(r"/comments/\d+$", r.path)]
    assert [(r.method, r.path.rsplit("/", 2)[-2]) for r in commit_writes] == [
        ("POST", SHA),
        ("PATCH", "comments"),
    ]
    assert fake.commit_comments[SHA][0]["body"] == _preview_body(summary)


def test_push_branch_needs_opt_in(tmp_path, fake) -> None:  # AC #5
    summary = _stage(tmp_path, event="push", branch="feature/x", pr=None)
    assert _run(summary, "--live") == 0
    assert fake.writes() == []
    assert _delivery(summary)["delivered_to"] == []
    assert _run(summary, "--live", "--comment-on-branch-push", "true") == 0
    assert [(r.method, r.path) for r in fake.writes()] == [
        ("POST", f"/repos/{REPO}/commits/{SHA}/comments")
    ]


# ---- AC5: flaky --------------------------------------------------------------------------


def test_flaky_pr_gets_label_not_comment(tmp_path, fake) -> None:  # AC #8
    summary = _stage(tmp_path, golden="flake-same-sha")
    assert _run(summary, "--live") == 0
    delivery = _delivery(summary)
    assert delivery["suppressed_by"] == "flaky"
    assert delivery["delivered_to"] == ["label:ci:flaky"]
    assert fake.issue_comments.get(5, []) == []
    assert fake.labels[5] == ["ci:flaky"]
    assert [(r.method, r.path, r.json) for r in fake.writes()] == [
        ("POST", f"/repos/{REPO}/issues/5/labels", {"labels": ["ci:flaky"]})
    ]


# ---- AC6: 403 --------------------------------------------------------------------------


def test_403_records_scope_and_delivery_error(tmp_path, fake) -> None:  # AC #22
    fake.faults.append(Fault("POST", r"/issues/5/comments$", status=403))
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    delivery = _delivery(summary)
    assert delivery["suppressed_by"] == "delivery_error"
    assert delivery["delivered_to"] == []
    assert any("pull-requests: write" in e for e in delivery["errors"])
    text = _preview(summary)
    assert "→ failed" in text and COMMENT_BEGIN in text
    assert len(_posts(fake)) == 1
    fake.faults.append(Fault("POST", r"/issues/5/comments$", status=403))
    assert _run(summary, "--live", "--strict") == 1


def test_lookup_failure_never_creates_blind(tmp_path, fake) -> None:
    fake.faults.append(Fault("GET", r"/issues/5/comments$", status=500, times=3))
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    assert fake.writes() == []
    delivery = _delivery(summary)
    assert delivery["suppressed_by"] == "delivery_error"
    assert any("could not be listed" in e for e in delivery["errors"])


# ---- AC7 / AC8 / AC9 through the CLI ---------------------------------------------------------


def test_create_timeout_that_landed_counts_once(tmp_path, fake) -> None:
    fake.faults.append(Fault("POST", r"/issues/5/comments$", lost_response=True))
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    assert _delivery(summary)["delivered_to"] == ["pr_comment"]
    assert len(_posts(fake)) == 1 and len(fake.issue_comments[5]) == 1


def test_blockquoted_marker_is_not_ours(tmp_path, fake) -> None:
    summary = _stage(tmp_path)
    assert _run(summary) == 0  # dry run to learn the exact marker/body
    body = _preview_body(summary)
    human = fake.seed_issue_comment(5, "> " + body.replace("\n", "\n> ") + "\nIs this right?")
    assert _run(summary, "--live") == 0
    assert [r.method for r in fake.writes()] == ["POST"]
    assert fake.issue_comments[5][0] == human  # the human comment is untouched
    assert fake.issue_comments[5][1]["body"] == body


def test_budget_exceeded_skips_write(tmp_path, fake, monkeypatch) -> None:
    ticks = iter([0.0] + [31.0] * 50)
    monkeypatch.setattr(cli, "_delivery_clock", lambda: next(ticks))
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    assert fake.writes() == []
    delivery = _delivery(summary)
    assert "delivery budget exceeded" in delivery["errors"]
    assert delivery["delivered_to"] == []


# ---- AC10: safety ------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [(), ("--dry-run",), ("--live", "--dry-run")])
def test_without_live_only_gets(tmp_path, fake, flags) -> None:  # AC #23
    fake.seed_issue_comment(5, "unrelated")
    summary = _stage(tmp_path)
    assert _run(summary, *flags) == 0
    assert fake.requests, "dry run still reads"
    assert {r.method for r in fake.requests} == {"GET"}
    delivery = _delivery(summary)
    assert delivery["dry_run"] is True and delivery["delivered_to"] == []
    assert "→ would create" in _preview(summary)


def test_dry_run_reports_would_edit_and_unchanged(tmp_path, fake) -> None:
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    assert _run(summary) == 0
    assert "→ unchanged" in _preview(summary)
    assert _run(_stage(tmp_path, seen=4)) == 0
    text = _preview(tmp_path / "rca" / "summary.json")
    assert "→ would edit" in text and fake.issue_comments[5][0]["html_url"] in text
    assert [r.method for r in fake.writes()] == ["POST"]


@pytest.mark.parametrize("flags", [("--offline",), ("--offline", "--live")])
def test_offline_makes_no_requests(tmp_path, fake, flags) -> None:
    summary = _stage(tmp_path)
    assert _run(summary, *flags) == 0
    assert fake.requests == []
    assert _delivery(summary)["dry_run"] is True


# ---- AC11: posted body == preview body, clean -------------------------------------------------


def test_posted_body_is_the_preview_body_and_clean(tmp_path, fake) -> None:  # AC #24, #25
    secret = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
    summary = _stage(tmp_path, primary=f"release already exists; token {secret} R19 leaked")
    assert _run(summary, "--live") == 0
    posted = _posts(fake)[0].json["body"]
    assert posted == _preview_body(summary)
    assert len(posted) <= 4000
    assert "ghp_" not in posted and redact_text(posted)[1] == 0
    assert not _RULE_ID.search(posted.split("\n", 1)[1])


def test_outputs_carry_url_and_channel(tmp_path, fake, monkeypatch) -> None:
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    values = dict(line.split("=", 1) for line in out_file.read_text(encoding="utf-8").splitlines())
    assert values["delivered-to"] == "pr_comment"
    assert values["comment-url"] == fake.issue_comments[5][0]["html_url"]
    assert values["suppressed-by"] == ""
