"""Step 3 — terminal-cause table + symptom / teardown-phase demotion.

When the failed step already carries a line that alone explains the exit, the
deterministic path must not let R14/blast-radius or a follow-on symptom win, and
the AI evidence pack must drop log_templates/pipeline_logs up front. Everything
is driven by the config regex tables + structural phase parsing — no hardcoded
failure strings in the collector.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

from tools.rca.diagnose import (
    _sorted_pipeline_streams,
    diagnose,
    specific_log_cause,
    terminal_cause_present,
)
from tools.rca.models import (
    BudgetReport,
    ChangeContext,
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
from tools.rca.pipeline_logs import pipeline_phase, parse_pipeline_zip
from tools.rca.prompt import build_evidence, focused_evidence

_CAUSE = "This release already exists on Artifactory. You need to update package.json"
_EXIT = "##[error]Process completed with exit code 1."
_SYMPTOM = "Not enough permissions to delete the release during docker down"


def _run() -> RunMeta:
    return RunMeta(
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
    )


def _window(content: str, label: str = "first_error") -> LogWindow:
    lines = content.splitlines() or [content]
    return LogWindow(
        label=label,  # type: ignore[arg-type]
        start_line=1,
        end_line=len(lines),
        total_lines=len(lines),
        content=content,
    )


def _summary(
    *,
    primary_line: str | None,
    excerpt: list[str],
    job_window: str,
    job_errors: list[str],
    step_name: str = "Publish Package",
    pipeline: list[PipelineLogStream] | None = None,
    drain: DrainReport | None = None,
    changes: ChangeContext | None = None,
    exit_only_job: bool = False,
) -> Summary:
    job = FailedJob(
        job_id=1,
        name="release",
        failed_step_name=step_name,
        failed_step_number=5,
        exit_code=1,
        duration_seconds=10,
        windows=[_window(job_window)] if job_window else [],
        error_lines=[ErrorLine(line_number=i + 1, text=t) for i, t in enumerate(job_errors)],
        failed_step_excerpt=FailedStepExcerpt(name=step_name, lines=excerpt),
        primary_failure_line=primary_line,
    )
    return Summary(
        collector_version="0.1.0",
        collected_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        run=_run(),
        verdict=Verdict(requires_analysis=True),
        classification=Classification(
            category="unknown",
            confidence="low",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
        ),
        failed_jobs=[job],
        pipeline_logs=pipeline or [],
        drain=drain,
        changes=changes,
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )


def _teardown_stream() -> PipelineLogStream:
    return PipelineLogStream(
        artifact_name="docker-logs",
        stage="test",
        file="docker down test.log",
        phase="teardown",
        step_name="docker down test.log",
        windows=[_window(_SYMPTOM)],
        error_lines=[ErrorLine(line_number=1, text=_SYMPTOM)],
    )


def _drain_with_symptom() -> DrainReport:
    return DrainReport(
        baseline_available=False,
        total_clusters=1,
        tier_counts={"T1": 1},
        templates=[
            LogTemplate(
                template_id=1,
                template=_SYMPTOM,
                count=3,
                first_line=90,
                has_error_match=True,
                tier="T1",
                representative_line=_SYMPTOM,
            )
        ],
    )


# --- 1. terminal cause + teardown symptom → terminal cause wins -------------


def test_terminal_cause_beats_teardown_symptom_and_r14() -> None:
    summary = _summary(
        primary_line=_CAUSE,
        excerpt=[_CAUSE, _EXIT],
        job_window=f"{_CAUSE}\n{_EXIT}",
        job_errors=[_CAUSE],
        pipeline=[_teardown_stream()],
        drain=_drain_with_symptom(),
        changes=ChangeContext(head_sha="abc", classes=["ci_config"]),
    )
    assert terminal_cause_present(summary) is True

    verdict = diagnose(summary)
    assert "already exists" in verdict.one_liner.lower()
    assert "workflow or action definition changed" not in verdict.one_liner.lower()
    assert verdict.rule_id != "R14"
    # The follow-on teardown symptom must never be cited as the cause.
    assert not any("not enough permissions" in q.lower() for q in verdict.citations)

    # AI evidence pack drops symptom-prone sections up front.
    evidence = build_evidence(summary)
    assert "### log_templates" not in evidence
    assert "### pipeline_logs" not in evidence
    assert "### primary_failure_line" in evidence
    assert "### failed_step_excerpt" in evidence
    # The anchor still reaches the focused pack.
    assert _CAUSE in focused_evidence(summary)


def test_specific_log_cause_skips_teardown_symptom() -> None:
    summary = _summary(
        primary_line=_CAUSE,
        excerpt=[_CAUSE, _EXIT],
        job_window=f"{_CAUSE}\n{_EXIT}",
        job_errors=[_CAUSE],
        pipeline=[_teardown_stream()],
    )
    assert specific_log_cause(summary) == _CAUSE


# --- 2. phase parsing + teardown demotion -----------------------------------


def test_pipeline_phase_from_docker_substep_names() -> None:
    assert pipeline_phase("docker up test.log") == "setup"
    assert pipeline_phase("docker flyway.log") == "setup"
    assert pipeline_phase("docker down test.log") == "teardown"


def test_phase_parsed_from_zip_and_teardown_sorts_last() -> None:
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("docker up test.log", "INFO starting db container\nready\n")
        archive.writestr("docker flyway.log", "INFO applying migration V1\napplied\n")
        archive.writestr("docker down test.log", f"{_SYMPTOM}\nStopping container\n")
    streams = parse_pipeline_zip(blob.getvalue(), artifact_name="docker-logs")
    phases = {s.file: s.phase for s in streams}
    assert phases["docker up test.log"] == "setup"
    assert phases["docker flyway.log"] == "setup"
    assert phases["docker down test.log"] == "teardown"

    summary = _summary(
        primary_line="step exited",
        excerpt=["step exited", _EXIT],
        job_window=f"step exited\n{_EXIT}",
        job_errors=["step exited"],
        pipeline=streams,
    )
    ordered = _sorted_pipeline_streams(summary)
    assert ordered[-1].phase == "teardown"
    assert ordered[-1].file == "docker down test.log"


# --- 3. no terminal cause → nothing changes ---------------------------------


def test_no_terminal_cause_keeps_templates_pipeline_and_r14() -> None:
    stream = PipelineLogStream(
        artifact_name="pipeline-logs-build",
        stage="build",
        file="build.log",
        phase="run",
        windows=[_window("INFO build step ran\nnothing conclusive here")],
    )
    summary = _summary(
        primary_line="the build step exited",
        excerpt=["the build step exited", _EXIT],
        job_window=f"the build step exited\n{_EXIT}",
        job_errors=["the build step exited"],
        step_name="Build project",
        pipeline=[stream],
        drain=DrainReport(
            baseline_available=False,
            total_clusters=1,
            tier_counts={"T1": 1},
            templates=[
                LogTemplate(
                    template_id=1,
                    template="INFO build step ran",
                    count=2,
                    first_line=1,
                    tier="T1",
                    representative_line="INFO build step ran",
                )
            ],
        ),
        changes=ChangeContext(head_sha="abc", classes=["ci_config"]),
    )
    assert terminal_cause_present(summary) is False

    evidence = build_evidence(summary)
    assert "### log_templates" in evidence
    assert "### pipeline_logs" in evidence

    verdict = diagnose(summary)
    assert verdict.rule_id == "R14"


def test_r17_pipeline_over_exit_only_job_still_works() -> None:
    stream = PipelineLogStream(
        artifact_name="pipeline-logs-build",
        stage="build",
        file="build.log",
        phase="run",
        windows=[_window("panic: runtime error: invalid memory address", "first_error")],
        error_lines=[ErrorLine(line_number=1, text="panic: runtime error: invalid memory address")],
    )
    summary = _summary(
        primary_line=None,
        excerpt=[_EXIT],
        job_window=_EXIT,
        job_errors=[_EXIT],
        step_name="Build project",
        pipeline=[stream],
    )
    assert terminal_cause_present(summary) is False

    verdict = diagnose(summary)
    assert verdict.rule_id == "R17"  # pipeline is not suppressed; it is the cause


# --- 4. graceful degradation ------------------------------------------------


def test_unrecognised_or_empty_names_do_not_raise() -> None:
    assert pipeline_phase("weird_output_42.log") == "run"
    assert pipeline_phase("") is None
    assert pipeline_phase(None) is None
    assert pipeline_phase(None, "docker-logs") == "run"


def test_cause_selection_on_empty_summary_does_not_crash() -> None:
    summary = Summary(
        collector_version="0.1.0",
        collected_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        run=_run(),
        verdict=Verdict(requires_analysis=True),
        classification=Classification(
            category="unknown",
            confidence="low",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
        ),
        failed_jobs=[],
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )
    assert terminal_cause_present(summary) is False
    assert specific_log_cause(summary) is None
