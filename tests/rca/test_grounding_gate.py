"""Step 2 — citation-grounding gate: the answer must cite the failed step.

Grounding is judged structurally: is a citation quote a verbatim substring of
the failed-step anchor (primary_failure_line + failed_step_excerpt + the
first_error/merged window)? Nothing keyword-specific — the "cause" and
"symptom" strings below are arbitrary placeholders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tools.rca.analyze import _as_chat_result, analyze_summary
from tools.rca.models import (
    BudgetReport,
    ChangeContext,
    Classification,
    DeterministicDiagnosis,
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
from tools.rca.outputs import analysis_outputs
from tools.rca.stgpt_client import ChatResult

_CAUSE = "STEP-CAUSE config key MISSING_TOKEN is required but was unset"
_EXIT = "Process completed with exit code 1."
_SYMPTOM = "SYMPTOM unable to remove stale lock after the step already exited"


def _caller(completions: list[dict[str, Any]]):
    """A counting from_completion-style stub. Returns (fn, calls, remaining)."""
    remaining = [_as_chat_result(item) for item in completions]
    calls: list[str] = []

    def _fn(persona: str, _messages: object) -> ChatResult:
        calls.append(persona)
        return remaining.pop(0) if remaining else ChatResult(200, {}, None, None)

    return _fn, calls, remaining


def _cite(quote: str, source: str) -> dict[str, str]:
    return {"quote": quote, "source": source}


def _completion(root: str, quote: str, source: str, *, confidence: str = "high") -> dict[str, Any]:
    return {
        "root_cause": root,
        "suggested_fix": "fix the named cause",
        "confidence": confidence,
        "citations": [_cite(quote, source)],
    }


def _summary(
    *,
    drain: DrainReport | None = None,
    pipeline: list[PipelineLogStream] | None = None,
    changes: ChangeContext | None = None,
    diagnosis: DeterministicDiagnosis | None = None,
    anchored_evidence: bool = True,
) -> Summary:
    if anchored_evidence:
        windows = [
            LogWindow(
                label="first_error",
                start_line=1,
                end_line=2,
                total_lines=2,
                content=f"{_CAUSE}\n{_EXIT}",
            )
        ]
        error_lines = [ErrorLine(line_number=1, text=_CAUSE)]
    else:
        windows = []
        error_lines = []
    job = FailedJob(
        job_id=1,
        name="release",
        failed_step_name="Publish Package",
        failed_step_number=5,
        exit_code=1,
        duration_seconds=10,
        windows=windows,
        error_lines=error_lines,
        failed_step_excerpt=FailedStepExcerpt(name="Publish Package", lines=[_CAUSE, _EXIT]),
        primary_failure_line=_CAUSE,
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
        drain=drain,
        pipeline_logs=pipeline or [],
        changes=changes,
        diagnosis=diagnosis,
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
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
                first_line=40,
                has_error_match=True,
                tier="T1",
                representative_line=_SYMPTOM,
            )
        ],
    )


# --- 1. ungrounded → focused re-ask → grounded ------------------------------


def test_ungrounded_then_focused_reask_becomes_grounded(tmp_path) -> None:
    summary = _summary(drain=_drain_with_symptom())
    caller, calls, remaining = _caller(
        [
            _completion("blamed the cleanup step", _SYMPTOM, "log_templates"),
            _completion(f"the step failed because {_CAUSE}", _CAUSE, "failed_step_excerpt"),
        ]
    )
    record = analyze_summary(summary, chat_fn=caller, cache_dir=tmp_path / "cache")

    assert len(calls) == 2  # first answer + one focused re-ask
    assert not remaining
    assert record.status == "ok"
    assert record.grounded is True
    assert record.result is not None
    assert _CAUSE in record.result.root_cause
    assert record.result.source in {"ai", "hybrid"}


# --- 2. grounded first time → no re-ask -------------------------------------


def test_grounded_first_time_skips_reask(tmp_path) -> None:
    summary = _summary(drain=_drain_with_symptom())
    caller, calls, remaining = _caller(
        [
            _completion(f"the step failed because {_CAUSE}", _CAUSE, "failed_step_excerpt"),
            _completion("should never be consumed", _SYMPTOM, "log_templates"),
        ]
    )
    record = analyze_summary(summary, chat_fn=caller, cache_dir=tmp_path / "cache")

    assert len(calls) == 1  # no focused re-ask
    assert len(remaining) == 1  # the second completion is untouched
    assert record.grounded is True
    assert record.result is not None
    assert record.result.confidence == "high"  # grounded + cited may stay high


# --- 3. still ungrounded after re-ask, deterministic grounded → det wins -----


def test_deterministic_wins_when_model_stays_ungrounded(tmp_path) -> None:
    diagnosis = DeterministicDiagnosis(
        rule_id="R18",
        one_liner=_CAUSE,
        fix_one_liner="set the missing config key",
    )
    summary = _summary(drain=_drain_with_symptom(), diagnosis=diagnosis)
    caller, calls, _remaining = _caller(
        [
            _completion("blamed the cleanup step", _SYMPTOM, "log_templates"),
            _completion("still blaming cleanup", _SYMPTOM, "log_templates"),
        ]
    )
    record = analyze_summary(summary, chat_fn=caller, cache_dir=tmp_path / "cache")

    assert record.result is not None
    # Step 12b: the replacement card is 100% deterministic (model text only in
    # notes), so it is labelled deterministic, not hybrid.
    assert record.result.source == "deterministic"
    assert analysis_outputs(record, summary)["diagnosis-source"] == "deterministic"
    assert record.grounded is True  # the chosen (deterministic) card cites the step
    assert record.result.confidence in {"low", "medium"}
    # The model's text is preserved for the record, not thrown away.
    assert any("blamed the cleanup step" in note for note in record.notes)


# --- 4. confidence cap: ungrounded final answer is never high ---------------


def test_ungrounded_final_answer_confidence_is_capped(tmp_path) -> None:
    # Templates present (a focused re-ask is attempted) but no grounded
    # deterministic card exists, so the ungrounded model answer is kept — capped.
    summary = _summary(drain=_drain_with_symptom(), anchored_evidence=False)
    caller, _calls, _remaining = _caller(
        [
            _completion("blamed the cleanup step", _SYMPTOM, "log_templates", confidence="high"),
            _completion("still blaming cleanup", _SYMPTOM, "log_templates", confidence="high"),
        ]
    )
    record = analyze_summary(summary, chat_fn=caller, cache_dir=tmp_path / "cache")

    assert record.grounded is False
    assert record.result is not None
    assert record.result.confidence != "high"


# --- 5. no-op safety: nothing to remove → no extra call ---------------------


def test_no_templates_no_extra_call(tmp_path) -> None:
    # No templates/pipeline to drop, so focused == full evidence: no re-ask.
    # The model cites change_context (in evidence, outside the anchor) → ungrounded.
    changes = ChangeContext(head_sha="abc", classes=["ci_config"])
    summary = _summary(changes=changes, anchored_evidence=False)
    caller, calls, remaining = _caller(
        [_completion("the workflow changed", "classes=ci_config", "change_context")]
    )
    record = analyze_summary(summary, chat_fn=caller, cache_dir=tmp_path / "cache")

    assert len(calls) == 1  # no extra model call
    assert not remaining
    assert record.status == "ok"
    assert record.grounded is False
    assert record.result is not None
    assert record.result.confidence != "high"
