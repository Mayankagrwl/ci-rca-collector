"""Step 10 — production grounding validation (validate.py) + telemetry carry.

check_grounding is pure post-hoc scoring (no model call). The §4.2 normalisation
fixes false-ungrounded on whitespace/case/punctuation. It is distinct from and
complementary to the failed-step anchor grounding (analyze.is_grounded) and the
Step 2 focused re-ask, both of which stay unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tools.rca import validate
from tools.rca.analyze import _attach_validation, analyze_summary, is_grounded
from tools.rca.cli import main
from tools.rca.models import (
    AnalysisCitation,
    AnalysisRecord,
    AnalysisResult,
    BudgetReport,
    ChangeContext,
    Classification,
    CommitInfo,
    ErrorLine,
    FailedJob,
    FailedStepExcerpt,
    LogWindow,
    RunMeta,
    StackTrace,
    Summary,
    Verdict,
)

_EVIDENCE = "### first_error_window\nerror: build failed at line 12\nmore context here"


def _summary(*, changes: ChangeContext | None = None, jobs: list[FailedJob] | None = None) -> Summary:
    return Summary(
        collector_version="0.1.0",
        collected_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        run=RunMeta(run_id=1, run_attempt=1, workflow_name="CI", html_url="https://x/1",
                    event="push", actor="bot", head_sha="a", head_branch="main",
                    failed_job_total=1, failed_jobs_analysed=1),
        verdict=Verdict(requires_analysis=True),
        classification=Classification(category="unknown", confidence="low", matched_pattern=None,
                                      matched_line=None, is_infra_vs_code="unknown"),
        failed_jobs=jobs or [],
        changes=changes,
        fingerprint="f" * 16, fingerprint_coarse="c" * 16, budget_report=BudgetReport(),
    )


def _result(*citations: AnalysisCitation, root: str = "build failed", fix: str = "fix it",
            source: str = "ai") -> AnalysisResult:
    return AnalysisResult(root_cause=root, suggested_fix=fix, confidence="high",
                          citations=list(citations), source=source)


def _cite(quote: str, source: str = "first_error_window") -> AnalysisCitation:
    return AnalysisCitation(quote=quote, source=source)  # type: ignore[arg-type]


# --- 1. fabricated citation lowers the rate ---------------------------------


def test_fabricated_citation_is_ungrounded() -> None:
    res = _result(
        _cite("error: build failed at line 12"),
        _cite("this line was never in the evidence xyz", "log_templates"),
    )
    g = validate.check_grounding(res, _EVIDENCE, _summary())
    assert g.grounded is False
    assert g.citation_count == 2
    assert g.grounding_rate == 0.5
    assert "this line was never in the evidence xyz" in g.ungrounded_citations


# --- 2. §4.2 normalisation flips a false-ungrounded citation to grounded -----


def test_normalisation_flips_false_ungrounded() -> None:
    quote = "  ERROR:   Build FAILED at line 12.  "  # ws + case + trailing punct
    assert quote not in _EVIDENCE  # old raw substring check would fail
    g = validate.check_grounding(_result(_cite(quote)), _EVIDENCE, _summary())
    assert g.grounded is True
    assert g.grounding_rate == 1.0


# --- 3. rate < 0.5 → needs-review via deterministic card, not suppressed -----


def test_low_rate_falls_back_to_needs_review() -> None:
    summary = _summary()
    record = AnalysisRecord(
        status="ok", model_called=True,
        result=_result(_cite("totally invented citation aaa", "log_templates")),
        analyzed_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )
    from tools.rca.analyze import display_status

    finalized = _attach_validation(summary, record, _EVIDENCE)
    assert finalized.result is not None  # section not suppressed
    assert display_status(finalized) == "needs-review"
    assert finalized.grounding is not None and finalized.grounding.grounding_rate < 0.5
    assert any("not grounded in evidence" in n for n in finalized.notes)
    assert any("model root_cause" in n for n in finalized.notes)  # model text kept


# --- 4. 0.5 <= rate < 1.0 → strip ungrounded citations, keep result ----------


def test_partial_rate_strips_ungrounded_and_keeps_result() -> None:
    summary = _summary()
    record = AnalysisRecord(
        status="ok", model_called=True,
        result=_result(
            _cite("error: build failed at line 12"),
            _cite("invented follow-on line bbb", "log_templates"),
        ),
        analyzed_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )
    finalized = _attach_validation(summary, record, _EVIDENCE)
    assert finalized.result is not None
    quotes = [c.quote for c in finalized.result.citations]
    assert "error: build failed at line 12" in quotes
    assert "invented follow-on line bbb" not in quotes  # stripped
    assert any("stripped" in n for n in finalized.notes)


# --- 5. check_grounding makes no model call; re-ask unchanged ----------------


def test_check_grounding_makes_no_model_call() -> None:
    calls: list[int] = []

    def boom(*_a: object, **_k: object):  # would fire if a call were attempted
        calls.append(1)
        raise AssertionError("no model call expected")

    validate.check_grounding(_result(_cite("error: build failed at line 12")), _EVIDENCE, _summary())
    assert calls == []


def test_step2_focused_reask_still_one_call(tmp_path: Path) -> None:
    # Anchored + a real drain template so an ungrounded first answer triggers
    # exactly one focused re-ask (Step 2 behaviour, unchanged by Step 10).
    from tools.rca.models import DrainReport, LogTemplate

    cause = "npm ERR! ERESOLVE could not resolve dependency"
    symptom = "SYMPTOM stale lock could not be removed"
    job = FailedJob(job_id=1, name="build", failed_step_name="Install", failed_step_number=2,
                    exit_code=1, duration_seconds=5,
                    windows=[LogWindow(label="first_error", start_line=1, end_line=1,
                                       total_lines=1, content=cause)],
                    error_lines=[ErrorLine(line_number=1, text=cause)],
                    failed_step_excerpt=FailedStepExcerpt(name="Install", lines=[cause]),
                    primary_failure_line=cause)
    summary = _summary(jobs=[job])
    summary.drain = DrainReport(baseline_available=False, total_clusters=1, tier_counts={"T1": 1},
        templates=[LogTemplate(template_id=1, template=symptom, count=1, first_line=9,
                               has_error_match=True, tier="T1", representative_line=symptom)])

    calls: list[str] = []

    def _caller(completions):
        from tools.rca.analyze import _as_chat_result
        from tools.rca.stgpt_client import ChatResult
        remaining = [_as_chat_result(c) for c in completions]

        def _fn(persona: str, _messages: object) -> ChatResult:
            calls.append(persona)
            return remaining.pop(0) if remaining else ChatResult(200, {}, None, None)

        return _fn

    ungrounded = {"root_cause": "blamed cleanup", "suggested_fix": "x", "confidence": "high",
                  "citations": [{"quote": symptom, "source": "log_templates"}]}
    grounded = {"root_cause": f"resolve failed: {cause}", "suggested_fix": "pin dep",
                "confidence": "high", "citations": [{"quote": cause, "source": "failed_step_excerpt"}]}
    caller = _caller([ungrounded, grounded])
    record = analyze_summary(summary, chat_fn=caller, cache_dir=tmp_path / "cache")
    assert len(calls) == 2  # first answer + exactly one focused re-ask
    assert record.grounded is True


# --- 6/7. component_grounded / shas_grounded absence semantics ---------------


def test_component_grounded_false_and_none() -> None:
    job = FailedJob(job_id=1, name="build-web", failed_step_name="Build", failed_step_number=1,
                    exit_code=1, duration_seconds=1)
    res = _result(root="x", fix="y")
    res.suspected_files = ["src/absent.ts"]
    with_data = _summary(changes=ChangeContext(head_sha="a", files=["src/other.ts"]), jobs=[job])
    assert validate.check_grounding(res, _EVIDENCE, with_data).component_grounded is False
    assert validate.check_grounding(res, _EVIDENCE, _summary()).component_grounded is None


def test_component_grounded_true_from_stack() -> None:
    job = FailedJob(job_id=1, name="build", failed_step_name="Build", failed_step_number=1,
                    exit_code=1, duration_seconds=1,
                    stack_traces=[StackTrace(headline="E", content='File "app/calc.py", line 5')])
    res = _result(root="x", fix="y")
    res.suspected_files = ["app/calc.py"]
    assert validate.check_grounding(res, _EVIDENCE, _summary(jobs=[job])).component_grounded is True


def test_shas_grounded_none_and_false() -> None:
    res = _result(root="regressed in deadbeef1234567", fix="revert it")
    assert validate.check_grounding(res, _EVIDENCE, _summary()).shas_grounded is None
    changes = ChangeContext(head_sha="a", commits=[
        CommitInfo(sha="cafef00d1234567", subject="s",
                   authored_at=datetime(2026, 9, 28, tzinfo=timezone.utc), files_changed=1)])
    assert validate.check_grounding(res, _EVIDENCE, _summary(changes=changes)).shas_grounded is False


# --- 8. broken validator never suppresses the RCA ---------------------------


def test_validator_error_publishes_rca_with_grounding_none(monkeypatch) -> None:
    def boom(*_a: object, **_k: object):
        raise RuntimeError("boom")

    monkeypatch.setattr("tools.rca.validate.check_grounding", boom)
    summary = _summary()
    record = AnalysisRecord(status="ok", model_called=True,
                            result=_result(_cite("error: build failed at line 12")),
                            analyzed_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
    finalized = _attach_validation(summary, record, _EVIDENCE)
    assert finalized.result is not None  # RCA still published
    assert finalized.grounding is None
    assert any("validation skipped" in n for n in finalized.notes)


# --- 9. summary.json analysis block carries grounding + telemetry ------------

_FIXTURE = "tests/rca/fixtures/sample-failure"
_OK_COMPLETION = "tests/rca/fixtures/analyze/ok-completion.json"


def test_summary_json_carries_grounding_and_telemetry(tmp_path: Path) -> None:
    out = tmp_path / "rca"
    assert main(["collect", "--from-fixture", _FIXTURE, "--out", str(out),
                 "--history-backend", "none", "--drain-dir", str(tmp_path / "drain")]) == 0
    assert main(["analyze", "--summary", str(out / "summary.json"), "--out", str(out),
                 "--cache-dir", str(tmp_path / "cache"), "--from-completion", _OK_COMPLETION,
                 "--requires-analysis", "true"]) == 0
    analysis = json.loads((out / "summary.json").read_text(encoding="utf-8"))["analysis"]
    assert "grounding" in analysis and analysis["grounding"] is not None
    assert set(analysis["grounding"]) >= {"grounded", "citation_count", "grounding_rate", "checks_run"}
    assert analysis["validation_telemetry"] is not None
    assert analysis["call_telemetry"] is not None
    assert analysis["call_telemetry"]["prompt_tokens_est"] >= 0
