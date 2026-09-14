"""ST ChatGPT bridge — SHA1 token and mocked httpx. No real network."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from tools.rca.config import (
    PERSONAS,
    PROMPT_VERSION,
    STGPT_API_URL,
    STGPT_CLIENT_APP_NAME,
    STGPT_SERVICE,
    resolve_stgpt_api_key,
)
from tools.rca.models import AnalysisCitation, AnalysisRecord, AnalysisResult
from tools.rca.stgpt_client import ChatResult, StgptError, generate_auth_token, post_chat

_SSL_ENV = (
    "RCA_SSL_CERT_FILE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "RCA_SSL_VERIFY",
    "STGPT_API",
)

_BRIDGE = "https://stgpt.test.invalid/chatgpt/api/client-apps"
_APP = "gtrd_srmtdpplm"
_KEY = "super-secret-stgpt-key"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SSL_ENV:
        monkeypatch.delenv(name, raising=False)


def test_generate_auth_token_is_sha1_hex_of_known_inputs() -> None:
    client, service, key, ts, nonce = "app", "chatgpt", "secret", "1700000000", "n1"
    raw = f"{client}_{service}_{key}_{ts}_{nonce}"
    expected = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    got = generate_auth_token(client, service, key, ts, nonce)
    assert got == expected
    assert got == "516a5e2377dab683b49958f56e4b763d876c028a"
    assert len(got) == 40
    assert all(ch in "0123456789abcdef" for ch in got)


def test_config_phase2_defaults() -> None:
    assert STGPT_API_URL == "https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps"
    assert STGPT_CLIENT_APP_NAME == "gtrd_srmtdpplm"
    assert STGPT_SERVICE == "chatgpt"
    assert PERSONAS == ("trinity_for_api", "alfred_for_api")
    assert PROMPT_VERSION == "p2.1"


def test_resolve_stgpt_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_stgpt_api_key() is None
    monkeypatch.setenv("STGPT_API", "  from-env  ")
    assert resolve_stgpt_api_key() == "from-env"
    assert resolve_stgpt_api_key("cli-wins") == "cli-wins"


def test_post_chat_extracts_completion_and_auth_headers() -> None:
    seen: list[httpx.Request] = []
    ts, nonce = "1700000000", "abcnonce"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"completion": "root cause is the lockfile", "id": "chat-99"},
        )

    result = post_chat(
        _BRIDGE,
        _KEY,
        _APP,
        "trinity_for_api",
        [{"role": "user", "content": "why did ci fail?"}],
        transport=httpx.MockTransport(handler),
        timestamp=ts,
        nonce=nonce,
    )
    assert result == ChatResult(
        200,
        {"completion": "root cause is the lockfile", "id": "chat-99"},
        "root cause is the lockfile",
        "chat-99",
    )
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{_BRIDGE}/{_APP}"
    token = generate_auth_token(_APP, STGPT_SERVICE, _KEY, ts, nonce)
    assert request.headers["stchatgpt-auth-token"] == token
    assert request.headers["stchatgpt-auth-nonce"] == nonce
    assert request.headers["stchatgpt-auth-timestamp"] == ts
    body = json.loads(request.content.decode("utf-8"))
    assert body["persona"] == "trinity_for_api"
    assert body["messages"] == [{"role": "user", "content": "why did ci fail?"}]
    assert _KEY not in request.content.decode("utf-8")
    assert "Authorization" not in request.headers


def test_post_chat_completion_missing_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x"})

    result = post_chat(
        _BRIDGE,
        _KEY,
        _APP,
        "alfred_for_api",
        [{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(handler),
        timestamp="1",
        nonce="n",
    )
    assert result.status_code == 200
    assert result.completion is None
    assert result.response_id == "x"


def test_post_chat_http_error_status_is_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "nope"})

    result = post_chat(
        _BRIDGE,
        _KEY,
        _APP,
        "trinity_for_api",
        [{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(handler),
        timestamp="1",
        nonce="n",
    )
    assert result.status_code == 500
    assert result.body == {"error": "nope"}
    assert result.completion is None


def test_post_chat_uses_ssl_cert_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("RCA_SSL_CERT_FILE", str(bundle))
    monkeypatch.setenv("RCA_SSL_VERIFY", "false")
    seen: dict[str, object] = {}
    real_client = httpx.Client

    def wrapper(*args: object, **kwargs: object) -> httpx.Client:
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", wrapper)
    post_chat(
        _BRIDGE,
        _KEY,
        _APP,
        "trinity_for_api",
        [{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"completion": "ok"})),
        timestamp="1",
        nonce="n",
    )
    assert seen["verify"] == str(bundle)


def test_post_chat_ssl_verify_false(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("RCA_SSL_VERIFY", "false")
    seen: dict[str, object] = {}
    real_client = httpx.Client

    def wrapper(*args: object, **kwargs: object) -> httpx.Client:
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", wrapper)
    with caplog.at_level(logging.WARNING):
        post_chat(
            _BRIDGE,
            _KEY,
            _APP,
            "trinity_for_api",
            [{"role": "user", "content": "hi"}],
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"completion": "ok"})
            ),
            timestamp="1",
            nonce="n",
        )
    assert seen["verify"] is False
    assert any("TLS verification disabled" in rec.message for rec in caplog.records)
    blob = " ".join(rec.message for rec in caplog.records)
    assert _KEY not in blob


def test_post_chat_does_not_log_api_key_or_token(caplog: pytest.LogCaptureFixture) -> None:
    ts, nonce = "42", "nonce-1"
    token = generate_auth_token(_APP, STGPT_SERVICE, _KEY, ts, nonce)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"completion": "ok"})

    with caplog.at_level(logging.DEBUG):
        post_chat(
            _BRIDGE,
            _KEY,
            _APP,
            "trinity_for_api",
            [{"role": "user", "content": "hi"}],
            transport=httpx.MockTransport(handler),
            timestamp=ts,
            nonce=nonce,
        )
    blob = " ".join(f"{rec.message} {rec.getMessage()}" for rec in caplog.records)
    assert _KEY not in blob
    assert token not in blob


def test_transport_error_redacts_token() -> None:
    ts, nonce = "42", "nonce-secret"
    token = generate_auth_token(_APP, STGPT_SERVICE, _KEY, ts, nonce)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom " + token, request=request)

    with pytest.raises(StgptError) as caught:
        post_chat(
            _BRIDGE,
            _KEY,
            _APP,
            "trinity_for_api",
            [{"role": "user", "content": "hi"}],
            transport=httpx.MockTransport(handler),
            timestamp=ts,
            nonce=nonce,
        )
    text = str(caught.value)
    assert token not in text
    assert _KEY not in text
    assert "stgpt.test.invalid" in text


def test_analysis_models_status_enum() -> None:
    result = AnalysisResult(
        root_cause="missing lockfile pin",
        suggested_fix="pin requests==2.32.0",
        confidence="high",
        citations=[
            AnalysisCitation(
                quote="Could not find a version that satisfies",
                source="first_error_window",
                line=12,
            )
        ],
    )
    record = AnalysisRecord(
        status="ok",
        prompt_version=PROMPT_VERSION,
        persona="trinity_for_api",
        fingerprint="abc",
        result=result,
    )
    dumped = record.model_dump()
    assert dumped["status"] == "ok"
    assert dumped["result"]["citations"][0]["source"] == "first_error_window"
    for status in (
        "ok",
        "cached",
        "gated",
        "unvalidated",
        "failed",
        "parse_error",
        "citation_invalid",
        "bridge_error",
        "unusable",
    ):
        AnalysisRecord(status=status)
    with pytest.raises(ValidationError):
        AnalysisRecord(status="success")  # type: ignore[arg-type]


def test_client_source_never_prints_secrets() -> None:
    source = Path(__file__).resolve().parents[2] / "tools" / "rca" / "stgpt_client.py"
    text = source.read_text(encoding="utf-8")
    assert "print(" not in text
    assert "api.github.com" not in text
