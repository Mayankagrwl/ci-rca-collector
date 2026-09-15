"""Write composite-action outputs to $GITHUB_OUTPUT.

Booleans are the strings 'true' / 'false'. None of these values are multi-line.
Never write tokens.
"""

from __future__ import annotations

import os
from pathlib import Path

from .models import AnalysisRecord, Summary

OUTPUT_KEYS = (
    "summary-path",
    "json-path",
    "category",
    "confidence",
    "is-infra-vs-code",
    "is-flaky",
    "short-circuit",
    "requires-analysis",
    "fingerprint",
    "seen-count",
    "recurrence",
    "failed-job-count",
)

ANALYSIS_OUTPUT_KEYS = (
    "root-cause",
    "suggested-fix",
    "rca-confidence",
    "analysis-status",
    "analysis-notes",
)


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _one_line(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def action_outputs(summary: Summary, out_dir: Path) -> dict[str, str]:
    """Map a Summary to GITHUB_OUTPUT keys. Never emit multiline values."""
    out_dir = Path(out_dir)
    history = summary.history
    raw = {
        "summary-path": str(out_dir / "summary.md"),
        "json-path": str(out_dir / "summary.json"),
        "category": summary.classification.category or "unknown",
        "confidence": summary.classification.confidence or "low",
        "is-infra-vs-code": summary.classification.is_infra_vs_code or "unknown",
        "is-flaky": _bool_str(bool(summary.classification.is_flaky)),
        "short-circuit": summary.verdict.short_circuit or "",
        "requires-analysis": _bool_str(bool(summary.verdict.requires_analysis)),
        "fingerprint": summary.fingerprint or "",
        "seen-count": str(history.seen_count if history is not None else 0),
        "recurrence": (history.match if history is not None else "new"),
        "failed-job-count": str(summary.run.failed_job_total),
    }
    return {key: _one_line(raw[key]) for key in OUTPUT_KEYS}


def failure_outputs(out_dir: Path) -> dict[str, str]:
    """Guaranteed keys when collection itself errors (spec §13)."""
    out_dir = Path(out_dir)
    return {
        "summary-path": _one_line(str(out_dir / "summary.md")),
        "json-path": _one_line(str(out_dir / "summary.json")),
        "category": "unknown",
        "confidence": "low",
        "is-infra-vs-code": "unknown",
        "is-flaky": "false",
        "short-circuit": "",
        "requires-analysis": "true",
        "fingerprint": "",
        "seen-count": "0",
        "recurrence": "new",
        "failed-job-count": "0",
    }


def analysis_outputs(record: AnalysisRecord) -> dict[str, str]:
    """Map an AnalysisRecord to GITHUB_OUTPUT keys. Single-line; never tokens."""
    result = record.result
    raw = {
        "root-cause": result.root_cause if result is not None else "",
        "suggested-fix": result.suggested_fix if result is not None else "",
        "rca-confidence": result.confidence if result is not None else "",
        "analysis-status": record.status or "",
        "analysis-notes": "; ".join(record.notes),
    }
    return {key: _one_line(raw[key]) for key in ANALYSIS_OUTPUT_KEYS}


def write_analysis_github_output(
    record: AnalysisRecord,
    *,
    output_file: str | Path | None = None,
) -> None:
    path = output_file if output_file is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    values = analysis_outputs(record)
    with dest.open("a", encoding="utf-8") as handle:
        for key in ANALYSIS_OUTPUT_KEYS:
            handle.write(f"{key}={values.get(key, '')}\n")


def write_github_output(
    summary: Summary,
    out_dir: Path,
    *,
    output_file: str | Path | None = None,
) -> None:
    """Append key=value lines to GITHUB_OUTPUT. No-op when the file is unset."""
    path = output_file if output_file is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    values = action_outputs(summary, out_dir)
    _append_output_file(path, values)


def write_failure_outputs(
    out_dir: Path,
    *,
    output_file: str | Path | None = None,
) -> None:
    path = output_file if output_file is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    _append_output_file(path, failure_outputs(out_dir))


def _append_output_file(path: str | Path, values: dict[str, str]) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as handle:
        for key in OUTPUT_KEYS:
            handle.write(f"{key}={values.get(key, '')}\n")
