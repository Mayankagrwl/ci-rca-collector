"""Step 18a — email + chat notifications, and the Step 17 follow-ups (F1–F3).

Offline only: fake webhook (httpx.MockTransport), fake SMTP factory, fake GitHub.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.rca.deliver._fake_channels import (
    CHAT_URL,
    SMTP_PASSWORD,
    SMTP_URL,
    FakeSmtp,
    FakeWebhook,
    smtp_error_with_secret,
)
from tests.rca.deliver._fake_github import Fault, FakeGitHub
from tools.rca import cli
from tools.rca.cli import CHAT_BEGIN, CHAT_END, main
from tools.rca.deliver import DeliveryContext, DeliveryInputs, SuppressionDecision
from tools.rca.deliver.notify import (
    Scrubber,
    header_text,
    in_quiet_window,
    notify_config,
    plan_notification,
    summarize_burst,
)
from tools.rca.deliver.owners import parse_codeowners, resolve_path_owners
from tools.rca.models import HistoryContext, Summary

_ROOT = Path(__file__).resolve().parents[3]
_GOLDENS = _ROOT / "tools" / "eval" / "goldens"
REPO = "acme/widgets"
SHA = "c0ffee" + "0" * 34
_RULE_ID = re.compile(r"\bR\d+\b")
NOW = datetime.now(timezone.utc)
CODEOWNERS = "/src/ lead@example.invalid @alice\n"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_OUTPUT", "GITHUB_REPOSITORY", "RCA_DEFAULT_BRANCH", "RCA_GITHUB_API_URL",
                 "GITHUB_API_URL", "GITHUB_SERVER_URL", "GH_HOST", "RCA_GITHUB_HOST", "RCA_SSL_VERIFY",
                 "RCA_WORKSPACE", "GITHUB_WORKSPACE", "RCA_CHAT_WEBHOOK_URL", "RCA_SMTP_URL",
                 "RCA_CHAT_PAYLOAD_FIELD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


@pytest.fixture
def fake(monkeypatch) -> FakeGitHub:
    server = FakeGitHub()
    server.files[(".github/CODEOWNERS", "main")] = CODEOWNERS
    monkeypatch.setattr(cli, "_delivery_client", lambda _args: server.client())
    return server


@pytest.fixture
def hook(monkeypatch) -> FakeWebhook:
    webhook = FakeWebhook()
    monkeypatch.setattr(cli, "_notify_chat_transport", webhook.transport())
    return webhook


@pytest.fixture
def smtp(monkeypatch) -> FakeSmtp:
    server = FakeSmtp()
    monkeypatch.setattr(cli, "_notify_smtp_factory", server.factory)
    return server


@pytest.fixture
def channels(monkeypatch, hook, smtp):
    monkeypatch.setenv("RCA_CHAT_WEBHOOK_URL", CHAT_URL)
    monkeypatch.setenv("RCA_SMTP_URL", SMTP_URL)
    return hook, smtp


def _stage(
    tmp_path: Path,
    *,
    golden: str = "java-compile-in-pipeline",
    event: str = "push",
    branch: str = "main",
    pr: int | None = None,
    run_id: int = 1,
    fine: str = "f1e2d3c4b5a69788",
    seen: int = 0,
    short_circuit: str | None = None,
    infra: bool = False,
    draft: bool = False,
    primary: str | None = None,
    workflow: str = "CI",
    name: str = "rca",
) -> Path:
    summary = Summary.model_validate_json((_GOLDENS / golden / "summary.json").read_text(encoding="utf-8"))
    summary.run.event = event
    summary.run.head_branch = branch
    summary.run.head_sha = SHA
    summary.run.pr_number = pr
    summary.run.run_id = run_id
    summary.run.workflow_name = workflow
    summary.fingerprint = fine
    summary.fingerprint_coarse = "c" * 16
    summary.history = HistoryContext(seen_count=seen)
    if short_circuit:
        summary.verdict.short_circuit = short_circuit
        summary.verdict.requires_analysis = False
    if infra:
        summary.classification.is_infra_vs_code = "infra"
        summary.classification.category = "infra_runner"
        summary.classification.confidence = "high"
    if draft:
        from tools.rca.models import ChangeContext

        summary.changes = ChangeContext(head_sha=SHA, pr_is_draft=True)
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


def _preview_chat(summary: Path) -> str:
    return _preview(summary).split(CHAT_BEGIN + "\n", 1)[1].split("\n" + CHAT_END, 1)[0]


def _secrets_absent(*texts: str) -> None:
    for text in texts:
        for secret in (CHAT_URL, "SECRETPATHxyz", "QUERYSECRET123", SMTP_PASSWORD, "mailer:"):
            assert secret not in text, secret


# ---- AC1 ------------------------------------------------------------------------------------


def test_no_channel_configured(tmp_path, fake, hook, smtp) -> None:
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    assert "- notifications: none configured" in _preview(summary)
    assert hook.posts == [] and smtp.connections == []
    assert not [e for e in _delivery(summary)["errors"] if "chat" in e or "SMTP" in e]


# ---- AC2 ------------------------------------------------------------------------------------


def test_push_default_high_sends_chat_and_email(tmp_path, fake, channels) -> None:
    hook, smtp = channels
    summary = _stage(tmp_path)
    assert _run(summary, "--live", "--default-notify", "oncall@example.invalid") == 0
    delivery = _delivery(summary)
    assert delivery["severity"] == "high"
    assert "commit_comment" in delivery["delivered_to"]
    assert any(d.startswith("issue:") for d in delivery["delivered_to"])
    assert delivery["delivered_to"][-2:] == ["chat", "email"]
    assert len(hook.posts) == 1 and len(smtp.messages) == 1
    msg = smtp.messages[0]
    assert msg["Subject"] == "[CI] high: CI failed on main"
    assert msg["To"] == "lead@example.invalid, oncall@example.invalid"
    assert msg["From"] == "ci-bot@example.invalid"
    assert smtp.connections[0]["starttls"] is True
    assert "no email address for @alice" in _preview(summary)
    # The preview's chat text is byte-identical to the POSTed field.
    assert hook.posts[0] == {"text": _preview_chat(summary)}
    assert "Authorization" not in hook.headers[0] and "authorization" not in hook.headers[0]


def test_payload_field_override(tmp_path, fake, channels, monkeypatch) -> None:
    hook, _smtp = channels
    monkeypatch.setenv("RCA_CHAT_PAYLOAD_FIELD", "content")
    assert _run(_stage(tmp_path), "--live") == 0
    assert list(hook.posts[0]) == ["content"]


# ---- AC3 ------------------------------------------------------------------------------------


def test_severity_gate_normal_pr_silent_schedule_sends(tmp_path, fake, channels) -> None:
    hook, _smtp = channels
    pr = _stage(tmp_path, event="pull_request", branch="feature", pr=5)
    assert _run(pr, "--live") == 0
    assert _delivery(pr)["severity"] == "normal"
    assert hook.posts == []
    assert "severity normal below high" in _preview(pr)
    sched = _stage(tmp_path, event="schedule", run_id=2, fine="0000aaaa0000aaaa")
    assert _run(sched, "--live") == 0
    assert _delivery(sched)["severity"] == "normal"
    assert len(hook.posts) == 1


# ---- AC4 ------------------------------------------------------------------------------------


def test_quiet_window_uses_the_previous_issue_update(tmp_path, fake, channels) -> None:
    hook, _smtp = channels
    fake.start = NOW - timedelta(minutes=10)
    first = _stage(tmp_path, run_id=1)
    assert _run(first, "--live") == 0
    assert len(hook.posts) == 1  # this run's own create is never the clock
    second = _stage(tmp_path, run_id=2)
    assert _run(second, "--live") == 0
    assert len(hook.posts) == 1
    assert "quiet window" in _preview(second)


def test_after_the_window_it_sends_again(tmp_path, fake, channels) -> None:
    hook, _smtp = channels
    fake.start = NOW - timedelta(hours=3)
    assert _run(_stage(tmp_path, run_id=1), "--live") == 0
    assert _run(_stage(tmp_path, run_id=2), "--live") == 0
    assert len(hook.posts) == 2


def test_update_quiet_dedupe_is_quiet() -> None:
    decision = SuppressionDecision(dedupe="update_quiet")
    assert in_quiet_window(decision, None, NOW, 60)
    assert not in_quiet_window(SuppressionDecision(), None, NOW, 60)
    assert in_quiet_window(SuppressionDecision(), NOW - timedelta(minutes=59), NOW, 60)
    assert not in_quiet_window(SuppressionDecision(), NOW - timedelta(minutes=61), NOW, 60)


# ---- AC5: widespread bursts (coordinated via a platform-incident issue; Step 18b F1) -------------------
# Step 18b replaced run-list leader election; the incident scenarios live in test_deliver_incidents.


def _burst(fake: FakeGitHub, ids: list[int]) -> None:
    for i, rid in enumerate(ids):
        fake.runs.append({
            "id": rid, "conclusion": "failure", "name": ["CI", "Deploy", "Lint"][i % 3],
            "head_branch": ["main", "release/2.0"][i % 2],
            "created_at": (NOW - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            "workflow_id": 7,
        })
    fake.runs.append({"id": 1, "conclusion": "failure", "name": "Old", "head_branch": "x",
                      "created_at": (NOW - timedelta(hours=5)).isoformat(), "workflow_id": 7})


def test_five_widespread_runs_send_exactly_one(tmp_path, fake, channels) -> None:  # AC #9
    hook, _smtp = channels
    fake.start = NOW - timedelta(minutes=1)
    ids = [505, 502, 504, 501, 503]
    _burst(fake, ids)
    previews = {}
    for rid in ids:
        summary = _stage(tmp_path, event="pull_request", branch="feature", pr=rid,
                         run_id=rid, short_circuit="infra_widespread", name=f"rca{rid}")
        assert _run(summary, "--live", "--platform-team", "platform@example.invalid") == 0
        previews[rid] = _preview(summary)
    assert len(hook.posts) == 1
    text = hook.posts[0]["text"]
    assert "widespread CI failures (5 runs)" in text
    assert "Workflows: CI, Deploy, Lint" in text and "release/2.0" in text and "Old" not in text
    assert all(not fake.issue_comments.get(rid) for rid in ids)  # zero PR comments
    incident = [i for i in fake.issues.values() if i["title"].startswith("[RCA] platform incident")]
    assert len(incident) == 1
    assert "platform notified via #" in previews[503]
    assert "- chat: skipped (platform notified via #" in previews[503]


def test_incident_lookup_failure_sends_anyway(tmp_path, fake, channels) -> None:
    hook, _smtp = channels
    fake.faults.append(Fault("GET", r"/issues$", status=500, times=3))
    summary = _stage(tmp_path, event="pull_request", branch="feature", pr=9, run_id=900,
                     short_circuit="infra_widespread")
    assert _run(summary, "--live") == 0
    assert len(hook.posts) == 1
    assert "burst dedupe unavailable" in _preview(summary)


# ---- AC6 / AC7 / AC8 -------------------------------------------------------------------------


def test_infra_runner_goes_to_platform_only_and_bursts_once(tmp_path, fake, channels) -> None:
    hook, smtp = channels
    fake.start = NOW - timedelta(minutes=1)
    _burst(fake, [301, 302])
    for rid in (302, 301):
        summary = _stage(tmp_path, run_id=rid, short_circuit="infra_runner", infra=True, name=f"r{rid}")
        assert _run(summary, "--live", "--platform-team", "platform@example.invalid") == 0
    assert len(hook.posts) == 1 and len(smtp.messages) == 1
    assert smtp.messages[0]["To"] == "platform@example.invalid"
    assert "@bot" not in hook.posts[0]["text"]  # never the actor


@pytest.mark.parametrize("kwargs", [{"golden": "flake-same-sha"},
                                    {"event": "pull_request", "branch": "f", "pr": 3, "draft": True}])
def test_flaky_and_draft_never_notify(tmp_path, fake, channels, kwargs) -> None:
    hook, smtp = channels
    summary = _stage(tmp_path, **kwargs)
    assert _run(summary, "--live") == 0
    assert hook.posts == [] and smtp.connections == []
    assert "never notifies" in _preview(summary)


def test_dispatch_notifies_actor_only_and_opens_no_issue(tmp_path, fake, channels) -> None:  # AC #14
    hook, _smtp = channels
    summary = _stage(tmp_path, event="workflow_dispatch", seen=50)
    assert _run(summary, "--live") == 0
    assert len(hook.posts) == 1
    assert fake.issues == {}
    assert "- audience: @bot" in _preview(summary)


# ---- AC9: failures ----------------------------------------------------------------------------------


def test_smtp_failure_is_scrubbed_and_comment_still_posts(tmp_path, fake, channels) -> None:
    _hook, smtp = channels
    smtp.fail_on_send = smtp_error_with_secret()
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    delivery = _delivery(summary)
    assert any(e.startswith("SMTP send failed") for e in delivery["errors"])
    assert "commit_comment" in delivery["delivered_to"] and "chat" in delivery["delivered_to"]
    assert delivery["suppressed_by"] is None
    _secrets_absent(json.dumps(delivery), _preview(summary), summary.read_text(encoding="utf-8"))


def test_webhook_500_and_exception_are_scrubbed(tmp_path, fake, channels) -> None:
    hook, _smtp = channels
    hook.status = 500
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    assert "chat webhook failed (status 500)" in _delivery(summary)["errors"]
    import httpx

    hook.status = 200
    hook.raise_exc = httpx.ConnectError(f"cannot connect to {CHAT_URL}")
    summary = _stage(tmp_path, run_id=2, fine="1111222233334444")
    assert _run(summary, "--live") == 0
    errors = _delivery(summary)["errors"]
    assert "chat webhook failed: ConnectError" in errors
    _secrets_absent(json.dumps(errors), _preview(summary))


def test_notification_only_failure_is_a_delivery_error(tmp_path, fake, channels) -> None:
    hook, smtp = channels
    hook.status = 500
    smtp.fail_on_send = RuntimeError("down")
    summary = _stage(tmp_path, event="schedule")
    assert _run(summary, "--live", "--create-issues", "false") == 0
    assert _delivery(summary)["suppressed_by"] == "delivery_error"


# ---- AC10: transport security --------------------------------------------------------------------------


def test_http_webhook_is_rejected(tmp_path, fake, hook, monkeypatch) -> None:
    monkeypatch.setenv("RCA_CHAT_WEBHOOK_URL", "http://hooks.example.invalid/x")
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    assert hook.posts == []
    assert "chat webhook must be https://" in _preview(summary)


def test_smtp_without_starttls_sends_nothing(tmp_path, fake, smtp, monkeypatch) -> None:
    monkeypatch.setenv("RCA_SMTP_URL", SMTP_URL)
    smtp.offer_starttls = False
    summary = _stage(tmp_path)
    assert _run(summary, "--live", "--default-notify", "oncall@example.invalid") == 0
    assert smtp.messages == [] and smtp.connections[0]["login"] is None
    assert "SMTP server does not offer STARTTLS; not sent" in _delivery(summary)["errors"]


def test_tls_none_sends_in_clear_with_note(tmp_path, fake, smtp, monkeypatch) -> None:
    monkeypatch.setenv("RCA_SMTP_URL", "smtp://relay.example.invalid:25?from=ci@example.invalid&tls=none")
    smtp.offer_starttls = False
    summary = _stage(tmp_path)
    assert _run(summary, "--live", "--default-notify", "oncall@example.invalid") == 0
    assert len(smtp.messages) == 1 and smtp.connections[0]["starttls"] is False
    assert "sending in clear text" in _preview(summary)


def test_smtps_uses_implicit_tls(tmp_path, fake, smtp, monkeypatch) -> None:
    monkeypatch.setenv("RCA_SMTP_URL", "smtps://relay.example.invalid?from=ci@example.invalid")
    assert _run(_stage(tmp_path), "--live", "--default-notify", "oncall@example.invalid") == 0
    assert smtp.connections[0]["implicit_tls"] is True and smtp.connections[0]["port"] == 465


def test_missing_from_is_a_configuration_note(tmp_path, fake, smtp, monkeypatch) -> None:
    monkeypatch.setenv("RCA_SMTP_URL", "smtp://relay.example.invalid")
    summary = _stage(tmp_path)
    assert _run(summary, "--live", "--default-notify", "oncall@example.invalid") == 0
    assert smtp.connections == []
    assert "no ?from= address" in _preview(summary)


# ---- AC11: header injection ------------------------------------------------------------------------------


def test_branch_header_injection(tmp_path, fake, channels) -> None:
    _hook, smtp = channels
    summary = _stage(tmp_path, event="schedule", branch="main\r\nBcc: x@evil.example")
    assert _run(summary, "--live", "--default-notify", "oncall@example.invalid") == 0
    msg = smtp.messages[0]
    assert "\r" not in msg["Subject"] and "\n" not in msg["Subject"]
    assert msg["Bcc"] is None
    assert list(msg.keys()) == ["Subject", "From", "To", "Content-Type", "Content-Transfer-Encoding", "MIME-Version"]
    assert header_text("a\r\nb\x00c") == "a b c"


# ---- AC12: safety ------------------------------------------------------------------------------------------


def test_chat_and_email_are_safe(tmp_path, fake, channels) -> None:
    hook, smtp = channels
    secret = "ghp_" + "Z1x2C3v4B5n6M7a8S9d0F1g2H3j4K5l6Q7w8"
    primary = f"release already exists <!channel> <@U123> @everyone @here rule R8 token {secret}"
    summary = _stage(tmp_path, golden="artifactory-version-exists", primary=primary)
    assert _run(summary, "--live", "--default-notify", "oncall@example.invalid") == 0
    chat = hook.posts[0]["text"]
    body = smtp.messages[0].get_content()
    for text in (chat, body, smtp.messages[0]["Subject"]):
        assert "ghp_" not in text
        assert not _RULE_ID.search(text), text
        assert "@everyone" not in text and "@here" not in text
    assert "<!channel>" not in chat and "&lt;!channel&gt;" in chat
    assert "<@U123>" not in chat
    assert "exit code" not in body  # no evidence / log lines


def _ctx(trigger="push_default") -> DeliveryContext:
    return DeliveryContext(trigger=trigger, branch="main", default_branch="main",
                           is_default_branch=True, actor="octocat", commit_sha="f" * 40)


def test_c2_omitted_root_cause_makes_no_claim() -> None:
    summary = Summary.model_validate_json((_GOLDENS / "artifactory-version-exists" / "summary.json").read_text(encoding="utf-8"))
    from tools.rca.diagnose import apply_verdict, diagnose

    summary = apply_verdict(summary, diagnose(summary.model_copy(deep=True)))
    plan = plan_notification(
        notify_config(DeliveryInputs(chat_webhook_url="https://h.example.invalid/x"), {}),
        summary=summary, record=None, decision=SuppressionDecision(omit_root_cause=True),
        context=_ctx(), route_notify=["team"], severity="high", inputs=DeliveryInputs(),
        owners=["@a"], owner_resolved_by="actor", issue_url=None, previous_updated_at=None, now=NOW,
    )
    assert plan.send
    assert "already exists" not in plan.chat_text and "already exists" not in plan.email_body
    assert "CI failure — release version already published" in plan.chat_text


# ---- AC13: dry run + offline -------------------------------------------------------------------------------


def test_dry_run_would_send_with_zero_channel_calls(tmp_path, fake, channels) -> None:
    hook, smtp = channels
    summary = _stage(tmp_path)
    assert _run(summary, "--default-notify", "oncall@example.invalid") == 0
    text = _preview(summary)
    assert "- chat: would send" in text
    assert "- email: would send to lead@example.invalid, oncall@example.invalid" in text
    assert hook.posts == [] and smtp.connections == []
    assert CHAT_BEGIN in text


def test_offline_zero_network(tmp_path, fake, channels) -> None:
    hook, smtp = channels
    summary = _stage(tmp_path)
    assert _run(summary, "--live", "--offline") == 0
    assert fake.requests == [] and hook.posts == [] and smtp.connections == []
    assert "- chat: would send" in _preview(summary)


# ---- pure helpers -------------------------------------------------------------------------------------------


def test_summarize_burst() -> None:
    runs = [{"id": 7, "created_at": (NOW - timedelta(minutes=5)).isoformat(), "name": "A", "head_branch": "m"},
            {"id": 3, "created_at": (NOW - timedelta(hours=2)).isoformat(), "name": "Old", "head_branch": "x"}]
    burst = summarize_burst(runs, run_id=9, workflow="B", branch="n", now=NOW, window_minutes=60)
    assert (burst.run_count, burst.workflows, burst.branches) == (2, ["A", "B"], ["m", "n"])
    solo = summarize_burst([], run_id=9, workflow="B", branch="n", now=NOW, window_minutes=60)
    assert solo.run_count == 1


def test_scrubber() -> None:
    scrub = Scrubber([CHAT_URL, SMTP_URL])
    text = f"x {CHAT_URL} y token=QUERYSECRET123 pw {SMTP_PASSWORD} mailer:{SMTP_PASSWORD}"
    cleaned = scrub(text)
    _secrets_absent(cleaned)


def test_secrets_never_come_from_argv() -> None:
    parser = cli._build_parser()
    help_text = parser.format_help() + "".join(
        a.format_help() for a in parser._subparsers._group_actions[0].choices.values()
    )
    assert "--smtp-url" not in help_text and "--chat-webhook-url" not in help_text


# ---- Part 0 --------------------------------------------------------------------------------------------------


_MONO = "* @default\n/services/billing/ @billing\n/services/auth/ @auth\n/src/ @src\n"


def test_f1_same_named_file_never_borrows_an_owner() -> None:
    rules, _ = parse_codeowners(_MONO)
    known = ["services/auth/pom.xml"]
    assert resolve_path_owners("/work/services/billing/pom.xml", rules, workspace=None,
                               known_files=known).owners == ("@billing",)
    assert resolve_path_owners("pom.xml", rules, workspace=None, known_files=known).owners == ("@default",)
    assert resolve_path_owners("test/A.java", rules, workspace=None,
                               known_files=["src/test/A.java"]).owners == ("@src",)
    notes: list[str] = []
    ambiguous = resolve_path_owners("test/A.java", rules, workspace=None,
                                    known_files=["src/test/A.java", "lib/test/A.java"], notes=notes)
    assert ambiguous.owners == ("@default",)
    assert notes and "ambiguous" in notes[0]


def test_f2_large_codeowners_is_not_an_empty_file(tmp_path, fake) -> None:
    fake.files.clear()
    fake.large_files.add(".github/CODEOWNERS")
    fake.files[("CODEOWNERS", "main")] = "* @root\n"
    summary = _stage(tmp_path)
    assert _run(summary) == 0
    assert "too large for the contents API; ignored" in _preview(summary)
    assert [r.path.split("/contents/")[1] for r in fake.requests if "/contents/" in r.path] == [".github/CODEOWNERS"]
    fake.large_files.clear()
    fake.files[(".github/CODEOWNERS", "main")] = ""
    assert _run(summary) == 0
    assert _delivery(summary)["owner_resolved_by"] == "actor"  # a real empty file still wins


@pytest.mark.parametrize(("assignable", "assigned", "claimed"), [({"alice"}, ["alice"], True),
                                                                  (set(), [], False)])
def test_f3_assigned_only_when_github_kept_the_login(tmp_path, fake, assignable, assigned, claimed) -> None:
    fake.files[(".github/CODEOWNERS", "main")] = "/src/ @alice @bob\n"
    fake.assignable = assignable
    summary = _stage(tmp_path)
    assert _run(summary, "--live") == 0
    issue = [i for i in fake.issues.values() if i["title"] != "[RCA] fingerprint index"][0]
    assert [a["login"] for a in issue["assignees"]] == assigned
    delivery = _delivery(summary)
    assert ("issue:assigned" in delivery["delivered_to"]) is claimed
    assert "GitHub did not assign bob" in _preview(summary)
    if not claimed:
        assert "GitHub did not assign alice" in _preview(summary)
