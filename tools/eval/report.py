"""Eval report + baseline diffing + gating (eval-spec §8.3).

Parses 11a's ``eval-results.json`` (and an optional baseline of the same shape);
it does not recompute metrics. Renders markdown (default) and a machine JSON
summary, shows per-metric deltas against a baseline — labelling any delta
smaller than the noise band ``max(current.stdev, baseline.stdev)`` as within
run-to-run variance — and lists every golden whose verdict changed. No network.

    python -m tools.eval.report --results eval-results.json \
        [--baseline tools/eval/eval-baseline.json] \
        [--fail-under category_accuracy=0.70,grounding_rate=0.95] --format markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .metrics import EvalMetrics

# friendly name → (EvalMetrics field, attribute). All Stat fields expose .mean.
_STAT_METRICS = {
    "category_accuracy": ("category_accuracy", "mean"),
    "infra_vs_code_accuracy": ("infra_vs_code_accuracy", "mean"),
    "short_circuit_accuracy": ("short_circuit_accuracy", "mean"),
    "grounding_rate": ("grounding_rate", "mean"),
    "fully_grounded_share": ("fully_grounded_share", "mean"),
    "schema_first_try_rate": ("schema_first_try_rate", "mean"),
    "repair_success_rate": ("repair_success_rate", "mean"),
    "fallback_rate": ("fallback_rate", "mean"),
    "mean_prompt_tokens": ("mean_prompt_tokens", "mean"),
    "mean_latency_ms": ("mean_latency_ms", "mean"),
    "short_circuit_share": ("short_circuit_share", "mean"),
}
_FLAKE_METRICS = {"flake_precision": "precision", "flake_recall": "recall", "flake_f1": "f1"}

_HEADLINE_ORDER = [
    "category_accuracy",
    "infra_vs_code_accuracy",
    "short_circuit_accuracy",
    "grounding_rate",
    "fully_grounded_share",
    "schema_first_try_rate",
    "fallback_rate",
    "mean_prompt_tokens",
    "mean_latency_ms",
    "short_circuit_share",
]
_VERDICT_FIELDS = ("category", "infra_vs_code", "is_flaky", "short_circuit")


def load_results(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _metrics(results: dict[str, Any]) -> EvalMetrics:
    return EvalMetrics.model_validate(results.get("metrics", {}))


def headline_values(metrics: EvalMetrics) -> dict[str, float]:
    """Friendly-name → scalar (Stat .mean, plus flake precision/recall/F1)."""
    out: dict[str, float] = {}
    for name, (field, attr) in _STAT_METRICS.items():
        out[name] = float(getattr(getattr(metrics, field), attr))
    for name, attr in _FLAKE_METRICS.items():
        out[name] = float(getattr(metrics.flake_detection, attr))
    return out


def _noise_band(metrics: EvalMetrics, baseline: EvalMetrics, field: str) -> float:
    return max(getattr(metrics, field).stdev, getattr(baseline, field).stdev)


def changed_goldens(
    current: dict[str, Any], baseline: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Goldens whose deterministic verdict changed vs baseline (+ added/removed)."""
    if not baseline:
        return []
    cur_rows = {r["slug"]: r for r in current.get("per_golden", [])}
    base_rows = {r["slug"]: r for r in baseline.get("per_golden", [])}
    changes: list[dict[str, Any]] = []
    for slug, row in cur_rows.items():
        if slug not in base_rows:
            changes.append({"slug": slug, "kind": "added"})
            continue
        base = base_rows[slug]
        diffs = {
            field: (base.get(field), row.get(field))
            for field in _VERDICT_FIELDS
            if base.get(field) != row.get(field)
        }
        if diffs:
            changes.append({"slug": slug, "kind": "changed", "diffs": diffs})
    for slug in base_rows:
        if slug not in cur_rows:
            changes.append({"slug": slug, "kind": "removed"})
    return sorted(changes, key=lambda c: c["slug"])


