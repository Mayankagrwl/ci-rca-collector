"""Deterministic rules R1–R18. requires_analysis is false except R18-style fixtures."""

from __future__ import annotations

from datetime import datetime, timezone

from tools.rca.diagnose import apply_verdict, diagnose, user_facing
from tools.rca.models import (
    BudgetReport,
    ChangeContext,
    Classification,
    CodeContext,
    CodeHunk,
    DrainReport,
    FailedJob,
    HistoryContext,
    JUnitFailure,
    JUnitReport,
    LastGreenCompare,
    LogTemplate,
    LogWindow,
    PipelineLogStream,
    RunMeta,
    StackTrace,
    Summary,
    Verdict,
)

_NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _window(text: str, label: str = "first_error") -> LogWindow:
    lines = text.splitlines() or [text]
    return LogWindow(
        label=label,  # type: ignore[arg-type]
        start_line=1,
        end_line=len(lines),
        total_lines=len(lines),
        content=text,
    )


def _job(text: str, *, name: str = "build", step: str = "Build") -> FailedJob:
    return FailedJob(
        job_id=1,
        name=name,
        failed_step_name=step,
        failed_step_number=2,
        exit_code=1,
        duration_seconds=40,
        windows=[_window(text)],
        error_lines=[],
    )


def _summary(
    *,
    job_text: str = "something failed",
    category: str = "unknown",
    confidence: str = "low",
    side: str = "unknown",
    short_circuit: str | None = None,
    requires_analysis: bool = True,
    reason: str | None = None,
    is_flaky: bool = False,
    **kwargs: object,
) -> Summary:
    defaults: dict[str, object] = dict(
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
        verdict=Verdict(
            short_circuit=short_circuit,  # type: ignore[arg-type]
            requires_analysis=requires_analysis,
            reason=reason,
        ),
        classification=Classification(
            category=category,
            confidence=confidence,  # type: ignore[arg-type]
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code=side,  # type: ignore[arg-type]
            is_flaky=is_flaky,
        ),
        failed_jobs=[_job(job_text)],
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )
    defaults.update(kwargs)
    return Summary(**defaults)  # type: ignore[arg-type]


def _pipe(
    text: str,
    *,
    artifact: str = "pipeline-logs-build",
    stage: str = "build",
    file: str = "build.log",
) -> PipelineLogStream:
    return PipelineLogStream(
        artifact_name=artifact,
        stage=stage,
        file=file,
        windows=[_window(text)],
    )


