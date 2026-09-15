"""Host and token resolution (AGENTS.md)."""

from __future__ import annotations

import pytest

from tools.rca.config import (
    STGPT_API_URL,
    STGPT_CLIENT_APP_NAME,
    resolve_github_api_url,
    resolve_github_server_url,
    resolve_github_token,
    resolve_ssl_verify,
    resolve_stgpt_api_key,
    resolve_stgpt_api_url,
    resolve_stgpt_client_app_name,
)
from tools.rca.config_host import (
    resolve_github_api_url as host_resolve_api_url,
    resolve_github_token as host_resolve_token,
)

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
_STGPT_ENV = (
    "STGPT_API",
    "STGPT_API_URL",
    "STGPT_CLIENT_APP_NAME",
    "CLIENT_APP_NAME",
    "API_KEY",
    "API_URL",
)


@pytest.fixture(autouse=True)
def _clear_host_and_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HOST_ENV + _TOKEN_ENV + _SSL_ENV + _STGPT_ENV:
        monkeypatch.delenv(name, raising=False)


def test_default_api_url_is_github_com() -> None:
    assert resolve_github_api_url() == "https://api.github.com"
    assert host_resolve_api_url() == "https://api.github.com"
    assert resolve_github_server_url() == "https://github.com"


def test_github_api_url_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_API_URL", "https://github.example.com/api/v3")
    assert resolve_github_api_url() == "https://github.example.com/api/v3"


def test_ghes_host_derives_api_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_HOST", "github.st.com")
    assert resolve_github_api_url() == "https://github.st.com/api/v3"

    monkeypatch.delenv("GH_HOST")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.st.com")
    assert resolve_github_api_url() == "https://github.st.com/api/v3"
    assert resolve_github_server_url() == "https://github.st.com"


def test_common_actions_pat_wins_over_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "pat-from-common")
    monkeypatch.setenv("GITHUB_TOKEN", "pat-from-github")
    assert resolve_github_token() == "pat-from-common"
    assert host_resolve_token() == "pat-from-common"


def test_ssl_verify_default_true() -> None:
    assert resolve_ssl_verify() is True


def test_ssl_cert_file_wins_over_verify_false(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = tmp_path / "ca.pem"
    pem.write_text("ca")
    monkeypatch.setenv("SSL_CERT_FILE", str(pem))
    monkeypatch.setenv("RCA_SSL_VERIFY", "false")
    assert resolve_ssl_verify() == str(pem)


def test_ssl_verify_false_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCA_SSL_VERIFY", "false")
    assert resolve_ssl_verify() is False
    monkeypatch.setenv("RCA_SSL_VERIFY", "true")
    assert resolve_ssl_verify() is True


def test_stgpt_api_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STGPT_API_URL", raising=False)
    assert resolve_stgpt_api_url() == STGPT_API_URL
    monkeypatch.setenv("STGPT_API_URL", "https://bridge.example.invalid/chatgpt/api/client-apps/")
    assert resolve_stgpt_api_url() == "https://bridge.example.invalid/chatgpt/api/client-apps"
    assert resolve_stgpt_api_url("https://explicit.example.invalid/") == "https://explicit.example.invalid"
    monkeypatch.delenv("STGPT_API_URL")
    monkeypatch.setenv("API_URL", "https://from-api-url.example.invalid/chatgpt/api/client-apps/")
    assert resolve_stgpt_api_url() == "https://from-api-url.example.invalid/chatgpt/api/client-apps"
    assert STGPT_CLIENT_APP_NAME not in resolve_stgpt_api_url()


def test_stgpt_client_app_name_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_stgpt_client_app_name() == "gtrd_srmtdpplm"
    assert len(resolve_stgpt_client_app_name()) == 14
    monkeypatch.setenv("STGPT_CLIENT_APP_NAME", "gtrd_srmtdpplm\n")
    assert resolve_stgpt_client_app_name() == "gtrd_srmtdpplm"
    assert len(resolve_stgpt_client_app_name()) == 14
    monkeypatch.delenv("STGPT_CLIENT_APP_NAME")
    monkeypatch.setenv("CLIENT_APP_NAME", "  gtrd_srmtdpplm  ")
    assert resolve_stgpt_client_app_name() == "gtrd_srmtdpplm"


def test_stgpt_api_key_from_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_stgpt_api_key() is None
    monkeypatch.setenv("API_KEY", "  laptop-key\n")
    assert resolve_stgpt_api_key() == "laptop-key"
    monkeypatch.setenv("STGPT_API", "stgpt-wins")
    assert resolve_stgpt_api_key() == "stgpt-wins"
