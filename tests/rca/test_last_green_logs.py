"""Last-green pipeline-log compare: novel templates, missing green degrades."""

from __future__ import annotations

from tools.rca.diagnose import diagnose
from tools.rca.models import (
    BudgetReport,
    Classification,
    FailedJob,
    LastGreenCompare,
    LogWindow,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.pipeline_logs import compare_green_fail
from datetime import datetime, timezone


def test_novel_template_on_fail() -> None:
    green = [f"INFO processing record {i} of 20 [ok]" for i in range(1, 21)]
    fail = list(green) + ["error TS2345: Type 'string' is not assignable"]
    compare = compare_green_fail(green, fail, artifact_name="pipeline-logs-build")
    assert compare.available is True
    assert any("error TS" in item or "TS2345" in item for item in compare.novel_templates)
    assert compare.skipped_reason is None


def test_missing_green_artifact_degrades() -> None:
    fail = ["error TS2345: Type 'string' is not assignable"]
    compare = compare_green_fail([], fail, artifact_name="pipeline-logs-build")
    assert compare.available is False
    assert compare.skipped_reason == "green artifact missing"
    assert compare.novel_templates == []


def test_r13_skips_when_green_missing() -> None:
    summary = Summary(
        collector_version="0.1.0",
        collected_at=datetime.now(timezone.utc),
        run=RunMeta(
            run_id=1,
            run_attempt=1,
            workflow_name="CI",
            html_url="",
            event="push",
            actor="a",
            head_sha="abc",
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
                failed_step_name="Build",
                failed_step_number=1,
                exit_code=1,
                duration_seconds=10,
                windows=[
                    LogWindow(
                        label="first_error",
                        start_line=1,
                        end_line=1,
                        total_lines=1,
                        content="mysterious failure",
                    )
                ],
            )
        ],
        last_green_compare=LastGreenCompare(
            artifact_name="pipeline-logs-build",
            available=False,
            skipped_reason="green artifact missing",
        ),
        fingerprint="",
        fingerprint_coarse="",
        budget_report=BudgetReport(),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id != "R13"
    assert verdict.requires_analysis is True
