"""§1.3 / acceptance 35: source-agnostic modules stay GitHub-free."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / "tools" / "rca"
_MODULES = (
    "cleaner.py",
    "drain_index.py",
    "budget.py",
    "redact.py",
    "history.py",
)


def test_core_modules_have_no_github_identifiers() -> None:
    for name in _MODULES:
        source = (_ROOT / name).read_text(encoding="utf-8")
        assert "github_api" not in source, name
        assert "job_id" not in source, name
        assert "run_id" not in source, name
