"""Step 4 — refine the AI gate: skip the model only when deterministic is anchored.

`decide_stgpt_call(policy="auto")` skips only for a trustworthy, anchored
deterministic answer (hard short-circuit, an anchored high-confidence signature,
or an exact history resolution). R14/R18 or "no terminal cause" always call.
`always`/`never` override; `from_completion` always calls. No network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tools.rca.analyze import (
    analysis_result_from_summary,
    decide_stgpt_call,
    is_grounded,
    skipped_record,
)
from tools.rca.cli import _build_parser
from tools.rca.models import (
    BudgetReport,
    ChangeContext,
    Classification,
    DeterministicDiagnosis,
    ErrorLine,
    FailedJob,
    FailedStepExcerpt,
    HistoryContext,
    LogWindow,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.prompt import failed_step_anchor_text

_ERESOLVE = "npm ERR! ERESOLVE could not resolve dependency left-pad@1.3.0"
_RUNNER = "The runner has received a shutdown signal"
_GENERIC = "the pipeline step did not complete cleanly"


def _window(content: str) -> LogWindow:
    lines = content.splitlines() or [content]
    return LogWindow(
        label="first_error",
        start_line=1,
        end_line=len(lines),
        total_lines=len(lines),
        content=content,
    )


def _summary(
    *,
    anchor_line: str,
    category: str = "unknown",
    confidence: str = "low",
    short_circuit: str | None = None,
    rule_id: str | None = None,
    requires_analysis: bool = True,
    history: HistoryContext | None = None,
    changes: ChangeContext | None = None,
) -> Summary:
    job = FailedJob(
        job_id=1,
        name="release",
        failed_step_name="Publish Package",
        failed_step_number=5,
        exit_code=1,
        duration_seconds=10,
        windows=[_window(anchor_line)],
        error_lines=[ErrorLine(line_number=1, text=anchor_line)],
        failed_step_excerpt=FailedStepExcerpt(name="Publish Package", lines=[anchor_line]),
        primary_failure_line=anchor_line,
    )
    diagnosis = None
    if rule_id is not None:
        diagnosis = DeterministicDiagnosis(rule_id=rule_id, one_liner=anchor_line)
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
        verdict=Verdict(
            short_circuit=short_circuit,  # type: ignore[arg-type]
            requires_analysis=requires_analysis,
        ),
        classification=Classification(
            category=category,
            confidence=confidence,  # type: ignore[arg-type]
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
        ),
        failed_jobs=[job],
        diagnosis=diagnosis,
        history=history,
        changes=changes,
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )


def _signature_summary() -> Summary:
    """High-confidence dependency signature with an anchored terminal cause."""
    return _summary(
        anchor_line=_ERESOLVE,
        category="dependency",
        confidence="high",
        rule_id="R8",
        requires_analysis=False,
    )


# --- 1. short-circuit → skip ------------------------------------------------


def test_short_circuit_skips_and_yields_grounded_card() -> None:
    summary = _summary(
        anchor_line=_RUNNER,
        category="infra_runner",
        confidence="high",
        short_circuit="infra_runner",
        rule_id="R1",
        requires_analysis=False,
    )
    call, reason = decide_stgpt_call(summary, policy="auto", stgpt_key_present="true")
    assert (call, reason) == (False, "short_circuit")

    record = skipped_record(summary, reason or "")
    assert record.model_called is False
    assert record.result is not None
    assert record.result.source == "deterministic"
    assert is_grounded(record.result, failed_step_anchor_text(summary))


# --- 2. anchored signature → skip -------------------------------------------


def test_anchored_signature_skips() -> None:
    summary = _signature_summary()
    # Precondition: the deterministic card really is grounded on the anchor.
    card = analysis_result_from_summary(summary, source="deterministic")
    assert is_grounded(card, failed_step_anchor_text(summary))

    call, reason = decide_stgpt_call(summary, policy="auto", stgpt_key_present="true")
    assert (call, reason) == (False, "deterministic_sufficient")


def test_history_resolution_skips() -> None:
    summary = _summary(
        anchor_line=_GENERIC,
        history=HistoryContext(match="exact", previous_resolution="pin left-pad@1.3.0"),
    )
    call, reason = decide_stgpt_call(summary, policy="auto", stgpt_key_present="true")
    assert (call, reason) == (False, "history_resolution")


# --- 3. Run-B guard → CALL --------------------------------------------------


def test_r14_config_guess_without_terminal_cause_calls() -> None:
    summary = _summary(
        anchor_line=_GENERIC,
        category="unknown",
        confidence="high",
        rule_id="R14",
        requires_analysis=False,
        changes=ChangeContext(head_sha="abc", classes=["ci_config"]),
    )
    assert decide_stgpt_call(summary, policy="auto", stgpt_key_present="true") == (True, None)


def test_r18_residual_calls() -> None:
    summary = _summary(anchor_line=_GENERIC, category="unknown", rule_id="R18")
    assert decide_stgpt_call(summary, policy="auto", stgpt_key_present="true") == (True, None)


def test_high_confidence_signature_without_terminal_cause_calls() -> None:
    # Signature + high confidence, but the anchor has no terminal cause → call.
    summary = _summary(
        anchor_line=_GENERIC,
        category="dependency",
        confidence="high",
        rule_id="R8",
    )
    assert decide_stgpt_call(summary, policy="auto", stgpt_key_present="true") == (True, None)


# --- 4. policy switches -----------------------------------------------------


def test_policy_always_and_never() -> None:
    summary = _signature_summary()  # auto would skip this
    assert decide_stgpt_call(summary, policy="always", stgpt_key_present="true") == (True, None)
    assert decide_stgpt_call(summary, policy="never", stgpt_key_present="true") == (
        False,
        "policy_never",
    )


# --- 5. replay always calls -------------------------------------------------


def test_from_completion_always_calls_under_auto() -> None:
    summary = _summary(
        anchor_line=_RUNNER,
        category="infra_runner",
        confidence="high",
        short_circuit="infra_runner",
        rule_id="R1",
    )
    # No key needed: from_completion bypasses the key gate and always calls.
    assert decide_stgpt_call(summary, policy="auto", from_completion=[{"completion": "{}"}]) == (
        True,
        None,
    )


# --- 6. fail-open -----------------------------------------------------------


def test_gate_fails_open_when_sufficiency_check_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr("tools.rca.analyze.is_grounded", _boom)
    summary = _signature_summary()  # reaches the is_grounded call inside the gate
    # The internal error is swallowed → the model is called, nothing propagates.
    assert decide_stgpt_call(summary, policy="auto", stgpt_key_present="true") == (True, None)


# --- 7. CLI wiring ----------------------------------------------------------


def test_cli_analyze_policy_parses_with_auto_default() -> None:
    parser = _build_parser()
    args = parser.parse_args(["analyze", "--summary", "s.json", "--out", "out"])
    assert args.analyze_policy == "auto"
    args = parser.parse_args(
        ["analyze", "--summary", "s.json", "--out", "out", "--analyze-policy", "never"]
    )
    assert args.analyze_policy == "never"
