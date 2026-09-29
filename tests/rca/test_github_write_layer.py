"""Step 12: GitHub write layer — body support, duplicate-safe retries, new methods."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tools.rca.github_api import GitHubAPIError, GitHubClient

_ENV = (
    "RCA_GITHUB_API_URL",
    "GITHUB_API_URL",
    "GITHUB_SERVER_URL",
    "GH_HOST",
    "RCA_GITHUB_HOST",
    "RCA_GITHUB_TOKEN",
    "COMMON_ACTIONS_PAT",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "RCA_SSL_CERT_FILE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "RCA_SSL_VERIFY",
)

_API = "https://api.github.com"
_REPO = "acme/widgets"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    sleeps: list[float] | None = None,
    **kwargs: Any,
) -> GitHubClient:
    return GitHubClient(
        transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else (lambda _d: None)),
        **kwargs,
    )


def _recorder(
    status: int = 200, payload: Any = None
) -> tuple[list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={} if payload is None else payload)

    return seen, handler


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content.decode("utf-8"))


# ---- AC 1: GET unchanged -------------------------------------------------


def test_get_request_is_unchanged_and_sends_no_body() -> None:
    seen, handler = _recorder(payload={"id": 5})
    with _client(handler) as client:
        client.get_run(_REPO, 5)
    (req,) = seen
    assert req.method == "GET"
    assert str(req.url) == f"{_API}/repos/{_REPO}/actions/runs/5"
    assert req.content == b""
    assert "content-type" not in req.headers
    assert "content-length" not in req.headers
    # Header set is exactly the pre-Step-12 set (plus httpx transport defaults).
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert req.headers["User-Agent"] == "ci-rca-collector"
    assert req.headers["Authorization"] == "Bearer test-token"
    assert set(req.headers.keys()) == {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "x-github-api-version",
        "authorization",
    }


def test_request_omits_json_kwarg_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    _seen, handler = _recorder()
    client = _client(handler)
    original = client._client.request

    def spy(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured.append(kwargs)
        return original(method, url, **kwargs)

    monkeypatch.setattr(client._client, "request", spy)
    with client:
        client.get_repo(_REPO)
        client.update_issue(_REPO, 3, state="closed")
    assert "json" not in captured[0]
    assert set(captured[0]) == {"follow_redirects", "headers"}
    assert captured[1]["json"] == {"state": "closed"}


# ---- AC 2: get_repo -------------------------------------------------------


def test_get_repo_returns_payload_with_default_branch() -> None:
    seen, handler = _recorder(payload={"full_name": _REPO, "default_branch": "main"})
    with _client(handler) as client:
        repo = client.get_repo(_REPO)
    assert repo["default_branch"] == "main"
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/repos/{_REPO}"


def test_get_repo_404_raises_with_status() -> None:
    _seen, handler = _recorder(status=404, payload={"message": "Not Found"})
    with _client(handler) as client, pytest.raises(GitHubAPIError) as info:
        client.get_repo(_REPO)
    assert info.value.status_code == 404


# ---- AC 3: method + path + JSON body -------------------------------------

_WRITE_CASES: list[tuple[str, Callable[[GitHubClient], Any], str, str, Any]] = [
    (
        "create_issue_comment",
        lambda c: c.create_issue_comment(_REPO, 12, "hello"),
        "POST",
        f"/repos/{_REPO}/issues/12/comments",
        {"body": "hello"},
    ),
    (
        "update_issue_comment",
        lambda c: c.update_issue_comment(_REPO, 777, "edited"),
        "PATCH",
        f"/repos/{_REPO}/issues/comments/777",
        {"body": "edited"},
    ),
    (
        "create_commit_comment",
        lambda c: c.create_commit_comment(_REPO, "abc123", "on commit"),
        "POST",
        f"/repos/{_REPO}/commits/abc123/comments",
        {"body": "on commit"},
    ),
    (
        "update_commit_comment",
        lambda c: c.update_commit_comment(_REPO, 55, "commit edit"),
        "PATCH",
        f"/repos/{_REPO}/comments/55",
        {"body": "commit edit"},
    ),
    (
        "create_issue",
        lambda c: c.create_issue(
            _REPO, "CI failure", "details", labels=["ci-rca"], assignees=["octocat"]
        ),
        "POST",
        f"/repos/{_REPO}/issues",
        {
            "title": "CI failure",
            "body": "details",
            "labels": ["ci-rca"],
            "assignees": ["octocat"],
        },
    ),
    (
        "create_issue_minimal",
        lambda c: c.create_issue(_REPO, "t", "b"),
        "POST",
        f"/repos/{_REPO}/issues",
        {"title": "t", "body": "b"},
    ),
    (
        "update_issue",
        lambda c: c.update_issue(_REPO, 9, body="new", state="closed", labels=["a"]),
        "PATCH",
        f"/repos/{_REPO}/issues/9",
        {"body": "new", "state": "closed", "labels": ["a"]},
    ),
    (
        "add_labels",
        lambda c: c.add_labels(_REPO, 9, ["ci-rca", "flaky"]),
        "POST",
        f"/repos/{_REPO}/issues/9/labels",
        {"labels": ["ci-rca", "flaky"]},
    ),
    (
        "create_reaction",
        lambda c: c.create_reaction(_REPO, 321, "+1"),
        "POST",
        f"/repos/{_REPO}/issues/comments/321/reactions",
        {"content": "+1"},
    ),
]


@pytest.mark.parametrize(
    ("call", "method", "path", "body"),
    [case[1:] for case in _WRITE_CASES],
    ids=[case[0] for case in _WRITE_CASES],
)
def test_write_methods_send_method_path_and_json(
    call: Callable[[GitHubClient], Any], method: str, path: str, body: Any
) -> None:
    seen, handler = _recorder(status=201, payload={"id": 1})
    with _client(handler) as client:
        call(client)
    (req,) = seen
    assert req.method == method
    assert req.url.path == path
    assert req.headers["content-type"] == "application/json"
    assert req.headers["Authorization"] == "Bearer test-token"
    assert _body(req) == body


def test_write_methods_return_parsed_payload() -> None:
    _seen, handler = _recorder(status=201, payload={"id": 42, "html_url": "x"})
    with _client(handler) as client:
        assert client.create_issue_comment(_REPO, 1, "b")["id"] == 42
        assert client.create_issue(_REPO, "t", "b")["id"] == 42


def test_add_labels_wraps_list_response() -> None:
    _seen, handler = _recorder(payload=[{"name": "ci-rca"}])
    with _client(handler) as client:
        assert client.add_labels(_REPO, 1, ["ci-rca"]) == {"labels": [{"name": "ci-rca"}]}


def test_create_reaction_sends_reactions_accept_header() -> None:
    seen, handler = _recorder(status=201, payload={"id": 1, "content": "+1"})
    with _client(handler) as client:
        assert client.create_reaction(_REPO, 1, "+1") == {"id": 1, "content": "+1"}
    assert "squirrel-girl-preview" in seen[0].headers["Accept"]


# ---- AC 4: duplicate safety ----------------------------------------------

_CREATE_CALLS: list[tuple[str, Callable[[GitHubClient], Any]]] = [
    ("create_issue_comment", lambda c: c.create_issue_comment(_REPO, 1, "b")),
    ("create_commit_comment", lambda c: c.create_commit_comment(_REPO, "abc", "b")),
    ("create_issue", lambda c: c.create_issue(_REPO, "t", "b")),
]


def _timeout_handler(calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("read timed out", request=request)

    return handler


@pytest.mark.parametrize(
    "call", [c[1] for c in _CREATE_CALLS], ids=[c[0] for c in _CREATE_CALLS]
)
def test_create_timeout_does_not_retry(call: Callable[[GitHubClient], Any]) -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []
    with _client(_timeout_handler(calls), sleeps) as client:
        with pytest.raises(GitHubAPIError, match="timeout"):
            call(client)
    assert len(calls) == 1
    assert sleeps == []


def test_create_remote_disconnect_does_not_retry() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    with _client(handler) as client, pytest.raises(GitHubAPIError):
        client.create_issue(_REPO, "t", "b")
    assert len(calls) == 1


def test_create_reaction_timeout_does_not_retry_and_returns_empty() -> None:
    calls: list[httpx.Request] = []
    with _client(_timeout_handler(calls)) as client:
        assert client.create_reaction(_REPO, 1, "+1") == {}
    assert len(calls) == 1


def test_get_timeout_still_retries() -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []
    with _client(_timeout_handler(calls), sleeps) as client:
        with pytest.raises(GitHubAPIError, match="timeout"):
            client.get_repo(_REPO)
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_idempotent_patch_timeout_retries_then_succeeds() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"id": 7})

    with _client(handler) as client:
        assert client.update_issue_comment(_REPO, 7, "b")["id"] == 7
    assert len(calls) == 2


def test_create_connect_error_is_retried_since_nothing_was_sent() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(201, json={"id": 1})

    with _client(handler) as client:
        assert client.create_issue_comment(_REPO, 1, "b")["id"] == 1
    assert len(calls) == 2


# ---- AC 5: 5xx still retries ---------------------------------------------


def test_create_5xx_retries_then_raises_with_status() -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(502, text="bad gateway")

    with _client(handler, sleeps) as client, pytest.raises(GitHubAPIError) as info:
        client.create_issue_comment(_REPO, 1, "b")
    assert info.value.status_code == 502
    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert all(_body(c) == {"body": "b"} for c in calls)


# ---- AC 6: 403 surfaces --------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.create_issue_comment(_REPO, 1, "b"),
        lambda c: c.update_issue(_REPO, 1, state="closed"),
        lambda c: c.list_issue_comments(_REPO, 1),
        lambda c: c.get_repo(_REPO),
    ],
    ids=["create_issue_comment", "update_issue", "list_issue_comments", "get_repo"],
)
def test_403_raises_and_is_not_swallowed(call: Callable[[GitHubClient], Any]) -> None:
    _seen, handler = _recorder(
        status=403, payload={"message": "Resource not accessible by integration"}
    )
    with _client(handler) as client, pytest.raises(GitHubAPIError) as info:
        call(client)
    assert info.value.status_code == 403


def test_422_surfaces_as_error_with_status() -> None:
    _seen, handler = _recorder(status=422, payload={"message": "Validation Failed"})
    with _client(handler) as client, pytest.raises(GitHubAPIError) as info:
        client.create_issue(_REPO, "t", "b")
    assert info.value.status_code == 422


def test_error_messages_do_not_leak_token() -> None:
    _seen, handler = _recorder(status=404)
    with _client(handler) as client, pytest.raises(GitHubAPIError) as info:
        client.create_issue_comment(_REPO, 1, "b")
    assert "test-token" not in str(info.value)


# ---- AC 7: best-effort reaction ------------------------------------------


@pytest.mark.parametrize("status", [403, 404, 422, 500])
def test_create_reaction_non_2xx_returns_empty(status: int) -> None:
    _seen, handler = _recorder(status=status, payload={"message": "nope"})
    with _client(handler) as client:
        assert client.create_reaction(_REPO, 1, "+1") == {}


# ---- AC 8: pagination -----------------------------------------------------

_LIST_CASES: list[tuple[str, Callable[[GitHubClient], Any], str]] = [
    ("list_issue_comments", lambda c: c.list_issue_comments(_REPO, 4), f"/repos/{_REPO}/issues/4/comments"),
    ("list_commit_comments", lambda c: c.list_commit_comments(_REPO, "abc"), f"/repos/{_REPO}/commits/abc/comments"),
    ("list_issues", lambda c: c.list_issues(_REPO), f"/repos/{_REPO}/issues"),
]


@pytest.mark.parametrize(
    ("call", "path"), [c[1:] for c in _LIST_CASES], ids=[c[0] for c in _LIST_CASES]
)
def test_list_methods_follow_next_link(
    call: Callable[[GitHubClient], Any], path: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == path
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 2}, {"id": 3}])
        nxt = request.url.copy_merge_params({"page": "2"})
        return httpx.Response(
            200, json=[{"id": 1}], headers={"Link": f'<{nxt}>; rel="next"'}
        )

    with _client(handler) as client:
        items = call(client)
    assert [i["id"] for i in items] == [1, 2, 3]
    assert len(seen) == 2
    assert all(r.method == "GET" and r.content == b"" for r in seen)


def test_list_pagination_is_capped() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        nxt = request.url.copy_set_param("page", str(len(seen) + 1))
        return httpx.Response(200, json=[{"id": len(seen)}], headers={"Link": f'<{nxt}>; rel="next"'})

    with _client(handler) as client:
        items = client.list_issue_comments(_REPO, 1)
    assert len(seen) == 10
    assert len(items) == 10


def test_list_issues_query_params() -> None:
    seen, handler = _recorder(payload=[])
    with _client(handler) as client:
        client.list_issues(_REPO, labels=["ci-rca", "fp:abc"], state="all")
    params = seen[0].url.params
    assert params["state"] == "all"
    assert params["labels"] == "ci-rca,fp:abc"
    assert params["per_page"] == "100"


# ---- AC 9: enterprise -----------------------------------------------------


def test_enterprise_api_url_is_used_for_writes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1})

    ghes = "https://ghe.example.internal/api/v3"
    with _client(handler, api_url=ghes) as client:
        client.get_repo(_REPO)
        client.create_issue_comment(_REPO, 2, "b")
        client.create_issue(_REPO, "t", "b")
        client.list_issues(_REPO)
    assert seen
    for req in seen:
        assert req.url.host == "ghe.example.internal"
        assert req.url.path.startswith("/api/v3/repos/")


def test_no_hardcoded_public_api_host_in_github_api() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "tools" / "rca" / "github_api.py"
    ).read_text(encoding="utf-8")
    assert not re.search(r"api\.github\.com", source)
