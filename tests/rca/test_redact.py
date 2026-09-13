"""Redaction must strip secrets before anything is written."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.rca.models import (
    BudgetReport,
    Classification,
    FailedJob,
    LogWindow,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.redact import REPLACEMENT, redact_summary, redact_text
from tools.rca.render import render_markdown

_SECRET = "Bearer sk-live-abc123"


def test_redact_bearer_token() -> None:
    text, count = redact_text(f"Authorization: {_SECRET}")
    assert _SECRET not in text
    assert "sk-live-abc123" not in text
    assert REPLACEMENT in text
    assert count >= 1


def test_redact_github_and_aws_keys() -> None:
    blob = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 AKIAIOSFODNN7EXAMPLE"
    text, count = redact_text(blob)
    assert "ghp_" not in text
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert count >= 2


def test_secret_never_appears_in_summary_output() -> None:
    summary = Summary(
        collector_version="0.1.0",
        collected_at=datetime.now(timezone.utc),
        run=RunMeta(
            run_id=1,
            run_attempt=1,
            workflow_name="CI",
            html_url="https://example.invalid/acme/widgets/actions/runs/1",
            event="push",
            actor="bot",
            head_sha="abc1234",
            head_branch="main",
            failed_job_total=1,
            failed_jobs_analysed=1,
        ),
        verdict=Verdict(requires_analysis=True),
        classification=Classification(
            category="unknown",
            confidence="low",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
        ),
        failed_jobs=[
            FailedJob(
                job_id=1,
                name="build",
                failed_step_name="Install",
                failed_step_number=3,
                exit_code=1,
                duration_seconds=10,
                windows=[
                    LogWindow(
                        label="first_error",
                        start_line=1,
                        end_line=1,
                        total_lines=1,
                        content=f"curl failed {_SECRET}",
                    )
                ],
            )
        ],
        fingerprint="",
        fingerprint_coarse="",
        kubernetes=None,
        budget_report=BudgetReport(),
    )
    redacted, count = redact_summary(summary)
    assert count >= 1
    dumped = redacted.model_dump_json()
    md = render_markdown(redacted)
    assert "sk-live-abc123" not in dumped
    assert _SECRET not in dumped
    assert "sk-live-abc123" not in md
    assert _SECRET not in md
    assert REPLACEMENT in dumped


def test_redact_and_budget_have_no_github_ids() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("redact.py", "budget.py"):
        source = (root / "tools" / "rca" / name).read_text(encoding="utf-8")
        assert "job_id" not in source
        assert "run_id" not in source
        assert "github_api" not in source
