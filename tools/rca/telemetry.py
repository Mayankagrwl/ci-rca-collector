"""Per-run measurement telemetry: one JSON line per run, plus aggregation.

Source-agnostic and best-effort: no GitHub imports, never raises, a no-op when
the history dir is unavailable. Metrics only — the record is redacted before it
is written. The store is ``<history_dir>/telemetry.jsonl`` in the (cached)
history directory, so records accumulate per repo+workflow across runs.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from .redact import redact_text

_LOG = logging.getLogger(__name__)
_FILENAME = "telemetry.jsonl"
_MAX_LINES = 5000


def telemetry_path(history_dir: str | Path | None) -> Path | None:
    if not history_dir:
        return None
    return Path(history_dir) / _FILENAME


def append_telemetry(
    history_dir: str | Path | None,
    record: dict[str, Any],
    *,
    cap: int = _MAX_LINES,
) -> bool:
    """Append one redacted JSON line; keep only the last ``cap`` lines.

    Returns True on write, False on any no-op/error. Never raises.
    """
    path = telemetry_path(history_dir)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line, _n = redact_text(json.dumps(record, ensure_ascii=False, sort_keys=True))
        line = line.replace("\n", " ").strip()
        existing: list[str] = []
        if path.exists():
            existing = [
                ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
        existing.append(line)
        if cap and len(existing) > cap:
            existing = existing[-cap:]
        path.write_text("\n".join(existing) + "\n", encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 — telemetry is best-effort, never fatal
        _LOG.debug("telemetry append skipped", exc_info=True)
        return False


def read_telemetry(history_dir: str | Path | None) -> list[dict[str, Any]]:
    """Read the JSONL store; skip corrupt/partial lines. Never raises."""
    path = telemetry_path(history_dir)
    if path is None or not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        _LOG.debug("telemetry read skipped", exc_info=True)
        return []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and rates over telemetry records. Tells which rules/categories
    are safe to skip the model on and which the model genuinely improves."""
    total = len(records)
    by_category: Counter[str] = Counter()
    by_decision: Counter[str] = Counter()
    model_called = 0
    grounded_of_called = 0
    needs_review = 0
    skipped_det = 0
    skipped_sc = 0
    per_category: dict[str, dict[str, Any]] = {}

    for rec in records:
        category = str(rec.get("category") or "unknown")
        decision = str(rec.get("analyze_decision") or "")
        by_category[category] += 1
        by_decision[decision] += 1
        cat = per_category.setdefault(
            category,
            {"runs": 0, "model_called": 0, "grounded": 0, "source_ai_hybrid": 0, "source_deterministic": 0},
        )
        cat["runs"] += 1

        called = bool(rec.get("model_called"))
        if called:
            model_called += 1
            cat["model_called"] += 1
            if rec.get("grounded") is True:
                grounded_of_called += 1
                cat["grounded"] += 1
        if str(rec.get("display_status") or "") == "needs-review":
            needs_review += 1
        if decision == "skipped:deterministic_sufficient":
            skipped_det += 1
        if decision == "skipped:short_circuit":
            skipped_sc += 1
        source = str(rec.get("source") or "")
        if source in {"ai", "hybrid"}:
            cat["source_ai_hybrid"] += 1
        elif source in {"deterministic", "cached"}:
            cat["source_deterministic"] += 1

    for cat in per_category.values():
        cat["grounded_rate"] = _pct(cat["grounded"], cat["model_called"])

    return {
        "total_runs": total,
        "by_category": dict(by_category),
        "by_decision": dict(by_decision),
        "pct_model_called": _pct(model_called, total),
        "pct_grounded_of_called": _pct(grounded_of_called, model_called),
        "pct_skipped_deterministic_sufficient": _pct(skipped_det, total),
        "pct_skipped_short_circuit": _pct(skipped_sc, total),
        "pct_needs_review": _pct(needs_review, total),
        "per_category": per_category,
    }


def format_summary(agg: dict[str, Any], fmt: str = "json") -> str:
    if (fmt or "json").strip().lower() == "md":
        return _format_markdown(agg)
    return json.dumps(agg, indent=2, sort_keys=True)


def _format_markdown(agg: dict[str, Any]) -> str:
    lines = [
        "# RCA telemetry summary",
        "",
        f"- total runs: {agg['total_runs']}",
        f"- model called: {agg['pct_model_called']}%",
        f"- grounded (of model-called): {agg['pct_grounded_of_called']}%",
        f"- needs-review: {agg['pct_needs_review']}%",
        f"- skipped:deterministic_sufficient: {agg['pct_skipped_deterministic_sufficient']}%",
        f"- skipped:short_circuit: {agg['pct_skipped_short_circuit']}%",
        "",
        "## By decision",
    ]
    for decision, count in sorted(agg["by_decision"].items()):
        lines.append(f"- {decision or '(none)'}: {count}")
    lines.extend(["", "## By category", "", "| category | runs | model called | grounded rate | ai/hybrid | deterministic |", "| --- | --- | --- | --- | --- | --- |"])
    for category, stats in sorted(agg["per_category"].items()):
        lines.append(
            f"| {category} | {stats['runs']} | {stats['model_called']} | "
            f"{stats['grounded_rate']}% | {stats['source_ai_hybrid']} | "
            f"{stats['source_deterministic']} |"
        )
    return "\n".join(lines) + "\n"
