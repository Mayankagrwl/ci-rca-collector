"""Offline collect --from-fixture and action output wiring."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from tools.rca.cli import main
from tools.rca.github_api import GitHubClient
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
    notes = " ".join(payload["collection_notes"])
    assert "no success jobs" in notes


def _write_success_train_fixture(root: Path, *, with_log: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "run.json").write_text(
        json.dumps(
            {
                "id": 9100,
                "name": "Test Failure Scenarios",
                "html_url": "https://github.com/acme/widgets/actions/runs/9100",
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "cccccccccccccccccccccccccccccccccccccccc",
                "head_branch": "main",
                "run_attempt": 1,
                "actor": {"login": "tester"},
                "pull_requests": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": 101,
                        "name": "baseline",
                        "conclusion": "success",
                        "status": "completed",
                        "created_at": "2026-09-13T12:00:00Z",
                        "started_at": "2026-09-13T12:00:05Z",
                        "completed_at": "2026-09-13T12:01:10Z",
                        "steps": [
                            {
                                "number": 1,
                                "name": "Set up job",
                                "conclusion": "success",
                            },
                            {
                                "number": 2,
                                "name": "Run noisy log",
                                "conclusion": "success",
                            },
                        ],
                    },
                    {
                        "id": 102,
                        "name": "skipped-leg",
                        "conclusion": "skipped",
                        "status": "completed",
                    },
                    {
                        "id": 103,
                        "name": "cancelled-leg",
                        "conclusion": "cancelled",
                        "status": "completed",
                    },
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "meta.json").write_text(
        json.dumps({"repo": "acme/widgets", "run_id": 9100}) + "\n",
        encoding="utf-8",
    )
    if with_log:
        lines = [
            "2026-09-13T12:00:05.0000000Z Current runner version: '2.329.0'",
            "2026-09-13T12:00:06.0000000Z ##[group]Run noisy",
        ]
        lines.extend(
            f"2026-09-13T12:00:07.{i:07d}Z INFO processing record {i} of 200 [ok]"
            for i in range(1, 201)
        )
        lines.append("2026-09-13T12:01:10.0000000Z ##[error]Process completed with exit code 0.")
        (root / "logs" / "101.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_train_success_baseline_writes_drain_state(tmp_path: Path) -> None:
    fixture = _write_success_train_fixture(tmp_path / "baseline-success")
    out = tmp_path / "rca"
    drain = tmp_path / "drain"
    rc = main(
        [
            "train",
            "--from-fixture",
            str(fixture),
            "--out",
            str(out),
            "--drain-dir",
            str(drain),
        ]
    )
    assert rc == 0
    bins = [path for path in drain.glob("*.bin") if path.stat().st_size > 0]
    assert bins, f"expected Drain3 state under {drain}, found {list(drain.iterdir())}"
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    notes = " ".join(payload["collection_notes"])
    match = re.search(r"trained (\d+) jobs", notes)
    assert match is not None, notes
    assert int(match.group(1)) >= 1
    assert "lines" in notes
    assert payload["verdict"]["requires_analysis"] is False


def test_train_success_jobs_without_logs_notes_why(tmp_path: Path) -> None:
    fixture = _write_success_train_fixture(tmp_path / "baseline-nolog", with_log=False)
    out = tmp_path / "rca"
    rc = main(
        [
            "train",
            "--from-fixture",
            str(fixture),
            "--out",
            str(out),
            "--drain-dir",
            str(tmp_path / "drain"),
        ]
    )
    assert rc == 0
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    notes = " ".join(payload["collection_notes"])
    assert "no logs" in notes


def test_train_drain_dir_uses_workspace_not_action_path(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "ws"
    action = tmp_path / "action"
    workspace.mkdir()
    action.mkdir()
    fixture = _write_success_train_fixture(tmp_path / "baseline-success")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("GITHUB_ACTION_PATH", str(action))
    monkeypatch.chdir(action)
    rc = main(
        [
            "train",
            "--from-fixture",
            str(fixture),
            "--out",
            str(workspace / "rca"),
            "--drain-dir",
            ".drain",
        ]
    )
    assert rc == 0
    bins = list((workspace / ".drain").glob("*.bin"))
    assert bins and all(path.stat().st_size > 0 for path in bins)
    assert not list(action.glob("*.bin"))
    assert not list((action / ".drain").glob("*.bin")) if (action / ".drain").exists() else True


def test_train_live_paginates_and_fetches_success_logs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")
    log_urls: list[str] = []
    body = "\n".join(f"INFO processing record {i} of 80 [ok]" for i in range(1, 81))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if re.search(r"/actions/runs/55$", url):
            return httpx.Response(
                200, json={"id": 55, "name": "CI", "conclusion": "success"}
            )
        if "/actions/runs/55/jobs" in url:
            if "page=2" in url:
                return httpx.Response(
                    200,
                    json={
                        "jobs": [
                            {"id": 202, "name": "baseline", "conclusion": "success"}
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"id": 201, "name": "cancelled-leg", "conclusion": "cancelled"}
                    ]
                },
                headers={
                    "Link": (
                        "<https://api.github.com/repos/acme/widgets/actions/runs/55"
                        '/jobs?per_page=100&page=2>; rel="next"'
                    )
                },
            )
        if "/actions/jobs/" in url and url.endswith("/logs"):
            log_urls.append(url)
            if url.endswith("/actions/jobs/202/logs"):
                return httpx.Response(200, text=body)
            return httpx.Response(200, text="cancelled job log")
        return httpx.Response(404, json={"message": "not found"})

    real = GitHubClient

    def factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        kwargs.setdefault("sleep", lambda _d: None)
        return real(**kwargs)

    monkeypatch.setattr("tools.rca.cli.GitHubClient", factory)
    drain = tmp_path / "drain"
    out = tmp_path / "rca"
    rc = main(
        [
            "train",
            "--run-id",
            "55",
            "--repo",
            "acme/widgets",
            "--out",
            str(out),
            "--drain-dir",
            str(drain),
        ]
    )
    assert rc == 0
    assert log_urls == ["https://api.github.com/repos/acme/widgets/actions/jobs/202/logs"]
    bins = [path for path in drain.glob("*.bin") if path.stat().st_size > 0]
    assert bins
    notes = " ".join(
        json.loads((out / "summary.json").read_text(encoding="utf-8"))["collection_notes"]
    )
    match = re.search(r"trained (\d+) jobs", notes)
    assert match is not None and int(match.group(1)) >= 1
