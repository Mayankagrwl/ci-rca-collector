"""GitHub Actions cache-key sanitization.

actions/cache keys must match ^[a-zA-Z0-9!#%'()+,-./<=>@_[{}~]+$
Spaces, colons, and newlines in workflow names (e.g. "Build Api-gateway")
yield HTTP 400 and the restore never hits.

History must key on repository + workflow, not run-id: a run-id-only key
never restores on the next failure of the same workflow. actions/cache
is still a ~7 day LRU.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_ILLEGAL = re.compile(r"[^a-z0-9._-]+")
_HYPHENS = re.compile(r"-{2,}")
_CACHE_KEY_SAFE = re.compile(r"^[a-zA-Z0-9!#%'()+,\-./<=>@_[{}~]+$")


def cache_key_part(s: str | None, max_len: int = 80) -> str:
    """Lowercase, hyphenate whitespace, strip illegal cache-key characters."""
    text = (s or "unknown").strip().lower()
    text = _WS.sub("-", text)
    text = _ILLEGAL.sub("-", text)
    text = _HYPHENS.sub("-", text).strip("-")
    text = text[:max_len] or "unknown"
    return text


def drain_cache_key(workflow_name: str | None, job_name: str | None = None) -> str:
    """drain3-{sanitized_workflow}-{sanitized_job}."""
    wf = cache_key_part(workflow_name)
    job = cache_key_part(job_name or "job")
    return f"drain3-{wf}-{job}"


def drain_restore_key(workflow_name: str | None) -> str:
    """Prefix restore-key: drain3-{sanitized_workflow}-"""
    return f"drain3-{cache_key_part(workflow_name)}-"


def history_cache_key(repository: str | None, workflow_name: str | None) -> str:
    """rca-history-{owner-repo}-{sanitized_workflow}."""
    repo = cache_key_part(repository)
    wf = cache_key_part(workflow_name)
    return f"rca-history-{repo}-{wf}"


def history_restore_keys(repository: str | None) -> list[str]:
    """Restore prefixes that hit across workflows of the same repo, then any."""
    repo = cache_key_part(repository)
    return [f"rca-history-{repo}-", "rca-history-"]


def is_legal_cache_key(key: str) -> bool:
    return bool(key) and _CACHE_KEY_SAFE.match(key) is not None
