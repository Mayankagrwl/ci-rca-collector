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
TOKEN_BUDGET_ANALYZE = 3000
ANALYZE_EVIDENCE_CAP = 3000
FAILED_STEP_EXCERPT_LINES = 40
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
    "pipeline_logs": 1000,
    "code_context": 400,
    "last_green_compare": 200,
}

MAX_FAILED_JOBS_ANALYSED = 3
QUEUE_SECONDS_THRESHOLD = 300
ARTIFACT_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
MAX_PIPELINE_LOG_ARTIFACTS = 3
PIPELINE_STREAM_MAX_LINES = 20_000
PIPELINE_STREAM_MAX_BYTES = 2 * 1024 * 1024
CODE_HUNK_RADIUS = 25
CODE_HUNK_MAX_LINES = 80
CODE_HUNK_MAX_FILES = 3

# Case-insensitive artifact names. Stage group is optional.
PIPELINE_ARTIFACT_NAME_RE = (
    r"^(?:pipelines?[-_]logs?(?:[-_](?P<stage>build|unit-tests?|tests?|"
    r"integration|e2e|lint|compile|package))?|docker-logs|container-logs)$"
)
PIPELINE_STAGE_MAP: dict[str, str] = {
    "build": "build",
    "compile": "build",
    "package": "build",
    "unit-test": "test",
    "unit-tests": "test",
    "test": "test",
    "tests": "test",
    "integration": "e2e",
    "e2e": "e2e",
    "lint": "lint",
}
RATE_LIMIT_OPTIONAL_FLOOR = 50
PR_BODY_EXCERPT_CHARS = 500
MAX_COMMITS = 10
JUNIT_FAILURE_CAP = 5
# Max JUnit failures packed into the analyze evidence pack (Step 6).
JUNIT_EVIDENCE_CAP = 3

# Category-aware evidence ordering (Step 6). Each value is an ordered list of
# section keys build_evidence packs; primary_failure_line + failed_step_excerpt
# stay mandatory and first in every profile. Unlisted categories use the default
# (the "unknown / residual" full set), which is where the model needs the most.
_EVIDENCE_CODE = [
    "primary_failure_line",
    "failed_step_excerpt",
    "first_error_window",
    "stack_traces",
    "junit",
    "code_context",
    "deterministic_hint",
    "log_templates",
    "pipeline_logs",
    "change_context",
]
_EVIDENCE_DEPENDENCY = [
    "primary_failure_line",
    "failed_step_excerpt",
    "first_error_window",
    "deterministic_hint",
    "change_context",
    "log_templates",
]
_EVIDENCE_INFRA = [
    "primary_failure_line",
    "failed_step_excerpt",
    "first_error_window",
    "deterministic_hint",
]
EVIDENCE_PROFILE_DEFAULT: list[str] = list(_EVIDENCE_CODE)
EVIDENCE_PROFILES: dict[str, list[str]] = {
    "compile": list(_EVIDENCE_CODE),
    "crash": list(_EVIDENCE_CODE),
    "test_failure": list(_EVIDENCE_CODE),
    "dependency": list(_EVIDENCE_DEPENDENCY),
    "oom": list(_EVIDENCE_INFRA),
    "timeout": list(_EVIDENCE_INFRA),
    "disk_space": list(_EVIDENCE_INFRA),
    "image_pull": list(_EVIDENCE_INFRA),
    "auth": list(_EVIDENCE_INFRA),
    "network_dns": list(_EVIDENCE_INFRA),
    "infra_runner": list(_EVIDENCE_INFRA),
}
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
PROMPT_VERSION = "p2.4"


