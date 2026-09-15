"""Budgets, regex tables, tunables, and GitHub host/token helpers."""

from __future__ import annotations

import os
from typing import TypedDict

from .config_host import (
    resolve_github_api_url,
    resolve_github_server_url,
    resolve_github_token,
    resolve_ssl_verify,
)

COLLECTOR_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"

# Bump in the same commit as any drain3.ini masking / DRAIN edit (§10.3).
MASKING_CONFIG_VERSION = "1"

TOKEN_BUDGET_TOTAL = 6000
SECTION_TOKEN_CAPS: dict[str, int] = {
    "metadata": 300,
    "step_table": 250,
    "annotations": 200,
    "first_error_window": 1000,
    "tail_window": 1000,
    "stack_traces": 600,
    "log_templates": 1200,
    "junit": 800,
    "change_context": 900,
    "history": 200,
}

MAX_FAILED_JOBS_ANALYSED = 3
QUEUE_SECONDS_THRESHOLD = 300
ARTIFACT_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
RATE_LIMIT_OPTIONAL_FLOOR = 50
PR_BODY_EXCERPT_CHARS = 500
MAX_COMMITS = 10
JUNIT_FAILURE_CAP = 5
FIRST_ERROR_CONTEXT_LINES = 30
TAIL_WINDOW_LINES = 150
STACK_TRACE_TOP_FRAMES = 10
STACK_TRACE_BOTTOM_FRAMES = 5

# Phase 2 — ST ChatGPT bridge (§16). Analyze CLI reads these; collect does not.
STGPT_API_URL = "https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps"
STGPT_CLIENT_APP_NAME = "gtrd_srmtdpplm"
STGPT_SERVICE = "chat"
STGPT_VERSION = "1.0"
PERSONAS = ("trinity_for_api", "alfred_for_api")
PROMPT_VERSION = "p2.1"


def resolve_stgpt_api_key(explicit: str | None = None) -> str | None:
    """Resolve the ST ChatGPT bridge key from ``STGPT_API``. Never log it."""
    if explicit and explicit.strip():
        return explicit.strip()
    value = os.environ.get("STGPT_API")
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def resolve_stgpt_api_url(explicit: str | None = None) -> str:
    """Resolve the ST ChatGPT bridge base URL. Never log secrets."""
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    value = os.environ.get("STGPT_API_URL")
    if value and value.strip():
        return value.strip().rstrip("/")
    return STGPT_API_URL


# Case-insensitive. Matched against the raw job log (Stage 1).
RUNNER_FAILURE_PATTERNS: list[str] = [
    r"The runner has received a shutdown signal",
    r"lost communication with the server",
    r"The operation was canceled",
    r"The self-hosted runner .* lost communication",
    r"Failed to initialize container",
    r"no space left on device",
    r"Error response from daemon: .*pull access denied",
    r"The job was not acquired",
    r"Received request to deprovision",
]


class ClassifyRule(TypedDict):
    category: str
    pattern: str
    confidence: str


