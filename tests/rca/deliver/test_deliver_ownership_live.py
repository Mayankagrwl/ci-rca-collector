"""Step 17 AC7–AC11 + Part 0 (F1–F3) — ownership wiring against the fake GitHub (no network)."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from tests.rca.deliver._fake_github import Fault, FakeGitHub
from tests.rca.deliver.test_deliver_issues import (
    FINE,
    NOW,
    SHA,
    _delivery,
    _fp_issues,
    _preview,
    _record,
    _run,
    _seed_fp_issue,
    _stage,
)
from tools.rca import cli
from tools.rca.cli import COMMENT_BEGIN, COMMENT_END
from tools.rca.deliver.issues import INDEX_TITLE, parse_record, render_issue_body

CODEOWNERS = "* @org/everyone\n/src/ @alice @org/backend backend@example.com\n"
_RULE_ID = re.compile(r"\bR\d+\b")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_OUTPUT", "GITHUB_REPOSITORY", "RCA_DEFAULT_BRANCH", "RCA_GITHUB_API_URL",
                 "GITHUB_API_URL", "GITHUB_SERVER_URL", "GH_HOST", "RCA_GITHUB_HOST",
                 "RCA_SSL_VERIFY", "RCA_WORKSPACE", "GITHUB_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    server = FakeGitHub()
    monkeypatch.setattr(cli, "_delivery_client", lambda _args: server.client())
    return server


def _java(tmp_path: Path, **kwargs) -> Path:
    return _stage(tmp_path, golden="java-compile-in-pipeline", **kwargs)


def _content_gets(fake: FakeGitHub) -> list:
    return [r for r in fake.requests if "/contents/" in r.path]


def _preview_body(summary: Path) -> str:
    return _preview(summary).split(COMMENT_BEGIN + "\n", 1)[1].split(COMMENT_END, 1)[0]


# ---- AC7: CODEOWNERS lookup ---------------------------------------------------------------------


def test_github_dir_beats_root_and_reads_default_branch(tmp_path, fake) -> None:
    fake.default_branch = "master"
    fake.files[(".github/CODEOWNERS", "master")] = CODEOWNERS
    fake.files[("CODEOWNERS", "master")] = "* @root-owner\n"
    summary = _java(tmp_path, branch="master")
    assert _run(summary) == 0
    gets = _content_gets(fake)
    assert [(r.path.split("/contents/")[1], r.query) for r in gets] == [(".github/CODEOWNERS", "ref=master")]
    delivery = _delivery(summary)
    assert delivery["owners"] == ["@alice", "@org/backend", "backend@example.com"]
    assert delivery["owner_resolved_by"] == "codeowners_suspected"


def test_lookup_order_and_at_most_three_gets(tmp_path, fake) -> None:
    fake.files[("docs/CODEOWNERS", "main")] = "* @docs-owner\n"
    summary = _java(tmp_path)
    assert _run(summary) == 0
    assert [r.path.split("/contents/")[1] for r in _content_gets(fake)] == [
        ".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]
    assert _delivery(summary)["owners"] == ["@docs-owner"]
    fake.requests.clear()
    fake.files.clear()
    assert _run(summary) == 0
    assert len(_content_gets(fake)) == 3
    assert "no CODEOWNERS at main" in _preview(summary)


def test_empty_codeowners_still_wins(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = ""
    fake.files[("CODEOWNERS", "main")] = "* @root\n"
    summary = _java(tmp_path)
    assert _run(summary) == 0
    assert len(_content_gets(fake)) == 1
    assert _delivery(summary)["owner_resolved_by"] == "actor"


def test_pr_head_ref_is_never_used(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    fake.files[(".github/CODEOWNERS", "evil-fork-branch")] = "* @attacker\n"
    summary = _java(tmp_path, event="pull_request", branch="evil-fork-branch", pr=4, fork=True, seen=9)
    assert _run(summary) == 0
    assert all(r.query == "ref=main" for r in _content_gets(fake))
    assert "@attacker" not in _delivery(summary)["owners"]


def test_offline_makes_zero_requests(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    summary = _java(tmp_path)
    assert _run(summary, "--offline") == 0
    assert fake.requests == []
    delivery = _delivery(summary)
    assert delivery["owner_resolved_by"] == "actor"
    assert "CODEOWNERS not read" in _preview(summary)


def test_oversized_codeowners_is_ignored(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = "* @x\n" + "#" * (3 * 1024 * 1024 + 1)
    summary = _java(tmp_path)
    assert _run(summary) == 0
    assert "over 3 MB" in _preview(summary)
    assert _delivery(summary)["owner_resolved_by"] == "actor"


def test_workspace_env_strips_the_runner_prefix(tmp_path, fake, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", "/work")
    fake.files[(".github/CODEOWNERS", "main")] = "/src/test/ @tests-owner\n"
    summary = _java(tmp_path)
    assert _run(summary) == 0
    assert _delivery(summary)["owners"] == ["@tests-owner"]


# ---- AC8: assignees ----------------------------------------------------------------------------


def test_create_then_assign_users_only(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    summary = _java(tmp_path)
    assert _run(summary, "--live") == 0
    issue = _fp_issues(fake)[0]
    create = [r for r in fake.writes() if r.path.endswith("/issues") and r.json.get("title") != INDEX_TITLE][0]
    assert "assignees" not in create.json  # never passed to create
    assign = [r for r in fake.writes() if r.path.endswith("/assignees")]
    assert [(r.path, r.json) for r in assign] == [
        (f"/repos/acme/widgets/issues/{issue['number']}/assignees", {"assignees": ["alice"]})]
    assert issue["assignees"] == [{"login": "alice"}]
    assert "issue:assigned" in _delivery(summary)["delivered_to"]


def test_assign_422_is_a_note_not_an_error(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    fake.faults.append(Fault("POST", r"/assignees$", status=422))
    summary = _java(tmp_path)
    assert _run(summary, "--live") == 0
    delivery = _delivery(summary)
    assert len(_fp_issues(fake)) == 1
    assert any("could not assign" in n for n in delivery["notes"])
    assert not any("assign" in e for e in delivery["errors"])
    assert "issue:assigned" not in delivery["delivered_to"]
    assert delivery["suppressed_by"] is None
    assert "commit_comment" in delivery["delivered_to"]


def test_existing_assignees_are_left_alone(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    seeded = _seed_fp_issue(fake, _record(FINE, run_ids=(1,), category="compile"))
    seeded["assignees"] = [{"login": "a-human"}]
    assert _run(_java(tmp_path, run_id=2), "--live") == 0
    assert [r for r in fake.writes() if r.path.endswith("/assignees")] == []
    assert fake.issues[seeded["number"]]["assignees"] == [{"login": "a-human"}]


def test_unassigned_existing_issue_gets_assigned_on_update(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    seeded = _seed_fp_issue(fake, _record(FINE, run_ids=(1,), category="compile"))
    assert _run(_java(tmp_path, run_id=2), "--live") == 0
    assert fake.issues[seeded["number"]]["assignees"] == [{"login": "alice"}]


def test_dry_run_makes_no_assign_calls(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    summary = _java(tmp_path)
    assert _run(summary) == 0
    assert fake.writes() == []
    assert _content_gets(fake)  # reads are allowed in dry run


# ---- AC9: report, preview, comment bodies ----------------------------------------------------------


def test_report_and_preview_owner_line(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    summary = _java(tmp_path)
    assert _run(summary) == 0
    assert "- owner: @alice, @org/backend, backend@example.com (resolved by codeowners_suspected)" in _preview(summary)
    infra = _stage(tmp_path, golden="timeout")
    assert _run(infra, "--platform-team", "@org/platform") == 0
    assert "- owner: @org/platform (resolved by platform_team)" in _preview(infra)
    assert _delivery(infra)["owners"] == ["@org/platform"]


def test_owners_never_change_the_comment_body(tmp_path, fake) -> None:
    summary = _java(tmp_path)
    assert _run(summary) == 0
    without = _preview_body(summary)
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    assert _run(summary) == 0
    assert _preview_body(summary) == without
    assert "Owner" not in without and "@alice" not in without


# ---- AC10: Part 0 --------------------------------------------------------------------------------


def test_f1_comment_is_posted_before_housekeeping_spends_the_budget(tmp_path, fake, monkeypatch) -> None:
    state = {"commented": False}

    def watch(server, req) -> None:
        if req.method == "POST" and "/commits/" in req.path:
            state["commented"] = True

    fake.after_request.append(watch)
    monkeypatch.setattr(cli, "_delivery_clock", lambda: 31.0 if state["commented"] else 0.0)
    for i in range(3):
        _seed_fp_issue(fake, _record(f"5a1e{i:012d}", last_seen=NOW - timedelta(days=20)))
    summary = _stage(tmp_path, golden="timeout")
    assert _run(summary, "--live") == 0
    delivery = _delivery(summary)
    assert "commit_comment" in delivery["delivered_to"]
    assert any("delivery budget exceeded" in e for e in delivery["errors"])  # the sweep ran out
    assert all(i["state"] == "open" for i in fake.issues.values() if i["title"] != INDEX_TITLE)
    comment_at = next(i for i, r in enumerate(fake.requests) if r.method == "POST" and "/commits/" in r.path)
    last_scan = max(i for i, r in enumerate(fake.requests) if "labels=rca-fingerprint" in r.query)
    assert last_scan < comment_at or state["commented"]  # no sweep write before the comment
    assert not [r for r in fake.requests[:comment_at] if r.method == "PATCH" and "/issues/" in r.path]


def test_f2_duplicate_close_records_occurrence_on_kept_issue(tmp_path, fake) -> None:
    def race(server, req) -> None:
        if req.method == "POST" and req.path.endswith("/issues") and req.json.get("title") != INDEX_TITLE:
            server.after_request.clear()
            _seed_fp_issue(server, _record(FINE, run_ids=(99,)), number=50)

    fake.after_request.append(race)
    summary = _stage(tmp_path, golden="timeout", run_id=7)
    assert _run(summary, "--live") == 0
    kept = parse_record(fake.issues[50]["body"])
    assert kept.count == 2 and kept.run_ids == [99, 7]
    assert any(c.startswith("Recurred in [run 7]") for c in fake.issue_comments_on(50))
    delivered = _delivery(summary)["delivered_to"]
    assert "issue:duplicate-closed" in delivered and "issue:updated" in delivered


def test_f3_fix_is_its_own_paragraph() -> None:
    body, _ = render_issue_body(_record(), fix="Raise the limit.")
    assert "\n\n**Suggested fix** — Raise the limit.\n\nFirst seen " in body


# ---- AC11: safety + issue body owner line -------------------------------------------------------------


def test_issue_owner_line_is_code_spans_and_pings_nobody(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = "/src/ @alice @R2 @org/backend dev@example.com\n"
    summary = _java(tmp_path)
    assert _run(summary, "--live") == 0
    body = _fp_issues(fake)[0]["body"]
    owner = [line for line in body.splitlines() if line.startswith("Owner: ")]
    assert len(owner) == 1
    assert re.fullmatch(r"Owner: `[^`]+`(, `[^`]+`)*", owner[0])
    assert "@alice" not in body and "@org/backend" not in body  # defused inside the spans
    assert not _RULE_ID.search(body)
    assert "ghp_" not in body


def test_no_owner_line_without_owners() -> None:
    body, _ = render_issue_body(_record(), owners=[])
    assert "Owner:" not in body
    body, _ = render_issue_body(_record(), owners=["@a"])
    assert "Owner: `@​a`" in body


def test_report_owners_field_is_optional_for_old_files() -> None:
    from tools.rca.models import DeliveryReport

    old = DeliveryReport.model_validate({"trigger": "x", "severity": "low"})
    assert old.owners == [] and old.owner_resolved_by is None


def test_write_back_owner_fields(tmp_path, fake) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    summary = _java(tmp_path)
    assert _run(summary) == 0
    delivery = json.loads(summary.read_text(encoding="utf-8"))["delivery"]
    assert delivery["owner_resolved_by"] == "codeowners_suspected"
    assert delivery["owners"][0] == "@alice"
