"""Step 19 Part 0 F1 — `/resolved` from private org members via the repo-permission fallback.

GitHub reports members with a private org membership as CONTRIBUTOR / NONE in
issue_comment payloads; their repository role decides instead. Never allowed on error.
"""

from __future__ import annotations

import pytest

from tests.rca.deliver._fake_github import Fault
from tests.rca.deliver.test_deliver_feedback import _env as _feedback_env  # noqa: F401 (autouse)
from tests.rca.deliver.test_deliver_feedback import _event, _feedback, _fp_issue, _report, fake  # noqa: F401
from tools.rca.deliver.feedback import allowed_permissions


def _perm_calls(fake) -> list:
    return [r for r in fake.requests if "/collaborators/" in r.path]


def _resolve_as(tmp_path, fake, association: str, *flags: str):
    issue = _fp_issue(fake)
    comment = fake.seed_issue_comment(issue["number"], "/resolved fixed the runner image")
    event = _event(tmp_path, body=comment["body"], issue=dict(issue), comment_id=comment["id"],
                   association=association)
    assert _feedback(tmp_path, event, *flags) == 0
    return issue


def test_private_member_with_write_is_allowed(tmp_path, fake) -> None:
    fake.permissions["alice"] = "write"
    issue = _resolve_as(tmp_path, fake, "CONTRIBUTOR", "--live")
    assert fake.issues[issue["number"]]["state"] == "closed"
    assert [r.path for r in _perm_calls(fake)] == ["/repos/acme/widgets/collaborators/alice/permission"]
    assert "- permission: role write / base write" in _report(tmp_path)


@pytest.mark.parametrize("role", ["maintain", "admin"])
def test_higher_roles_are_allowed(tmp_path, fake, role) -> None:
    fake.permissions["alice"] = role
    issue = _resolve_as(tmp_path, fake, "NONE", "--live")
    assert fake.issues[issue["number"]]["state"] == "closed"


def test_read_permission_is_refused(tmp_path, fake) -> None:
    fake.permissions["alice"] = "read"
    issue = _resolve_as(tmp_path, fake, "CONTRIBUTOR", "--live")
    assert fake.issues[issue["number"]]["state"] == "open"
    assert fake.writes() == []
    assert "repository permission (role read / base read) may not resolve" in _report(tmp_path)


def test_not_a_collaborator_404_is_refused(tmp_path, fake) -> None:
    issue = _resolve_as(tmp_path, fake, "CONTRIBUTOR", "--live")
    assert fake.issues[issue["number"]]["state"] == "open" and fake.writes() == []
    assert "repository permission (role none / base none) may not resolve" in _report(tmp_path)


def test_custom_role_with_write_base_is_allowed(tmp_path, fake) -> None:  # Step 19b fix 4
    fake.permissions["alice"] = ("dev-write", "write")
    issue = _resolve_as(tmp_path, fake, "CONTRIBUTOR", "--live")
    assert fake.issues[issue["number"]]["state"] == "closed"
    assert "- permission: role dev-write / base write" in _report(tmp_path)


def test_triage_with_read_base_is_refused(tmp_path, fake) -> None:  # Step 19b fix 4
    fake.permissions["alice"] = ("triage", "read")
    issue = _resolve_as(tmp_path, fake, "CONTRIBUTOR", "--live")
    assert fake.issues[issue["number"]]["state"] == "open" and fake.writes() == []
    assert "(role triage / base read) may not resolve" in _report(tmp_path)


def test_api_error_is_refused_never_allowed(tmp_path, fake) -> None:
    fake.permissions["alice"] = "admin"
    fake.faults.append(Fault("GET", r"/collaborators/alice/permission$", status=500, times=3))
    issue = _resolve_as(tmp_path, fake, "CONTRIBUTOR", "--live")
    assert fake.issues[issue["number"]]["state"] == "open" and fake.writes() == []
    assert "permission lookup failed" in _report(tmp_path)


def test_owner_makes_no_permission_call(tmp_path, fake) -> None:
    issue = _resolve_as(tmp_path, fake, "OWNER", "--live")
    assert fake.issues[issue["number"]]["state"] == "closed"
    assert _perm_calls(fake) == []


def test_bots_are_refused_before_any_lookup(tmp_path, fake) -> None:
    fake.permissions["dependabot[bot]"] = "admin"
    issue = _fp_issue(fake)
    event = _event(tmp_path, body="/resolved x", issue=dict(issue), comment_id=1,
                   association="CONTRIBUTOR", login="dependabot[bot]", user_type="Bot")
    assert _feedback(tmp_path, event, "--live") == 0
    assert fake.requests == []


def test_offline_refuses_without_calling(tmp_path, fake) -> None:
    fake.permissions["alice"] = "write"
    _resolve_as(tmp_path, fake, "CONTRIBUTOR", "--live", "--offline")
    assert fake.requests == []
    assert "repository permission not checked" in _report(tmp_path)


def test_dry_run_may_read_the_permission(tmp_path, fake) -> None:
    fake.permissions["alice"] = "write"
    _resolve_as(tmp_path, fake, "CONTRIBUTOR")
    assert _perm_calls(fake) and fake.writes() == []
    assert "would resolve and close" in _report(tmp_path)


def test_permissions_env_narrows(tmp_path, fake, monkeypatch) -> None:
    monkeypatch.setenv("RCA_RESOLVE_PERMISSIONS", "admin")
    fake.permissions["alice"] = "write"
    issue = _resolve_as(tmp_path, fake, "CONTRIBUTOR", "--live")
    assert fake.issues[issue["number"]]["state"] == "open"
    assert allowed_permissions({}) == {"admin", "maintain", "write"}
    assert allowed_permissions({"RCA_RESOLVE_PERMISSIONS": "Admin, Maintain"}) == {"admin", "maintain"}