# Ordered; first match wins. Category order: oom, timeout, disk_space, crash,
# dependency, network_dns, auth, image_pull, test_failure, compile, unknown.
# Confidence: high for exact signatures, low for generic.
CLASSIFY_RULES: list[ClassifyRule] = [
    {"category": "oom", "pattern": r"OOMKilled", "confidence": "high"},
    {"category": "oom", "pattern": r"Killed process", "confidence": "high"},
    {"category": "oom", "pattern": r"signal: killed", "confidence": "high"},
    {"category": "oom", "pattern": r"JavaScript heap out of memory", "confidence": "high"},
    {"category": "oom", "pattern": r"MemoryError", "confidence": "high"},
    {"category": "oom", "pattern": r"exit code 137", "confidence": "high"},
    {"category": "timeout", "pattern": r"timed out", "confidence": "medium"},
    {"category": "timeout", "pattern": r"context deadline exceeded", "confidence": "high"},
    {"category": "timeout", "pattern": r"ETIMEDOUT", "confidence": "high"},
    {"category": "timeout", "pattern": r"exceeded the maximum execution time", "confidence": "high"},
    {"category": "timeout", "pattern": r"The operation was canceled", "confidence": "high"},
    {"category": "timeout", "pattern": r"Terminate orphan process", "confidence": "high"},
    {"category": "disk_space", "pattern": r"no space left on device", "confidence": "high"},
    {"category": "disk_space", "pattern": r"ENOSPC", "confidence": "high"},
    {"category": "crash", "pattern": r"panic:", "confidence": "high"},
    {"category": "crash", "pattern": r"SIGSEGV", "confidence": "high"},
    {"category": "crash", "pattern": r"segmentation violation", "confidence": "high"},
    {"category": "crash", "pattern": r"Segmentation fault", "confidence": "high"},
    {"category": "crash", "pattern": r"nil pointer dereference", "confidence": "high"},
    {"category": "crash", "pattern": r"core dumped", "confidence": "high"},
    {"category": "crash", "pattern": r"fatal error:", "confidence": "high"},
    {"category": "dependency", "pattern": r"npm ERR!", "confidence": "high"},
    {"category": "dependency", "pattern": r"ERESOLVE", "confidence": "high"},
    {"category": "dependency", "pattern": r"Could not resolve dependency", "confidence": "high"},
    {"category": "dependency", "pattern": r"ModuleNotFoundError", "confidence": "high"},
    {"category": "dependency", "pattern": r"go: .* not found", "confidence": "high"},
    {"category": "dependency", "pattern": r"Could not find artifact", "confidence": "high"},
    {
        "category": "dependency",
        "pattern": r"Could not find a version that satisfies the requirement",
        "confidence": "high",
    },
    {
        "category": "dependency",
        "pattern": r"Could not find a version that satisfies",
        "confidence": "high",
    },
    {
        "category": "dependency",
        "pattern": r"No matching distribution found",
        "confidence": "high",
    },
    {
        "category": "dependency",
        "pattern": r"ERROR: No matching distribution",
        "confidence": "high",
    },
    {
        "category": "dependency",
        "pattern": r"pip: command not found",
        "confidence": "medium",
    },
    {"category": "network_dns", "pattern": r"Could not resolve host", "confidence": "high"},
    {"category": "network_dns", "pattern": r"Temporary failure in name resolution", "confidence": "high"},
    {"category": "network_dns", "pattern": r"ECONNREFUSED", "confidence": "high"},
    {"category": "network_dns", "pattern": r"Connection refused", "confidence": "high"},
    {"category": "network_dns", "pattern": r"connection reset by peer", "confidence": "high"},
    {"category": "network_dns", "pattern": r"Connection reset", "confidence": "high"},
    {"category": "network_dns", "pattern": r"EAI_AGAIN", "confidence": "high"},
    {"category": "auth", "pattern": r"401 Unauthorized", "confidence": "high"},
    {"category": "auth", "pattern": r"403 Forbidden", "confidence": "medium"},
    {"category": "auth", "pattern": r"authentication required", "confidence": "high"},
    {"category": "auth", "pattern": r"permission denied", "confidence": "medium"},
    {"category": "auth", "pattern": r"invalid credentials", "confidence": "high"},
    {"category": "image_pull", "pattern": r"ImagePullBackOff", "confidence": "high"},
    {"category": "image_pull", "pattern": r"ErrImagePull", "confidence": "high"},
    {"category": "image_pull", "pattern": r"pull access denied", "confidence": "high"},
    {"category": "image_pull", "pattern": r"manifest unknown", "confidence": "high"},
    {"category": "test_failure", "pattern": r"AssertionError", "confidence": "high"},
    {"category": "test_failure", "pattern": r"Test run failed", "confidence": "high"},
    # Case-sensitive: a bare "failed" in echo/script text is not a pytest FAILED line.
    {"category": "test_failure", "pattern": r"(?-i:\bFAILED\b)", "confidence": "medium"},
    {"category": "compile", "pattern": r"error TS", "confidence": "high"},
    {"category": "compile", "pattern": r"cannot find symbol", "confidence": "high"},
    {"category": "compile", "pattern": r"SyntaxError", "confidence": "high"},
    {"category": "compile", "pattern": r"undefined reference to", "confidence": "high"},
    # Negative lookbehind: "runtime error:" is a crash, not a compiler diagnostic.
    {"category": "compile", "pattern": r"(?<!runtime )error:", "confidence": "low"},
]

__all__ = [
    "CLASSIFY_RULES",
    "COLLECTOR_VERSION",
    "MASKING_CONFIG_VERSION",
    "MAX_FAILED_JOBS_ANALYSED",
    "PERSONAS",
    "PROMPT_VERSION",
    "QUEUE_SECONDS_THRESHOLD",
    "RUNNER_FAILURE_PATTERNS",
    "SCHEMA_VERSION",
    "SECTION_TOKEN_CAPS",
    "STGPT_API_URL",
    "STGPT_CLIENT_APP_NAME",
    "STGPT_SERVICE",
    "STGPT_VERSION",
    "TOKEN_BUDGET_TOTAL",
    "resolve_github_api_url",
    "resolve_github_server_url",
    "resolve_github_token",
    "resolve_ssl_verify",
    "resolve_stgpt_api_key",
    "resolve_stgpt_api_url",
]
