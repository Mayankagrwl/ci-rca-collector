"""Eval aggregation (eval-spec §8.2). Standard library only — no numpy."""

from __future__ import annotations

import statistics
from collections import defaultdict

from pydantic import BaseModel


class Stat(BaseModel):
    mean: float = 0.0
    stdev: float = 0.0
    min: float = 0.0
    max: float = 0.0


class ClassificationStat(BaseModel):
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0


class EvalMetrics(BaseModel):
    n_goldens: int = 0
    n_runs: int = 0
    category_accuracy: Stat = Stat()
    category_confusion: dict[str, dict[str, int]] = {}
    infra_vs_code_accuracy: Stat = Stat()
    flake_detection: ClassificationStat = ClassificationStat()
    short_circuit_accuracy: Stat = Stat()
    grounding_rate: Stat = Stat()
    fully_grounded_share: Stat = Stat()
    schema_first_try_rate: Stat = Stat()
    repair_success_rate: Stat = Stat()
    fallback_rate: Stat = Stat()
    mean_prompt_tokens: Stat = Stat()
    mean_latency_ms: Stat = Stat()
    short_circuit_share: Stat = Stat()
    median_golden_age_days: int = 0
    stale_goldens: list[str] = []


def stat(values: list[float]) -> Stat:
    """Mean/stdev/min/max over a run-level series. Empty → all zeros."""
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return Stat()
    return Stat(
        mean=round(statistics.fmean(clean), 6),
        stdev=round(statistics.pstdev(clean), 6) if len(clean) > 1 else 0.0,
        min=round(min(clean), 6),
        max=round(max(clean), 6),
    )


def classification_stat(y_true: list[bool], y_pred: list[bool]) -> ClassificationStat:
    """Precision/recall/F1 for the positive (True) class."""
    tp = sum(1 for t, p in zip(y_true, y_pred) if t and p)
    fp = sum(1 for t, p in zip(y_true, y_pred) if p and not t)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t and not p)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return ClassificationStat(
        precision=round(precision, 6),
        recall=round(recall, 6),
        f1=round(f1, 6),
    )


def confusion(y_true: list[str], y_pred: list[str]) -> dict[str, dict[str, int]]:
    """Full confusion matrix: matrix[true][pred] = count."""
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t, p in zip(y_true, y_pred):
        matrix[t][p] += 1
    return {t: dict(row) for t, row in matrix.items()}
