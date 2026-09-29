"""Load CODEOWNERS for delivery (thin I/O wrapper around the pure ``owners`` module).

Read at the **default branch**, never the PR head, so a fork PR cannot edit
CODEOWNERS to change who is blamed (v1.3 §11). At most one GET per location,
inside the delivery budget; never raises.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .sticky import BUDGET_EXCEEDED, DeliveryBudget

CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
CODEOWNERS_MAX_BYTES = 3 * 1024 * 1024  # GitHub ignores larger CODEOWNERS files
WORKSPACE_ENV = ("RCA_WORKSPACE", "GITHUB_WORKSPACE")


def load_codeowners(
    client: Any,
    repo: str,
    ref: str,
    *,
    budget: DeliveryBudget,
    clock: Callable[[], float],
) -> tuple[str | None, list[str]]:
    """(text, notes). The first location that exists wins, even when it is empty."""
    notes: list[str] = []
    for path in CODEOWNERS_LOCATIONS:
        if budget.exceeded(clock()):
            notes.append(f"CODEOWNERS lookup stopped: {BUDGET_EXCEEDED}")
            return None, notes
        try:
            text = client.get_file(repo, path, ref, allow_empty=True)
        except Exception as exc:  # noqa: BLE001 — never guess a later file after an error
            notes.append(f"CODEOWNERS lookup failed at {path}@{ref}: {type(exc).__name__}")
            return None, notes
        if text is None:
            continue
        if len(text.encode("utf-8")) > CODEOWNERS_MAX_BYTES:
            notes.append(f"{path}@{ref} is over 3 MB; ignored (as GitHub does)")
            return None, notes
        notes.append(f"CODEOWNERS: {path}@{ref}")
        return text, notes
    notes.append(f"no CODEOWNERS at {ref}")
    return None, notes


def resolve_workspace(env: Mapping[str, str]) -> str | None:
    for name in WORKSPACE_ENV:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


__all__ = ["CODEOWNERS_LOCATIONS", "load_codeowners", "resolve_workspace"]