def test_r1_infra_runner() -> None:
    summary = _summary(
        job_text="The runner has received a shutdown signal",
        category="infra_runner",
        confidence="high",
        side="infra",
        short_circuit="infra_runner",
        requires_analysis=False,
        reason="runner health matched",
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R1"
    assert verdict.requires_analysis is False
    assert verdict.category == "infra_runner"
    assert verdict.short_circuit == "infra_runner"


def test_r2_infra_widespread() -> None:
    summary = _summary(job_text="npm ERR! ERESOLVE")
    summary.run.concurrent_failures = 4
    summary.run.concurrent_branches = ["main", "release"]
    summary.run.concurrent_workflows = ["CI", "Nightly", "Deploy"]
    verdict = diagnose(summary)
    assert verdict.rule_id == "R2"
    assert verdict.requires_analysis is False
    assert verdict.short_circuit == "infra_widespread"
    assert verdict.is_infra_vs_code == "infra"


def test_r3_flake_same_sha() -> None:
    summary = _summary(
        job_text="npm ERR! ERESOLVE could not resolve",
        category="dependency",
        confidence="high",
        side="code",
        short_circuit="flake_same_sha_passed",
        requires_analysis=False,
        reason="same SHA previously succeeded for this job",
        is_flaky=True,
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R3"
    assert verdict.requires_analysis is False
    assert verdict.is_flaky is True
    assert verdict.short_circuit == "flake_same_sha_passed"


def test_r4_timeout_signature() -> None:
    text = "The action failed because it exceeded the maximum execution time"
    summary = _summary(job_text=text, category="timeout", confidence="high", side="infra")
    verdict = diagnose(summary)
    assert verdict.rule_id == "R4"
    assert verdict.requires_analysis is False
    assert verdict.category == "timeout"
    assert verdict.is_infra_vs_code == "infra"


def test_r5_oom_in_pipeline_is_code_not_infra_runner() -> None:
    summary = _summary(
        job_text="##[error]Process completed with exit code 137.",
        category="unknown",
        pipeline_logs=[
            _pipe("FATAL ERROR: JavaScript heap out of memory", artifact="docker-logs", stage=None, file="docker.log")
        ],
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R5"
    assert verdict.requires_analysis is False
    assert verdict.category == "oom"
    assert verdict.is_infra_vs_code == "code"
    assert verdict.short_circuit != "infra_runner"


def test_r6_disk_enospc() -> None:
    summary = _summary(job_text="write failed: ENOSPC: no space left on device")
    verdict = diagnose(summary)
    assert verdict.rule_id == "R6"
    assert verdict.requires_analysis is False
    assert verdict.category == "disk_space"


def test_r7_image_pull_in_docker_log() -> None:
    summary = _summary(
        job_text="##[error]Process completed with exit code 1.",
        pipeline_logs=[
            _pipe(
                "Error response from daemon: pull access denied for acme/app",
                artifact="docker-logs",
                stage=None,
                file="docker.log",
            )
        ],
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R7"
    assert verdict.requires_analysis is False
    assert verdict.category == "image_pull"


def test_r8_dependency_with_lockfile_change() -> None:
    summary = _summary(
        job_text="npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE could not resolve",
        changes=ChangeContext(
            head_sha="abc",
            range_basis="last_success",
            classes=["lockfile"],
            files=["package-lock.json"],
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R8"
    assert verdict.requires_analysis is False
    assert verdict.category == "dependency"
    assert "package-lock.json" in verdict.suspected_files
    assert "Package install failed" in verdict.one_liner
    assert "R8" not in verdict.one_liner
    assert "revert the pipeline edit" not in (verdict.one_liner + (verdict.fix_one_liner or ""))


def test_r9_compile_intersects_changed_source() -> None:
    err = "src/foo.ts(12,4): error TS2345: Type 'string' is not assignable"
    summary = _summary(
        job_text="##[error]Process completed with exit code 1.",
        pipeline_logs=[_pipe(err, artifact="pipeline-logs-build", stage="build")],
        changes=ChangeContext(
            head_sha="abc",
            range_basis="last_success",
            classes=["source"],
            files=["src/foo.ts", "README.md"],
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R9"
    assert verdict.requires_analysis is False
    assert verdict.category == "compile"
    assert verdict.is_infra_vs_code == "code"
    assert any(path.endswith("foo.ts") for path in verdict.suspected_files)


def test_r10_junit_specific_assertion() -> None:
    summary = _summary(
        job_text="FAILED t/test_x.py::test_fail",
        junit=JUnitReport(
            total_failures=1,
            total_tests=3,
            failures=[
                JUnitFailure(
                    classname="t.test_x",
                    name="test_fail",
                    message="AssertionError: list mismatch",
                    body="AssertionError: list mismatch",
                )
            ],
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R10"
    assert verdict.requires_analysis is False
    assert verdict.category == "test_failure"
    assert "test_fail" in verdict.one_liner


def test_r11_drain_flooding_retry_storm() -> None:
    flooding = LogTemplate(
        template_id=1,
        template="Retrying connection in <*>",
        count=500,
        baseline_count=20,
        is_novel=False,
        first_line=10,
        has_error_match=False,
        tier="T2",
        anomaly="flooding",
        anomaly_ratio=25.0,
    )
    summary = _summary(
        job_text="Retrying connection in 1s",
        drain=DrainReport(
            baseline_available=True,
            baseline_template_count=4,
            total_clusters=1,
            tier_counts={"T2": 1},
            templates=[flooding],
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R11"
    assert verdict.requires_analysis is False
    assert verdict.category in {"timeout", "crash"}


def test_r12_depleted_plus_novel_error() -> None:
    depleted = LogTemplate(
        template_id=1,
        template="INFO processing record <*>",
        count=2,
        baseline_count=200,
        is_novel=False,
        first_line=1,
        has_error_match=False,
        tier="T2",
        anomaly="depleted",
        anomaly_ratio=0.01,
    )
    novel = LogTemplate(
        template_id=2,
        template="Exception: Connection refused to db:<*>",
        count=1,
        baseline_count=0,
        is_novel=True,
        first_line=80,
        has_error_match=True,
        tier="T1",
        representative_line="Exception: Connection refused to db:5432",
    )
    summary = _summary(
        job_text="Exception: Connection refused to db:5432",
        drain=DrainReport(
            baseline_available=True,
            baseline_template_count=2,
            total_clusters=2,
            tier_counts={"T1": 1, "T2": 1},
            templates=[depleted, novel],
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R12"
    assert verdict.requires_analysis is False
    assert "novel error" in verdict.one_liner.lower() or "Connection refused" in verdict.one_liner


def test_r13_last_green_novel_template() -> None:
    summary = _summary(
        job_text="##[error]Process completed with exit code 1.",
        last_green_compare=LastGreenCompare(
            artifact_name="docker-logs",
            novel_templates=["error TS2345: Type 'string' is not assignable"],
            available=True,
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R13"
    assert verdict.requires_analysis is False
    assert verdict.category == "compile"
    assert verdict.confidence == "high"


def test_r14_ci_config_container_only() -> None:
    summary = _summary(
        job_text="Invalid workflow file: .github/workflows/ci.yml: Unexpected value 'on'",
        changes=ChangeContext(
            head_sha="abc",
            range_basis="last_success",
            classes=["ci_config"],
            files=[".github/workflows/ci.yml"],
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R14"
    assert verdict.requires_analysis is False
    assert "R14" not in verdict.one_liner
    assert "workflow" in verdict.one_liner.lower() or "CI" in verdict.one_liner


def test_r15_history_exact_reuses_resolution() -> None:
    summary = _summary(
        job_text="mysterious flaky failure without a signature",
        history=HistoryContext(
            match="exact",
            previous_resolution="pin lodash@4.17.21",
            seen_count=3,
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R15"
    assert verdict.requires_analysis is False
    assert "pin lodash" in verdict.one_liner or "pin lodash" in (verdict.fix_one_liner or "")


def test_r16_history_cross_branch_flake() -> None:
    summary = _summary(
        job_text="mysterious flaky failure without a signature",
        history=HistoryContext(
            match="exact",
            cross_branch=True,
            branches_seen=["main", "release"],
            seen_count=2,
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R16"
    assert verdict.requires_analysis is False
    assert verdict.is_flaky is True


def test_r17_pipeline_first_error_beats_exit_code_job_log() -> None:
    summary = _summary(
        job_text="##[error]Process completed with exit code 1.",
        pipeline_logs=[
            _pipe(
                "panic: runtime error: invalid memory address",
                artifact="pipeline-logs-build",
                stage="build",
            )
        ],
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R17"
    assert verdict.requires_analysis is False
    assert verdict.winning_stream_id is not None
    assert verdict.winning_stream_id.startswith("artifact:")


def test_r18_unknown_requires_analysis() -> None:
    summary = _summary(job_text="build failed for an unspecified reason")
    verdict = diagnose(summary)
    assert verdict.rule_id == "R18"
    assert verdict.requires_analysis is True
    applied = apply_verdict(summary, verdict)
    assert applied.verdict.requires_analysis is True
    assert applied.verdict.reason.startswith("R18:")
    assert applied.diagnosis is not None
    assert applied.diagnosis.rule_id == "R18"


def test_pip_missing_package_wins_over_ci_yml_in_diff() -> None:
    lines = (
        "Collecting this-package-does-not-exist-9f3a\n"
        "ERROR: Could not find a version that satisfies the requirement "
        "this-package-does-not-exist-9f3a (from versions: none)\n"
        "ERROR: No matching distribution found for this-package-does-not-exist-9f3a\n"
        "##[error]Process completed with exit code 1."
    )
    summary = _summary(
        job_text=lines,
        changes=ChangeContext(
            head_sha="abc",
            range_basis="last_success",
            classes=["ci_config"],
            files=[".github/workflows/rca-collect.yml"],
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id.startswith("R8")
    assert verdict.rule_id != "R18"
    assert verdict.requires_analysis is False
    card = user_facing(verdict, summary)
    assert "this-package-does-not-exist-9f3a" in card.root_cause
    assert "revert the pipeline edit" not in card.suggested_fix.lower()
    assert "missing package" in card.suggested_fix
    blob = card.root_cause + card.suggested_fix
    assert "R18" not in blob
    assert "R8" not in blob
    for cite in card.citations:
        assert not cite.quote.lower().startswith("use a published")
        assert "R8" not in cite.quote
        assert "R18" not in cite.quote


def test_ci_yml_only_workflow_syntax_uses_ci_config_copy() -> None:
    summary = _summary(
        job_text="Invalid workflow file: .github/workflows/ci.yml: Unexpected value 'on'",
        changes=ChangeContext(
            head_sha="abc",
            range_basis="last_success",
            classes=["ci_config"],
            files=[".github/workflows/ci.yml"],
        ),
    )
    verdict = diagnose(summary)
    card = user_facing(verdict, summary)
    assert verdict.rule_id == "R14"
    assert "workflow" in card.root_cause.lower() or "CI" in card.root_cause
    assert "revert the pipeline edit" in card.suggested_fix.lower()
    assert "R14" not in card.root_cause
    assert "R18" not in card.root_cause
    assert "R14" not in card.suggested_fix


def test_r10_generic_assertion_with_hunk_requires_analysis() -> None:
    summary = _summary(
        job_text="FAILED t/test_x.py::test_bool",
        junit=JUnitReport(
            total_failures=1,
            total_tests=1,
            failures=[
                JUnitFailure(
                    classname="t.test_x",
                    name="test_bool",
                    message="expected true to be false",
                    body="expected true to be false",
                )
            ],
        ),
        code_context=CodeContext(
            hunks=[CodeHunk(path="t/test_x.py", start_line=1, end_line=10, content="assert False")]
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R10"
    assert verdict.requires_analysis is True


def test_r9_multi_file_without_stack_may_call_ai() -> None:
    text = (
        "src/foo.ts(12,4): error TS2345: Type 'string' is not assignable\n"
        "src/bar.ts(8,1): error TS2304: Cannot find name 'x'."
    )
    summary = _summary(
        job_text="##[error]Process completed with exit code 1.",
        pipeline_logs=[_pipe(text, artifact="pipeline-logs-build", stage="build")],
        changes=ChangeContext(
            head_sha="abc",
            range_basis="last_success",
            classes=["source"],
            files=["src/foo.ts", "src/bar.ts"],
        ),
    )
    verdict = diagnose(summary)
    assert verdict.rule_id == "R9"
    assert verdict.requires_analysis is True
    assert len(verdict.suspected_files) >= 2
