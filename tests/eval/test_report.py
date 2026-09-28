"""Step 11b — report + baseline diffing + gating + redaction check (eval-spec §11)."""

from __future__ import annotations

import json
from pathlib import Path

from tools.eval import check_goldens_redacted, report
from tools.eval.metrics import ClassificationStat, EvalMetrics, Stat

_GOLDENS = Path("tools/eval/goldens")


def _results(
    *,
    grounding: tuple[float, float] = (0.90, 0.05),
    category: tuple[float, float] = (0.90, 0.0),
    per_golden: list[dict] | None = None,
    confusion: dict | None = None,
    flake: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> dict:
    metrics = EvalMetrics(
        n_goldens=len(per_golden or []),
        n_runs=3,
        category_accuracy=Stat(mean=category[0], stdev=category[1], min=category[0], max=category[0]),
        grounding_rate=Stat(mean=grounding[0], stdev=grounding[1], min=grounding[0], max=grounding[0]),
        flake_detection=ClassificationStat(precision=flake[0], recall=flake[1], f1=flake[2]),
        category_confusion=confusion or {"dependency": {"dependency": 1, "unknown": 1}},
    )
    return {
        "metrics": metrics.model_dump(),
        "per_golden": per_golden or [],
        "provenance": {"collector_version": "0.1.0", "prompt_version": "p2.4",
                       "prompt_hash": "abc0000000000000", "git_sha": "deadbeefcafe"},
        "no_llm": False,
    }


def _write(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# --- #10: a delta smaller than stdev is rendered as within-variance ---------


def test_within_variance_delta_not_a_change(tmp_path: Path) -> None:
    baseline = _write(tmp_path, "base.json", _results(grounding=(0.90, 0.05)))
    current = _write(tmp_path, "cur.json", _results(grounding=(0.92, 0.05)))  # delta 0.02 < 0.05
    rendered, summary, _ = report.run(
        results_path=current, baseline_path=baseline, fail_under={}, fmt="markdown",
        summary_out=tmp_path / "s.json")
    assert "within run-to-run variance" in rendered
    assert "grounding_rate" in summary["within_variance"]


def test_real_change_beyond_variance_is_flagged(tmp_path: Path) -> None:
    baseline = _write(tmp_path, "base.json", _results(grounding=(0.90, 0.01)))
    current = _write(tmp_path, "cur.json", _results(grounding=(0.60, 0.01)))  # delta 0.30 >> 0.01
    rendered, summary, _ = report.run(
        results_path=current, baseline_path=baseline, fail_under={}, fmt="markdown",
        summary_out=tmp_path / "s.json")
    assert "grounding_rate" not in summary["within_variance"]
    assert "changed" in rendered


# --- #11: --fail-under both directions --------------------------------------


def test_fail_under_passes_and_fails(tmp_path: Path) -> None:
    res = _write(tmp_path, "cur.json", _results(grounding=(0.90, 0.0), category=(0.90, 0.0)))
    _, _, passed_ok = report.run(results_path=res, baseline_path=None,
                                 fail_under={"category_accuracy": 0.70}, fmt="markdown",
                                 summary_out=tmp_path / "a.json")
    assert passed_ok is True
    _, summary, passed_bad = report.run(results_path=res, baseline_path=None,
                                        fail_under={"grounding_rate": 0.95}, fmt="markdown",
                                        summary_out=tmp_path / "b.json")
    assert passed_bad is False
    assert summary["fail_under"]["grounding_rate"]["passed"] is False


def test_main_exit_codes(tmp_path: Path) -> None:
    res = _write(tmp_path, "cur.json", _results())
    ok = report.main(["--results", str(res), "--fail-under", "category_accuracy=0.5",
                      "--summary-out", str(tmp_path / "s1.json")])
    assert ok == 0
    bad = report.main(["--results", str(res), "--fail-under", "grounding_rate=0.99",
                       "--summary-out", str(tmp_path / "s2.json")])
    assert bad == 1


# --- #17: list every golden whose verdict changed vs baseline ---------------


def test_changed_goldens_listed(tmp_path: Path) -> None:
    base_rows = [
        {"slug": "g1", "category": "compile", "infra_vs_code": "code", "is_flaky": False, "short_circuit": None},
        {"slug": "g2", "category": "oom", "infra_vs_code": "code", "is_flaky": False, "short_circuit": None},
    ]
    cur_rows = [
        {"slug": "g1", "category": "test_failure", "infra_vs_code": "code", "is_flaky": False, "short_circuit": None},
        {"slug": "g2", "category": "oom", "infra_vs_code": "code", "is_flaky": False, "short_circuit": None},
    ]
    baseline = _write(tmp_path, "base.json", _results(per_golden=base_rows))
    current = _write(tmp_path, "cur.json", _results(per_golden=cur_rows))
    rendered, summary, _ = report.run(results_path=current, baseline_path=baseline,
                                      fail_under={}, fmt="markdown", summary_out=tmp_path / "s.json")
    changed = summary["changed_goldens"]
    assert any(c["slug"] == "g1" and c["kind"] == "changed" for c in changed)
    assert all(c["slug"] != "g2" for c in changed)  # unchanged golden not listed
    assert "category: compile → test_failure" in rendered
    assert "`g1`" in rendered


# --- #15/#16 surfaced: confusion matrix + flake P/R/F1 rendered -------------


def test_confusion_and_flake_rendered(tmp_path: Path) -> None:
    res = _write(tmp_path, "cur.json", _results(
        confusion={"dependency": {"dependency": 1, "unknown": 1}, "compile": {"compile": 1}},
        flake=(1.0, 0.5, 0.6667)))
    rendered, _, _ = report.run(results_path=res, baseline_path=None, fail_under={},
                                fmt="markdown", summary_out=tmp_path / "s.json")
    assert "Category confusion matrix" in rendered
    assert "true \\ pred" in rendered
    assert "P=1.0000 R=0.5000 F1=0.6667" in rendered


# --- #6: report runs offline, emits markdown + machine JSON summary ---------


def test_report_writes_machine_summary(tmp_path: Path) -> None:
    res = _write(tmp_path, "cur.json", _results())
    out = tmp_path / "machine.json"
    rendered, _, _ = report.run(results_path=res, baseline_path=None, fail_under={},
                                fmt="markdown", summary_out=out)
    assert rendered.startswith("# RCA eval report")
    machine = json.loads(out.read_text(encoding="utf-8"))
    assert "headline" in machine and "gate_passed" in machine


def test_json_format_emits_machine_summary(tmp_path: Path) -> None:
    res = _write(tmp_path, "cur.json", _results())
    rendered, _, _ = report.run(results_path=res, baseline_path=None, fail_under={},
                                fmt="json", summary_out=tmp_path / "s.json")
    parsed = json.loads(rendered)
    assert "headline" in parsed


# --- #19: redaction check fails on a planted secret, passes on the seed set --


def test_check_goldens_passes_on_seed_set() -> None:
    assert check_goldens_redacted.scan(_GOLDENS) == []
    assert check_goldens_redacted.main(["--goldens", str(_GOLDENS)]) == 0


def test_check_goldens_fails_on_planted_secret(tmp_path: Path) -> None:
    golden = tmp_path / "leaky"
    golden.mkdir()
    clean = (_GOLDENS / "npm-eresolve" / "summary.json").read_text(encoding="utf-8")
    # Plant a token matching a redact.py pattern inside a captured field.
    leaked = clean.replace('"ci"', '"ci ghp_' + "A" * 30 + '"', 1)
    (golden / "summary.json").write_text(leaked, encoding="utf-8")
    (golden / "label.yaml").write_text("slug: leaky\n", encoding="utf-8")
    offenders = check_goldens_redacted.scan(tmp_path)
    assert offenders and offenders[0][0].name == "summary.json"
    assert check_goldens_redacted.main(["--goldens", str(tmp_path)]) == 1
