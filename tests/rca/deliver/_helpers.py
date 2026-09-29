"""Shared builders for Step 13 delivery-decision tests. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from tests.rca.test_diagnose import _summary
from tools.rca.models import ChangeContext, HistoryContext, Summary

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
REPO = "acme/widgets"


def make_summary(
    *,
    event: str = "push",
    branch: str = "main",
    pr_number: int | None = None,
    is_fork: bool = False,
    draft: bool | None = None,
    seen_count: int = 0,
    is_flaky: bool = False,
    short_circuit: str | None = None,
    confidence: str = "high",
    run_id: int = 100,
    workflow_name: str = "CI",
    history: HistoryContext | None = None,
) -> Summary:
    summary = _summary(confidence=confidence, is_flaky=is_flaky, short_circuit=short_circuit)
    run = summary.run
    run.event = event
    run.head_branch = branch
    run.pr_number = pr_number
    run.is_fork = is_fork
    run.run_id = run_id
    run.workflow_name = workflow_name
    run.actor = "octocat"
    run.head_sha = "f" * 40
    if draft is not None:
        summary.changes = ChangeContext(head_sha=run.head_sha, pr_is_draft=draft)
    summary.history = history or HistoryContext(seen_count=seen_count)
    return summary


def ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def run_row(
    run_id: int,
    conclusion: str,
    minutes_ago: float,
    *,
    sha: str | None = None,
    name: str = "CI",
    workflow_id: int = 7,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "conclusion": conclusion,
        "created_at": ago(minutes_ago),
        "head_sha": sha or f"sha{run_id}",
        "name": name,
        "workflow_id": workflow_id,
    }


class FakeClient:
    """Implements targets.ReadClient. Any configured exception is raised on call."""

    def __init__(
        self,
        *,
        default_branch: str | None = "main",
        repo_error: Exception | None = None,
        tags: set[str] | None = None,
        tag_error: Exception | None = None,
        runs: list[dict[str, Any]] | None = None,
        runs_error: Exception | None = None,
        workflow_id: int | None = 7,
        run_error: Exception | None = None,
    ) -> None:
        self.default_branch = default_branch
        self.repo_error = repo_error
        self.tags = tags or set()
        self.tag_error = tag_error
        self.runs = runs or []
        self.runs_error = runs_error
        self.workflow_id = workflow_id
        self.run_error = run_error
        self.calls: list[tuple[str, Any]] = []

    def get_repo(self, repo: str) -> dict[str, Any]:
        self.calls.append(("get_repo", repo))
        if self.repo_error:
            raise self.repo_error
        return {} if self.default_branch is None else {"default_branch": self.default_branch}

    def get_run(self, repo: str, run_id: int) -> dict[str, Any]:
        self.calls.append(("get_run", run_id))
        if self.run_error:
            raise self.run_error
        return {"id": run_id, "workflow_id": self.workflow_id}

    def list_runs(self, repo: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_runs", kwargs))
        if self.runs_error:
            raise self.runs_error
        rows = list(self.runs)
        if kwargs.get("workflow_id") is not None:
            rows = [r for r in rows if r.get("workflow_id") == kwargs["workflow_id"]]
        return rows

    def ref_is_tag(self, repo: str, name: str) -> bool:
        self.calls.append(("ref_is_tag", name))
        if self.tag_error:
            raise self.tag_error
        return name in self.tags
