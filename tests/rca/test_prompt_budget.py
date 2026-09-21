"""Analyze evidence packet stays within the analyze cap and prefers the failed step."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.rca.budget import token_count
from tools.rca.cli import main
from tools.rca.config import ANALYZE_EVIDENCE_CAP, TOKEN_BUDGET_ANALYZE
from tools.rca.models import (
    BudgetReport,
    ChangeContext,
    Classification,
    FailedJob,
    FailedStepExcerpt,
    LogTemplate,
    LogWindow,
    PipelineLogStream,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.prompt import build_evidence

_NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
_VERSION = "This release already exists on Artifactory. You need to update package.json"
_DELETE = "Not enough permissions to delete the Artifactory item"


def test_analyze_evidence_cap_on_noisy_fixture(tmp_path: Path) -> None:
    from tests.rca.test_cli import _write_noisy_fixture

    fixture = _write_noisy_fixture(tmp_path / "noisy")
    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--from-fixture",
            str(fixture),
            "--out",
            str(out),
            "--drain-dir",
            str(tmp_path / "drain"),
            "--history-backend",
            "none",
        ]
    )
    assert rc == 0
    summary = Summary.model_validate_json((out / "summary.json").read_text(encoding="utf-8"))
    evidence = build_evidence(summary)
    assert evidence.startswith("<EVIDENCE>")
    assert "DETERMINISTIC_HINT:" in evidence
    assert token_count(evidence) <= TOKEN_BUDGET_ANALYZE + 20
    assert token_count(evidence) <= ANALYZE_EVIDENCE_CAP + 20
    assert "### step_table" not in evidence
    assert "### tail_window" not in evidence


def _window(text: str, label: str = "first_error") -> LogWindow:
    rows = text.splitlines() or [text]
    return LogWindow(
        label=label,  # type: ignore[arg-type]
        start_line=1,
        end_line=len(rows),
        total_lines=len(rows),
        content=text,
    )


def _artifactory_summary(
    *,
    step: str = "Check Version in Artifactory",
    pipeline_text: str = _DELETE,
    pipeline_huge: bool = False,
) -> Summary:
    excerpt_lines = [
        "Checking package 3.1.21",
        _VERSION,
        "##[error]Process completed with exit code 1.",
    ]
    job = FailedJob(
        job_id=42,
        name="publish",
        failed_step_name=step,
        failed_step_number=3,
        exit_code=1,
        duration_seconds=12,
        windows=[_window("\n".join(excerpt_lines))],
        error_lines=[],
        failed_step_excerpt=FailedStepExcerpt(name=step, lines=excerpt_lines),
    )
    pipe_content = pipeline_text
    if pipeline_huge:
        pipe_content = ("INFO leftover publish line\n" * 4000) + pipeline_text
    stream = PipelineLogStream(
        artifact_name="pipeline-logs-build",
        stage="build",
        file="build.log",
        windows=[_window(pipe_content)],
        templates=[
            LogTemplate(
                template_id=9,
                template="Not enough permissions to delete <*>",
                count=12,
                first_line=80,
                has_error_match=True,
                tier="T1",
                representative_line=_DELETE,
            )
        ],
    )
    return Summary(
        collector_version="0.1.0",
        collected_at=_NOW,
        run=RunMeta(
            run_id=1,
            run_attempt=1,
            workflow_name="CI",
            html_url="https://example.invalid/acme/widgets/actions/runs/1",
            event="push",
            actor="bot",
            head_sha="abc",
            head_branch="main",
            failed_job_total=1,
            failed_jobs_analysed=1,
        ),
        verdict=Verdict(requires_analysis=False),
        classification=Classification(
            category="unknown",
            confidence="low",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
        ),
        failed_jobs=[job],
        pipeline_logs=[stream],
        changes=ChangeContext(
            head_sha="abc",
            range_basis="last_success",
            classes=["ci_config"],
            files=["package.json"],
        ),
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )


def test_evidence_keeps_artifactory_version_not_delete_permission() -> None:
    summary = _artifactory_summary()
    evidence = build_evidence(summary)
    assert "already exists on Artifactory" in evidence
    assert "Check Version" in evidence
    assert "failed_step: Check Version in Artifactory" in evidence
    delete_at = evidence.find(_DELETE)
    excerpt_at = evidence.find("already exists on Artifactory")
    assert excerpt_at != -1
    assert delete_at == -1 or delete_at > excerpt_at
    assert _DELETE not in evidence


def test_failed_step_excerpt_survives_huge_pipeline_logs() -> None:
    summary = _artifactory_summary(step="Build", pipeline_huge=True)
    evidence = build_evidence(summary)
    assert "already exists on Artifactory" in evidence
    assert "failed_step: Build" in evidence
    assert token_count(evidence) <= ANALYZE_EVIDENCE_CAP + 80
    excerpt_at = evidence.find("already exists on Artifactory")
    assert excerpt_at != -1
    delete_at = evidence.find(_DELETE)
    assert delete_at == -1 or delete_at > excerpt_at
