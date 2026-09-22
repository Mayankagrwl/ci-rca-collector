"""Scrub secrets from text and from a Summary. Source-agnostic."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .models import Summary

REPLACEMENT = "***REDACTED***"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:ghp|gho|ghs)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-(?:live|proj|svcacct)-[A-Za-z0-9_-]+"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # npm registry auth (//host/:_authToken= and //host/_authToken=)
    re.compile(r"//[^\s]*_authToken=\S+", re.IGNORECASE),
    re.compile(r"//[^\s]*_password=\S+", re.IGNORECASE),
    re.compile(r"(?i)(?:^|[\s;])_authToken\s*[:=]\s*\S+"),
    re.compile(r"(?i)(?:^|[\s;])_password\s*[:=]\s*\S+"),
    re.compile(r"(?i)(?:^|[\s;])_auth\s*[:=]\s*\S+"),
    # docker / registry / hub tokens (values and assignments)
    re.compile(
        r"(?i)(?:export\s+)?(?:DOCKER_PASSWORD|DOCKER_TOKEN|DOCKER_AUTH|"
        r"DOCKERHUB_TOKEN|GHCR_TOKEN|GITLAB_TOKEN|PYPI_TOKEN|"
        r"NPM_TOKEN|NODE_AUTH_TOKEN)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)(?:export\s+)?KUBECONFIG\s*[:=]\s*\S+"),
    re.compile(r"kubernetes\.io/service-account-token[^\s]*", re.IGNORECASE),
    re.compile(
        r"(?i)(?:client-key-data|client-certificate-data|certificate-authority-data)"
        r":\s*\S+"
    ),
    re.compile(
        r'(?i)\b(password|token|secret|api[_-]?key)\s*[:=]\s*([^\s"\']+)',
    ),
    re.compile(
        r"(?i)(?:export\s+)?[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY)"
        r"[A-Z0-9_]*\s*[:=]\s*\S+"
    ),
]


# Entropy backstop: a standalone long, high-entropy, mixed-charset token with
# no keyword. Deliberately conservative — SHAs/UUIDs, paths and ordinary
# identifiers must survive. Slashes and dots are excluded from the token so
# file paths break into short, unmatched segments.
_ENTROPY_TOKEN_RE = re.compile(r"[A-Za-z0-9+=_-]{32,}")
_HEXISH_RE = re.compile(r"^[0-9a-fA-F-]+$")
_ENTROPY_MIN = 4.0


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_like_secret(token: str) -> bool:
    if _HEXISH_RE.match(token):
        return False  # SHA / hash / UUID shown intentionally
    if token.count("_") >= 3 or token.count("-") >= 3:
        return False  # snake_case / kebab identifier
    classes = sum(
        (
            any(c.islower() for c in token),
            any(c.isupper() for c in token),
            any(c.isdigit() for c in token),
        )
    )
    if classes < 2:
        return False  # single-charset runs are almost never random secrets
    return _shannon_entropy(token) >= _ENTROPY_MIN


def _redact_high_entropy(text: str) -> tuple[str, int]:
    count = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal count
        token = match.group(0)
        if _looks_like_secret(token):
            count += 1
            return REPLACEMENT
        return token

    return _ENTROPY_TOKEN_RE.sub(_sub, text), count


def redact_text(text: str) -> tuple[str, int]:
    """Return (redacted_text, number of substitutions)."""
    total = 0
    out = text
    for compiled in _PATTERNS:
        out, n = compiled.subn(REPLACEMENT, out)
        total += n
    out, n = _redact_high_entropy(out)
    total += n
    return out, total


def _walk(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        items = []
        total = 0
        for item in value:
            redacted, n = _walk(item)
            items.append(redacted)
            total += n
        return items, total
    if isinstance(value, dict):
        mapped: dict[str, Any] = {}
        total = 0
        for key, item in value.items():
            redacted, n = _walk(item)
            mapped[key] = redacted
            total += n
        return mapped, total
    return value, 0


def redact_summary(summary: Summary) -> tuple[Summary, int]:
    payload, count = _walk(summary.model_dump(mode="json"))
    return Summary.model_validate(payload), count
