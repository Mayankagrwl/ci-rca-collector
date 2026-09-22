"""Step 1 — RCA evidence is anchored on the GitHub-designated failed step.

The fixture models a job whose failed step ("Check Version in Artifactory")
holds the real cause (a `_SEMANTIC_CAUSE` line + `Process completed with exit
code 1`), while an *earlier*, unrelated step holds a different error-ish line
(a `curl … manifest.json` 404 check) that a naive whole-log
`_first_error_index` would center the first-error window on. Nothing about
Artifactory / package.json / this pipeline is hardcoded in the collector — the
anchoring keys off the GitHub step name and the existing regex constants only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.rca.diagnose import DeterministicVerdict, specific_log_cause, user_facing
from tools.rca.extract import extract_from_lines, failed_step_excerpt_lines
from tools.rca.models import (
    BudgetReport,
    ChangeContext,
    Classification,
    FailedJob,
    FailedStepExcerpt,
    RunMeta,
    Summary,
    Verdict,
)

_FIXTURE = Path("tests/rca/fixtures/failed-step-anchoring/job.log")
_STEP = "Check Version in Artifactory"
_CAUSE = "This release already exists on Artifactory. You need to update package.json"
_EARLIER = "manifest.json not found"


def _lines() -> list[str]:
    return _FIXTURE.read_text(encoding="utf-8").splitlines()


def _first_error(extracted) -> str:
    window = next(
        item for item in extracted.windows if item.label in {"first_error", "merged"}
    )
    return window.content


# --- 1. extraction is anchored on the failed step ---------------------------


def test_excerpt_holds_the_semantic_cause_line() -> None:
    excerpt = failed_step_excerpt_lines(_lines(), step_name=_STEP)
    assert any(_CAUSE in line for line in excerpt)


def test_first_error_window_is_centered_inside_the_failed_step_group() -> None:
    extracted = extract_from_lines(_lines(), failed_step_name=_STEP)
    content = _first_error(extracted)
    assert _CAUSE in content
    # The earlier, unrelated curl check must NOT capture the window.
    assert _EARLIER not in content


def test_error_lines_are_scoped_to_the_failed_step_group() -> None:
    extracted = extract_from_lines(_lines(), failed_step_name=_STEP)
    texts = [item.text for item in extracted.error_lines]
    assert any(_CAUSE in text for text in texts)
    assert not any(_EARLIER in text for text in texts)


def test_primary_failure_line_equals_the_semantic_cause_line() -> None:
    extracted = extract_from_lines(_lines(), failed_step_name=_STEP)
    assert extracted.primary_failure_line == _CAUSE


# --- 2. the deterministic answer follows the anchored evidence --------------


def _summary_from_extraction() -> Summary:
    extracted = extract_from_lines(_lines(), failed_step_name=_STEP)
    job = FailedJob(
        job_id=1,
        name="release",
        failed_step_name=_STEP,
        failed_step_number=5,
        exit_code=extracted.exit_code,
        duration_seconds=12,
        windows=extracted.windows,
        error_lines=extracted.error_lines,
        annotations=extracted.annotations,
        failed_step_excerpt=FailedStepExcerpt(name=_STEP, lines=extracted.excerpt_lines),
        primary_failure_line=extracted.primary_failure_line,
    )
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    return Summary(
        collector_version="0.1.0",
        collected_at=now,
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
            category="unknown",
            confidence="low",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
        ),
        failed_jobs=[job],
        # A CI-config-only change is exactly what steers the R14 "workflow
        # changed" fallback; the anchored cause must still win over it.
        changes=ChangeContext(head_sha="abc", classes=["ci_config"]),
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )


def test_specific_log_cause_finds_the_semantic_line() -> None:
    summary = _summary_from_extraction()
    assert specific_log_cause(summary) == _CAUSE


def test_deterministic_answer_is_the_cause_not_the_generic_r14_text() -> None:
    summary = _summary_from_extraction()
    # An R14-style verdict is what produced the generic "workflow changed"
    # answer before this slice; the anchored cause must override it.
    verdict = DeterministicVerdict(
        category="unknown",
        confidence="low",
        is_infra_vs_code="unknown",
        requires_analysis=True,
        rule_id="R14",
        one_liner="The CI workflow or action definition changed in this range.",
    )
    result = user_facing(verdict, summary)
    assert "already exists" in result.root_cause.lower()
    assert "workflow or action definition changed" not in result.root_cause.lower()


# --- 3. regression: no group / no step name keeps whole-log behaviour -------


def test_whole_log_behaviour_is_unchanged_without_a_failed_step() -> None:
    extracted = extract_from_lines(_lines())
    content = _first_error(extracted)
    # With no failed step to anchor on, the naive whole-log scan centers on the
    # earliest cause-ish line — the earlier curl check — exactly as before.
    assert _EARLIER in content
    assert extracted.primary_failure_line == (
        "curl: (22) The requested URL returned error: 404 - manifest.json not found"
    )
