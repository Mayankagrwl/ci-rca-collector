"""Step 18b Part 0 — F1 platform-incident dedupe (replaces leader election) and F2 owners.

Offline: fake GitHub + fake webhook.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.rca.deliver._fake_channels import CHAT_URL, FakeWebhook
from tests.rca.deliver._fake_github import Fault, FakeGitHub
from tests.rca.deliver.test_deliver_notify import NOW, _preview, _run, _stage
from tools.rca import cli
from tools.rca.deliver.incidents import LABEL_INCIDENT, incident_body, incident_title
from tools.rca.deliver.owners import parse_codeowners, resolve_path_owners


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_OUTPUT", "GITHUB_REPOSITORY", "RCA_DEFAULT_BRANCH", "RCA_GITHUB_API_URL",
                 "GITHUB_API_URL", "GITHUB_SERVER_URL", "GH_HOST", "RCA_GITHUB_HOST", "RCA_SSL_VERIFY",
                 "RCA_WORKSPACE", "GITHUB_WORKSPACE", "RCA_SMTP_URL", "RCA_CHAT_PAYLOAD_FIELD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")
    monkeypatch.setenv("RCA_CHAT_WEBHOOK_URL", CHAT_URL)


@pytest.fixture
def fake(monkeypatch) -> FakeGitHub:
    server = FakeGitHub(start=NOW - timedelta(minutes=1))
    monkeypatch.setattr(cli, "_delivery_client", lambda _args: server.client())
    return server


@pytest.fixture
def hook(monkeypatch) -> FakeWebhook:
    webhook = FakeWebhook()
    monkeypatch.setattr(cli, "_notify_chat_transport", webhook.transport())
    return webhook


def _run_row(rid: int, minutes_ago: float, name: str = "CI", branch: str = "main") -> dict:
    return {"id": rid, "conclusion": "failure", "name": name, "head_branch": branch, "workflow_id": 7,
            "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")}


def _incidents(fake: FakeGitHub) -> list[dict]:
    return [i for i in fake.issues.values() if LABEL_INCIDENT in [l["name"] for l in i["labels"]]]


def _seed_incident(fake: FakeGitHub, kind: str, *, minutes_ago: float) -> dict:
    issue = fake.seed_issue(
        incident_title(kind),
        incident_body(kind, workflows=["CI"], branches=["main"], window_minutes=60),
        labels=[LABEL_INCIDENT],
    )
    stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    issue["created_at"] = issue["updated_at"] = stamp
    return issue


# ---- F1: the exact reproduction -------------------------------------------------------------------


def test_ordinary_failure_in_the_window_cannot_swallow_the_platform_alert(tmp_path, fake, hook) -> None:
    # Run 100 is an ordinary compile failure 20 minutes ago; 101 and 102 are runner-infra.
    # Step 18a's leader election picked 100 (which never takes the platform path) → 0 messages.
    fake.runs += [_run_row(100, 20, "Build"), _run_row(101, 2), _run_row(102, 1)]
    previews = {}
    for rid in (101, 102):
        summary = _stage(tmp_path, run_id=rid, short_circuit="infra_runner", infra=True, name=f"r{rid}")
        assert _run(summary, "--live", "--platform-team", "platform@example.invalid") == 0
        previews[rid] = _preview(summary)
    assert len(hook.posts) == 1
    assert len(_incidents(fake)) == 1
    number = _incidents(fake)[0]["number"]
    assert "- chat: sent" in previews[101]
    assert f"platform notified via #{number}" in previews[102]
    assert any(c.startswith("Also failed: [run 102]") for c in fake.issue_comments_on(number))


def test_two_concurrent_creates_send_once_and_close_a_duplicate(tmp_path, fake, hook) -> None:
    ids = [601, 602, 603, 604, 605]
    fake.runs += [_run_row(rid, 1) for rid in ids]
    state = {"raced": False}

    def race(server: FakeGitHub, req) -> None:
        # Run B executes completely right after run A's first incident lookup, so both
        # saw "no incident" and both create one.
        if not state["raced"] and req.method == "GET" and f"labels={LABEL_INCIDENT}" in req.query:
            state["raced"] = True
            b = _stage(tmp_path, event="pull_request", branch="f", pr=602, run_id=602,
                       short_circuit="infra_widespread", name="b")
            assert _run(b, "--live") == 0

    fake.after_request.append(race)
    for rid in (601, 603, 604, 605):
        summary = _stage(tmp_path, event="pull_request", branch="f", pr=rid, run_id=rid,
                         short_circuit="infra_widespread", name=f"w{rid}")
        assert _run(summary, "--live") == 0
    assert len(hook.posts) == 1
    incidents = sorted(_incidents(fake), key=lambda i: i["number"])
    assert len(incidents) == 2
    kept, dup = incidents
    assert kept["state"] == "open"
    assert dup["state"] == "closed" and dup["state_reason"] == "not_planned"
    assert fake.issue_comments_on(dup["number"]) == [f"Duplicate of #{kept['number']}"]


def test_run_inside_window_joins_the_incident(tmp_path, fake, hook) -> None:
    old = _seed_incident(fake, "infra_widespread", minutes_ago=30)
    summary = _stage(tmp_path, event="pull_request", branch="f", pr=7, run_id=700,
                     short_circuit="infra_widespread")
    assert _run(summary, "--live") == 0
    assert hook.posts == []
    assert f"platform notified via #{old['number']}" in _preview(summary)
    assert fake.issue_comments_on(old["number"])[0].startswith("Also failed: [run 700]")
    assert len(_incidents(fake)) == 1


def test_run_after_window_opens_new_incident_and_supersedes(tmp_path, fake, hook) -> None:
    old = _seed_incident(fake, "infra_widespread", minutes_ago=90)
    other_kind = _seed_incident(fake, "infra_runner", minutes_ago=90)
    summary = _stage(tmp_path, event="pull_request", branch="f", pr=8, run_id=800,
                     short_circuit="infra_widespread")
    assert _run(summary, "--live") == 0
    assert len(hook.posts) == 1
    new = max(_incidents(fake), key=lambda i: i["number"])
    assert new["number"] != old["number"] and new["state"] == "open"
    assert fake.issues[old["number"]]["state"] == "closed"
    assert fake.issue_comments_on(old["number"]) == [f"Superseded by #{new['number']}"]
    assert fake.issues[other_kind["number"]]["state"] == "open"  # a different kind is untouched


def test_403_fails_open_and_sends(tmp_path, fake, hook) -> None:
    fake.faults.append(Fault("POST", r"/repos/acme/widgets/issues$", status=403))
    summary = _stage(tmp_path, event="pull_request", branch="f", pr=9, run_id=900,
                     short_circuit="infra_widespread")
    assert _run(summary, "--live") == 0
    assert len(hook.posts) == 1
    assert "burst dedupe unavailable (403: the token needs `issues: write`)" in _preview(summary)


def test_create_issues_off_sends_without_dedupe(tmp_path, fake, hook) -> None:
    summary = _stage(tmp_path, event="pull_request", branch="f", pr=9, run_id=901,
                     short_circuit="infra_widespread")
    assert _run(summary, "--live", "--create-issues", "false") == 0
    assert len(hook.posts) == 1 and _incidents(fake) == []
    assert "burst dedupe unavailable (create-issues is off)" in _preview(summary)


def test_dry_run_and_offline(tmp_path, fake, hook) -> None:
    summary = _stage(tmp_path, event="pull_request", branch="f", pr=9, run_id=902,
                     short_circuit="infra_widespread")
    assert _run(summary) == 0
    assert fake.writes() == [] and hook.posts == []
    assert "would open a platform incident issue" in _preview(summary)
    fake.requests.clear()
    assert _run(summary, "--live", "--offline") == 0
    assert fake.requests == [] and hook.posts == []
    assert "not checked (no API access)" in _preview(summary)


def test_incident_issues_are_invisible_to_the_fingerprint_machinery(tmp_path, fake, hook) -> None:
    incident = _seed_incident(fake, "infra_widespread", minutes_ago=60 * 24 * 30)
    summary = _stage(tmp_path, run_id=950)  # an ordinary push_default failure (sweeps stale issues)
    assert _run(summary, "--live") == 0
    assert fake.issues[incident["number"]]["state"] == "open"
    assert LABEL_INCIDENT not in [l["name"] for i in fake.issues.values()
                                  if i["title"] == "[RCA] fingerprint index" for l in i["labels"]]


# ---- F2: a bare known file never borrows ownership ---------------------------------------------------------


def test_f2_bare_root_file_is_only_itself() -> None:
    rules, _ = parse_codeowners("/pom.xml @root\n/services/billing/ @billing\n")
    deep = resolve_path_owners("/work/services/billing/pom.xml", rules, workspace=None, known_files=["pom.xml"])
    assert deep.owners == ("@billing",)
    root = resolve_path_owners("pom.xml", rules, workspace=None, known_files=["pom.xml"])
    assert root.owners == ("@root",)
