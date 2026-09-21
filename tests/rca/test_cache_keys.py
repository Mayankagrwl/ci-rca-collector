"""GitHub Actions cache-key sanitization."""

from __future__ import annotations

import re
from pathlib import Path

from tools.rca.cache_keys import (
    cache_key_part,
    drain_cache_key,
    drain_restore_key,
    history_cache_key,
    history_restore_keys,
    is_legal_cache_key,
)
from tools.rca.cli import main

_LEGAL = re.compile(r"^[a-zA-Z0-9!#%'()+,\-./<=>@_[{}~]+$")


def test_cache_key_part_build_api_gateway() -> None:
    assert cache_key_part("Build Api-gateway") == "build-api-gateway"


def test_cache_key_part_ci_foo_bar_no_spaces_or_slashes() -> None:
    got = cache_key_part("CI: foo/bar")
    assert " " not in got
    assert "/" not in got
    assert got == "ci-foo-bar"


def test_cache_key_part_strips_illegal_chars() -> None:
    got = cache_key_part("CI: foo/bar\nbaz!")
    assert " " not in got
    assert ":" not in got
    assert "/" not in got
    assert "\n" not in got
    assert "!" not in got
    assert re.fullmatch(r"[a-z0-9._-]+", got)
    assert is_legal_cache_key(got)


def test_drain_and_history_keys_are_legal() -> None:
    drain = drain_cache_key("Build Api-gateway", "Build job")
    assert drain == "drain3-build-api-gateway-build-job"
    assert " " not in drain
    assert _LEGAL.match(drain)
    restore = drain_restore_key("Build Api-gateway")
    assert restore == "drain3-build-api-gateway-"
    hist = history_cache_key("acme/widgets", "Build Api-gateway")
    assert hist == "rca-history-acme-widgets-build-api-gateway"
    assert " " not in hist
    prefixes = history_restore_keys("acme/widgets")
    assert prefixes[0] == "rca-history-acme-widgets-"
    assert prefixes[-1] == "rca-history-"


def test_collect_logs_cache_keys_in_notes(tmp_path: Path) -> None:
    import json

    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--from-fixture",
            "tests/rca/fixtures/sample-failure",
            "--out",
            str(out),
            "--history-backend",
            "none",
            "--drain-dir",
            str(tmp_path / "drain"),
            "--repo",
            "acme/widgets",
        ]
    )
    assert rc == 0
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    notes = " ".join(payload["collection_notes"])
    assert "drain cache key:" in notes
    assert "history cache key:" in notes
    assert " " not in notes.split("drain cache key: ", 1)[1].split()[0]


def test_cache_keys_cli_writes_github_output(tmp_path, monkeypatch) -> None:
    dest = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(dest))
    rc = main(
        [
            "cache-keys",
            "--workflow-name",
            "Build Api-gateway",
            "--job-name",
            "collect",
            "--repository",
            "acme/widgets",
        ]
    )
    assert rc == 0
    text = dest.read_text(encoding="utf-8")
    assert "drain-cache-key=drain3-build-api-gateway-collect\n" in text
    assert "history-cache-key=rca-history-acme-widgets-build-api-gateway\n" in text
    assert " " not in text.split("drain-cache-key=", 1)[1].split("\n", 1)[0]
