"""Sticky PR comment (v1.3 §4.1, §7.3). Key: (pr_number, fingerprint_coarse).

PR comments live on the issue-comments API. A 403 means the token lacks
``pull-requests: write``. Shared find / write logic is in ``sticky``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import ExistingComment
from .sticky import DeliveryBudget, WriteResult, find_existing as _find, post_or_update as _post


def find_existing(
    client: Any, repo: str, pr_number: int, *, fingerprint_coarse: str, branch: str
) -> tuple[ExistingComment | None, list[str]]:
    return _find(
        client,
        repo,
        target_kind="pr",
        target=pr_number,
        fingerprint_coarse=fingerprint_coarse,
        branch=branch,
    )


def post_or_update(
    client: Any,
    repo: str,
    pr_number: int,
    body: str,
    existing: ExistingComment | None,
    *,
    clock: Callable[[], float],
    budget: DeliveryBudget,
) -> WriteResult:
    return _post(
        client, repo, pr_number, body, existing, target_kind="pr", clock=clock, budget=budget
    )


__all__ = ["find_existing", "post_or_update"]
