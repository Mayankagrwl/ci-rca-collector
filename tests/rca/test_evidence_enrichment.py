"""Step 6 — category-aware evidence enrichment (stack_traces / junit / code_context).

The model only runs on ambiguous failures now (Step 4), so those calls carry the
richest *relevant* evidence — but bounded and category-aware, so infra/dependency
cases aren't bloated with irrelevant stack traces. Selection is by category and
the collected structures only; no hardcoded failure strings.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tools.rca.analyze import analyze_summary, is_grounded
from tools.rca.budget import token_count
from tools.rca.config import SECTION_TOKEN_CAPS
from tools.rca.models import (
    AnalysisResult,
    BudgetReport,
    ChangeContext,
    Classification,
    CodeContext,
    CodeHunk,
    DrainReport,
    ErrorLine,
    FailedJob,
    FailedStepExcerpt,
    JUnitFailure,
    JUnitReport,
    LogTemplate,
    LogWindow,
    PipelineLogStream,
    RunMeta,
    StackTrace,
    Summary,
    Verdict,
)
from tools.rca.prompt import (
    _stack_trace_lines,
    build_evidence,
    failed_step_anchor_text,
    focused_evidence,
)

_FIRST_ERROR = "ZeroDivisionError: division by zero"
_FRAME = 'Traceback (most recent call last):\n  File "app/calc.py", line 12, in divide\n    return a / b\nZeroDivisionError: division by zero'


def _stack() -> list[StackTrace]:
    return [StackTrace(headline=_FIRST_ERROR, content=_FRAME, frame_count=3)]


def _junit() -> JUnitReport:
    return JUnitReport(
        total_failures=1,
        total_tests=10,
        failures=[
            JUnitFailure(
                classname="tests.test_calc",
                name="test_divide",
                message="assert result == 2",
                body="E   assert 0 == 2",
            )
        ],
    )


def _code_context() -> CodeContext:
    return CodeContext(
        hunks=[
            CodeHunk(
                path="app/calc.py",
                start_line=10,
                end_line=14,
                content="def divide(a, b):\n    return a / b",
            )
        ]
    )


def _drain() -> DrainReport:
    return DrainReport(
        baseline_available=False,
        total_clusters=1,
        tier_counts={"T1": 1},
        templates=[
            LogTemplate(
                template_id=1,
                template="ZeroDivisionError in divide",
                count=1,
                first_line=5,
                has_error_match=True,
                tier="T1",
                representative_line="ZeroDivisionError: division by zero",
            )
        ],
    )


def _pipeline() -> list[PipelineLogStream]:
    return [
        PipelineLogStream(
            artifact_name="pipeline-logs-test",
            stage="test",
            file="test.log",
            phase="run",
            windows=[
                LogWindow(
                    label="first_error",
                    start_line=1,
                    end_line=1,
                    total_lines=1,
                    content="pipeline test stream ran here",
                )
            ],
        )
    ]


def _summary(
    *,
    category: str = "crash",
    confidence: str = "high",
    stack: bool = True,
    junit: bool = True,
    code: bool = True,
    drain: bool = True,
    pipeline: bool = True,
    changes: bool = True,
    first_error: str = _FIRST_ERROR,
    step_name: str = "Run pytest",
) -> Summary:
    job = FailedJob(
        job_id=1,
        name="tests",
        failed_step_name=step_name,
        failed_step_number=4,
        exit_code=1,
        duration_seconds=9,
        windows=[
            LogWindow(
                label="first_error",
                start_line=1,
                end_line=1,
                total_lines=1,
                content=first_error,
            )
        ],
        error_lines=[ErrorLine(line_number=1, text=first_error)],
        stack_traces=_stack() if stack else [],
        failed_step_excerpt=FailedStepExcerpt(name=step_name, lines=[first_error]),
        primary_failure_line=first_error,
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
            category=category,
            confidence=confidence,  # type: ignore[arg-type]
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="code",
        ),
        failed_jobs=[job],
        junit=_junit() if junit else None,
        code_context=_code_context() if code else None,
        drain=_drain() if drain else None,
        pipeline_logs=_pipeline() if pipeline else [],
        changes=ChangeContext(head_sha="abc", classes=["src"]) if changes else None,
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )


# --- 1. code category packs the rich sections, ordered ----------------------


def test_code_category_packs_stack_junit_code_before_templates() -> None:
    ev = build_evidence(_summary(category="crash"))
    for header in ("### stack_traces", "### junit", "### code_context"):
        assert header in ev
    assert "### failed_step_excerpt" in ev
    assert ev.index("### failed_step_excerpt") < ev.index("### stack_traces")
    assert ev.index("### stack_traces") < ev.index("### log_templates")
    assert ev.index("### code_context") < ev.index("### log_templates")


# --- 2. dependency stays lean -----------------------------------------------


def test_dependency_excludes_stack_but_keeps_change_and_templates() -> None:
    ev = build_evidence(_summary(category="dependency"))
    assert "### stack_traces" not in ev
    assert "### junit" not in ev
    assert "### change_context" in ev
    assert "### log_templates" in ev


# --- 3. infra minimal -------------------------------------------------------


def test_infra_is_minimal() -> None:
    ev = build_evidence(_summary(category="oom"))
    assert "### primary_failure_line" in ev
    assert "### failed_step_excerpt" in ev
    assert "### first_error_window" in ev
    assert "DETERMINISTIC_HINT:" in ev
    for header in (
        "### stack_traces",
        "### junit",
        "### code_context",
        "### log_templates",
        "### pipeline_logs",
    ):
        assert header not in ev


# --- 4. unknown / residual includes everything relevant ---------------------


def test_unknown_includes_everything_relevant() -> None:
    ev = build_evidence(_summary(category="unknown"))
    for header in (
        "### stack_traces",
        "### junit",
        "### code_context",
        "### log_templates",
        "### pipeline_logs",
        "### change_context",
    ):
        assert header in ev


# --- 5. budget respected ----------------------------------------------------


def test_per_section_cap_and_total_budget() -> None:
    # A huge stack trace is trimmed to its per-section cap (trim_middle overshoots
    # only by its ~5-token middle marker).
    big = _summary(category="crash")
    big.failed_jobs[0].stack_traces = [
        StackTrace(headline="Boom", content="frame line\n" * 4000, frame_count=4000)
    ]
    block = "\n".join(_stack_trace_lines(big))
    assert token_count(block) <= SECTION_TOKEN_CAPS["stack_traces"] + 6

    # A tiny total cap keeps mandatory sections and drops the optional ones.
    lean = build_evidence(_summary(category="crash"), cap_tokens=5)
    assert "### primary_failure_line" in lean
    assert "### failed_step_excerpt" in lean
    assert "### stack_traces" not in lean
    assert "### first_error_window" not in lean


# --- 6. grounding on a stack frame ------------------------------------------


def test_anchor_includes_stack_traces() -> None:
    summary = _summary(category="crash")
    anchor = failed_step_anchor_text(summary)
    assert 'File "app/calc.py", line 12, in divide' in anchor
    result = AnalysisResult(
        root_cause="ZeroDivisionError in divide",
        suggested_fix="guard against b == 0",
        confidence="high",
        citations=[],
    )
    from tools.rca.models import AnalysisCitation

    result.citations = [
        AnalysisCitation(quote='File "app/calc.py", line 12, in divide', source="stack_traces")
    ]
    assert is_grounded(result, anchor) is True


def test_stack_frame_citation_renders_status_ok(tmp_path) -> None:
    summary = _summary(category="crash")
    completion = {
        "root_cause": "divide by zero at app/calc.py:12",
        "suggested_fix": "guard against b == 0",
        "confidence": "high",
        "citations": [
            {"quote": 'File "app/calc.py", line 12, in divide', "source": "stack_traces"}
        ],
    }
    record = analyze_summary(summary, from_completion=[completion], cache_dir=tmp_path / "cache")
    assert record.status == "ok"
    assert record.grounded is True
    from tools.rca.analyze import display_status

    assert display_status(record) == "ok"


# --- 7. terminal-cause + focused paths unchanged ----------------------------


def test_terminal_cause_still_excludes_templates_and_pipeline() -> None:
    # A terminal cause in the failed step (unknown category → full profile).
    summary = _summary(
        category="unknown",
        first_error="This release already exists on Artifactory; update package.json",
    )
    ev = build_evidence(summary)
    assert "### log_templates" not in ev
    assert "### pipeline_logs" not in ev
    # Non-symptom rich sections are still present.
    assert "### stack_traces" in ev
    assert "### junit" in ev


def test_focused_evidence_still_drops_templates_and_pipeline() -> None:
    ev = focused_evidence(_summary(category="unknown"))
    assert "### log_templates" not in ev
    assert "### pipeline_logs" not in ev
    assert "### stack_traces" in ev
