"""Commit comment for default-branch / tag / opted-in branch pushes (v1.3 §4.1, §4.2).

Key: (sha, fingerprint_coarse). A 403 means the token lacks ``contents: write``.
Shared find / write logic is in ``sticky``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import ExistingComment
from .sticky import DeliveryBudget, WriteResult, find_existing as _find, post_or_update as _post


def find_existing(
    client: Any, repo: str, sha: str, *, fingerprint_coarse: str, branch: str
) -> tuple[ExistingComment | None, list[str]]:
    return _find(
        client,
        repo,
        target_kind="commit",
        target=sha,
        fingerprint_coarse=fingerprint_coarse,
        branch=branch,
    )


def post_or_update(
    client: Any,
    repo: str,
    sha: str,
    body: str,
    existing: ExistingComment | None,
    *,
    clock: Callable[[], float],
    budget: DeliveryBudget,
) -> WriteResult:
    return _post(
        client, repo, sha, body, existing, target_kind="commit", clock=clock, budget=budget
    )


__all__ = ["find_existing", "post_or_update"]
