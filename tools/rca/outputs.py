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
    "suspected-stage",
    "suspected-files",
    "deterministic-rule",
    "diagnosis-source",
)

ANALYSIS_OUTPUT_KEYS = (
    "root-cause",
    "suggested-fix",
    "rca-confidence",
    "analysis-status",
    "analysis-notes",
    "diagnosis-source",
    "requires-analysis",
    "suspected-stage",
    "suspected-files",
    "deterministic-rule",
)

_STAGE_VALUES = frozenset({"build", "test", "e2e", "lint", "unknown"})
_PATH_CAP = 200
_FILES_CAP = 5


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _one_line(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _normalize_stage(raw: str | None) -> str:
    if not raw:
        return ""
    lowered = raw.strip().lower()
    if lowered in _STAGE_VALUES:
        return lowered
    if any(token in lowered for token in ("build", "compile", "package")):
        return "build"
    if any(token in lowered for token in ("unit", "test")):
        return "test"
    if any(token in lowered for token in ("e2e", "integration")):
        return "e2e"
    if "lint" in lowered:
        return "lint"
    return "unknown"


def _norm_path(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("./").strip()


def _format_files(paths: list[str] | None) -> str:
    out: list[str] = []
    for path in paths or []:
        item = str(path).replace("\\", "/").strip()[:_PATH_CAP]
        if item:
            out.append(item)
        if len(out) >= _FILES_CAP:
            break
    return ",".join(out)


def suspected_files_for_output(
    summary: Summary,
    record: AnalysisRecord | None = None,
) -> list[str]:
    """Deterministic shortlist first; AI may only keep candidates, never add."""
    shortlist = list(
        summary.diagnosis.suspected_files if summary.diagnosis is not None else []
    )
    if (
        record is None
        or record.status != "ok"
        or record.result is None
        or not record.result.suspected_files
    ):
        return shortlist
    if not shortlist:
        return []
    allowed = {_norm_path(path): path for path in shortlist}
    chosen: list[str] = []
    seen: set[str] = set()
    for path in record.result.suspected_files:
        key = _norm_path(path)
        if key not in allowed or key in seen:
            continue
        chosen.append(allowed[key])
        seen.add(key)
    return chosen


def _deterministic_rule(summary: Summary) -> str:
    rule = summary.diagnosis.rule_id if summary.diagnosis is not None else ""
    if rule.startswith("R") and len(rule) <= 8:
        return rule
    return ""


def diagnosis_source(summary: Summary, record: AnalysisRecord | None = None) -> str:
    if record is not None:
        if record.status in {"ok", "cached"}:
            return "ai"
        code = record.reason_code or (record.notes[0] if record.notes else "")
        if record.status in {"skipped", "gated"}:
            if code in {"deterministic_sufficient", "short_circuit"}:
                return "deterministic"
            return "gated"
    if (
        summary.diagnosis is not None
        and summary.verdict.requires_analysis is False
    ):
        return "deterministic"
    return "none"


def action_outputs(summary: Summary, out_dir: Path) -> dict[str, str]:
    """Map a Summary to GITHUB_OUTPUT keys. Never emit multiline values."""
    out_dir = Path(out_dir)
    history = summary.history
    files = suspected_files_for_output(summary)
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
        "suspected-stage": _normalize_stage(
            summary.diagnosis.suspected_stage if summary.diagnosis else None
        ),
        "suspected-files": _format_files(files),
        "deterministic-rule": _deterministic_rule(summary),
        "diagnosis-source": diagnosis_source(summary),
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
        "suspected-stage": "",
        "suspected-files": "",
        "deterministic-rule": "",
        "diagnosis-source": "none",
    }


def analysis_outputs(
    record: AnalysisRecord,
    summary: Summary | None = None,
) -> dict[str, str]:
    """Map an AnalysisRecord to GITHUB_OUTPUT keys. Single-line; never tokens."""
    result = record.result
    source = diagnosis_source(summary, record) if summary is not None else (
        "ai"
        if record.status in {"ok", "cached"}
        else "gated"
        if record.status in {"gated", "skipped"}
        else "none"
    )
    stage = ""
    files = ""
    rule = ""
    requires = ""
    if summary is not None:
        stage = _normalize_stage(
            summary.diagnosis.suspected_stage if summary.diagnosis else None
        )
        files = _format_files(suspected_files_for_output(summary, record))
        rule = _deterministic_rule(summary)
        requires = _bool_str(bool(summary.verdict.requires_analysis))
    raw = {
        "root-cause": result.root_cause if result is not None else "",
        "suggested-fix": result.suggested_fix if result is not None else "",
        "rca-confidence": result.confidence if result is not None else "",
        "analysis-status": record.status or "",
        "analysis-notes": "; ".join(record.notes),
        "diagnosis-source": source,
        "requires-analysis": requires,
        "suspected-stage": stage,
        "suspected-files": files,
        "deterministic-rule": rule,
    }
    return {key: _one_line(raw[key]) for key in ANALYSIS_OUTPUT_KEYS}


def write_analysis_github_output(
    record: AnalysisRecord,
    *,
    summary: Summary | None = None,
    output_file: str | Path | None = None,
) -> None:
    path = output_file if output_file is not None else os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    values = analysis_outputs(record, summary)
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
