"""Redaction must strip secrets before anything is written."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.rca.models import (
    BudgetReport,
    Classification,
    FailedJob,
    LogWindow,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.redact import REPLACEMENT, redact_summary, redact_text
from tools.rca.render import render_markdown

_SECRET = "Bearer sk-live-abc123"


def test_redact_bearer_token() -> None:
    text, count = redact_text(f"Authorization: {_SECRET}")
    assert _SECRET not in text
    assert "sk-live-abc123" not in text
    assert REPLACEMENT in text
    assert count >= 1


def test_redact_github_and_aws_keys() -> None:
    blob = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 AKIAIOSFODNN7EXAMPLE"
    text, count = redact_text(blob)
    assert "ghp_" not in text
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert count >= 2


def test_secret_never_appears_in_summary_output() -> None:
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
            head_sha="abc1234",
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
        failed_jobs=[
            FailedJob(
                job_id=1,
                name="build",
                failed_step_name="Install",
                failed_step_number=3,
                exit_code=1,
                duration_seconds=10,
                windows=[
                    LogWindow(
                        label="first_error",
                        start_line=1,
                        end_line=1,
                        total_lines=1,
                        content=f"curl failed {_SECRET}",
                    )
                ],
            )
        ],
        fingerprint="",
        fingerprint_coarse="",
        kubernetes=None,
        budget_report=BudgetReport(),
    )
    redacted, count = redact_summary(summary)
    assert count >= 1
    dumped = redacted.model_dump_json()
    md = render_markdown(redacted)
    assert "sk-live-abc123" not in dumped
    assert _SECRET not in dumped
    assert "sk-live-abc123" not in md
    assert _SECRET not in md
    assert REPLACEMENT in dumped


def test_pipeline_logs_secrets_fixture_collect(tmp_path: Path) -> None:
    from tools.rca.cli import main
    from tools.rca.redact import REPLACEMENT as REDACT

    fixture = Path("tests/rca/fixtures/pipeline-logs-secrets")
    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--from-fixture",
            str(fixture),
            "--out",
            str(out),
            "--history-backend",
            "none",
            "--drain-dir",
            str(tmp_path / "drain"),
        ]
    )
    assert rc == 0
    secrets = ("hunter2", "npm_live_abc", "sk-live-abc123")
    texts: list[str] = []
    for name in ("summary.json", "summary.md", "analysis.json"):
        path = out / name
        if path.is_file():
            texts.append(path.read_text(encoding="utf-8"))
    payload = (out / "summary.json").read_text(encoding="utf-8")
    texts.append(payload)
    import json

    summary = json.loads(payload)
    for stream in summary.get("pipeline_logs") or []:
        for window in stream.get("windows") or []:
            texts.append(window.get("content") or "")
        for tmpl in stream.get("templates") or []:
            texts.append(tmpl.get("template") or "")
            texts.append(tmpl.get("representative_line") or "")
            for var in tmpl.get("variables") or []:
                texts.append(" ".join(var.get("values") or []))
    for drain in (summary.get("drain"),):
        if not drain:
            continue
        for tmpl in drain.get("templates") or []:
            texts.append(tmpl.get("template") or "")
            texts.append(tmpl.get("representative_line") or "")
            for var in tmpl.get("variables") or []:
                texts.append(" ".join(var.get("values") or []))
    blob = "\n".join(texts)
    for secret in secrets:
        assert secret not in blob
    assert REDACT in blob


def test_redact_docker_npm_kube_and_registry_tokens() -> None:
    samples = [
        ("DOCKER_PASSWORD=hunter2", "hunter2"),
        ("DOCKER_TOKEN=docktok", "docktok"),
        ("DOCKER_AUTH=authblob", "authblob"),
        ("NPM_TOKEN=npm_live_zzz", "npm_live_zzz"),
        ("NODE_AUTH_TOKEN=node_zzz", "node_zzz"),
        ("DOCKERHUB_TOKEN=dhub_zzz", "dhub_zzz"),
        ("GHCR_TOKEN=ghcr_zzz", "ghcr_zzz"),
        ("GITLAB_TOKEN=glpat-zzz", "glpat-zzz"),
        ("PYPI_TOKEN=pypi-zzz", "pypi-zzz"),
        ("//npm.example/_authToken=npm_live_abc", "npm_live_abc"),
        ("//registry.npmjs.org/:_authToken=tok123", "tok123"),
        ("//host.example/:_password=s3cret", "s3cret"),
        ("_authToken=npm_live_abc", "npm_live_abc"),
        ("_password=s3cret", "s3cret"),
        ("_auth=YmFzZTY0", "YmFzZTY0"),
        ("KUBECONFIG=/tmp/kube", "/tmp/kube"),
        ("type: kubernetes.io/service-account-token", "service-account-token"),
    ]
    for blob, secret in samples:
        text, count = redact_text(blob)
        assert count >= 1, blob
        assert secret not in text, blob
        assert REPLACEMENT in text, blob
    still = redact_text("token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")[0]
    assert "ghp_" not in still
    bearer = redact_text("Authorization: Bearer sk-live-abc123")[0]
    assert "sk-live-abc123" not in bearer
    assert "Bearer " not in bearer or REPLACEMENT in bearer


def test_redact_and_budget_have_no_github_ids() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("redact.py", "budget.py"):
        source = (root / "tools" / "rca" / name).read_text(encoding="utf-8")
        assert "job_id" not in source
        assert "run_id" not in source
        assert "github_api" not in source
