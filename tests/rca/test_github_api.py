"""GitHub REST client — host/token from config; tests use github.com."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tools.rca.github_api import GitHubAPIError, GitHubClient

_HOST_ENV = (
    "RCA_GITHUB_API_URL",
    "GITHUB_API_URL",
    "GITHUB_SERVER_URL",
    "GH_HOST",
    "RCA_GITHUB_HOST",
)
_TOKEN_ENV = (
    "RCA_GITHUB_TOKEN",
    "COMMON_ACTIONS_PAT",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
_SSL_ENV = (
    "RCA_SSL_CERT_FILE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "RCA_SSL_VERIFY",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HOST_ENV + _TOKEN_ENV + _SSL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


def test_client_uses_resolved_github_com_api() -> None:
    sleeps: list[float] = []
    client = GitHubClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
        sleep=sleeps.append,
    )
    try:
        assert client.api_url == "https://api.github.com"
        source = (
            Path(__file__).resolve().parents[2] / "tools" / "rca" / "github_api.py"
        ).read_text(encoding="utf-8")
        assert "api.github.com" not in source
    finally:
        client.close()


def test_get_run_sends_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"id": 99, "name": "CI"},
            headers={"x-ratelimit-remaining": "900"},
        )

    with GitHubClient(transport=httpx.MockTransport(handler), sleep=lambda _d: None) as client:
        run = client.get_run("acme/widgets", 99)
    assert run["id"] == 99
    assert str(seen[0].url) == "https://api.github.com/repos/acme/widgets/actions/runs/99"
    assert seen[0].headers["Authorization"] == "Bearer test-token"
    assert seen[0].headers["Accept"] == "application/vnd.github+json"
    assert client.rate_limit_remaining == 900


def test_list_jobs_paginates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return httpx.Response(200, json={"jobs": [{"id": 2}]})
        return httpx.Response(
            200,
            json={"jobs": [{"id": 1}]},
            headers={
                "Link": '<https://api.github.com/repos/acme/widgets/actions/runs/9/jobs?per_page=100&page=2>; rel="next"'
            },
        )

    with GitHubClient(transport=httpx.MockTransport(handler), sleep=lambda _d: None) as client:
        jobs = client.list_jobs("acme/widgets", 9)
    assert [j["id"] for j in jobs] == [1, 2]


def test_job_log_follows_redirect_without_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/logs"):
            assert request.headers["Authorization"] == "Bearer test-token"
            return httpx.Response(
                302,
                headers={"Location": "https://pipelines.example.invalid/raw-log"},
            )
        assert request.url.host == "pipelines.example.invalid"
        assert request.headers.get("Authorization") is None
        return httpx.Response(200, text="the log")

    with GitHubClient(transport=httpx.MockTransport(handler), sleep=lambda _d: None) as client:
        text = client.get_job_log("acme/widgets", 7)
    assert text == "the log"


def test_retries_5xx_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="nope")
        return httpx.Response(200, json={"id": 1})

    with GitHubClient(transport=httpx.MockTransport(handler), sleep=sleeps.append) as client:
        run = client.get_run("acme/widgets", 1)
    assert run["id"] == 1
    assert calls["n"] == 3
    assert sleeps == [1, 2]


def test_rate_limit_exhausted_does_not_retry() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"},
        )

    with GitHubClient(transport=httpx.MockTransport(handler), sleep=sleeps.append) as client:
        with pytest.raises(GitHubAPIError, match="rate limit exhausted"):
            client.get_run("acme/widgets", 1)
    assert calls["n"] == 1
    assert sleeps == []
    assert client.optional_collection_allowed is False


def test_ssl_verify_defaults_true() -> None:
    with GitHubClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
        sleep=lambda _d: None,
    ) as client:
        assert client.verify is True


def test_ssl_cert_file_env_sets_verify_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("RCA_SSL_CERT_FILE", str(bundle))
    monkeypatch.setenv("RCA_SSL_VERIFY", "false")
    with GitHubClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
        sleep=lambda _d: None,
    ) as client:
        assert client.verify == str(bundle)


def test_ssl_verify_false_notes_api_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCA_SSL_VERIFY", "false")
    monkeypatch.setenv("RCA_GITHUB_API_URL", "https://ghes.example.invalid/api/v3")
    with GitHubClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"id": 1})),
        sleep=lambda _d: None,
    ) as client:
        assert client.verify is False
        assert client.api_url == "https://ghes.example.invalid/api/v3"
        blob = " ".join(client.collection_notes)
        assert "https://ghes.example.invalid/api/v3" in blob
        assert "TLS verification disabled" in blob


def test_ssl_error_includes_api_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCA_GITHUB_API_URL", "https://ghes.example.invalid/api/v3")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self-signed certificate in certificate chain",
            request=request,
        )

    with GitHubClient(transport=httpx.MockTransport(handler), sleep=lambda _d: None) as client:
        with pytest.raises(GitHubAPIError, match="ghes.example.invalid/api/v3") as caught:
            client.get_run("acme/widgets", 1)
        assert "SSL" in str(caught.value)
        assert any("ghes.example.invalid/api/v3" in note for note in client.collection_notes)