def _strip_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def resolve_stgpt_api_key(explicit: str | None = None) -> str | None:
    """Resolve the ST ChatGPT bridge key. Never log it.

    Order: explicit argument, ``STGPT_API``, ``API_KEY``.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    return _strip_env("STGPT_API", "API_KEY")


def resolve_stgpt_api_url(explicit: str | None = None) -> str:
    """Resolve the ST ChatGPT bridge base URL. Never append clientAppName."""
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    value = _strip_env("STGPT_API_URL", "API_URL")
    if value:
        return value.rstrip("/")
    return STGPT_API_URL.rstrip("/")


def resolve_stgpt_client_app_name(explicit: str | None = None) -> str:
    """Resolve clientAppName. Strip surrounding whitespace/newlines."""
    if explicit and explicit.strip():
        return explicit.strip()
    return _strip_env("STGPT_CLIENT_APP_NAME", "CLIENT_APP_NAME") or STGPT_CLIENT_APP_NAME


# Case-insensitive regex tables driving Step 3 terminal-cause / symptom demotion.
# Extend by editing these lists, never by branching in code.
#
# TERMINAL_CAUSE_PATTERNS: a line that ALONE explains the exit. Seeded as a
# superset of extract._SEMANTIC_CAUSE and diagnose._SPECIFIC_CAUSE_RE markers
# (kept working there) plus quality-gate / compile / dependency shapes.
TERMINAL_CAUSE_PATTERNS: list[str] = [
    # "already exists" only in a publish/version failure context — never the
    # bare Docker layer line "<hex> Already exists 0B".
    r"(?:release|version|tag|artifact|image|package)\b[^\n]*already exists",
    r"already exists[^\n]*(?:you need to update|update (?:the )?package|overwrite|on\s+\w+)",
    r"version exists",
    r"must update",
    r"you need to update",
    r"ERESOLVE",
    r"No matching distribution",
    r"Could not find a version",
    r"ModuleNotFoundError",
    r"Cannot find module",
    r"error TS\d+",
    r"cannot find symbol",
    r"AssertionError",
    r"\bFAILED\s+\S+",
    r"ENOSPC",
    r"quality gate (?:failed|not passed)",
    r"coverage .*(?:below|threshold|did not meet)",
]

# BENIGN_LINE_PATTERNS: normal/informational output that must NEVER be treated
# as a cause (terminal cause, primary_failure_line, first-error anchor, or
# citation) — even when it also matches a cause pattern. Benign takes precedence.
BENIGN_LINE_PATTERNS: list[str] = [
    # Docker pull progress: a leading layer id, then a docker status word.
    # Anchored so "ERROR <hex> ... downloading" (a real error) is not swallowed.
    r"^\s*[0-9a-f]{6,}:?\s+(?:already exists|pull complete|pulling fs layer|"
    r"waiting|downloading|download complete|verifying checksum|extracting|retrying)\b",
    # "Already exists 0B" style progress (status followed by a byte size).
    r"\balready exists\b[^\n]*\b\d+(?:\.\d+)?\s*[kmgt]?i?b\b",
    # A line that is itself only a docker progress status (status + a progress
    # bar / size / nothing) — not a sentence like "Downloading failed: ...".
    r"^\s*(?:already exists|pull complete|pulling fs layer|downloading|"
    r"download complete|verifying checksum|extracting|waiting|retrying)\b"
    r"(?:\s*$|\s*\[|\s+\d)",
    r"\bdigest:\s*sha256:",  # image digest line
    r"\bstatus:\s*(?:image is up to date|downloaded newer image)",  # pull status
    r"\bimage is up to date\b",  # nothing to pull
    r"\bloaded image(?:\s+id)?:",  # docker load output
    r"\blevel=warning\b",  # structured warning (not an error level)
    r"\bno services to build\b",  # compose orchestration echo
    # Setup echo "Using <name> service: …" — anchored to line start so a real
    # error that merely contains "using … service" is not marked benign.
    r"^\s*using\b[^\n]*\bservice\b\s*:?",
    r"\bexited with code 0\b",  # normal container/process completion
]

# SYMPTOM_PATTERNS: follow-on lines that must never be the root cause when a
# terminal cause precedes them (permission-to-delete/overwrite, 401/403 on
# upload, connection reset during teardown, cleanup / container-stop noise).
SYMPTOM_PATTERNS: list[str] = [
    r"not enough permissions to (?:delete|overwrite|update|remove)",
    r"(?:401|403)\b.*(?:upload|push|publish|delete|overwrite)",
    r"connection reset",
    r"Post job cleanup",
    r"Cleaning up orphan processes",
    r"Terminate orphan process",
    r"\bStopping\b",
    r"exited with code 0",
    r"Removing (?:network|container|volume)",
]

# Docker sub-step name tokens → coarse pipeline phase. Anything unmatched is
# treated as "run"; an empty/odd name yields phase None (see pipeline_logs).
PIPELINE_PHASE_TOKENS: dict[str, str] = {
    "up": "setup",
    "start": "setup",
    "flyway": "setup",
    "migrate": "setup",
    "seed": "setup",
    "init": "setup",
    "down": "teardown",
    "stop": "teardown",
    "rm": "teardown",
    "cleanup": "teardown",
    "teardown": "teardown",
    "prune": "teardown",
}


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
    {"category": "oom", "pattern": r"heap out of memory", "confidence": "high"},
    {"category": "oom", "pattern": r"Cannot allocate memory", "confidence": "high"},
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
    {"category": "dependency", "pattern": r"ResolutionImpossible", "confidence": "high"},
    {"category": "dependency", "pattern": r"Cannot find module", "confidence": "high"},
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
    "TOKEN_BUDGET_ANALYZE",
    "ANALYZE_EVIDENCE_CAP",
    "FAILED_STEP_EXCERPT_LINES",
    "MAX_PIPELINE_LOG_ARTIFACTS",
    "PIPELINE_ARTIFACT_NAME_RE",
    "PIPELINE_PHASE_TOKENS",
    "PIPELINE_STAGE_MAP",
    "SYMPTOM_PATTERNS",
    "TERMINAL_CAUSE_PATTERNS",
    "PIPELINE_STREAM_MAX_BYTES",
    "PIPELINE_STREAM_MAX_LINES",
    "CODE_HUNK_MAX_FILES",
    "CODE_HUNK_MAX_LINES",
    "CODE_HUNK_RADIUS",
    "BENIGN_LINE_PATTERNS",
    "EVIDENCE_PROFILES",
    "EVIDENCE_PROFILE_DEFAULT",
    "JUNIT_EVIDENCE_CAP",
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
    "resolve_stgpt_client_app_name",
]
