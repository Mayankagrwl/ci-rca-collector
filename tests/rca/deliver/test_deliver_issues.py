"""Step 16 — delivery-owned issues, index issue, IssuesHistoryStore, migration.

All against the in-memory fake GitHub (no network).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.rca.deliver._fake_github import Fault, FakeGitHub
from tools.rca import cli
from tools.rca.cli import main
from tools.rca.deliver.issues import (
    BODY_CAP,
    INDEX_TITLE,
    LABEL_FINGERPRINT,
    LABEL_INDEX,
    IssuesHistoryStore,
    issue_marker,
    marker_fingerprint,
    parse_record,
    parse_record_block,
    record_from_summary,
    render_index_body,
    render_issue_body,
)
from tools.rca.deliver.sticky import DeliveryBudget
from tools.rca.history import CacheHistoryStore
from tools.rca.models import FailureRecord, HistoryContext, Summary

_ROOT = Path(__file__).resolve().parents[3]
_GOLDENS = _ROOT / "tools" / "eval" / "goldens"
REPO = "acme/widgets"
SHA = "c0ffee" + "0" * 34
FINE = "f1e2d3c4b5a69788"
_RULE_ID = re.compile(r"\bR\d+\b")
NOW = datetime.now(timezone.utc)


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
    golden: str = "timeout",
    event: str = "push",
    branch: str = "main",
    pr: int | None = None,
    fork: bool = False,
    run_id: int = 1,
    fine: str = FINE,
    seen: int = 0,
    primary: str | None = None,
) -> Path:
    summary = Summary.model_validate_json((_GOLDENS / golden / "summary.json").read_text(encoding="utf-8"))
    summary.run.event = event
    summary.run.head_branch = branch
    summary.run.head_sha = SHA
    summary.run.pr_number = pr
    summary.run.is_fork = fork
    summary.run.run_id = run_id
    summary.fingerprint = fine
    summary.fingerprint_coarse = "c" * 16
    summary.history = HistoryContext(seen_count=seen)
    if primary is not None:
        summary.failed_jobs[0].primary_failure_line = primary
    out = tmp_path / "rca"
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


def _fp_issues(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [i for i in fake.issues.values() if i["title"] != INDEX_TITLE]


def _labels(issue: dict[str, Any]) -> list[str]:
    return [label["name"] for label in issue["labels"]]


def _index(fake: FakeGitHub) -> dict[str, int]:
    index = [i for i in fake.issues.values() if i["title"] == INDEX_TITLE]
    assert len(index) == 1
    assert _labels(index[0]) == [LABEL_INDEX]
    return parse_record_block(index[0]["body"])


def _record(fp: str = FINE, *, last_seen: datetime = NOW, run_ids=(1,), **extra) -> FailureRecord:
    return FailureRecord(
        fingerprint=fp,
        fingerprint_coarse=extra.pop("coarse", "c" * 16),
        first_seen=last_seen,
        last_seen=last_seen,
        branches=["main"],
        run_ids=list(run_ids),
        category=extra.pop("category", "timeout"),
        masking_config_hash="m" * 12,
        **extra,
    )


def _seed_fp_issue(fake: FakeGitHub, record: FailureRecord, *, state="open", number=None) -> dict[str, Any]:
    body, _ = render_issue_body(record)
    return fake.seed_issue(f"[RCA] {record.category}", body, labels=[LABEL_FINGERPRINT], state=state, number=number)


# ---- AC1 / AC2: create, then update + recurrence comment ----------------------------------------


def test_first_push_default_failure_opens_one_issue(tmp_path, fake) -> None:  # AC #3
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    issues = _fp_issues(fake)
    assert len(issues) == 1
    issue = issues[0]
    assert _labels(issue) == ["rca-fingerprint", "rca:timeout", "rca:infra"]
    assert issue["title"].startswith("[RCA] timed out: ")
    record = parse_record(issue["body"])
    assert record is not None and record.fingerprint == FINE and record.count == 1
    assert _index(fake) == {FINE: issue["number"]}
    comment = fake.commit_comments[SHA][0]["body"]
    assert f"tracked in #{issue['number']}" in comment
    delivery = _delivery(summary)
    assert delivery["issue_url"] == issue["html_url"]
    assert delivery["delivered_to"] == ["issue:created", "commit_comment"]
    assert "owner resolution is Step 17" in _preview(summary)
    assert fake.search_calls() == []


def test_recurrence_updates_and_comments_same_run_does_not_bump(tmp_path, fake) -> None:  # AC #7
    assert _run(_stage(tmp_path, run_id=1), "--live") == 0
    number = _fp_issues(fake)[0]["number"]
    summary = _stage(tmp_path, run_id=2, seen=1)
    assert _run(summary, "--live") == 0
    assert len(_fp_issues(fake)) == 1
    assert parse_record(fake.issues[number]["body"]).count == 2
    recurrences = fake.issue_comments_on(number)
    assert len(recurrences) == 1 and "Recurred in [run 2]" in recurrences[0] and SHA[:7] in recurrences[0]
    assert "issue:updated" in _delivery(summary)["delivered_to"]

    assert _run(summary, "--live") == 0  # same run_id again
    assert parse_record(fake.issues[number]["body"]).count == 2
    assert len(fake.issue_comments_on(number)) == 1
    assert fake.search_calls() == []


# ---- AC3–AC6: gating ------------------------------------------------------------------------


def test_schedule_opens_issue_without_commit_comment(tmp_path, fake) -> None:  # AC #13
    summary = _stage(tmp_path, event="schedule")
    assert _run(summary, "--live") == 0
    assert len(_fp_issues(fake)) == 1
    assert fake.commit_comments == {}
    assert _delivery(summary)["delivered_to"] == ["issue:created"]


def test_dispatch_opens_no_issue(tmp_path, fake) -> None:  # AC #14
    summary = _stage(tmp_path, event="workflow_dispatch", seen=99)
    assert _run(summary, "--live") == 0
    assert fake.issues == {}
    assert "issue: none (policy)" in _preview(summary)


def test_fork_pr_issue_only_with_opt_in(tmp_path, fake) -> None:  # AC #17
    summary = _stage(tmp_path, event="pull_request", branch="fork/feature", pr=9, fork=True, seen=5)
    assert _run(summary, "--live") == 0
    assert _fp_issues(fake) == []
    assert _run(summary, "--live", "--allow-fork-issues", "true") == 0
    assert len(_fp_issues(fake)) == 1


def test_pr_recurrence_threshold(tmp_path, fake) -> None:
    below = _stage(tmp_path, event="pull_request", branch="feature", pr=5, seen=2)
    assert _run(below, "--live") == 0
    assert _fp_issues(fake) == []
    at = _stage(tmp_path, event="pull_request", branch="feature", pr=5, seen=3)
    assert _run(at, "--live") == 0
    assert len(_fp_issues(fake)) == 1


def test_create_issues_false_and_flaky_open_nothing(tmp_path, fake) -> None:
    assert _run(_stage(tmp_path), "--live", "--create-issues", "false") == 0
    assert fake.issues == {}
    flaky = _stage(tmp_path, golden="flake-same-sha")
    assert _run(flaky, "--live") == 0
    assert fake.issues == {}
    assert "suppressed by flaky" in _preview(flaky)


# ---- AC7 / AC8 / AC9: lifecycle ---------------------------------------------------------------


def test_closed_issue_reopens_on_recurrence(tmp_path, fake) -> None:
    seeded = _seed_fp_issue(fake, _record(run_ids=(1,)), state="closed")
    summary = _stage(tmp_path, run_id=7)
    assert _run(summary, "--live") == 0
    issue = fake.issues[seeded["number"]]
    assert issue["state"] == "open"
    assert parse_record(issue["body"]).count == 2
    assert "issue:reopened" in _delivery(summary)["delivered_to"]
    assert len(_fp_issues(fake)) == 1


@pytest.mark.parametrize("index_body", [None, "garbage without a record", "<!-- rca-record -->\n```json\n{not json\n```\n"])
def test_missing_or_corrupt_index_falls_back_and_repairs(tmp_path, fake, index_body) -> None:  # AC #21
    seeded = _seed_fp_issue(fake, _record(run_ids=(1,)))
    other = _seed_fp_issue(fake, _record("0123456789abcdef", run_ids=(3,)))
    if index_body is not None:
        fake.seed_issue(INDEX_TITLE, index_body, labels=[LABEL_INDEX])
    summary = _stage(tmp_path, run_id=8)
    assert _run(summary, "--live") == 0
    assert len(_fp_issues(fake)) == 2  # found the existing one, no new issue
    assert parse_record(fake.issues[seeded["number"]]["body"]).count == 2
    assert _index(fake) == {FINE: seeded["number"], "0123456789abcdef": other["number"]}
    assert fake.search_calls() == []


def test_concurrent_duplicate_is_closed_and_index_points_to_lowest(tmp_path, fake) -> None:
    def race(server: FakeGitHub, req) -> None:
        # Another run creates the same fingerprint's issue right after ours (lower number).
        if req.method == "POST" and req.path.endswith("/issues") and req.json.get("title") != INDEX_TITLE:
            server.after_request.clear()
            _seed_fp_issue(server, _record(run_ids=(99,)), number=50)

    fake.after_request.append(race)
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    ours = max(i["number"] for i in _fp_issues(fake))
    assert fake.issues[ours]["state"] == "closed"
    assert fake.issue_comments_on(ours) == ["Duplicate of #50"]
    assert fake.issues[50]["state"] == "open"
    assert _index(fake) == {FINE: 50}
    delivery = _delivery(summary)
    assert "issue:duplicate-closed" in delivery["delivered_to"]
    assert delivery["issue_url"] == fake.issues[50]["html_url"]
    assert "tracked in #50" in fake.commit_comments[SHA][0]["body"]


# ---- AC10: body cap -------------------------------------------------------------------------


def test_oversized_record_stays_under_cap_and_parses() -> None:
    record = _record(templates=[f"template {i} " + "x" * 900 for i in range(200)], last_summary="y" * 5000)
    body, notes = render_issue_body(record, fix="z" * 2000)
    assert len(body) <= BODY_CAP
    parsed = parse_record(body)
    assert parsed is not None and parsed.fingerprint == FINE
    assert 0 < len(parsed.templates) < 200
    assert any("dropped" in n for n in notes)


def test_record_block_parse_is_strict() -> None:
    body, _ = render_issue_body(_record(templates=["has ``` backticks ```` inside"]))
    assert parse_record(body).templates == ["has ``` backticks ```` inside"]
    assert marker_fingerprint(body) == FINE
    assert parse_record("no marker here") is None
    assert parse_record("<!-- rca-record -->\n```json\n[1,2]\n```\n") is None
    assert parse_record(body.replace("<!-- rca-record -->", "")) is None
    quoted = "> " + body.replace("\n", "\n> ")
    assert marker_fingerprint(quoted) is None


# ---- AC11: migration ------------------------------------------------------------------------


def _cache(tmp_path: Path, *fps: str) -> Path:
    root = tmp_path / "hist"
    store = CacheHistoryStore(root)
    for i, fp in enumerate(fps):
        store.upsert(_record(fp, run_ids=(100 + i,), last_seen=NOW - timedelta(hours=i)))
    return root


def _migrate(tmp_path: Path, root: Path, *flags: str) -> int:
    return main(["deliver", "--migrate-history", "--history-dir", str(root), "--out", str(tmp_path / "rca"),
                 "--repo", REPO, *flags])


def test_migration_creates_only_new_and_is_idempotent(tmp_path, fake, monkeypatch) -> None:  # AC #20
    sleeps: list[float] = []
    monkeypatch.setattr(cli, "_migrate_sleep", sleeps.append)
    root = _cache(tmp_path, "aaaa000000000001", "aaaa000000000002", "aaaa000000000003")
    _seed_fp_issue(fake, _record("aaaa000000000002"))
    before = {p.name: p.read_bytes() for p in root.iterdir()}

    assert _migrate(tmp_path, root) == 0  # dry run
    assert fake.writes() == []
    report = (tmp_path / "rca" / "history-migration.md").read_text(encoding="utf-8")
    assert report.count("would create issue") == 2 and "already tracked" in report

    assert _migrate(tmp_path, root, "--live") == 0
    assert {marker_fingerprint(i["body"]) for i in _fp_issues(fake)} == {
        "aaaa000000000001", "aaaa000000000002", "aaaa000000000003"}
    assert sleeps == [1.0]  # paced between the two creates

    writes_before = len(fake.writes())
    assert _migrate(tmp_path, root, "--live") == 0
    assert [w for w in fake.writes()[writes_before:] if w.path.endswith("/issues")] == []
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before  # cache untouched
    assert fake.search_calls() == []


def test_migration_limit_then_continue(tmp_path, fake, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_migrate_sleep", lambda _s: None)
    root = _cache(tmp_path, "bbbb000000000001", "bbbb000000000002", "bbbb000000000003")
    assert _migrate(tmp_path, root, "--live", "--migrate-limit", "2") == 0
    assert len(_fp_issues(fake)) == 2
    assert "limit 2 reached" in (tmp_path / "rca" / "history-migration.md").read_text(encoding="utf-8")
    assert _migrate(tmp_path, root, "--live", "--migrate-limit", "2") == 0
    assert len(_fp_issues(fake)) == 3


def test_migration_missing_dir_is_not_created(tmp_path, fake) -> None:
    missing = tmp_path / "nope"
    assert _migrate(tmp_path, missing, "--live") == 0
    assert not missing.exists()
    assert fake.requests == []


# ---- AC12: 403 on issue create ------------------------------------------------------------------


def test_issue_403_does_not_block_the_comment(tmp_path, fake) -> None:
    fake.faults.append(Fault("POST", r"/repos/acme/widgets/issues$", status=403))
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    delivery = _delivery(summary)
    assert any("issues: write" in e for e in delivery["errors"])
    assert delivery["delivered_to"] == ["commit_comment"]
    assert delivery["suppressed_by"] is None
    assert "tracked in #" not in fake.commit_comments[SHA][0]["body"]


def test_failed_label_scan_never_creates_blind(tmp_path, fake) -> None:
    fake.faults.append(Fault("GET", r"/issues$", status=500, times=6))
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    assert [w for w in fake.writes() if w.path.endswith("/issues")] == []
    delivery = _delivery(summary)
    assert any("could not be listed" in e for e in delivery["errors"])
    assert delivery["delivered_to"] == ["commit_comment"]  # the comment still goes out


def test_schedule_issue_failure_is_a_delivery_error(tmp_path, fake) -> None:
    fake.faults.append(Fault("POST", r"/repos/acme/widgets/issues$", status=403))
    summary = _stage(tmp_path, event="schedule")
    assert _run(summary, "--live") == 0
    assert _delivery(summary)["suppressed_by"] == "delivery_error"


# ---- AC13: safety ------------------------------------------------------------------------------


def test_titles_bodies_comments_are_safe(tmp_path, fake) -> None:  # AC #25
    secret = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8"
    primary = f"release already exists; ping @alice token {secret} rule R8"
    assert _run(_stage(tmp_path, golden="artifactory-version-exists", primary=primary, run_id=1), "--live") == 0
    assert _run(_stage(tmp_path, golden="artifactory-version-exists", primary=primary, run_id=2,
                       branch="main"), "--live") == 0
    texts = [i["title"] for i in fake.issues.values()] + [i["body"] for i in fake.issues.values()]
    texts += [c["body"] for comments in fake.issue_comments.values() for c in comments]
    assert texts
    for text in texts:
        assert "ghp_" not in text
        assert "@alice" not in text
        assert not _RULE_ID.search(text), text[:200]
    assert any("@​alice" in t for t in texts)


# ---- AC14: stale sweep ----------------------------------------------------------------------------


def test_stale_sweep(tmp_path, fake) -> None:
    fresh = _seed_fp_issue(fake, _record("dddd000000000000", last_seen=NOW - timedelta(days=10)))
    resolved = _seed_fp_issue(
        fake, _record("eeee000000000000", last_seen=NOW - timedelta(days=30), resolution="fixed it")
    )
    stale = [
        _seed_fp_issue(fake, _record(f"5a1e{i:012d}", last_seen=NOW - timedelta(days=15 + i)))
        for i in range(7)
    ]
    assert _run(_stage(tmp_path), "--live") == 0
    closed = [i for i in stale if fake.issues[i["number"]]["state"] == "closed"]
    assert len(closed) == 5  # at most 5 per run
    for issue in closed:
        assert fake.issue_comments_on(issue["number"]) == [
            "No recurrence in 14 days — closing. Reopens automatically if it recurs."
        ]
    assert fake.issues[fresh["number"]]["state"] == "open"
    assert fake.issues[resolved["number"]]["state"] == "open"
    ours = [i for i in _fp_issues(fake) if marker_fingerprint(i["body"]) == FINE][0]
    assert ours["state"] == "open"


def test_dry_run_sweeps_nothing(tmp_path, fake) -> None:
    _seed_fp_issue(fake, _record("5a1e000000000000", last_seen=NOW - timedelta(days=40)))
    assert _run(_stage(tmp_path)) == 0
    assert fake.writes() == []


# ---- dry run / offline ------------------------------------------------------------------------------


def test_dry_run_previews_issue_actions_with_zero_writes(tmp_path, fake) -> None:
    summary = _stage(tmp_path)
    assert _run(summary) == 0
    assert "issue: would create issue" in _preview(summary)
    assert fake.writes() == []

    seeded = _seed_fp_issue(fake, _record(run_ids=(1,)))
    summary = _stage(tmp_path, run_id=2)
    assert _run(summary) == 0
    assert f"issue: would update issue #{seeded['number']} (count 1→2)" in _preview(summary)
    assert f"tracked in #{seeded['number']}" in _preview(summary)
    fake.issues[seeded["number"]]["state"] = "closed"
    assert _run(summary) == 0
    assert f"would reopen #{seeded['number']}" in _preview(summary)
    assert fake.writes() == []


def test_offline_makes_no_issue_requests(tmp_path, fake) -> None:
    summary = _stage(tmp_path)
    assert _run(summary, "--live", "--offline") == 0
    assert fake.requests == []
    assert "not looked up" in _preview(summary)


# ---- record builder ----------------------------------------------------------------------------------


def test_record_from_summary_uses_headline_not_template() -> None:
    summary = Summary.model_validate_json((_GOLDENS / "npm-eresolve" / "summary.json").read_text(encoding="utf-8"))
    record = record_from_summary(summary, "Package install failed — npm ERR! ERESOLVE", NOW)
    assert record.last_summary == "Package install failed — npm ERR! ERESOLVE"
    assert record.fingerprint == summary.fingerprint and record.fingerprint_coarse == summary.fingerprint_coarse
    assert record.run_ids == [summary.run.run_id] and record.branches == [summary.run.head_branch]
    assert record.first_seen == record.last_seen == NOW
    assert record.category == summary.classification.category
    if summary.drain is not None:
        assert record.templates == [t.template for t in summary.drain.templates]


def test_index_body_round_trips() -> None:
    body = render_index_body({"b": 2, "a": 1})
    assert parse_record_block(body) == {"a": 1, "b": 2}


# ---- AC15: protocol conformance ----------------------------------------------------------------


@pytest.fixture(params=["cache", "issues"])
def store(request, tmp_path, monkeypatch):
    if request.param == "cache":
        yield CacheHistoryStore(tmp_path / "hist")
        return
    fake = FakeGitHub()
    client = fake.client()
    yield IssuesHistoryStore(client, REPO, budget=DeliveryBudget(started_at=0.0), clock=lambda: 0.0)
    client.close()


def test_store_upsert_is_idempotent(store) -> None:
    rec = _record("abc123abc123abcd", coarse="def456def456def0", run_ids=(99,))
    store.upsert(rec)
    store.upsert(rec)
    loaded = store.get("abc123abc123abcd")
    assert loaded is not None
    assert loaded.count == 1
    assert loaded.run_ids == [99]
    store.close()


def test_store_absorbs_new_occurrences_and_finds_by_coarse(store) -> None:
    store.upsert(_record("abc123abc123abcd", coarse="def456def456def0", run_ids=(1,)))
    store.upsert(_record("abc123abc123abcd", coarse="def456def456def0", run_ids=(2,)))
    store.upsert(_record("ffff00000000ffff", coarse="def456def456def0", run_ids=(3,)))
    store.upsert(_record("9999000000009999", coarse="other0000000other", run_ids=(4,)))
    assert store.get("abc123abc123abcd").count == 2
    assert store.get("abc123abc123abcd").run_ids == [1, 2]
    assert store.get("missing000000000") is None
    assert sorted(r.fingerprint for r in store.find_by_coarse("def456def456def0")) == [
        "abc123abc123abcd", "ffff00000000ffff"]
    store.close()


def test_issue_marker_is_first_line() -> None:
    body, _ = render_issue_body(_record())
    assert body.splitlines()[0] == issue_marker(FINE)
