"""Step 18b — `/resolved` parser + `feedback` subcommand, recurrence after resolution,
reactions rollup, and the workflow stub. Offline against the fake GitHub.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.rca.deliver._fake_github import FakeGitHub
from tests.rca.deliver.test_deliver_issues import FINE, SHA, _record, _seed_fp_issue, _stage
from tools.rca import cli
from tools.rca.cli import main
from tools.rca.deliver.feedback import allowed_associations, is_bot, parse_resolved, why_not_resolved
from tools.rca.deliver.issues import IssuesHistoryStore, parse_record
from tools.rca.deliver.render_comment import marker_line
from tools.rca.deliver.sticky import DeliveryBudget

_ROOT = Path(__file__).resolve().parents[3]
REPO = "acme/widgets"
COARSE = "c" * 16
NOW = datetime.now(timezone.utc)
_RULE_ID = re.compile(r"\bR\d+\b")
SECRET = "ghp_" + "P0o9I8u7Y6t5R4e3W2q1A2s3D4f5G6h7J8k9"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_OUTPUT", "GITHUB_REPOSITORY", "RCA_DEFAULT_BRANCH", "RCA_GITHUB_API_URL",
                 "GITHUB_API_URL", "GITHUB_SERVER_URL", "GH_HOST", "RCA_GITHUB_HOST", "RCA_SSL_VERIFY",
                 "RCA_RESOLVE_ASSOCIATIONS", "RCA_CHAT_WEBHOOK_URL", "RCA_SMTP_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


@pytest.fixture
def fake(monkeypatch) -> FakeGitHub:
    server = FakeGitHub()
    monkeypatch.setattr(cli, "_delivery_client", lambda _args: server.client())
    return server


def _event(
    tmp_path: Path,
    *,
    body: str,
    issue: dict[str, Any],
    comment_id: int,
    login: str = "alice",
    association: str = "MEMBER",
    user_type: str = "User",
    action: str = "created",
) -> Path:
    payload = {
        "action": action,
        "comment": {"id": comment_id, "body": body, "author_association": association,
                    "user": {"login": login, "type": user_type}},
        "issue": issue,
        "repository": {"full_name": REPO},
    }
    path = tmp_path / f"event-{comment_id}-{len(list(tmp_path.glob('event-*')))}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _feedback(tmp_path: Path, event: Path, *flags: str) -> int:
    return main(["feedback", "--event", str(event), "--out", str(tmp_path / "rca"), *flags])


def _report(tmp_path: Path) -> str:
    return (tmp_path / "rca" / "feedback-report.md").read_text(encoding="utf-8")


def _fp_issue(fake: FakeGitHub, *, fp: str = FINE, coarse: str = COARSE, state: str = "open") -> dict:
    return _seed_fp_issue(fake, _record(fp, coarse=coarse, run_ids=(1,)), state=state)


def _pr_issue(number: int = 5) -> dict:
    return {"number": number, "body": "PR description", "pull_request": {"url": "x"}}


def _issue_patches(fake: FakeGitHub, number: int) -> list:
    return [r for r in fake.requests if r.method == "PATCH" and r.path.endswith(f"/issues/{number}")]


# ---- AC1: parser ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "target", "text"),
    [
        ("/resolved bumped base image", None, "bumped base image"),
        ("/resolved #42 fixed flaky port", 42, "fixed flaky port"),
        ("\n\n/resolved  multi\nline   text", None, "multi line text"),
    ],
)
def test_parser_accepts(body, target, text) -> None:
    command = parse_resolved(body)
    assert command is not None and (command.target_issue, command.text) == (target, text)


@pytest.mark.parametrize(
    "body",
    ["> /resolved x", "```\n/resolved x\n```", "    /resolved x", "/resolvedx y", "/resolved",
     "/resolved   ", "/resolved #7", "thanks!\n/resolved x", "/Resolved x", "", None],
)
def test_parser_rejects(body) -> None:
    assert parse_resolved(body) is None
    assert why_not_resolved(body)


def test_parser_makes_text_safe() -> None:
    command = parse_resolved(f"/resolved ping @alice with {SECRET} per R8 " + "x" * 600)
    assert "ghp_" not in command.text and "@alice" not in command.text
    assert not _RULE_ID.search(command.text)
    assert len(command.text) <= 300


def test_associations_and_bots() -> None:
    assert allowed_associations({}) == {"OWNER", "MEMBER", "COLLABORATOR"}
    assert allowed_associations({"RCA_RESOLVE_ASSOCIATIONS": "owner, member"}) == {"OWNER", "MEMBER"}
    assert is_bot({"login": "dependabot[bot]"}) and is_bot({"login": "x", "type": "Bot"})
    assert not is_bot({"login": "alice", "type": "User"})


# ---- AC2: on a fingerprint issue --------------------------------------------------------------------


def test_resolved_on_fingerprint_issue(tmp_path, fake) -> None:
    issue = _fp_issue(fake)
    comment = fake.seed_issue_comment(issue["number"], "/resolved bumped the base image")
    event = _event(tmp_path, body=comment["body"], issue=dict(issue), comment_id=comment["id"])
    assert _feedback(tmp_path, event, "--live") == 0
    fresh = fake.issues[issue["number"]]
    record = parse_record(fresh["body"])
    assert record.human_verified and record.resolution == "bumped the base image"
    assert record.resolution_author == "alice" and record.resolution_run_id is None
    assert (fresh["state"], fresh["state_reason"]) == ("closed", "completed")
    assert "Resolved by `@​alice`: bumped the base image." in fresh["body"]
    assert fake.reactions == [(comment["id"], "+1")]
    assert f"#{issue['number']}: resolved" in _report(tmp_path)

    before = len(_issue_patches(fake, issue["number"]))
    assert _feedback(tmp_path, event, "--live") == 0  # re-delivered event
    assert len(_issue_patches(fake, issue["number"])) == before  # no second body write
    assert fake.reactions == [(comment["id"], "+1")] * 2  # reaction still attempted
    assert "unchanged" in _report(tmp_path)


def test_hash_target_ignored_on_fingerprint_issue(tmp_path, fake) -> None:
    issue = _fp_issue(fake)
    comment = fake.seed_issue_comment(issue["number"], "/resolved #999 did the thing")
    event = _event(tmp_path, body=comment["body"], issue=dict(issue), comment_id=comment["id"])
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.issues[issue["number"]]["state"] == "closed"
    assert "#999 ignored" in _report(tmp_path)


# ---- AC3: on a pull request ------------------------------------------------------------------------------


def _sticky(fake: FakeGitHub, pr: int, coarse: str) -> dict:
    return fake.seed_issue_comment(pr, f"{marker_line(coarse)}\n### CI failure — x\n", login="github-actions[bot]")


def test_resolved_on_pr_uses_the_sticky_fingerprint(tmp_path, fake) -> None:
    issue = _fp_issue(fake)
    other = _fp_issue(fake, fp="0" * 16, coarse="d" * 16)
    _sticky(fake, 5, COARSE)
    comment = fake.seed_issue_comment(5, "/resolved pinned the dependency")
    event = _event(tmp_path, body=comment["body"], issue=_pr_issue(5), comment_id=comment["id"])
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.issues[issue["number"]]["state"] == "closed"
    assert fake.issues[other["number"]]["state"] == "open"
    assert fake.reactions == [(comment["id"], "+1")]


def test_two_sticky_comments_use_the_most_recent(tmp_path, fake) -> None:
    older = _fp_issue(fake)
    newer = _fp_issue(fake, fp="0" * 16, coarse="d" * 16)
    _sticky(fake, 5, COARSE)
    _sticky(fake, 5, "d" * 16)  # stamped later
    comment = fake.seed_issue_comment(5, "/resolved fixed")
    event = _event(tmp_path, body=comment["body"], issue=_pr_issue(5), comment_id=comment["id"])
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.issues[newer["number"]]["state"] == "closed"
    assert fake.issues[older["number"]]["state"] == "open"
    assert "2 RCA comments on #5; used the most recently updated" in _report(tmp_path)


def test_hash_target_must_be_an_rca_issue(tmp_path, fake) -> None:
    plain = fake.seed_issue("a normal bug", "nothing to see", labels=["bug"])
    rca = _fp_issue(fake)
    comment = fake.seed_issue_comment(5, f"/resolved #{plain['number']} fixed")
    event = _event(tmp_path, body=comment["body"], issue=_pr_issue(5), comment_id=comment["id"])
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.writes() == []
    assert "is not an RCA fingerprint issue" in _report(tmp_path)
    comment = fake.seed_issue_comment(5, f"/resolved #{rca['number']} fixed for real")
    event = _event(tmp_path, body=comment["body"], issue=_pr_issue(5), comment_id=comment["id"])
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.issues[rca["number"]]["state"] == "closed"


def test_plain_issue_is_skipped(tmp_path, fake) -> None:
    plain = fake.seed_issue("a normal bug", "nothing", labels=["bug"])
    comment = fake.seed_issue_comment(plain["number"], "/resolved done")
    event = _event(tmp_path, body=comment["body"], issue=dict(plain), comment_id=comment["id"])
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.writes() == []
    assert "neither an RCA fingerprint issue nor a pull request" in _report(tmp_path)


# ---- AC4: authorisation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("association", "login", "user_type", "reason"),
    [
        ("CONTRIBUTOR", "alice", "User", "author association CONTRIBUTOR"),
        ("NONE", "alice", "User", "author association NONE"),
        ("OWNER", "rca-bot[bot]", "Bot", "author is a bot"),
    ],
)
def test_untrusted_authors_make_no_writes(tmp_path, fake, association, login, user_type, reason) -> None:
    issue = _fp_issue(fake)
    comment = fake.seed_issue_comment(issue["number"], "/resolved nope")
    event = _event(tmp_path, body=comment["body"], issue=dict(issue), comment_id=comment["id"],
                   association=association, login=login, user_type=user_type)
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.writes() == [] and fake.requests == []
    assert reason in _report(tmp_path)


def test_association_env_narrows_the_set(tmp_path, fake, monkeypatch) -> None:
    monkeypatch.setenv("RCA_RESOLVE_ASSOCIATIONS", "OWNER")
    issue = _fp_issue(fake)
    comment = fake.seed_issue_comment(issue["number"], "/resolved nope")
    member = _event(tmp_path, body=comment["body"], issue=dict(issue), comment_id=comment["id"])
    assert _feedback(tmp_path, member, "--live") == 0
    assert fake.writes() == []
    owner = _event(tmp_path, body=comment["body"], issue=dict(issue), comment_id=comment["id"], association="OWNER")
    assert _feedback(tmp_path, owner, "--live") == 0
    assert fake.issues[issue["number"]]["state"] == "closed"


def test_non_created_action_is_skipped(tmp_path, fake) -> None:
    issue = _fp_issue(fake)
    event = _event(tmp_path, body="/resolved x", issue=dict(issue), comment_id=1, action="edited")
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.requests == []


# ---- AC5: recurrence after resolution --------------------------------------------------------------------


def test_resolved_issue_reopens_with_history_line(tmp_path, fake) -> None:
    issue = _fp_issue(fake)
    comment = fake.seed_issue_comment(issue["number"], "/resolved bumped the image")
    assert _feedback(tmp_path, _event(tmp_path, body=comment["body"], issue=dict(issue),
                                      comment_id=comment["id"]), "--live") == 0
    summary = _stage(tmp_path, golden="timeout", run_id=7)  # the same fingerprint fails again
    assert main(["deliver", "--summary", str(summary), "--out", str(summary.parent), "--repo", REPO,
                 "--live"]) == 0
    fresh = fake.issues[issue["number"]]
    assert fresh["state"] == "open"
    assert "Previously resolved by `@​alice`: bumped the image — recurred since." in fresh["body"]
    record = parse_record(fresh["body"])
    assert record.resolution == "bumped the image" and record.count == 2


def test_sweep_closes_open_resolved_issue_but_never_touches_closed_ones() -> None:
    fake = FakeGitHub()
    stale = _record("5" * 16, last_seen=NOW - timedelta(days=15), resolution="old fix",
                    resolution_author="bob")
    open_resolved = _seed_fp_issue(fake, stale, state="open")
    closed_resolved = _seed_fp_issue(fake, stale.model_copy(update={"fingerprint": "6" * 16}), state="closed")
    with fake.client() as client:
        store = IssuesHistoryStore(client, REPO, budget=DeliveryBudget(started_at=0.0), clock=lambda: 0.0)
        assert store.sweep_stale(NOW) == [open_resolved["number"]]
    assert fake.issues[open_resolved["number"]]["state"] == "closed"
    assert fake.writes()[-1].path.endswith(f"/issues/{open_resolved['number']}")
    assert not [r for r in fake.writes() if r.path.endswith(f"/issues/{closed_resolved['number']}")]


# ---- AC6: reactions rollup -------------------------------------------------------------------------------


def test_reactions_rollup_in_preview_without_extra_calls(tmp_path, fake) -> None:
    summary = _stage(tmp_path, golden="timeout")
    main(["deliver", "--summary", str(summary), "--out", str(summary.parent), "--repo", REPO])
    body_first = json.loads(summary.read_text(encoding="utf-8"))
    assert body_first["delivery"]["feedback"] == {}
    sticky = fake.seed_commit_comment(SHA, f"{marker_line(COARSE)}\nold body\n")
    sticky["reactions"] = {"+1": 3, "-1": 1, "laugh": 2, "total_count": 6}
    fake.requests.clear()
    main(["deliver", "--summary", str(summary), "--out", str(summary.parent), "--repo", REPO])
    preview = (summary.parent / "delivery-preview.md").read_text(encoding="utf-8")
    assert "- feedback: 👍 3 · 👎 1" in preview
    assert json.loads(summary.read_text(encoding="utf-8"))["delivery"]["feedback"] == {"up": 3, "down": 1}
    assert not [r for r in fake.requests if "reactions" in r.path]


# ---- AC8: safety -------------------------------------------------------------------------------------------


def test_report_and_body_are_safe(tmp_path, fake) -> None:
    issue = _fp_issue(fake)
    text = f"/resolved ping @everyone token {SECRET} rule R14 <script>x</script>"
    comment = fake.seed_issue_comment(issue["number"], text)
    assert _feedback(tmp_path, _event(tmp_path, body=text, issue=dict(issue),
                                      comment_id=comment["id"]), "--live") == 0
    for out in (_report(tmp_path), fake.issues[issue["number"]]["body"]):
        assert "ghp_" not in out
        assert not _RULE_ID.search(out), out
        assert "@everyone" not in out and "@alice" not in out


def test_stub_never_passes_comment_text_to_argv_or_env() -> None:
    raw = (_ROOT / "docs" / "examples" / "rca-feedback.yml").read_text(encoding="utf-8")
    stub = yaml.safe_load(raw)
    trigger = stub.get("on") or stub.get(True)
    assert trigger == {"issue_comment": {"types": ["created"]}}
    assert stub["permissions"] == {"contents": "read", "issues": "write", "pull-requests": "write"}
    for step in stub["jobs"]["resolved"]["steps"]:
        for field in ("run", "env", "with"):
            assert "github.event.comment" not in json.dumps(step.get(field, "")), step
    assert "--event \"$GITHUB_EVENT_PATH\"" in raw
    assert (_ROOT / "docs" / "rca-feedback.md").exists()


# ---- AC9: modes ---------------------------------------------------------------------------------------


def test_dry_run_reads_only_and_offline_makes_no_calls(tmp_path, fake) -> None:
    issue = _fp_issue(fake)
    comment = fake.seed_issue_comment(issue["number"], "/resolved fixed it")
    event = _event(tmp_path, body=comment["body"], issue=dict(issue), comment_id=comment["id"])
    assert _feedback(tmp_path, event) == 0
    assert fake.writes() == [] and fake.requests  # reads happened
    assert "would resolve and close" in _report(tmp_path)
    fake.requests.clear()
    assert _feedback(tmp_path, event, "--live", "--offline") == 0
    assert fake.requests == []
    assert "offline" in _report(tmp_path)


def test_bad_event_file_exits_zero(tmp_path, fake) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _feedback(tmp_path, bad, "--live") == 0
    assert "feedback failed" in _report(tmp_path)
    assert _feedback(tmp_path, bad, "--live", "--strict") == 1
