"""Step 8 — measurement telemetry: append/read round-trip, no leakage, cap,
aggregate summary, and best-effort no-op. Offline only."""

from __future__ import annotations

import json
from pathlib import Path

from tools.rca.cli import main
from tools.rca.telemetry import (
    aggregate,
    append_telemetry,
    read_telemetry,
    telemetry_path,
)

_EXPECTED_KEYS = {
    "ts",
    "fingerprint",
    "fingerprint_coarse",
    "category",
    "confidence",
    "is_infra_vs_code",
    "short_circuit",
    "requires_analysis",
    "rule_id",
    "terminal_cause_present",
    "suspected_stage",
    "recurrence",
    "seen_count",
    "model_called",
    "analyze_decision",
    "analysis_status",
    "display_status",
    "grounded",
    "source",
    "rca_confidence",
    "collector_version",
    "prompt_version",
}


def _record(**over: object) -> dict[str, object]:
    base = {
        "ts": "2026-09-22T00:00:00+00:00",
        "fingerprint": "f" * 16,
        "fingerprint_coarse": "c" * 16,
        "category": "crash",
        "confidence": "high",
        "is_infra_vs_code": "code",
        "short_circuit": None,
        "requires_analysis": True,
        "rule_id": "R9",
        "terminal_cause_present": True,
        "suspected_stage": "build",
        "recurrence": "new",
        "seen_count": 0,
        "model_called": True,
        "analyze_decision": "called",
        "analysis_status": "ok",
        "display_status": "ok",
        "grounded": True,
        "source": "ai",
        "rca_confidence": "high",
        "collector_version": "0.1.0",
        "prompt_version": "p2.4",
    }
    base.update(over)
    return base


# --- 1. append + read round-trip; corrupt line skipped ----------------------


def test_append_and_read_round_trip_skips_corrupt(tmp_path: Path) -> None:
    assert append_telemetry(tmp_path, _record()) is True
    assert append_telemetry(tmp_path, _record(category="dependency")) is True
    # A corrupt/partial line must be skipped, not fatal.
    telemetry_path(tmp_path).open("a", encoding="utf-8").write("{ not json\n")

    records = read_telemetry(tmp_path)
    assert len(records) == 2
    assert set(records[0]) == _EXPECTED_KEYS
    assert records[1]["category"] == "dependency"


# --- 2. no content leakage --------------------------------------------------


def test_no_content_or_secret_leakage(tmp_path: Path) -> None:
    append_telemetry(tmp_path, _record())
    line = telemetry_path(tmp_path).read_text(encoding="utf-8")
    for banned in ("root_cause", "suggested_fix", "content", "window"):
        assert banned not in line
    # A secret that somehow reached a field is redacted by the write path.
    secret = "AbCd1234EfGh5678IjKl9012MnOp3456QrStUvWx"
    append_telemetry(tmp_path, _record(suspected_stage=secret))
    assert secret not in telemetry_path(tmp_path).read_text(encoding="utf-8")


# --- 3. cap -----------------------------------------------------------------


def test_cap_keeps_only_last_n(tmp_path: Path) -> None:
    for i in range(15):
        append_telemetry(tmp_path, _record(fingerprint=f"{i:016d}"), cap=10)
    records = read_telemetry(tmp_path)
    assert len(records) == 10
    assert records[0]["fingerprint"] == f"{5:016d}"  # oldest 5 dropped
    assert records[-1]["fingerprint"] == f"{14:016d}"


# --- 4. aggregate -----------------------------------------------------------


def test_aggregate_totals_and_per_category(tmp_path: Path) -> None:
    for rec in [
        _record(category="crash", model_called=True, grounded=True, analyze_decision="called",
                display_status="ok", source="ai"),
        _record(category="crash", model_called=True, grounded=False, analyze_decision="called",
                display_status="needs-review", source="deterministic"),
        _record(category="dependency", model_called=False, grounded=None,
                analyze_decision="skipped:deterministic_sufficient",
                display_status="deterministic", source="deterministic"),
        _record(category="infra_runner", model_called=False,
                analyze_decision="skipped:short_circuit",
                display_status="deterministic", source="deterministic"),
    ]:
        append_telemetry(tmp_path, rec)

    agg = aggregate(read_telemetry(tmp_path))
    assert agg["total_runs"] == 4
    assert agg["by_category"] == {"crash": 2, "dependency": 1, "infra_runner": 1}
    assert agg["pct_model_called"] == 50.0
    assert agg["pct_grounded_of_called"] == 50.0
    assert agg["pct_skipped_deterministic_sufficient"] == 25.0
    assert agg["pct_skipped_short_circuit"] == 25.0
    assert agg["pct_needs_review"] == 25.0
    crash = agg["per_category"]["crash"]
    assert crash["model_called"] == 2
    assert crash["grounded_rate"] == 50.0
    assert crash["source_ai_hybrid"] == 1
    assert crash["source_deterministic"] == 1


def test_telemetry_subcommand_writes_summary(tmp_path: Path) -> None:
    append_telemetry(tmp_path, _record())
    out = tmp_path / "summary.json"
    rc = main(["telemetry", "--history-dir", str(tmp_path), "--out", str(out), "--format", "json"])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["total_runs"] == 1


# --- 5. best-effort ---------------------------------------------------------


def test_append_is_noop_on_missing_dir() -> None:
    assert append_telemetry(None, _record()) is False
    assert read_telemetry(None) == []


def test_aggregate_on_empty_is_safe() -> None:
    agg = aggregate([])
    assert agg["total_runs"] == 0
    assert agg["pct_model_called"] == 0.0
    assert agg["pct_grounded_of_called"] == 0.0


# --- integration: analyze appends a line and exits 0 ------------------------

_FIXTURE = "tests/rca/fixtures/sample-failure"


def _collect(tmp_path: Path) -> Path:
    out = tmp_path / "rca"
    rc = main(
        [
            "collect", "--from-fixture", _FIXTURE, "--out", str(out),
            "--history-backend", "none", "--drain-dir", str(tmp_path / "drain"),
        ]
    )
    assert rc == 0
    return out


def test_analyze_appends_telemetry_and_exits_zero(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    history = tmp_path / "history"
    rc = main(
        [
            "analyze", "--summary", str(out / "summary.json"), "--out", str(out),
            "--cache-dir", str(tmp_path / "cache"), "--stgpt-key-present", "false",
            "--history-dir", str(history),
        ]
    )
    assert rc == 0
    records = read_telemetry(history)
    assert len(records) == 1
    assert set(records[0]) == _EXPECTED_KEYS
    assert records[0]["fingerprint"]


def test_analyze_exits_zero_when_telemetry_disabled(tmp_path: Path) -> None:
    out = _collect(tmp_path)
    history = tmp_path / "history"
    rc = main(
        [
            "analyze", "--summary", str(out / "summary.json"), "--out", str(out),
            "--cache-dir", str(tmp_path / "cache"), "--stgpt-key-present", "false",
            "--history-dir", str(history), "--write-telemetry", "false",
        ]
    )
    assert rc == 0
    assert telemetry_path(history) is not None and not telemetry_path(history).exists()
