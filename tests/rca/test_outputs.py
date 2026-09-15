"""GITHUB_OUTPUT writer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.rca.models import (
    AnalysisRecord,
    AnalysisResult,
    BudgetReport,
    Classification,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.outputs import (
    ANALYSIS_OUTPUT_KEYS,
    OUTPUT_KEYS,
    action_outputs,
    analysis_outputs,
    failure_outputs,
    write_analysis_github_output,
    write_failure_outputs,
    write_github_output,
)


def _summary() -> Summary:
    return Summary(
        collector_version="0.1.0",
        collected_at=datetime.now(timezone.utc),
        run=RunMeta(
            run_id=9,
            run_attempt=1,
            workflow_name="CI",
            html_url="",
            event="push",
            actor="a",
            head_sha="abc",
            head_branch="main",
            failed_job_total=2,
            failed_jobs_analysed=2,
        ),
        verdict=Verdict(requires_analysis=True, short_circuit=None),
        classification=Classification(
            category="unknown",
            confidence="low",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
            is_flaky=False,
        ),
        failed_jobs=[],
        fingerprint="",
        fingerprint_coarse="",
        kubernetes=None,
        budget_report=BudgetReport(),
    )


def test_action_outputs_stub_values(tmp_path: Path) -> None:
    values = action_outputs(_summary(), tmp_path)
    assert set(values) == set(OUTPUT_KEYS)
    assert values["category"] == "unknown"
    assert values["confidence"] == "low"
    assert values["is-infra-vs-code"] == "unknown"
    assert values["is-flaky"] == "false"
    assert values["short-circuit"] == ""
    assert values["requires-analysis"] == "true"
    assert values["fingerprint"] == ""
    assert values["seen-count"] == "0"
    assert values["recurrence"] == "new"
    assert values["failed-job-count"] == "2"
    assert values["summary-path"].endswith("summary.md")
    assert values["json-path"].endswith("summary.json")


def test_write_github_output_appends_key_value(tmp_path: Path) -> None:
    dest = tmp_path / "github_output"
    write_github_output(_summary(), tmp_path / "rca", output_file=dest)
    text = dest.read_text(encoding="utf-8")
    assert "category=unknown\n" in text
    assert "requires-analysis=true\n" in text
    assert "short-circuit=\n" in text
    assert "Bearer " not in text
    assert "token" not in text.lower()
    assert text.startswith("summary-path=")


def test_outputs_are_single_line(tmp_path: Path) -> None:
    summary = _summary()
    summary.classification.category = "dep\nendency"
    values = action_outputs(summary, tmp_path)
    for key, value in values.items():
        assert "\n" not in value, key
        assert "\r" not in value, key
    dest = tmp_path / "out"
    write_failure_outputs(tmp_path / "rca", output_file=dest)
    text = dest.read_text(encoding="utf-8")
    assert "category=unknown\n" in text
    assert "requires-analysis=true\n" in text
    assert "short-circuit=\n" in text
    assert failure_outputs(tmp_path)["short-circuit"] == ""


def test_analysis_outputs_are_single_line(tmp_path: Path) -> None:
    record = AnalysisRecord(
        status="ok",
        result=AnalysisResult(
            root_cause="first line\nsecond line",
            suggested_fix="pin it",
            confidence="high",
        ),
    )
    values = analysis_outputs(record)
    assert set(values) == set(ANALYSIS_OUTPUT_KEYS)
    assert values["analysis-status"] == "ok"
    assert values["rca-confidence"] == "high"
    assert "\n" not in values["root-cause"]
    dest = tmp_path / "out"
    write_analysis_github_output(record, output_file=dest)
    text = dest.read_text(encoding="utf-8")
    assert "analysis-status=ok\n" in text
    assert "analysis-notes=\n" in text
    assert "rca-confidence=high\n" in text
    assert "root-cause=first line second line\n" in text
    assert "STGPT_API" not in text
    assert "sk-live" not in text
