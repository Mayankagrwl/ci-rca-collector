"""Analyze evidence packet stays within the 2500-token cap."""

from __future__ import annotations

from pathlib import Path

from tools.rca.budget import token_count
from tools.rca.cli import main
from tools.rca.config import TOKEN_BUDGET_ANALYZE
from tools.rca.models import Summary
from tools.rca.prompt import build_evidence


def test_analyze_evidence_cap_on_noisy_fixture(tmp_path: Path) -> None:
    from tests.rca.test_cli import _write_noisy_fixture

    fixture = _write_noisy_fixture(tmp_path / "noisy")
    out = tmp_path / "rca"
    rc = main(
        [
            "collect",
            "--from-fixture",
            str(fixture),
            "--out",
            str(out),
            "--drain-dir",
            str(tmp_path / "drain"),
            "--history-backend",
            "none",
        ]
    )
    assert rc == 0
    summary = Summary.model_validate_json((out / "summary.json").read_text(encoding="utf-8"))
    evidence = build_evidence(summary)
    assert evidence.startswith("<EVIDENCE>")
    assert "DETERMINISTIC_HINT:" in evidence
    assert token_count(evidence) <= TOKEN_BUDGET_ANALYZE + 20
    assert "### step_table" not in evidence
    assert "### tail_window" not in evidence or "### first_error_window" in evidence
