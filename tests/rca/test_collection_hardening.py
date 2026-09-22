"""Step 7 — collection-side hardening (7a error-centered trim, 7b JUnit collapse,
7c stack-frame ordering, 7d entropy backstop, 7e deferred download gate).

Each part is independent and tested in isolation. No hardcoded failure strings.
"""

from __future__ import annotations

from tools.rca.budget import token_count, trim_around, trim_middle
from tools.rca.extract import order_stack_traces_by_paths
from tools.rca.junit import collapse_failures, parse_junit_xml
from tools.rca.models import JUnitFailure, StackTrace
from tools.rca.redact import redact_text

_CAUSAL = "ERROR: the real root cause is right here in the middle"


# --- 7a. error-centered trimming --------------------------------------------


def _long_window_with_middle_cause() -> str:
    head = "\n".join(f"INFO noise line {i} padding padding padding" for i in range(60))
    tail = "\n".join(f"INFO trailing line {i} padding padding padding" for i in range(60))
    return f"{head}\n{_CAUSAL}\n{tail}"


def test_trim_around_keeps_the_middle_causal_line() -> None:
    text = _long_window_with_middle_cause()
    cap = 40
    around, trimmed, before, after = trim_around(text, cap)
    assert trimmed is True
    assert _CAUSAL in around
    # A head+tail middle-elision would have dropped exactly this line.
    middle, *_ = trim_middle(text, cap)
    assert _CAUSAL not in middle
    # Stays within the cap (plus the small elision markers).
    assert after <= cap + 8


def test_trim_around_falls_back_when_no_anchor() -> None:
    text = "\n".join(f"plain info line {i} nothing notable here" for i in range(200))
    cap = 20
    around, trimmed, _b, _a = trim_around(text, cap)
    middle, m_trimmed, _mb, _ma = trim_middle(text, cap)
    assert trimmed and m_trimmed
    assert around == middle  # no error anchor → identical to middle-elision


def test_apply_budget_uses_error_centered_for_first_error_only() -> None:
    from datetime import datetime, timezone

    from tools.rca.budget import apply_budget
    from tools.rca.models import (
        BudgetReport,
        Classification,
        FailedJob,
        LogWindow,
        RunMeta,
        Summary,
        Verdict,
    )

    text = _long_window_with_middle_cause()
    job = FailedJob(
        job_id=1,
        name="build",
        failed_step_name="Build",
        failed_step_number=2,
        exit_code=1,
        duration_seconds=5,
        windows=[
            LogWindow(label="first_error", start_line=1, end_line=1, total_lines=1, content=text),
            LogWindow(label="tail", start_line=1, end_line=1, total_lines=1, content=text),
        ],
    )
    summary = Summary(
        collector_version="0.1.0",
        collected_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        run=RunMeta(run_id=1, run_attempt=1, workflow_name="CI", html_url="https://x/1",
                    event="push", actor="bot", head_sha="a", head_branch="main",
                    failed_job_total=1, failed_jobs_analysed=1),
        verdict=Verdict(requires_analysis=True),
        classification=Classification(category="unknown", confidence="low", matched_pattern=None,
                                      matched_line=None, is_infra_vs_code="unknown"),
        failed_jobs=[job],
        fingerprint="f" * 16,
        fingerprint_coarse="c" * 16,
        budget_report=BudgetReport(),
    )
    apply_budget(summary, total_cap=200)
    first = next(w for w in summary.failed_jobs[0].windows if w.label == "first_error")
    tail = next(w for w in summary.failed_jobs[0].windows if w.label == "tail")
    assert _CAUSAL in first.content  # error-centered kept the cause
    assert "… middle elided …" in tail.content  # tail still uses middle-elision


# --- 7b. JUnit collapse-by-message ------------------------------------------


def _xml_with_repeated_message(n: int) -> str:
    cases = "\n".join(
        f'<testcase classname="pkg.M" name="test_{i}">'
        f'<failure message="boom: same root cause">stack</failure></testcase>'
        for i in range(n)
    )
    distinct = (
        '<testcase classname="pkg.M" name="test_other">'
        '<failure message="a different failure">other</failure></testcase>'
    )
    return f'<testsuite tests="{n + 1}" failures="{n + 1}">{cases}{distinct}</testsuite>'


def test_junit_collapses_shared_message_and_preserves_total() -> None:
    report = parse_junit_xml(_xml_with_repeated_message(12))
    assert report.total_failures == 13  # true count unchanged
    messages = {f.message for f in report.failures}
    assert "boom: same root cause" in messages
    assert "a different failure" in messages  # distinct preserved
    rep = next(f for f in report.failures if f.message == "boom: same root cause")
    assert rep.count == 12
    other = next(f for f in report.failures if f.message == "a different failure")
    assert other.count == 1


def test_collapse_is_count_aware_across_merge() -> None:
    # Two already-collapsed reps of the same message sum their counts.
    reps = [
        JUnitFailure(classname="p.M", name="t1", message="X fails", count=6),
        JUnitFailure(classname="p.M", name="t9", message="x   fails", count=6),  # normalizes equal
    ]
    collapsed = collapse_failures(reps)
    assert len(collapsed) == 1
    assert collapsed[0].count == 12


# --- 7c. stack-frame relevance ordering -------------------------------------


def test_changed_path_stack_trace_is_ordered_first() -> None:
    changed = StackTrace(
        headline="ValueError: bad",
        content='  File "app/service.py", line 20, in handle\n    raise ValueError("bad")',
    )
    unrelated = StackTrace(
        headline="RuntimeError: later",
        content='  File "lib/vendor/other.py", line 5, in run\n    do()',
    )
    ordered = order_stack_traces_by_paths([unrelated, changed], ["app/service.py"])
    assert ordered[0] is changed  # relevant leads
    assert len(ordered) == 2 and unrelated in ordered  # nothing dropped


def test_stack_ordering_no_changes_is_identity() -> None:
    a = StackTrace(headline="A", content='File "x.py", line 1')
    b = StackTrace(headline="B", content='File "y.py", line 2')
    assert order_stack_traces_by_paths([a, b], []) == [a, b]


# --- 7d. redaction entropy backstop -----------------------------------------


def test_bare_high_entropy_token_is_redacted() -> None:
    secret = "AbCd1234EfGh5678IjKl9012MnOp3456QrStUvWx"  # 40, mixed, not hex, no _/-
    out, n = redact_text(f"value={secret}")
    # keyword 'value' isn't in the allow-list; the entropy backstop catches it.
    assert n >= 1
    assert secret not in out


def test_entropy_backstop_does_not_over_redact() -> None:
    sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # 40-char SHA
    path = "src/main/java/com/example/service/OrderServiceImpl.java"
    ident = "some_ordinary_configuration_identifier"
    for token in (sha, path, ident):
        out, n = redact_text(token)
        assert n == 0
        assert out == token


# --- 7e. deferred: default behaviour unchanged, TODO recorded ---------------


def test_7e_deferred_todo_present_and_download_not_gated() -> None:
    from pathlib import Path

    source = Path("tools/rca/cli.py").read_text(encoding="utf-8")
    assert "TODO(7e)" in source
    assert "RCA_GATE_PIPELINE_DOWNLOAD" in source
    # The flag is only named in the TODO; it is never actually consulted yet,
    # so the current download behaviour is unchanged.
    assert 'RCA_GATE_PIPELINE_DOWNLOAD"' not in source
    assert "getattr(args, \"gate_pipeline_download\"" not in source
