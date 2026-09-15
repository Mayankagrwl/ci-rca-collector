"""Phase 2 analyze: gate, cache, citations, redaction. Offline only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

from tools.rca.analyze import analyze_summary, cache_key, write_analysis
from tools.rca.budget import token_count
from tools.rca.cli import main
from tools.rca.config import PROMPT_VERSION, STGPT_CLIENT_APP_NAME
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
    raw_path = out / "raw-completion.txt"
    assert raw_path.is_file()
    assert secret not in raw_path.read_text(encoding="utf-8")


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


def test_fenced_json_completion_parses(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    fenced = "Here you go:\n```json\n" + json.dumps(_result(_QUOTE)) + "\n```\n"
    record = analyze_summary(
        summary,
        from_completion={"completion": fenced},
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert record.status == "ok"
    assert record.result is not None
    assert record.result.citations[0].quote == _QUOTE
    assert record.fallback_used is False
    assert (out / "raw-completion.txt").is_file()


def test_prose_only_200_publishes_root_cause(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    prose = (
        "The install failed because npm could not resolve a dependency. "
        "Pin the versions in package.json and retry."
    )
    record = analyze_summary(
        summary,
        from_completion={"completion": prose},
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert record.status == "unvalidated"
    assert record.result is not None
    assert record.result.root_cause.startswith("The install failed")
    assert record.result.cannot_determine is True
    assert record.fallback_used is False
    assert any("completion_preview=" in note for note in record.notes)
    merged = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert merged["analysis"]["result"]["root_cause"]
    raw = (out / "raw-completion.txt").read_text(encoding="utf-8")
    assert "The install failed" in raw


def test_camelcase_keys_are_mapped(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    payload = {
        "rootCause": "npm ERESOLVE could not resolve a dependency",
        "suggestedFix": "pin the conflicting package versions",
        "confidence": "High",
        "citations": [{"quote": _QUOTE, "source": "first_error_window"}],
    }
    record = analyze_summary(
        summary,
        from_completion={"completion": json.dumps(payload)},
        cache_dir=tmp_path / "cache",
    )
    assert record.status == "ok"
    assert record.result is not None
    assert record.result.root_cause.startswith("npm ERESOLVE")
    assert record.result.suggested_fix.startswith("pin the conflicting")
    assert record.result.confidence == "high"


def test_parse_error_does_not_call_second_persona(tmp_path: Path) -> None:
    from tools.rca.stgpt_client import ChatResult

    out = _collect(tmp_path)
    summary = _summary(out)
    seen: list[str] = []

    def chat_fn(persona: str, messages: object) -> ChatResult:
        seen.append(persona)
        return ChatResult(200, {"completion": "{not-json"}, "{not-json", None)

    record = analyze_summary(
        summary,
        chat_fn=chat_fn,
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert seen == ["trinity_for_api", "trinity_for_api"]
    assert "alfred_for_api" not in seen
    assert record.status == "unvalidated"
    assert record.fallback_used is False
    assert record.result is not None
    assert record.result.root_cause
    assert record.result.cannot_determine is True
    assert any("parse_error" in note for note in record.notes)
    assert any("completion_preview=" in note for note in record.notes)
    gh_keys = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert gh_keys["analysis"]["result"]["root_cause"]


def test_invalid_application_name_fails_without_root_cause(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    err = "Invalid application name: gtrd_srmtdpplm"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["clientAppName"] == "gtrd_srmtdpplm"
        assert len(payload["clientAppName"]) == 14
        return httpx.Response(
            200,
            json={
                "responseId": "rid-app",
                "errorCode": "VALIDATION",
                "message": err,
                "service": "chat",
                "duration": 5,
            },
        )

    record = analyze_summary(
        summary,
        api_key="test-stgpt-key",
        url="https://stgpt.test.invalid/chatgpt/api/client-apps",
        client_app_name="gtrd_srmtdpplm\n",
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert record.status == "failed"
    assert record.result is None
    blob = " ".join(record.notes)
    assert "Invalid application name" in blob
    assert "clientAppName_repr='gtrd_srmtdpplm'" in blob
    assert "clientAppName_len=14" in blob
    assert "test-stgpt-key" not in blob
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "**Root cause:**" not in md
    assert err in md


def test_errorcode_payload_fails_without_root_cause(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    formats: list[str] = []
    err = "responseFormat must be one of the following values: text, json_object"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        formats.append(payload["responseFormat"])
        assert payload["responseFormat"] in {"json_object", "text"}
        return httpx.Response(
            200,
            json={
                "responseId": "rid-err",
                "errorCode": "VALIDATION",
                "message": err,
                "service": "chat",
                "duration": 8,
            },
        )

    record = analyze_summary(
        summary,
        api_key="test-stgpt-key",
        url="https://stgpt.test.invalid/chatgpt/api/client-apps",
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert formats[0] == "json_object"
    assert formats[0] not in {"json", "JSON", "json-schema", ""}
    assert record.status == "failed"
    assert record.result is None
    blob = " ".join(record.notes)
    assert err in blob
    assert "test-stgpt-key" not in blob
    merged = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert merged["analysis"]["status"] == "failed"
    assert merged["analysis"].get("result") is None
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert err in md
    assert "**Root cause:**" not in md


def test_empty_completion_200_is_failed_with_keys(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content.decode("utf-8"))
        assert body["service"] == "chat"
        assert body["version"]
        assert body["clientAppName"]
        assert body["timestamp"]
        assert body["messages"] == [
            {"role": "user", "content": body["messages"][0]["content"]}
        ]
        assert body["messages"][0]["content"].strip()
        return httpx.Response(
            200,
            json={"completion": "", "responseId": "rid-empty", "foo": "bar"},
        )

    record = analyze_summary(
        summary,
        api_key="test-stgpt-key",
        url="https://stgpt.test.invalid/chatgpt/api/client-apps",
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    assert record.status == "failed"
    assert calls["n"] == 1
    blob = " ".join(record.notes)
    assert "completion_len=0" in blob
    assert "keys=" in blob
    assert "foo" in blob
    assert "responseId=rid-empty" in blob
    assert "user_message_chars=" in blob
    assert "parse_error" not in blob
    assert "test-stgpt-key" not in blob
    assert record.fallback_used is False
    saved = json.loads((out / "stgpt-response.json").read_text(encoding="utf-8"))
    assert saved[0]["completion_len"] == 0
    assert "foo" in saved[0]["keys"]
    assert "test-stgpt-key" not in json.dumps(saved)


def test_http_404_fails_with_status_persona_and_url_note(
    tmp_path: Path, monkeypatch
) -> None:
    out = _collect(tmp_path)
    summary = _summary(out)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert STGPT_CLIENT_APP_NAME not in request.url.path
        return httpx.Response(404, json={"message": "no route to app"})

    gh_out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    record = analyze_summary(
        summary,
        api_key="test-stgpt-key",
        url="https://stgpt.test.invalid/chatgpt/api/client-apps",
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path / "cache",
    )
    write_analysis(record, summary_path=out / "summary.json", out_dir=out)
    from tools.rca.outputs import write_analysis_github_output

    write_analysis_github_output(record, output_file=gh_out)
    assert record.status == "failed"
    assert calls["n"] == 2
    blob = " ".join(record.notes)
    assert "HTTP 404" in blob
    assert "persona=trinity_for_api" in blob
    assert "fallback to alfred_for_api" in blob
    assert "stgpt.test.invalid/chatgpt/api/client-apps" in blob
    assert "no route to app" in blob
    assert "?" not in blob.split("url=", 1)[-1].split(" ", 1)[0]
    assert "test-stgpt-key" not in blob
    assert STGPT_CLIENT_APP_NAME not in "".join(
        n.split("url=", 1)[-1].split(" ", 1)[0] for n in record.notes if "url=" in n
    )
    text = gh_out.read_text(encoding="utf-8")
    assert "analysis-status=failed\n" in text
    assert "HTTP 404" in text
    assert "test-stgpt-key" not in text
    merged = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert merged["analysis"]["status"] == "failed"
    assert any("HTTP 404" in n for n in merged["analysis"]["notes"])


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
