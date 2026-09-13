"""Compile SyntaxError fixture: setup chrome must not pollute evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tools.rca.cleaner import clean_log
from tools.rca.cli import main

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_FIXTURE = "tests/rca/fixtures/compile-syntax"


def test_cleaner_strips_bom_and_does_not_join_shell_to_file() -> None:
    raw = (_FIXTURE + "/logs/42.log")
    text = Path(raw).read_text(encoding="utf-8")
    result = clean_log(text)
    joined = "\n".join(result.lines)
    assert "\ufeff" not in joined
    assert "2026-09-12T" not in joined
    assert "Azure Region" not in joined
    assert "Hosted Compute Agent" not in joined
    assert "SyntaxError" in joined
    file_rec = next(line for line in result.lines if 'File "' in line)
    assert not file_rec.startswith("shell:")
    assert "shell:" not in file_rec.split("\n")[0]


def test_compile_syntax_fixture_collect(tmp_path: Path) -> None:
    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--from-fixture",
            _FIXTURE,
            "--out",
            str(out),
            "--drain-dir",
            str(tmp_path / "drain"),
            "--history-backend",
            "none",
        ]
    )
    assert rc == 0
    payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    job = payload["failed_jobs"][0]
    windows = "\n".join(w["content"] for w in job["windows"])
    templates = payload.get("drain") or {}
    tmpl_blob = json.dumps(templates)
    reps = "\n".join(
        t.get("representative_line") or "" for t in templates.get("templates") or []
    )

    for blob in (windows, tmpl_blob, reps):
        assert _TS.search(blob) is None
        assert "\ufeff" not in blob

    first = next(
        (w for w in job["windows"] if w["label"] in ("first_error", "merged")),
        None,
    )
    assert first is not None
    assert "SyntaxError" in first["content"]
    assert "Azure Region" not in first["content"]
    assert "Hosted Compute Agent" not in first["content"]
    assert any(
        "SyntaxError" in (e.get("text") or "") or 'File "' in (e.get("text") or "")
        for e in job["error_lines"]
    )

    assert "Hosted Compute Agent" not in tmpl_blob
    stacks = "\n".join(s["content"] for s in job["stack_traces"])
    assert "SyntaxError" in stacks
    assert job["exit_code"] == 1
    assert payload["classification"]["category"] == "compile"
