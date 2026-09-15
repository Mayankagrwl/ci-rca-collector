"""ST ChatGPT client-apps bridge. Never log api_key or auth token."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx

from .config import STGPT_SERVICE
from .config_host import resolve_ssl_verify

_LOG = logging.getLogger(__name__)
_DEFAULT_TIMEOUT = 60.0
_REDACT = "<redacted>"


class StgptError(Exception):
    """Transport-level failure talking to the ST ChatGPT bridge."""


class ChatResult(NamedTuple):
    status_code: int
    body: dict[str, Any]
    completion: str | None
    response_id: str | None
    url: str | None = None


def generate_auth_token(client: str, service: str, key: str, ts: str | int, nonce: str) -> str:
    """Return SHA1 hex of ``f"{client}_{service}_{key}_{ts}_{nonce}"``."""
    raw = f"{client}_{service}_{key}_{ts}_{nonce}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def post_chat(
    url: str,
    api_key: str,
    client_app_name: str,
    persona: str,
    messages: Sequence[Mapping[str, str]],
    *,
    service: str = STGPT_SERVICE,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
    timestamp: str | None = None,
    nonce: str | None = None,
    verify: bool | str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> ChatResult:
    """POST a chat turn. HTTP error statuses are returned, not raised."""
    ts = timestamp if timestamp is not None else str(int(time.time()))
    nonce_value = nonce if nonce is not None else uuid.uuid4().hex
    token = generate_auth_token(client_app_name, service, api_key, ts, nonce_value)
    endpoint = url.strip().rstrip("/")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "stchatgpt-auth-token": token,
        "stchatgpt-auth-nonce": nonce_value,
        "stchatgpt-auth-timestamp": str(ts),
    }
    payload: dict[str, Any] = {}
    if extra:
        payload.update(dict(extra))
    payload["persona"] = persona
    payload["messages"] = [dict(item) for item in messages]
    payload["clientAppName"] = client_app_name

    ssl_verify = resolve_ssl_verify() if verify is None else verify
    if ssl_verify is False:
        _LOG.warning(
            "TLS verification disabled (RCA_SSL_VERIFY=false); ST ChatGPT %s",
            endpoint,
        )
    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "verify": ssl_verify,
        "follow_redirects": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        with httpx.Client(**client_kwargs) as client:
            response = client.post(endpoint, headers=headers, json=payload)
    except (httpx.HTTPError, OSError) as exc:
        raise StgptError(_public_error(exc, endpoint)) from None

    body = _json_object(response)
    completion = body.get("completion")
    if not isinstance(completion, str):
        completion = None
    response_id = _response_id(body)
    return ChatResult(response.status_code, body, completion, response_id, endpoint)


def public_request_url(url: str | None) -> str:
    """Host + path only. No query, fragment, or credentials."""
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.split("@")[-1] if parsed.netloc else ""
    path = parsed.path or ""
    if parsed.scheme and host:
        return f"{parsed.scheme}://{host}{path}"
    return f"{host}{path}" or url.split("?", 1)[0].split("#", 1)[0]


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return {"raw": text[:500]} if text else {}
    return payload if isinstance(payload, dict) else {}


def _response_id(body: Mapping[str, Any]) -> str | None:
    for key in ("id", "response_id"):
        value = body.get(key)
        if value is None or value == "":
            continue
        return str(value)
    return None


def _public_error(exc: BaseException, endpoint: str) -> str:
    host = urlparse(endpoint).netloc or endpoint
    text = str(exc)
    for secret in (_secrets_from(exc)):
        if secret:
            text = text.replace(secret, _REDACT)
    return f"ST ChatGPT bridge error talking to {host}: {text}"


def _secrets_from(exc: BaseException) -> list[str]:
    found: list[str] = []
    request = getattr(exc, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        token = headers.get("stchatgpt-auth-token")
        if token:
            found.append(token)
    return found
