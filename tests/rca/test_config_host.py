"""Host and token resolution (AGENTS.md)."""

from __future__ import annotations

import pytest

from tools.rca.config import (
    resolve_github_api_url,
    resolve_github_server_url,
    resolve_github_token,
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


@pytest.fixture(autouse=True)
def _clear_host_and_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HOST_ENV + _TOKEN_ENV:
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
