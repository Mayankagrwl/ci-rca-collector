"""Step 9 — benign-line filtering + uninformative-job → pipeline-sourced cause.

Regression: a Docker layer line "<hex> Already exists 0B" was read as an
Artifactory terminal cause, excluding the pipeline logs that held the real
compile error. Fixes are structural (benign vs cause, informative vs
uninformative, job vs pipeline) — no hardcoded project/file/message.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tools.rca.analyze import analyze_summary, display_status, finalize_user_card
from tools.rca.diagnose import (
    _first_pipeline_error_line,
    _job_logs_are_exit_only,
    diagnose,
    display_citation_quotes,
    specific_log_cause,
    terminal_cause_present,
)
from tools.rca.extract import is_benign_line
from tools.rca.models import (
    AnalysisRecord,
    AnalysisResult,
    BudgetReport,
    Classification,
    DrainReport,
    ErrorLine,
    FailedJob,
    FailedStepExcerpt,
    LogTemplate,
    LogWindow,
    PipelineLogStream,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.prompt import build_evidence, failed_step_anchor_text

_BENIGN_JOB = "\n".join(
    [
        "Using unit test service: payments",
        'time="2026-09-23T00:00:00Z" level=warning msg="No services to build"',
        "a1b2c3d4e5f6 Already exists 0B",
        "##[error]Process completed with exit code 1.",
    ]
)
_COMPILE = "\n".join(
    [
        "[ERROR] COMPILATION ERROR :",
        "[ERROR] /work/src/test/TestConfig.java:[19,40] ';' expected",
        "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.11:testCompile",
    ]
)
_ARTIFACTORY = "This release already exists on Artifactory. You need to update package.json"


def _window(content: str, label: str = "first_error") -> LogWindow:
    lines = content.splitlines() or [content]
    return LogWindow(label=label, start_line=1, end_line=len(lines),  # type: ignore[arg-type]
                     total_lines=len(lines), content=content)


def _run() -> RunMeta:
    return RunMeta(run_id=1, run_attempt=1, workflow_name="CI", html_url="https://x/1",
                   event="push", actor="bot", head_sha="a", head_branch="main",
                   failed_job_total=1, failed_jobs_analysed=1)


def _drain(rep: str) -> DrainReport:
    return DrainReport(
        baseline_available=False, total_clusters=1, tier_counts={"T1": 1},
        templates=[LogTemplate(template_id=1, template=rep, count=1, first_line=1,
                               has_error_match=True, tier="T1", representative_line=rep)],
    )


def _summary(
    *,
    job_content: str,
    job_errors: list[str],
    step_name: str = "Run unit tests",
    primary_failure_line: str | None = None,
    pipeline: list[PipelineLogStream] | None = None,
    drain: DrainReport | None = None,
    category: str = "unknown",
    confidence: str = "low",
) -> Summary:
    job = FailedJob(
        job_id=1, name="tests", failed_step_name=step_name, failed_step_number=6,
        exit_code=1, duration_seconds=30,
        windows=[_window(job_content)],
        error_lines=[ErrorLine(line_number=i + 1, text=t) for i, t in enumerate(job_errors)],
        failed_step_excerpt=FailedStepExcerpt(name=step_name, lines=job_content.splitlines()),
        primary_failure_line=primary_failure_line,
    )
    return Summary(
        collector_version="0.1.0", collected_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        run=_run(), verdict=Verdict(requires_analysis=True),
        classification=Classification(category=category, confidence=confidence,  # type: ignore[arg-type]
                                      matched_pattern=None, matched_line=None, is_infra_vs_code="unknown"),
        failed_jobs=[job], pipeline_logs=pipeline or [], drain=drain,
        fingerprint="f" * 16, fingerprint_coarse="c" * 16, budget_report=BudgetReport(),
    )


def _uninformative_summary() -> Summary:
    pipe = PipelineLogStream(
        artifact_name="pipeline-logs-test", stage="test", file="mvn.log", phase="run",
        windows=[_window(_COMPILE)],
        error_lines=[ErrorLine(line_number=1, text="[ERROR] COMPILATION ERROR :")],
    )
    summary = _summary(
        job_content=_BENIGN_JOB,
        job_errors=["a1b2c3d4e5f6 Already exists 0B", "##[error]Process completed with exit code 1."],
        pipeline=[pipe],
        drain=_drain("[ERROR] COMPILATION ERROR :"),
    )
    # Mirror the collect layer: seed primary_failure_line from the pipeline when
    # the job is uninformative.
    if _job_logs_are_exit_only(summary):
        summary.failed_jobs[0].primary_failure_line = _first_pipeline_error_line(summary)
    return summary


# --- 1. regression reproduced then fixed ------------------------------------


def test_benign_docker_line_is_not_a_terminal_cause() -> None:
    assert is_benign_line("a1b2c3d4e5f6 Already exists 0B") is True
    summary = _uninformative_summary()
    assert terminal_cause_present(summary) is False


def test_build_evidence_keeps_pipeline_and_templates() -> None:
    ev = build_evidence(_uninformative_summary())
    assert "### pipeline_logs" in ev
    assert "### log_templates" in ev


def test_deterministic_cause_and_primary_line_come_from_compile() -> None:
    summary = _uninformative_summary()
    primary = summary.failed_jobs[0].primary_failure_line or ""
    assert "already exists" not in primary.lower()
    assert "COMPILATION ERROR" in primary

    verdict = diagnose(summary)
    assert verdict.rule_id == "R17"
    root = verdict.one_liner.lower()
    assert "already exists" not in root
    assert "bump" not in root  # not the Artifactory "bump the package version" fix
    assert "compilation error" in root or "testcompile" in root


def test_model_citing_compile_line_is_grounded_and_status_ok(tmp_path) -> None:
    summary = _uninformative_summary()
    completion = {
        "root_cause": "Test sources failed to compile: ';' expected in TestConfig.java",
        "suggested_fix": "Fix the syntax error in TestConfig.java and re-run",
        "confidence": "high",
        "citations": [
            {"quote": "[ERROR] /work/src/test/TestConfig.java:[19,40] ';' expected",
             "source": "pipeline_logs"}
        ],
    }
    record = analyze_summary(summary, from_completion=[completion], cache_dir=tmp_path / "cache")
    assert record.status == "ok"
    assert record.grounded is True
    assert display_status(record) == "ok"
    assert "already exists" not in (record.result.root_cause or "").lower()


# --- 2. Artifactory case still works (no regression) ------------------------


def test_artifactory_terminal_cause_still_detected() -> None:
    summary = _summary(
        job_content=f"Checking version 3.1.21\n{_ARTIFACTORY}\n##[error]Process completed with exit code 1.",
        job_errors=[_ARTIFACTORY],
        step_name="Check Version in Artifactory",
        primary_failure_line=_ARTIFACTORY,
    )
    assert terminal_cause_present(summary) is True
    verdict = diagnose(summary)
    root = verdict.one_liner.lower()
    assert "already exists" in root or "version" in root


# --- 3. benign never cited / anchored ---------------------------------------


def test_benign_is_never_a_cause_or_citation() -> None:
    summary = _uninformative_summary()
    assert specific_log_cause(summary) != "a1b2c3d4e5f6 Already exists 0B"
    for quote in display_citation_quotes(summary):
        assert not is_benign_line(quote)
    # The first-error anchor is not the benign line.
    anchor = failed_step_anchor_text(summary)
    assert "COMPILATION ERROR" in anchor  # pipeline error reached the anchor


# --- 4. cannot-determine safety net -----------------------------------------


def test_cannot_determine_with_weak_deterministic_renders_needs_review() -> None:
    # An unknown, low-confidence summary whose only "cause" would be a benign
    # line; a model cannot_determine must not ship a confident deterministic card.
    summary = _summary(
        job_content="a1b2c3d4e5f6 Already exists 0B\n##[error]Process completed with exit code 1.",
        job_errors=["a1b2c3d4e5f6 Already exists 0B"],
    )
    record = AnalysisRecord(
        status="ok",
        model_called=True,
        result=AnalysisResult(
            root_cause="I cannot tell from this evidence",
            suggested_fix="",
            confidence="low",
            cannot_determine=True,
        ),
        notes=["model root_cause: I cannot tell from this evidence"],
        analyzed_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    finalized = finalize_user_card(summary, record)
    assert display_status(finalized) == "needs-review"
    assert finalized.result is not None
    assert "cannot determine" in finalized.result.root_cause.lower()
    assert finalized.result.confidence != "high"
    assert any("I cannot tell" in note for note in finalized.notes)
