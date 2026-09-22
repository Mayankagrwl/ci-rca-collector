"""Step 5 — one consistent "AI Diagnosis" section, always, for any status.

The section renders every time with the same fields, never blank, whether the
answer came from the model or from code. The internal status is normalized for
display but preserved in the machine outputs and in Notes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tools.rca.analyze import (
    _diagnosis_markdown,
    _upsert_diagnosis,
    display_status,
    finalize_user_card,
)
from tools.rca.models import (
    AnalysisRecord,
    AnalysisResult,
    BudgetReport,
    Classification,
    DeterministicDiagnosis,
    ErrorLine,
    FailedJob,
    FailedStepExcerpt,
    LogWindow,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.outputs import analysis_outputs

_CAUSE = "npm ERR! ERESOLVE could not resolve dependency left-pad@1.3.0"


def _record(status: str, *, result: AnalysisResult | None = None, model_called: bool = False,
            notes: list[str] | None = None, reason_code: str | None = None) -> AnalysisRecord:
    return AnalysisRecord(
        status=status,  # type: ignore[arg-type]
        result=result,
        model_called=model_called,
        notes=notes or [],
        reason_code=reason_code,
        analyzed_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )


def _result(root: str = "root cause here", fix: str = "do the fix",
            *, source: str = "deterministic", confidence: str = "low") -> AnalysisResult:
    return AnalysisResult(
        root_cause=root,
        suggested_fix=fix,
        confidence=confidence,  # type: ignore[arg-type]
        source=source,
    )


def _summary() -> Summary:
    job = FailedJob(
        job_id=1,
        name="release",
        failed_step_name="Install deps",
        failed_step_number=3,
        exit_code=1,
        duration_seconds=8,
        windows=[
            LogWindow(
                label="first_error",
                start_line=1,
                end_line=1,
                total_lines=1,
                content=_CAUSE,
            )
        ],
        error_lines=[ErrorLine(line_number=1, text=_CAUSE)],
        failed_step_excerpt=FailedStepExcerpt(name="Install deps", lines=[_CAUSE]),
        primary_failure_line=_CAUSE,
    )
    return Summary(
        collector_version="0.1.0",
        collected_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
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
        verdict=Verdict(requires_analysis=True),
        classification=Classification(
            category="dependency",
            confidence="high",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="code",
        ),
        failed_jobs=[job],
        diagnosis=DeterministicDiagnosis(
            rule_id="R8", one_liner=_CAUSE, fix_one_liner="pin left-pad@1.3.0"
        ),
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )


def _status_line(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("**Status:**"):
            return line
    raise AssertionError("no Status line")


# --- 1. status mapping ------------------------------------------------------


@pytest.mark.parametrize(
    "internal, shown",
    [
        ("ok", "ok"),
        ("cached", "cached"),
        ("skipped", "deterministic"),
        ("gated", "deterministic"),
        ("unvalidated", "needs-review"),
        ("failed", "needs-review"),
        ("bridge_error", "needs-review"),
    ],
)
def test_display_status_mapping(internal: str, shown: str) -> None:
    record = _record(internal, result=_result())
    assert display_status(record) == shown
    md = _diagnosis_markdown(record, summary=_summary())
    assert _status_line(md) == f"**Status:** {shown}"


# --- 2. skipped reads as a real diagnosis -----------------------------------


def test_deterministic_sufficient_skip_reads_as_diagnosis() -> None:
    summary = _summary()
    record = finalize_user_card(
        summary,
        _record(
            "skipped",
            result=_result(root=_CAUSE, fix="pin left-pad@1.3.0", source="deterministic"),
            reason_code="deterministic_sufficient",
        ),
    )
    md = _diagnosis_markdown(record, summary=summary)
    assert "**Status:** deterministic" in md
    assert "**Model called:** no" in md
    assert "**Source:** deterministic" in md
    assert "**Root cause:**" in md and _CAUSE in md
    assert "**Suggested fix:** pin left-pad@1.3.0" in md


# --- 3. never blank ---------------------------------------------------------


@pytest.mark.parametrize(
    "internal",
    ["ok", "cached", "skipped", "gated", "unvalidated", "failed", "bridge_error"],
)
def test_root_cause_and_fix_never_blank(internal: str) -> None:
    summary = _summary()
    record = finalize_user_card(summary, _record(internal, result=_result()))
    assert record.result is not None
    assert record.result.root_cause.strip()
    assert record.result.suggested_fix.strip()
    md = _diagnosis_markdown(record, summary=summary)
    # Rendered lines are non-empty too.
    assert "**Root cause:** \n" not in md and "**Root cause:**\n" not in md
    assert "**Suggested fix:** \n" not in md and "**Suggested fix:**\n" not in md


def test_none_result_guard_fills_both_fields() -> None:
    summary = _summary()
    record = finalize_user_card(summary, _record("failed", result=None))
    assert record.result is not None
    assert record.result.root_cause.strip()
    assert record.result.suggested_fix.strip()
    assert (record.result.source or "").strip()


# --- 4. needs-review --------------------------------------------------------


def test_needs_review_caps_confidence_and_notes_review_hint() -> None:
    summary = _summary()
    record = _record(
        "failed",
        result=_result(root="model guessed something", fix="a fix", source="deterministic",
                       confidence="high"),
        model_called=True,
        notes=["model root_cause: model guessed something"],
    )
    md = _diagnosis_markdown(record, summary=summary)
    assert "**Status:** needs-review" in md
    assert "**Confidence:** high" not in md
    assert "answer not grounded to the failed step" in md  # review hint in Notes
    assert "model guessed something" in md  # preserved model text
    assert "internal_status=failed" in md


# --- 5. idempotent header ---------------------------------------------------


def test_rendering_twice_yields_one_section() -> None:
    summary = _summary()
    record = finalize_user_card(summary, _record("skipped", result=_result()))
    once = _upsert_diagnosis("# Summary\n\nbody\n", record, summary=summary)
    twice = _upsert_diagnosis(once, record, summary=summary)
    assert twice.count("## AI Diagnosis") == 1


# --- 6. machine analysis-status output unchanged ----------------------------


def test_machine_analysis_status_output_is_internal_status() -> None:
    summary = _summary()
    record = finalize_user_card(summary, _record("skipped", result=_result()))
    values = analysis_outputs(record, summary)
    assert values["analysis-status"] == "skipped"  # internal value preserved
    assert values["diagnosis-display-status"] == "deterministic"
