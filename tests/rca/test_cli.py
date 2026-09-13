"""Offline collect --from-fixture and action output wiring."""

from __future__ import annotations

import json
from pathlib import Path

from tools.rca.cli import main
from tools.rca.outputs import OUTPUT_KEYS


def test_collect_smoke_fixture(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "rca"
    gh_out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    rc = main(
        [
            "collect",
            "--from-fixture",
            "tests/rca/fixtures/smoke",
            "--out",
            str(out),
            "--history-backend",
            "none",
        ]
    )
    assert rc == 0
    assert (out / "summary.json").is_file()
    assert (out / "summary.md").is_file()
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["kubernetes"] is None
    text = gh_out.read_text(encoding="utf-8")
    assert "requires-analysis=" in text
    assert "short-circuit=" in text


def test_collect_from_fixture(tmp_path: Path) -> None:
    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--from-fixture",
            "tests/rca/fixtures/sample-failure",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert payload["schema_version"] == "1.0"
    assert payload["kubernetes"] is None
    assert payload["run"]["run_id"] == 4821
    assert payload["run"]["pr_number"] == 418
    assert payload["run"]["failed_job_total"] == 1
    job = payload["failed_jobs"][0]
    assert job["name"] == "build (node-20)"
    assert job["failed_step_name"] == "Install dependencies"
    assert job["log_unavailable"] is False
    assert job["log_lines_clean"] > 0
    assert job["runner"]["image"] == "ubuntu-24.04@20260818.1.0"
    assert job["runner"]["os"] == "Ubuntu 24.04.2 LTS"
    assert job["steps"][1]["suspected_cache_miss"] is True
    assert payload["classification"]["category"] == "dependency"
    assert payload["classification"]["is_infra_vs_code"] == "code"
    assert payload["verdict"]["requires_analysis"] is True
    assert payload["drain"] is not None
    assert payload["drain"]["baseline_available"] is False
    for tmpl in payload["drain"]["templates"]:
        assert tmpl["is_novel"] is None
        assert tmpl["baseline_count"] is None
        assert tmpl["tier"] not in ("T1", "T2")
    assert payload["fingerprint"]
    assert payload["changes"] is None
    assert payload["history"] is not None
    assert payload["history"]["last_success_sha"] is None
    assert "CI Failure Report" in md
    assert "## Verdict" in md
    assert "## Heuristic Classification" in md
    assert "Install dependencies" in md
    assert "sk-" not in md
    assert "Bearer " not in (out / "summary.json").read_text(encoding="utf-8")


def test_collect_writes_github_output(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "rca"
    gh_out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    rc = main(
        [
            "collect",
            "--from-fixture",
            "tests/rca/fixtures/sample-failure",
            "--out",
            str(out),
            "--token-budget",
            "6000",
            "--drain-dir",
            str(tmp_path / "drain"),
            "--history-dir",
            str(tmp_path / "hist"),
            "--history-backend",
            "cache",
        ]
    )
    assert rc == 0
    text = gh_out.read_text(encoding="utf-8")
    for key in OUTPUT_KEYS:
        assert f"{key}=" in text
    assert "category=dependency" in text
    assert "requires-analysis=true" in text
    assert "failed-job-count=1" in text
    assert "recurrence=new" in text
    assert "seen-count=0" in text
    assert "fingerprint=" in text


def test_collect_error_still_writes_files_and_exits_zero(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "rca"
    gh_out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    rc = main(
        [
            "collect",
            "--from-fixture",
            str(tmp_path / "missing-fixture"),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert payload["classification"]["category"] == "unknown"
    assert payload["verdict"]["requires_analysis"] is True
    assert (out / "summary.md").is_file()
    text = gh_out.read_text(encoding="utf-8")
    assert "category=unknown" in text
    assert "requires-analysis=true" in text


def test_collect_strict_exits_nonzero_after_writing(tmp_path: Path) -> None:
    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--strict",
            "--from-fixture",
            str(tmp_path / "missing-fixture"),
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    assert (out / "summary.json").is_file()
    assert (out / "summary.md").is_file()


def test_refingerprint_dry_run(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from tools.rca.history import CacheHistoryStore
    from tools.rca.models import FailureRecord

    store = CacheHistoryStore(tmp_path / "hist")
    store.upsert(
        FailureRecord(
            fingerprint="0" * 16,
            fingerprint_coarse="1" * 16,
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
            category="dependency",
            templates=["npm ERR! ERESOLVE"],
            template_hashes=["abcdabcdabcdabcd"],
            masking_config_hash="oldoldoldold",
        )
    )
    store.close()
    rc = main(
        [
            "refingerprint",
            "--dry-run",
            "--history-dir",
            str(tmp_path / "hist"),
        ]
    )
    assert rc == 0


def test_train_from_fixture_exits_zero(tmp_path: Path) -> None:
    out = tmp_path / "rca"
    rc = main(
        [
            "train",
            "--from-fixture",
            "tests/rca/fixtures/sample-failure",
            "--out",
            str(out),
            "--drain-dir",
            str(tmp_path / "drain"),
        ]
    )
    assert rc == 0
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert payload["verdict"]["requires_analysis"] is False
