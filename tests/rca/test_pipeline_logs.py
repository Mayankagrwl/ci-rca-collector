"""Pipeline/docker artifact name match, zip extract, skip, redact, fingerprint."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from tools.rca.drain_index import fingerprint_fine, novelty_in_memory
from tools.rca.pipeline_logs import (
    is_text_member,
    match_pipeline_artifact_name,
    parse_pipeline_zip,
    rank_pipeline_artifacts,
)
from tools.rca.redact import REPLACEMENT


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buf.getvalue()


def test_name_match_and_stage_map() -> None:
    assert match_pipeline_artifact_name("pipeline-logs") == ("pipeline_logs", None)
    assert match_pipeline_artifact_name("pipelines-log") == ("pipeline_logs", None)
    assert match_pipeline_artifact_name("pipeline-logs-build") == ("pipeline_logs", "build")
    assert match_pipeline_artifact_name("pipeline-logs_compile") == ("pipeline_logs", "build")
    assert match_pipeline_artifact_name("pipeline-logs-package") == ("pipeline_logs", "build")
    assert match_pipeline_artifact_name("pipeline-logs-unit-tests") == ("pipeline_logs", "test")
    assert match_pipeline_artifact_name("pipeline-logs-tests") == ("pipeline_logs", "test")
    assert match_pipeline_artifact_name("pipeline-logs-e2e") == ("pipeline_logs", "e2e")
    assert match_pipeline_artifact_name("pipeline-logs-lint") == ("pipeline_logs", "lint")
    assert match_pipeline_artifact_name("docker-logs") == ("pipeline_logs", None)
    assert match_pipeline_artifact_name("container-logs") == ("pipeline_logs", None)
    assert match_pipeline_artifact_name("PIPELINE-LOGS-BUILD") == ("pipeline_logs", "build")
    assert match_pipeline_artifact_name("junit-results") is None
    assert match_pipeline_artifact_name("playwright-traces") is None


def test_failed_stage_ranked_first() -> None:
    names = rank_pipeline_artifacts(
        ["pipeline-logs-lint", "pipeline-logs-build", "docker-logs"],
        failed_stage="build",
    )
    assert names[0] == "pipeline-logs-build"


def test_zip_extracts_text_skips_binary_and_video() -> None:
    blob = _zip(
        {
            "logs/build.log": b"error TS2345: nope\nProcess completed with exit code 1.\n",
            "docker.log": b"pull access denied for acme/app\n",
            "trace.trace": b"not a log",
            "video.webm": b"\x00\x01\x02\x03",
            "screenshot.png": b"\x89PNG",
            "notes.txt": b"ghp_abcdefghijklmnopqrstuvwxyz0123456789 leftover\n",
        }
    )
    streams = parse_pipeline_zip(blob, artifact_name="pipeline-logs-build", stage="build")
    files = {item.file for item in streams}
    assert "logs/build.log" in files
    assert "docker.log" in files
    assert "notes.txt" in files
    assert "trace.trace" not in files
    assert "video.webm" not in files
    assert "screenshot.png" not in files
    notes = next(item for item in streams if item.file == "notes.txt")
    blob_text = "\n".join(window.content for window in notes.windows)
    assert "ghp_" not in blob_text
    assert REPLACEMENT in blob_text


def test_binary_nul_skipped_even_with_log_suffix() -> None:
    blob = _zip({"docker.log": b"ok\x00\x00binary"})
    streams = parse_pipeline_zip(blob, artifact_name="docker-logs")
    assert streams == []


def test_oversize_member_is_truncated_not_rejected() -> None:
    huge = (
        b"src/foo.ts(1,1): error TS1111: overflow\n"
        + (b"INFO padding line that is not unique enough\n" * 80)
        + (b"INFO more padding\n" * 80)
    )
    huge = huge + (b"INFO extra " + b"z" * 200 + b"\n") * 50
    huge = huge + b"trailing overflow " * 400
    assert len(huge) > 10_000
    blob = _zip({"build.log": huge})
    streams = parse_pipeline_zip(blob, artifact_name="pipeline-logs-build", stage="build")
    assert len(streams) == 1
    joined = "\n".join(w.content for w in streams[0].windows)
    assert "error TS1111" in joined or streams[0].error_lines or streams[0].templates


def test_is_text_member() -> None:
    assert is_text_member("foo.log")
    assert is_text_member("logs/inner/out.txt")
    assert is_text_member("docker-daemon.log")
    assert not is_text_member("trace.zip")
    assert not is_text_member("video.webm")
    assert not is_text_member("playwright.trace")


def test_fingerprint_stable_to_line_count_noise() -> None:
    a = ["error TS2345: Type 'string' is not assignable"] + [f"INFO ok {i}" for i in range(10)]
    b = ["error TS2345: Type 'string' is not assignable"] + [f"INFO ok {i}" for i in range(200)]
    left = novelty_in_memory(a)
    right = novelty_in_memory(b)
    assert fingerprint_fine(left.hash_input) == fingerprint_fine(right.hash_input)
    assert left.fingerprint_fine == right.fingerprint_fine


def test_collect_pipeline_docker_build_fixture(tmp_path: Path) -> None:
    from tools.rca.cli import main

    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--from-fixture",
            "tests/rca/fixtures/pipeline-docker-build",
            "--out",
            str(out),
            "--history-backend",
            "none",
            "--drain-dir",
            str(tmp_path / "drain"),
        ]
    )
    assert rc == 0
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert payload["pipeline_logs"]
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "## Deterministic diagnosis" in md
    assert "## Pipeline / Docker logs" in md
    assert payload["verdict"]["requires_analysis"] is False
    assert payload["diagnosis"]["rule_id"] in {"R9", "R13", "R17"}
    assert "ghp_" not in md
    kinds = {item["kind"] for item in payload["artifacts"]}
    assert "pipeline_logs" in kinds


def test_fixture_zip_roundtrip(tmp_path: Path) -> None:
    fixture = Path("tests/rca/fixtures/pipeline-docker-build")
    zips = list(fixture.glob("*.zip")) + list((fixture / "artifacts").glob("*.zip")) if fixture.exists() else []
    if not zips:
        payload = _zip(
            {
                "logs/build.log": (
                    b"src/foo.ts(1,1): error TS2345: Type 'string' is not assignable\n"
                    b"##[error]Process completed with exit code 1.\n"
                )
            }
        )
        path = tmp_path / "pipeline-logs-build.zip"
        path.write_bytes(payload)
        zips = [path]
    streams = parse_pipeline_zip(
        zips[0].read_bytes(), artifact_name="pipeline-logs-build", stage="build"
    )
    assert streams
    text = "\n".join(w.content for s in streams for w in s.windows)
    assert "error TS" in text or streams[0].error_lines
