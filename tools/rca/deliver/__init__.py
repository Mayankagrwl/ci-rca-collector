"""Phase 3 delivery — decision models (delivery spec v1.3).

This package module holds only pydantic models and constants so that the pure
decision modules (``severity``, ``suppress``) can import them without pulling
in any HTTP client. ``targets`` is the only module that talks to a client, and
only through its ``ReadClient`` Protocol. No module here writes to GitHub.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field, field_validator

Trigger = Literal[
    "pull_request",
    "push_default",
    "push_branch",
    "schedule",
    "dispatch",
    "tag",
    "merge_group",
    "unknown",
]
Severity = Literal["critical", "high", "normal", "low", "info"]
CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")


class DeliveryInputs(BaseModel):
    """Action inputs (v1.3 §13) with their defaults. Parsing from action inputs is Step 19."""

    deliver: bool = False
    comment_on_pr: bool = True
    comment_on_commit: bool = True
    comment_on_branch_push: bool = False
    create_issues: bool = True
    issue_threshold: int = 3
    allow_fork_issues: bool = False
    confidence_threshold: str = "medium"
    quiet_window_minutes: int = 60
    platform_team: str = ""
    default_notify: str = ""
    smtp_url: str = ""
    chat_webhook_url: str = ""

    @field_validator("confidence_threshold")
    @classmethod
    def _known_threshold(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in CONFIDENCE_LEVELS:
            raise ValueError(
                f"confidence_threshold must be one of {list(CONFIDENCE_LEVELS)}, got {value!r}"
            )
        return normalized


class DeliveryContext(BaseModel):
    """Where this failure should be delivered (v1.3 §4). Never assumes a PR or ``main``."""

    trigger: Trigger
    pr_number: int | None = None
    pr_is_draft: bool = False
    is_fork: bool = False
    branch: str
    default_branch: str
    is_default_branch: bool
    actor: str
    commit_sha: str


class DefaultBranchState(BaseModel):
    """How long / how often the default branch has been red (v1.3 §4.2)."""

    red_since: datetime | None = None
    red_duration_hours: float | None = None
    consecutive_failures: int | None = None
    last_green_sha: str | None = None
    source: Literal["run_list", "history", "none"] = "none"


class RoutePlan(BaseModel):
    """Channels this context routes to (v1.3 §4.1). Notify audiences are intent only."""

    job_summary: bool = True
    comment: Literal["pr", "commit"] | None = None
    issue: Literal["always", "on_recurrence"] | None = None
    notify: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SuppressionDecision(BaseModel):
    """Three-stage suppression outcome (v1.3 §5)."""

    suppressed_by: str | None = None  # Stage A only
    dedupe: Literal["edit", "update_quiet"] | None = None  # Stage B
    unverified_banner: bool = False  # C1
    omit_root_cause: bool = False  # C2
    add_flaky_label: bool = False
    notify_platform_once: bool = False
    reasons: list[str] = Field(default_factory=list)


class ExistingComment(NamedTuple):
    """A previously posted delivery comment, found by its marker (``deliver.sticky``)."""

    fingerprint_coarse: str
    branch: str
    updated_at: datetime
    comment_id: int | None = None
    html_url: str | None = None
    body: str = ""


__all__ = [
    "CONFIDENCE_LEVELS",
    "DefaultBranchState",
    "DeliveryContext",
    "DeliveryInputs",
    "ExistingComment",
    "RoutePlan",
    "Severity",
    "SuppressionDecision",
    "Trigger",
]
