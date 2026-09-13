"""GitHub host and token resolution.

REST clients must call these helpers rather than assuming github.com.
Never log the returned token.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

_GITHUB_COM_HOSTS = frozenset({"github.com", "www.github.com"})
_DEFAULT_SERVER_URL = "https://github.com"
_DEFAULT_API_URL = "https://api.github.com"


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalise_server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = f"https://{value}"
    return value.rstrip("/")


def _host(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower()


def _api_url_from_server(server: str) -> str:
    normalised = _normalise_server_url(server)
    if _host(normalised) in _GITHUB_COM_HOSTS:
        return _DEFAULT_API_URL
    return f"{normalised}/api/v3"


def resolve_github_server_url(server_url: str | None = None) -> str:
    """Return the GitHub web/server base URL (no trailing slash)."""
    if server_url and server_url.strip():
        return _normalise_server_url(server_url)
    for name in ("GITHUB_SERVER_URL", "RCA_GITHUB_HOST", "GH_HOST"):
        value = _env(name)
        if value:
            return _normalise_server_url(value)
    return _DEFAULT_SERVER_URL


def resolve_github_api_url(api_url: str | None = None) -> str:
    """Resolve the GitHub REST API base.

    Order: explicit argument / ``--api-url``, ``RCA_GITHUB_API_URL``,
    ``GITHUB_API_URL``, derive from ``GITHUB_SERVER_URL`` / ``GH_HOST`` /
    ``RCA_GITHUB_HOST``, then github.com.
    """
    if api_url and api_url.strip():
        return api_url.strip().rstrip("/")
    for name in ("RCA_GITHUB_API_URL", "GITHUB_API_URL"):
        value = _env(name)
        if value:
            return value.rstrip("/")
    for name in ("GITHUB_SERVER_URL", "GH_HOST", "RCA_GITHUB_HOST"):
        value = _env(name)
        if value:
            return _api_url_from_server(value)
    return _DEFAULT_API_URL


def resolve_github_token(token: str | None = None) -> str | None:
    """Resolve a GitHub token. Never log the result.

    Order: explicit argument / ``--token``, ``RCA_GITHUB_TOKEN``,
    ``COMMON_ACTIONS_PAT``, ``GITHUB_TOKEN``, ``GH_TOKEN``.
    """
    if token and token.strip():
        return token.strip()
    for name in (
        "RCA_GITHUB_TOKEN",
        "COMMON_ACTIONS_PAT",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    ):
        value = _env(name)
        if value:
            return value
    return None
