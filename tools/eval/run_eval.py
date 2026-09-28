"""Offline eval runner (eval-spec §8.1).

Replays goldens through the real analysis path — it does not reimplement it:
- deterministic metrics come from re-running ``diagnose.diagnose`` on each
  golden's Summary (so ``--no-llm`` genuinely tests current rule changes);
- LLM metrics are read off each run's ``AnalysisRecord`` (grounding / validation
  / call telemetry from Step 10).

    python -m tools.eval.run_eval --goldens tools/eval/goldens --runs 3 \
        --out eval-results.json [--filter category=dependency] [--no-llm]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from statistics import median
from typing import Any, Callable

from tools.rca import config
from tools.rca.diagnose import diagnose
from tools.rca.models import AnalysisRecord, Summary
from tools.rca.prompt import SYSTEM_PROMPT
from tools.rca.redact import redact_text

from .labels import GoldenLabel, load_goldens
from .metrics import (
    ClassificationStat,
    EvalMetrics,
    Stat,
    classification_stat,
    confusion,
    stat,
)

_STALE_DAYS = 180

AnalyzeFn = Callable[[Summary], AnalysisRecord]

_TODAY: Any = None  # test hook; None → date.today()


def _today():
    from datetime import date

    return _TODAY or date.today()


def _default_analyze(summary: Summary) -> AnalysisRecord:
    from tools.rca.analyze import analyze_summary

    return analyze_summary(summary, cache_dir=None)


def _accuracy(y_true: list[str], y_pred: list[str]) -> float:
    if not y_true:
        return 0.0
    return sum(1.0 for t, p in zip(y_true, y_pred) if t == p) / len(y_true)


def _matches_filters(label: GoldenLabel, filters: dict[str, str]) -> bool:
    for key, value in filters.items():
        got = getattr(label, key, None)
        if got is None or str(got) != value:
            return False
    return True


def _golden_age_days(label: GoldenLabel) -> int:
    stamp = label.labelled_at or label.captured_at
    return max(0, (_today() - stamp).days)


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001 — git may be absent
        return None
    sha = (out.stdout or "").strip()
    return sha or None


def _provenance(runs: int, seed: int | None) -> dict[str, Any]:
    endpoint, _ = redact_text(config.resolve_stgpt_api_url())
    model, _ = redact_text(",".join(config.PERSONAS))
    prompt_hash = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16]
    return {
        "model": model,
        "endpoint": endpoint,
        "prompt_version": config.PROMPT_VERSION,
        "prompt_hash": prompt_hash,
        "collector_version": config.COLLECTOR_VERSION,
        "git_sha": _git_sha(),
        "runs": runs,
        "seed": seed,
    }


def _grounding_rate(record: AnalysisRecord) -> float | None:
    return record.grounding.grounding_rate if record.grounding is not None else None


def _fully_grounded(record: AnalysisRecord) -> float | None:
    return float(record.grounding.grounded) if record.grounding is not None else None


def _vt_value(record: AnalysisRecord, field: str) -> float | None:
    vt = record.validation_telemetry
    if vt is None:
        return None
    value = getattr(vt, field, None)
    return None if value is None else float(value)


def _ct_value(record: AnalysisRecord, field: str) -> float | None:
    ct = record.call_telemetry
    if ct is None:
        return None
    value = getattr(ct, field, None)
    return None if value is None else float(value)


def _run_series(records: list[AnalysisRecord], extractor) -> float | None:
    vals = [v for v in (extractor(r) for r in records) if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def evaluate(
    goldens: list[tuple[str, Summary, GoldenLabel, Path]],
    *,
    runs: int = 3,
    no_llm: bool = False,
    analyze_fn: AnalyzeFn | None = None,
) -> tuple[EvalMetrics, list[dict[str, Any]]]:
    analyze_fn = analyze_fn or _default_analyze
    n_goldens = len(goldens)
    n_runs = 1 if no_llm else runs

    # --- deterministic classification (re-run the current rules once) ---
    y_true_cat = [label.true_category for _s, _sm, label, _p in goldens]
    y_pred_cat: list[str] = []
    y_true_infra = [label.is_infra_vs_code for _s, _sm, label, _p in goldens]
    y_pred_infra: list[str] = []
    y_true_flake = [label.is_flaky for _s, _sm, label, _p in goldens]
    y_pred_flake: list[bool] = []
    y_true_sc = [label.expect_short_circuit or "" for _s, _sm, label, _p in goldens]
    y_pred_sc: list[str] = []
    verdicts = []
    for _slug, summary, _label, _path in goldens:
        verdict = diagnose(summary)
        verdicts.append(verdict)
        y_pred_cat.append(verdict.category or "unknown")
        y_pred_infra.append(verdict.is_infra_vs_code or "unknown")
        y_pred_flake.append(bool(verdict.is_flaky))
        y_pred_sc.append(verdict.short_circuit or "")

    cat_acc = _accuracy(y_true_cat, y_pred_cat)
    infra_acc = _accuracy(y_true_infra, y_pred_infra)
    sc_acc = _accuracy(y_true_sc, y_pred_sc)
    flake = (
        classification_stat(y_true_flake, y_pred_flake)
        if n_goldens
        else ClassificationStat()
    )
    # Deterministic → identical across runs → stdev 0.
    cat_series = [cat_acc] * n_runs
    infra_series = [infra_acc] * n_runs
    sc_series = [sc_acc] * n_runs

    # --- LLM metrics (one AnalysisRecord per golden per run) ---
    grounding_s: list[float] = []
    full_s: list[float] = []
    schema_s: list[float] = []
    repair_s: list[float] = []
    fallback_s: list[float] = []
    tokens_s: list[float] = []
    latency_s: list[float] = []
    sc_share_s: list[float] = []
    records_per_run: list[list[AnalysisRecord]] = []
    if not no_llm:
        for _r in range(runs):
            records = [analyze_fn(summary) for _slug, summary, _label, _path in goldens]
            records_per_run.append(records)
            _append(grounding_s, _run_series(records, _grounding_rate))
            _append(full_s, _run_series(records, _fully_grounded))
            _append(schema_s, _run_series(records, lambda r: _vt_value(r, "schema_valid_first_try")))
            _append(repair_s, _run_series(records, lambda r: _vt_value(r, "repair_succeeded")))
            _append(fallback_s, _run_series(records, lambda r: _vt_value(r, "fallback_used")))
            _append(tokens_s, _run_series(records, lambda r: _ct_value(r, "prompt_tokens_est")))
            _append(latency_s, _run_series(records, lambda r: _ct_value(r, "latency_ms")))
            _append(sc_share_s, _run_series(records, lambda r: _ct_value(r, "short_circuited")))

    ages = [_golden_age_days(label) for _s, _sm, label, _p in goldens]
    stale = sorted(
        slug for (slug, _sm, label, _p) in goldens if _golden_age_days(label) > _STALE_DAYS
    )

    per_golden = _per_golden_rows(goldens, verdicts, records_per_run, no_llm=no_llm)

    metrics = EvalMetrics(
        n_goldens=n_goldens,
        n_runs=n_runs,
        category_accuracy=stat(cat_series),
        category_confusion=confusion(y_true_cat, y_pred_cat),
        infra_vs_code_accuracy=stat(infra_series),
        flake_detection=flake,
        short_circuit_accuracy=stat(sc_series),
        grounding_rate=stat(grounding_s),
        fully_grounded_share=stat(full_s),
        schema_first_try_rate=stat(schema_s),
        repair_success_rate=stat(repair_s),
        fallback_rate=stat(fallback_s),
        mean_prompt_tokens=stat(tokens_s),
        mean_latency_ms=stat(latency_s),
        short_circuit_share=stat(sc_share_s),
        median_golden_age_days=int(median(ages)) if ages else 0,
        stale_goldens=stale,
    )
    return metrics, per_golden


def _per_golden_rows(
    goldens: list[tuple[str, Summary, GoldenLabel, Path]],
    verdicts: list[Any],
    records_per_run: list[list[AnalysisRecord]],
    *,
    no_llm: bool,
) -> list[dict[str, Any]]:
    """One flat row per golden: deterministic verdict + correctness (+ LLM aggregates)."""
    from collections import Counter

    rows: list[dict[str, Any]] = []
    for gi, (slug, _summary, label, _path) in enumerate(goldens):
        verdict = verdicts[gi]
        category = verdict.category or "unknown"
        infra = verdict.is_infra_vs_code or "unknown"
        flaky = bool(verdict.is_flaky)
        short_circuit = verdict.short_circuit or None
        row: dict[str, Any] = {
            "slug": slug,
            "category": category,
            "infra_vs_code": infra,
            "is_flaky": flaky,
            "short_circuit": short_circuit,
            "category_correct": category == label.true_category,
            "infra_vs_code_correct": infra == label.is_infra_vs_code,
            "is_flaky_correct": flaky == bool(label.is_flaky),
            "short_circuit_correct": (short_circuit or "") == (label.expect_short_circuit or ""),
        }
        if not no_llm and records_per_run:
            recs = [run[gi] for run in records_per_run]
            rates = [
                r.grounding.grounding_rate for r in recs if r.grounding is not None
            ]
            row["grounding_rate"] = round(sum(rates) / len(rates), 6) if rates else None
            statuses = [_display_status(r) for r in recs]
            row["display_status"] = (
                Counter(statuses).most_common(1)[0][0] if statuses else None
            )
        rows.append(row)
    return rows


def _display_status(record: AnalysisRecord) -> str:
    from tools.rca.analyze import display_status

    return display_status(record)


def _append(series: list[float], value: float | None) -> None:
    if value is not None:
        series.append(value)


def run(
    *,
    goldens_dir: Path,
    runs: int,
    no_llm: bool,
    filters: dict[str, str],
    seed: int | None,
    analyze_fn: AnalyzeFn | None = None,
) -> dict[str, Any]:
    if not no_llm and runs < 2:
        raise ValueError("runs must be >= 2 when the model runs (variance unknown at 1)")
    goldens = [g for g in load_goldens(goldens_dir) if _matches_filters(g[2], filters)]
    metrics, per_golden = evaluate(goldens, runs=runs, no_llm=no_llm, analyze_fn=analyze_fn)
    return {
        "metrics": metrics.model_dump(),
        "per_golden": per_golden,
        "provenance": _provenance(runs, seed),
        "filters": filters,
        "no_llm": no_llm,
    }


def _parse_filters(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--filter must be key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.eval.run_eval")
    parser.add_argument("--goldens", default="tools/eval/goldens")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", default="eval-results.json")
    parser.add_argument("--filter", action="append", default=[])
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    filters = _parse_filters(args.filter)
    try:
        results = run(
            goldens_dir=Path(args.goldens),
            runs=args.runs,
            no_llm=args.no_llm,
            filters=filters,
            seed=args.seed,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results["metrics"], indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
