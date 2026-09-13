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
