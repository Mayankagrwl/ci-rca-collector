"""Step 14 AC6–AC8 — `deliver --dry-run`: preview, write-back, zero writes, resilience."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from tools.rca import cli
from tools.rca.cli import COMMENT_BEGIN, COMMENT_END, main
from tools.rca.deliver import render_comment as rc
from tools.rca.github_api import GitHubClient
from tools.rca.models import AnalysisRecord, AnalysisResult

_ROOT = Path(__file__).resolve().parents[3]
_GOLDENS = _ROOT / "tools" / "eval" / "goldens"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GITHUB_OUTPUT",
        "GITHUB_REPOSITORY",
        "RCA_DEFAULT_BRANCH",
        "RCA_GITHUB_API_URL",
        "GITHUB_API_URL",
        "GITHUB_SERVER_URL",
        "GH_HOST",
        "RCA_GITHUB_HOST",
        "RCA_SSL_VERIFY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_ACTIONS_PAT", "test-token")


def _analysis_dict() -> dict[str, Any]:
    record = AnalysisRecord(
        status="skipped",
        reason_code="deterministic_sufficient",
        notes=["deterministic_sufficient"],
        result=AnalysisResult(
            root_cause="card", suggested_fix="card fix", confidence="high", source="deterministic"
        ),
    )
    return json.loads(record.model_dump_json())


def _stage(tmp_path: Path, slug: str, *, analysis: bool = True) -> Path:
    out = tmp_path / "rca"
    out.mkdir()
    raw = json.loads((_GOLDENS / slug / "summary.json").read_text(encoding="utf-8"))
    if analysis:
        raw["analysis"] = _analysis_dict()
    path = out / "summary.json"
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return path


def _run(summary: Path, *extra: str) -> int:
    return main(["deliver", "--summary", str(summary), "--out", str(summary.parent), *extra])


def _preview(out: Path) -> str:
    return (out / "delivery-preview.md").read_text(encoding="utf-8")


def _body_from_preview(text: str) -> str:
    return text.split(COMMENT_BEGIN + "\n", 1)[1].split(COMMENT_END, 1)[0]


# ---- AC6: dry run ---------------------------------------------------------------------


def test_dry_run_preview_is_byte_identical_to_render(tmp_path, monkeypatch) -> None:  # AC #23
    rendered: list[str] = []
    real = rc.render_comment

    def spy(*args: Any, **kwargs: Any) -> str:
        body = real(*args, **kwargs)
        rendered.append(body)
        return body

    monkeypatch.setattr(rc, "render_comment", spy)
    summary = _stage(tmp_path, "artifactory-version-exists")
    assert _run(summary, "--dry-run", "--offline") == 0
    text = _preview(summary.parent)
    assert len(rendered) == 1
    assert _body_from_preview(text) == rendered[0]
    assert "release version already published" in rendered[0]
    assert "trigger: `push_default`" in text
    assert "offline: no GitHub API calls" in text


def test_write_back_sets_only_delivery_and_keeps_analysis(tmp_path) -> None:  # AC #29
    summary = _stage(tmp_path, "artifactory-version-exists")
    before = json.loads(summary.read_text(encoding="utf-8"))
    assert _run(summary, "--dry-run", "--offline") == 0
    after = json.loads(summary.read_text(encoding="utf-8"))
    assert after["analysis"] == before["analysis"]
    delivery = after.pop("delivery")
    assert after == before  # nothing else changed, nothing regenerated
    assert delivery["dry_run"] is True
    assert delivery["delivered_to"] == []
    assert delivery["trigger"] == "push_default"
    assert delivery["severity"] == "high"
    assert delivery["owner_resolved_by"] is None
    assert any("offline" in note for note in delivery["notes"])


def test_out_elsewhere_leaves_the_input_untouched(tmp_path) -> None:
    src = _GOLDENS / "artifactory-version-exists" / "summary.json"
    original = src.read_bytes()
    out = tmp_path / "elsewhere"
    assert main(["deliver", "--summary", str(src), "--out", str(out), "--dry-run", "--offline"]) == 0
    assert src.read_bytes() == original
    copied = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert copied["delivery"]["dry_run"] is True
    assert (out / "delivery-preview.md").exists()


def test_live_lookups_are_get_only(tmp_path, monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/acme/widgets"):
            return httpx.Response(200, json={"default_branch": "main"})
        if "/git/ref/tags/" in path:
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/actions/runs/1"):
            return httpx.Response(200, json={"id": 1, "workflow_id": 7})
        if "/runs" in path:
            return httpx.Response(200, json={"workflow_runs": []})
        return httpx.Response(404, json={})

    monkeypatch.setattr(
        cli,
        "_delivery_client",
        lambda _args: GitHubClient(transport=httpx.MockTransport(handler), sleep=lambda _d: None),
    )
    summary = _stage(tmp_path, "artifactory-version-exists")
    assert _run(summary, "--dry-run", "--repo", "acme/widgets") == 0
    assert seen, "expected read-only lookups"
    assert {r.method for r in seen} == {"GET"}
    assert all(r.content == b"" for r in seen)
    assert "trigger: `push_default`" in _preview(summary.parent)


def test_without_live_flag_is_a_dry_run(tmp_path, monkeypatch) -> None:
    # Step 15: posting needs an explicit --live; no flag is a dry run (not "live pending").
    def refuse(_args):
        raise AssertionError("no client may be built offline")

    monkeypatch.setattr(cli, "_delivery_client", refuse)
    summary = _stage(tmp_path, "artifactory-version-exists")
    assert _run(summary, "--offline") == 0
    delivery = json.loads(summary.read_text(encoding="utf-8"))["delivery"]
    assert delivery["dry_run"] is True
    assert delivery["delivered_to"] == []
    assert any("pass --live to post" in note for note in delivery["notes"])


def test_github_output_keys(tmp_path, monkeypatch) -> None:
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    summary = _stage(tmp_path, "artifactory-version-exists")
    assert _run(summary, "--dry-run", "--offline") == 0
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "severity=high",
        "suppressed-by=",
        "delivered-to=",
        "comment-url=",
        "issue-url=",
    ]


def test_inputs_flags_are_honoured(tmp_path) -> None:
    summary = _stage(tmp_path, "artifactory-version-exists")
    assert _run(
        summary, "--dry-run", "--offline", "--comment-on-commit", "false", "--create-issues", "false"
    ) == 0
    text = _preview(summary.parent)
    assert COMMENT_BEGIN not in text
    assert "comment_on_commit=false" in text
    assert "would open an issue: no" in text


def test_invalid_threshold_falls_back_to_defaults(tmp_path) -> None:
    summary = _stage(tmp_path, "artifactory-version-exists")
    assert _run(summary, "--dry-run", "--offline", "--confidence-threshold", "certain") == 0
    delivery = json.loads(summary.read_text(encoding="utf-8"))["delivery"]
    assert any("invalid delivery inputs" in err for err in delivery["errors"])
    assert COMMENT_BEGIN in _preview(summary.parent)


# ---- AC7: suppressed run ----------------------------------------------------------------


def test_flaky_golden_is_suppressed(tmp_path) -> None:  # delivery AC #12
    summary = _stage(tmp_path, "flake-same-sha")
    assert _run(summary, "--dry-run", "--offline") == 0
    text = _preview(summary.parent)
    assert "suppressed_by: `flaky`" in text
    assert "No comment would be posted: suppressed by flaky" in text
    assert COMMENT_BEGIN not in text and "rca-bot:fp=" not in text
    delivery = json.loads(summary.read_text(encoding="utf-8"))["delivery"]
    assert delivery["suppressed_by"] == "flaky"
    assert delivery["severity"] == "info"


# ---- AC8: resilience ---------------------------------------------------------------------


def test_corrupt_analysis_file_still_renders_deterministic_preview(tmp_path) -> None:
    summary = _stage(tmp_path, "artifactory-version-exists", analysis=False)
    analysis = summary.parent / "analysis.json"
    analysis.write_text("{ this is not json", encoding="utf-8")
    assert _run(summary, "--analysis", str(analysis), "--dry-run", "--offline") == 0
    text = _preview(summary.parent)
    assert "analysis file unreadable" in text
    body = _body_from_preview(text)
    primary = "This release already exists on Artifactory. You need to update package.json"
    assert f"**{primary}**" in body


def test_render_exception_still_writes_report(tmp_path, monkeypatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(rc, "render_comment", boom)
    summary = _stage(tmp_path, "artifactory-version-exists")
    assert _run(summary, "--dry-run", "--offline") == 0
    text = _preview(summary.parent)
    assert "comment rendering failed" in text
    assert COMMENT_BEGIN not in text
    after = json.loads(summary.read_text(encoding="utf-8"))
    assert any("comment rendering failed" in err for err in after["delivery"]["errors"])
    assert "analysis" in after
    assert _run(summary, "--dry-run", "--offline", "--strict") == 1


def test_unreadable_summary_writes_preview_and_never_a_stub(tmp_path) -> None:
    out = tmp_path / "rca"
    out.mkdir()
    bad = out / "summary.json"
    bad.write_text("not json at all", encoding="utf-8")
    assert _run(bad, "--dry-run", "--offline") == 0
    assert bad.read_text(encoding="utf-8") == "not json at all"
    assert "summary unreadable" in _preview(out)


def test_unexpected_crash_never_regenerates_summary(tmp_path, monkeypatch) -> None:
    def crash(_args):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "_cmd_deliver", crash)
    summary = _stage(tmp_path, "artifactory-version-exists")
    original = summary.read_bytes()
    assert _run(summary, "--dry-run", "--offline") == 0
    assert summary.read_bytes() == original


def test_old_summary_files_still_load() -> None:
    from tools.rca.models import Summary

    for path in _GOLDENS.glob("*/summary.json"):
        loaded = Summary.model_validate_json(path.read_text(encoding="utf-8"))
        assert loaded.delivery is None


def test_golden_used_by_cli_is_copied_not_moved(tmp_path) -> None:
    # Guard for the documented command: running against a golden must not mutate it.
    src = tmp_path / "golden.json"
    shutil.copy(_GOLDENS / "java-compile-in-pipeline" / "summary.json", src)
    before = src.read_bytes()
    out = tmp_path / "out"
    assert main(["deliver", "--summary", str(src), "--out", str(out), "--dry-run", "--offline"]) == 0
    assert src.read_bytes() == before
    assert "cannot find symbol" in _body_from_preview(_preview(out))