def evaluate_gate(
    metrics: EvalMetrics, fail_under: dict[str, float]
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Return (passed, {metric: {threshold, value, passed}}) for --fail-under."""
    values = headline_values(metrics)
    detail: dict[str, dict[str, Any]] = {}
    passed = True
    for name, threshold in fail_under.items():
        value = values.get(name)
        if value is None:
            detail[name] = {"threshold": threshold, "value": None, "passed": False,
                            "error": "unknown metric"}
            passed = False
            continue
        ok = value >= threshold
        detail[name] = {"threshold": threshold, "value": value, "passed": ok}
        passed = passed and ok
    return passed, detail


def _fmt(value: float) -> str:
    return f"{value:.4f}" if abs(value) < 1000 else f"{value:.1f}"


def _delta_line(name: str, cur: EvalMetrics, base: EvalMetrics) -> str:
    field = _STAT_METRICS[name][0]
    cur_v = getattr(cur, field).mean
    base_v = getattr(base, field).mean
    delta = cur_v - base_v
    band = _noise_band(cur, base, field)
    if delta == 0:
        return f"| {name} | {_fmt(cur_v)} | {_fmt(base_v)} | = {_fmt(delta)} | unchanged |"
    if abs(delta) < band:
        return (
            f"| {name} | {_fmt(cur_v)} | {_fmt(base_v)} | ≈ {_fmt(delta)} "
            f"| within run-to-run variance (±{_fmt(band)}) |"
        )
    arrow = "▲" if delta > 0 else "▼"
    return f"| {name} | {_fmt(cur_v)} | {_fmt(base_v)} | {arrow} {_fmt(delta)} | changed |"


def render_markdown(
    results: dict[str, Any],
    baseline: dict[str, Any] | None,
    gate: dict[str, dict[str, Any]] | None,
) -> str:
    metrics = _metrics(results)
    base_metrics = _metrics(baseline) if baseline else None
    lines: list[str] = ["# RCA eval report", ""]
    prov = results.get("provenance", {})
    lines.append(
        f"- collector `{prov.get('collector_version')}` · prompt `{prov.get('prompt_version')}`"
        f" (`{prov.get('prompt_hash')}`) · git `{(prov.get('git_sha') or 'n/a')[:12]}`"
    )
    lines.append(
        f"- goldens: {metrics.n_goldens} · runs: {metrics.n_runs}"
        f" · no_llm: {results.get('no_llm')}"
    )
    lines.append("")

    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| metric | mean | stdev |")
    lines.append("| --- | --- | --- |")
    for name in _HEADLINE_ORDER:
        field = _STAT_METRICS[name][0]
        st = getattr(metrics, field)
        lines.append(f"| {name} | {_fmt(st.mean)} | {_fmt(st.stdev)} |")
    fd = metrics.flake_detection
    lines.append(
        f"| flake_detection | P={_fmt(fd.precision)} R={_fmt(fd.recall)} "
        f"F1={_fmt(fd.f1)} | — |"
    )
    lines.append("")

    if base_metrics is not None:
        lines.append("## Deltas vs baseline")
        lines.append("")
        lines.append("| metric | current | baseline | delta | note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name in _HEADLINE_ORDER:
            lines.append(_delta_line(name, metrics, base_metrics))
        lines.append("")

    changes = changed_goldens(results, baseline)
    lines.append("## Changed goldens vs baseline")
    lines.append("")
    if not baseline:
        lines.append("_(no baseline provided)_")
    elif not changes:
        lines.append("_(no verdict changes)_")
    else:
        for change in changes:
            if change["kind"] == "changed":
                parts = ", ".join(
                    f"{field}: {old} → {new}"
                    for field, (old, new) in change["diffs"].items()
                )
                lines.append(f"- `{change['slug']}` — {parts}")
            else:
                lines.append(f"- `{change['slug']}` — {change['kind']}")
    lines.append("")

    lines.append("## Category confusion matrix")
    lines.append("")
    lines.extend(_confusion_lines(metrics.category_confusion))
    lines.append("")

    lines.append("## Golden freshness")
    lines.append("")
    lines.append(f"- median golden age: {metrics.median_golden_age_days} days")
    stale = metrics.stale_goldens
    lines.append(
        "- stale goldens (>180d): " + (", ".join(stale) if stale else "none")
    )
    lines.append("")

    if gate:
        lines.append("## Gate (--fail-under)")
        lines.append("")
        lines.append("| metric | value | threshold | pass |")
        lines.append("| --- | --- | --- | --- |")
        for name, info in gate.items():
            value = "n/a" if info["value"] is None else _fmt(info["value"])
            mark = "✅" if info["passed"] else "❌"
            lines.append(f"| {name} | {value} | {_fmt(info['threshold'])} | {mark} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _confusion_lines(matrix: dict[str, dict[str, int]]) -> list[str]:
    if not matrix:
        return ["_(empty)_"]
    preds: list[str] = sorted({p for row in matrix.values() for p in row})
    header = "| true \\ pred | " + " | ".join(preds) + " |"
    sep = "| --- | " + " | ".join("---" for _ in preds) + " |"
    out = [header, sep]
    for true_cat in sorted(matrix):
        cells = " | ".join(str(matrix[true_cat].get(p, 0)) for p in preds)
        out.append(f"| {true_cat} | {cells} |")
    return out


def machine_summary(
    results: dict[str, Any],
    baseline: dict[str, Any] | None,
    gate_detail: dict[str, dict[str, Any]] | None,
    gate_passed: bool,
) -> dict[str, Any]:
    metrics = _metrics(results)
    within_variance: list[str] = []
    deltas: dict[str, dict[str, float]] = {}
    if baseline:
        base_metrics = _metrics(baseline)
        for name in _HEADLINE_ORDER:
            field = _STAT_METRICS[name][0]
            cur_v = getattr(metrics, field).mean
            base_v = getattr(base_metrics, field).mean
            delta = round(cur_v - base_v, 6)
            band = _noise_band(metrics, base_metrics, field)
            deltas[name] = {"delta": delta, "noise_band": round(band, 6)}
            if delta != 0 and abs(delta) < band:
                within_variance.append(name)
    return {
        "headline": headline_values(metrics),
        "deltas": deltas,
        "within_variance": within_variance,
        "changed_goldens": changed_goldens(results, baseline),
        "fail_under": gate_detail or {},
        "gate_passed": gate_passed,
    }


def _parse_fail_under(spec: str | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if not spec:
        return out
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"--fail-under must be metric=threshold, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = float(value.strip())
    return out


def run(
    *,
    results_path: Path,
    baseline_path: Path | None,
    fail_under: dict[str, float],
    fmt: str,
    summary_out: Path | None = None,
) -> tuple[str, dict[str, Any], bool]:
    results = load_results(results_path)
    baseline = load_results(baseline_path) if baseline_path else None
    metrics = _metrics(results)
    gate_passed, gate_detail = evaluate_gate(metrics, fail_under)
    summary = machine_summary(results, baseline, gate_detail, gate_passed)
    markdown = render_markdown(results, baseline, gate_detail if fail_under else None)

    dest = summary_out or results_path.with_suffix(".summary.json")
    Path(dest).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    rendered = markdown if fmt != "json" else json.dumps(summary, indent=2) + "\n"
    return rendered, summary, gate_passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.eval.report")
    parser.add_argument("--results", default="eval-results.json")
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--fail-under", default=None)
    parser.add_argument("--format", default="markdown", choices=["markdown", "json"])
    parser.add_argument("--summary-out", default=None)
    args = parser.parse_args(argv)

    try:
        fail_under = _parse_fail_under(args.fail_under)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    rendered, _summary, gate_passed = run(
        results_path=Path(args.results),
        baseline_path=Path(args.baseline) if args.baseline else None,
        fail_under=fail_under,
        fmt=args.format,
        summary_out=Path(args.summary_out) if args.summary_out else None,
    )
    try:  # the report uses ≈/▼ (eval-spec §8.3); force UTF-8 on any console
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    sys.stdout.write(rendered)
    return 0 if gate_passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
