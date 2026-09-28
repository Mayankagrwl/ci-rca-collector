"""Step 11a — offline eval harness tests (eval-spec §11 items 9, 12-16, 18)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from tools.eval import run_eval
from tools.eval.labels import GoldenLabelError, load_goldens
from tools.eval.metrics import classification_stat, confusion, stat
from tools.rca.models import AnalysisRecord, CallTelemetry, GroundingResult, ValidationTelemetry

_GOLDENS = Path("tools/eval/goldens")


def _load():
    return load_goldens(_GOLDENS)


# --- helpers: injectable analyze_fn (no network) ----------------------------


def _record(rate: float) -> AnalysisRecord:
    return AnalysisRecord(
        status="ok",
        model_called=True,
        grounding=GroundingResult(grounded=rate >= 1.0, citation_count=1,
                                  grounding_rate=rate, checks_run=["fabrication"]),
        validation_telemetry=ValidationTelemetry(schema_valid_first_try=True,
                                                 repair_attempted=False, repair_succeeded=False),
        call_telemetry=CallTelemetry(prompt_tokens_est=500, completion_tokens_est=50,
                                     latency_ms=120, short_circuited=False),
    )


def _varying_analyze(n_goldens: int):
    """Records whose per-run mean grounding_rate varies across runs (LLM variance)."""
    per_run = [1.0, 0.8, 0.6]
    state = {"calls": 0}

    def _fn(_summary):
        run_index = state["calls"] // max(1, n_goldens)
        state["calls"] += 1
        return _record(per_run[min(run_index, len(per_run) - 1)])

    return _fn


# --- #9: --runs 3 reports mean and stdev for every metric -------------------


def test_runs_report_mean_and_stdev_llm_varies_deterministic_stable() -> None:
    result = run_eval.run(goldens_dir=_GOLDENS, runs=3, no_llm=False, filters={},
                          seed=0, analyze_fn=_varying_analyze(len(_load())))
    m = result["metrics"]
    assert m["n_runs"] == 3
    # Every Stat metric has the four fields.
    for key in ("category_accuracy", "grounding_rate", "mean_prompt_tokens", "mean_latency_ms"):
        assert set(m[key]) == {"mean", "stdev", "min", "max"}
    # Deterministic metric is stable across runs (stdev 0); LLM metric varies.
    assert m["category_accuracy"]["stdev"] == 0.0
    assert m["grounding_rate"]["stdev"] > 0.0


def test_runs_below_two_rejected_when_model_runs() -> None:
    with pytest.raises(ValueError):
        run_eval.run(goldens_dir=_GOLDENS, runs=1, no_llm=False, filters={}, seed=0,
                     analyze_fn=_varying_analyze(len(_load())))


# --- #12: --no-llm completes with no network, classification only -----------


def test_no_llm_runs_offline_and_reports_classification_only() -> None:
    def boom(_s):  # would fire if the runner tried to analyze
        raise AssertionError("no model call under --no-llm")

    result = run_eval.run(goldens_dir=_GOLDENS, runs=3, no_llm=True, filters={}, seed=0,
                          analyze_fn=boom)
    m = result["metrics"]
    assert m["n_runs"] == 1  # runs==1 allowed under --no-llm
    assert m["category_accuracy"]["mean"] > 0.0
    # LLM metrics are absent → zeros.
    assert m["grounding_rate"] == {"mean": 0.0, "stdev": 0.0, "min": 0.0, "max": 0.0}


def test_no_llm_allows_runs_one() -> None:
    result = run_eval.run(goldens_dir=_GOLDENS, runs=1, no_llm=True, filters={}, seed=0,
                          analyze_fn=lambda s: _record(1.0))
    assert result["metrics"]["n_runs"] == 1


# --- #13: a golden older than 180 days appears in stale_goldens -------------


def test_stale_golden_detected(tmp_path: Path) -> None:
    old = date.today() - timedelta(days=200)
    fresh = date.today() - timedelta(days=5)
    for slug, when in [("stale-one", old), ("fresh-one", fresh)]:
        d = tmp_path / slug
        d.mkdir()
        (d / "summary.json").write_text(
            (_GOLDENS / "npm-eresolve" / "summary.json").read_text(encoding="utf-8"), encoding="utf-8")
        (d / "label.yaml").write_text(
            f"schema_version: '1.0'\nslug: {slug}\nsource: synthetic\ncaptured_at: '{when}'\n"
            f"true_category: dependency\nis_infra_vs_code: code\nis_flaky: false\n"
            f"expect_short_circuit: null\nlabelled_at: '{when}'\n", encoding="utf-8")
    result = run_eval.run(goldens_dir=tmp_path, runs=1, no_llm=True, filters={}, seed=0)
    assert "stale-one" in result["metrics"]["stale_goldens"]
    assert "fresh-one" not in result["metrics"]["stale_goldens"]


# --- #14: a malformed label.yaml fails loudly and names the file ------------


def test_malformed_label_fails_and_names_file(tmp_path: Path) -> None:
    d = tmp_path / "broken"
    d.mkdir()
    (d / "summary.json").write_text(
        (_GOLDENS / "npm-eresolve" / "summary.json").read_text(encoding="utf-8"), encoding="utf-8")
    (d / "label.yaml").write_text("true_category: not_a_real_category\nsource: synthetic\n", encoding="utf-8")
    with pytest.raises(GoldenLabelError) as exc:
        load_goldens(tmp_path)
    assert "broken" in str(exc.value) and "label.yaml" in str(exc.value)


# --- #15: category metrics include a full confusion matrix ------------------


def test_confusion_matrix_is_full_and_captures_the_artifactory_miss() -> None:
    result = run_eval.run(goldens_dir=_GOLDENS, runs=1, no_llm=True, filters={}, seed=0)
    matrix = result["metrics"]["category_confusion"]
    assert isinstance(matrix, dict)
    # artifactory's true dependency is predicted as unknown (a captured miss).
    assert matrix["dependency"]["unknown"] >= 1
    assert matrix["dependency"]["dependency"] >= 1  # npm-eresolve hit


# --- #16: flake detection reports precision/recall/F1 -----------------------


def test_flake_detection_is_precision_recall_f1() -> None:
    result = run_eval.run(goldens_dir=_GOLDENS, runs=1, no_llm=True, filters={}, seed=0)
    flake = result["metrics"]["flake_detection"]
    assert set(flake) == {"precision", "recall", "f1"}
    assert flake["f1"] == 1.0  # the single flake golden is detected


# --- #18: results record model, prompt hash, collector version, git SHA -----


def test_provenance_recorded(tmp_path: Path) -> None:
    out = tmp_path / "eval.json"
    rc = run_eval.main(["--goldens", str(_GOLDENS), "--no-llm", "--out", str(out)])
    assert rc == 0
    prov = json.loads(out.read_text(encoding="utf-8"))["provenance"]
    assert prov["model"]
    assert prov["prompt_hash"] and len(prov["prompt_hash"]) == 16
    assert prov["collector_version"]
    assert "git_sha" in prov  # present (may be None if git absent)
    from tools.rca.config import PROMPT_VERSION
    assert prov["prompt_version"] == PROMPT_VERSION


# --- filter narrows goldens -------------------------------------------------


def test_filter_narrows_goldens() -> None:
    result = run_eval.run(goldens_dir=_GOLDENS, runs=1, no_llm=True,
                          filters={"true_category": "dependency"}, seed=0)
    # artifactory-version-exists + npm-eresolve are both labelled dependency.
    assert result["metrics"]["n_goldens"] == 2
    result2 = run_eval.run(goldens_dir=_GOLDENS, runs=1, no_llm=True,
                           filters={"is_infra_vs_code": "infra"}, seed=0)
    assert result2["metrics"]["n_goldens"] >= 2  # timeout, image_pull, infra_runner


# --- metric helpers ---------------------------------------------------------


def test_stat_and_classification_helpers() -> None:
    assert stat([]).mean == 0.0
    s = stat([1.0, 0.5])
    assert s.min == 0.5 and s.max == 1.0 and s.stdev > 0.0
    cs = classification_stat([True, False, True], [True, False, False])
    assert cs.precision == 1.0 and cs.recall == 0.5
    m = confusion(["a", "a", "b"], ["a", "b", "b"])
    assert m == {"a": {"a": 1, "b": 1}, "b": {"b": 1}}
