"""Phase 2 analyze: gate, cache, citations, redaction. Offline only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.rca.analyze import analyze_summary, cache_key, write_analysis
from tools.rca.budget import token_count
from tools.rca.cli import main
from tools.rca.config import PROMPT_VERSION
from tools.rca.models import Summary
from tools.rca.prompt import build_evidence
from tools.rca.redact import REPLACEMENT

_FIXTURE = "tests/rca/fixtures/sample-failure"
_OK_COMPLETION = "tests/rca/fixtures/analyze/ok-completion.json"
_QUOTE = "npm ERR! ERESOLVE could not resolve"
_INVENTED = "this citation was never in the collected evidence xyzzy"


def _collect(tmp_path: Path) -> Path:
    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--from-fixture",
            _FIXTURE,
            "--out",
            str(out),
            "--history-backend",
            "none",
            "--drain-dir",
            str(tmp_path / "drain"),
        ]
    )
    assert rc == 0
    return out


def _summary(out: Path) -> Summary:
    return Summary.model_validate_json((out / "summary.json").read_text(encoding="utf-8"))


def _result(quote: str, *, root: str = "npm ERESOLVE") -> dict[str, object]:
    return {
        "root_cause": root,
        "suggested_fix": "pin the conflicting dependency",
        "confidence": "high",
        "citations": [{"quote": quote, "source": "first_error_window"}],
    }


def test_skip_when_requires_analysis_false(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    payload["verdict"]["requires_analysis"] = False
    (out / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    rc = main(
        [
            "analyze",
            "--summary",
            str(out / "summary.json"),
            "--out",
            str(out),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert rc == 0
    analysis = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["status"] == "gated"
    assert analysis["cache_hit"] is False
    assert analysis["result"] is None
    merged = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert merged["analysis"]["status"] == "gated"
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "## AI diagnosis" in md
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_cache_hit_skips_http(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    cache = tmp_path / "cache"
    first = main(
        [
            "analyze",
            "--summary",
            str(out / "summary.json"),
            "--out",
            str(out),
            "--cache-dir",
            str(cache),
            "--from-completion",
            _OK_COMPLETION,
        ]
    )
    assert first == 0
    assert json.loads((out / "analysis.json").read_text(encoding="utf-8"))["status"] == "ok"
    second = main(
        [
            "analyze",
            "--summary",
            str(out / "summary.json"),
            "--out",
            str(out),
            "--cache-dir",
            str(cache),
        ]
    )
    assert second == 0
    analysis = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["status"] == "cached"
    assert analysis["cache_hit"] is True
    assert "ERESOLVE" in analysis["result"]["root_cause"]


def test_ungrounded_citation_repair_succeeds(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    evidence = build_evidence(summary)
    assert _QUOTE in evidence
    assert evidence.startswith("<EVIDENCE>")
    assert evidence.endswith("</EVIDENCE>")
    assert token_count(evidence) <= 6000 + 20

    completions = [_result(_INVENTED), _result(_QUOTE)]
    record = analyze_summary(
        summary,
        from_completion=completions,
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert record.status == "ok"
    assert record.result is not None
    assert record.result.citations[0].quote == _QUOTE
    assert any("ungrounded" in note for note in record.notes)
    assert record.fallback_used is False


def test_invented_citation_after_repair_unvalidated(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    record = analyze_summary(
        summary,
        from_completion=[_result(_INVENTED), _result(_INVENTED, root="still invented")],
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert record.status == "unvalidated"
    assert record.result is not None
    assert _INVENTED in record.result.citations[0].quote
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "## AI diagnosis" in md
    assert "unvalidated" in md


def test_redact_sk_live_in_completion(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    secret = "sk-live-abc123"
    record = analyze_summary(
        summary,
        from_completion=[_result(_QUOTE, root=f"leaked {secret} in the install")],
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert record.status == "ok"
    assert record.result is not None
    assert secret not in record.result.root_cause
    assert REPLACEMENT in record.result.root_cause
    blob = (out / "analysis.json").read_text(encoding="utf-8")
    md = (out / "summary.md").read_text(encoding="utf-8")
    merged = (out / "summary.json").read_text(encoding="utf-8")
    for text in (blob, md, merged):
        assert secret not in text


def test_from_completion_fixture_cli(tmp_path: Path, monkeypatch) -> None:
    out = _collect(tmp_path)
    gh_out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    rc = main(
        [
            "analyze",
            "--summary",
            str(out / "summary.json"),
            "--out",
            str(out),
            "--from-completion",
            _OK_COMPLETION,
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert rc == 0
    analysis = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["status"] == "ok"
    assert analysis["persona"] == "trinity_for_api"
    assert analysis["result"]["citations"][0]["quote"] == _QUOTE
    merged = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert merged["analysis"]["status"] == "ok"
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "## AI diagnosis" in md
    assert "Root cause" in md
    text = gh_out.read_text(encoding="utf-8")
    assert "analysis-status=ok\n" in text
    assert "rca-confidence=high\n" in text
    assert "root-cause=" in text
    assert "suggested-fix=" in text
    assert "STGPT_API" not in text


def test_analyze_error_exits_zero_unless_strict(tmp_path: Path) -> None:
    out = tmp_path / "rca"
    rc = main(
        [
            "analyze",
            "--summary",
            str(tmp_path / "missing.json"),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    analysis = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["status"] == "failed"
    rc_strict = main(
        [
            "analyze",
            "--strict",
            "--summary",
            str(tmp_path / "missing.json"),
            "--out",
            str(out),
        ]
    )
    assert rc_strict == 1


def test_cache_key_is_sha256_prefix() -> None:
    from datetime import datetime, timezone

    from tools.rca.models import (
        BudgetReport,
        Classification,
        FailedJob,
        RunMeta,
        Verdict,
    )

    summary = Summary(
        collector_version="0.1.0",
        collected_at=datetime.now(timezone.utc),
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
            category="dependency",
            confidence="high",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="code",
        ),
        failed_jobs=[
            FailedJob(
                job_id=1,
                name="build",
                failed_step_name="Install",
                failed_step_number=1,
                exit_code=1,
                duration_seconds=1,
            )
        ],
        fingerprint="finefinefinefine",
        fingerprint_coarse="coarsecoarsecoar",
        budget_report=BudgetReport(),
    )
    raw = f"finefinefinefine|dependency||{PROMPT_VERSION}"
    assert cache_key(summary) == hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert len(cache_key(summary)) == 16
