"""Stages 1, 3 and 5."""

from __future__ import annotations

from tools.rca.classify import (
    check_runner_health,
    check_same_sha_flake,
    classify_failure,
    classify_lines,
)


def test_oom() -> None:
    hit = classify_lines(["node: JavaScript heap out of memory"])
    assert hit.category == "oom"
    assert hit.confidence == "high"
    assert hit.is_infra_vs_code == "infra"
    assert hit.requires_analysis is True
    assert hit.matched_line == 1


def test_timeout() -> None:
    hit = classify_lines(
        ["##[error] The action failed because it exceeded the maximum execution time"]
    )
    assert hit.category == "timeout"
    assert hit.confidence == "high"
    hit = classify_lines(["request timed out after 30s"])
    assert hit.category == "timeout"


def test_dependency() -> None:
    hit = classify_lines(
        [
            "npm WARN deprecated foo@1.0.0",
            "npm ERR! code ERESOLVE",
            "npm ERR! ERESOLVE could not resolve",
        ]
    )
    assert hit.category == "dependency"
    assert hit.confidence == "high"
    assert hit.is_infra_vs_code == "code"
    assert hit.matched_line == 2


def test_pip_no_matching_distribution() -> None:
    lines = [
        "Collecting this-package-does-not-exist-9f3a",
        "  Downloading <unavailable>",
        "ERROR: Could not find a version that satisfies the requirement this-package-does-not-exist-9f3a (from versions: none)",
        "ERROR: No matching distribution found for this-package-does-not-exist-9f3a",
        "##[error]Process completed with exit code 1.",
        "Post job cleanup",
    ]
    hit = classify_lines(lines)
    assert hit.category == "dependency"
    assert hit.confidence in {"medium", "high"}
    assert hit.is_infra_vs_code == "code"


def test_infra_runner_pattern() -> None:
    raw = "The runner has received a shutdown signal\nmore log\n"
    hit = check_runner_health(
        raw,
        conclusion="failure",
        failed_step_name="Build",
        log_lines=200,
    )
    assert hit is not None
    assert hit.category == "infra_runner"
    assert hit.short_circuit == "infra_runner"
    assert hit.requires_analysis is False
    assert hit.is_infra_vs_code == "infra"


def test_infra_runner_short_log_no_failed_step() -> None:
    raw = "\n".join(f"line {i}" for i in range(12))
    hit = check_runner_health(
        raw,
        conclusion="failure",
        failed_step_name=None,
        log_lines=12,
    )
    assert hit is not None
    assert hit.short_circuit == "infra_runner"
    assert hit.requires_analysis is False


def test_infra_runner_skips_regex() -> None:
    hit = classify_failure(
        raw_logs=["The job was not acquired\n"],
        cleaned_lines=[["npm ERR! ERESOLVE"]],
        conclusions=["failure"],
        failed_step_names=["Install"],
        log_line_counts=[80],
    )
    assert hit.category == "infra_runner"
    assert hit.short_circuit == "infra_runner"
    assert hit.requires_analysis is False


def test_flake_same_sha_passed() -> None:
    hit = check_same_sha_flake(
        [{"name": "CI", "conclusion": "success"}],
        "CI",
    )
    assert hit is not None
    assert hit.short_circuit == "flake_same_sha_passed"
    assert hit.is_flaky is True
    assert hit.requires_analysis is False

    combined = classify_failure(
        raw_logs=["npm ERR! ERESOLVE could not resolve\n"],
        cleaned_lines=[["npm ERR! ERESOLVE could not resolve"]],
        conclusions=["failure"],
        failed_step_names=["Install"],
        log_line_counts=[80],
        workflow_name="CI",
        prior_same_sha_runs=[{"name": "CI", "conclusion": "success"}],
    )
    assert combined.category == "dependency"
    assert combined.is_flaky is True
    assert combined.short_circuit == "flake_same_sha_passed"
    assert combined.requires_analysis is False


def test_no_failed_jobs() -> None:
    hit = classify_failure(
        raw_logs=[],
        cleaned_lines=[],
        conclusions=[],
        failed_step_names=[],
        log_line_counts=[],
        no_failed_jobs=True,
    )
    assert hit.short_circuit == "no_failed_jobs"
    assert hit.requires_analysis is False


_CRASH_LINES = [
    "panic: runtime error: invalid memory address or nil pointer dereference",
    "[signal SIGSEGV: segmentation violation code=0x1 addr=0x0 pc=0x480000]",
]

_TIMEOUT_LINES = [
    "Run sleep 300",
    "sleep 300",
    "##[error]The operation was canceled.",
    "Terminate orphan process: pid (1815) (sleep)",
]


def test_crash_nil_pointer_not_compile() -> None:
    hit = classify_lines(_CRASH_LINES)
    assert hit.category == "crash"
    assert hit.confidence == "high"
    assert hit.is_infra_vs_code == "code"
    assert "compile" not in hit.other_matches

    combined = classify_failure(
        raw_logs=["\n".join(_CRASH_LINES) + "\n"],
        cleaned_lines=[_CRASH_LINES],
        conclusions=["failure"],
        failed_step_names=["Run"],
        log_line_counts=[len(_CRASH_LINES)],
    )
    assert combined.category == "crash"
    assert combined.confidence == "high"
    assert combined.short_circuit is None
    assert combined.requires_analysis is True


def test_timeout_minutes_cancel_is_not_infra_runner() -> None:
    raw = "\n".join(_TIMEOUT_LINES) + "\n"
    hit = classify_failure(
        raw_logs=[raw],
        cleaned_lines=[_TIMEOUT_LINES],
        conclusions=["cancelled"],
        failed_step_names=[None],
        log_line_counts=[4],
        step_conclusions=["cancelled"],
        durations=[72],
        timeout_minutes=1,
    )
    assert hit.category == "timeout"
    assert hit.short_circuit is None
    assert hit.requires_analysis is True
    assert hit.is_infra_vs_code == "infra"


def test_shutdown_signal_is_infra_runner() -> None:
    raw = "The runner has received a shutdown signal\nmore log\n"
    hit = classify_failure(
        raw_logs=[raw],
        cleaned_lines=[["The runner has received a shutdown signal", "more log"]],
        conclusions=["failure"],
        failed_step_names=["Build"],
        log_line_counts=[200],
    )
    assert hit.category == "infra_runner"
    assert hit.short_circuit == "infra_runner"
    assert hit.requires_analysis is False


def test_job_not_acquired_and_deprovision_are_infra_runner() -> None:
    for raw in (
        "The job was not acquired\n",
        "Received request to deprovision\n",
    ):
        hit = classify_failure(
            raw_logs=[raw],
            cleaned_lines=[[raw.strip()]],
            conclusions=["failure"],
            failed_step_names=[None],
            log_line_counts=[12],
        )
        assert hit.category == "infra_runner"
        assert hit.short_circuit == "infra_runner"


def test_short_log_no_failed_step_without_timeout_is_infra_runner() -> None:
    raw = "\n".join(f"line {i}" for i in range(12))
    hit = classify_failure(
        raw_logs=[raw],
        cleaned_lines=[raw.splitlines()],
        conclusions=["failure"],
        failed_step_names=[None],
        log_line_counts=[12],
    )
    assert hit.category == "infra_runner"
    assert hit.short_circuit == "infra_runner"
