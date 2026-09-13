"""Drain3 novelty, fingerprints, masking hash, redaction."""

from __future__ import annotations

from pathlib import Path

from tools.rca.drain_index import (
    fingerprint_coarse,
    fingerprint_fine,
    jaccard,
    masking_config_hash,
    novelty,
    template_hash,
    train,
)

_INI = Path(__file__).resolve().parents[2] / "drain3.ini"
_SECRET = "Bearer sk-live-abc123"


def test_no_baseline_tri_state(tmp_path: Path) -> None:
    lines = [
        "npm ERR! ERESOLVE could not resolve",
        "INFO processing record 1",
        "INFO processing record 2",
        "##[error]Process completed with exit code 1",
    ]
    result = novelty(lines, "CI_build", drain_dir=tmp_path, config_path=_INI)
    assert result.report.baseline_available is False
    assert result.report.fingerprint_degraded is True
    for tmpl in result.report.templates:
        assert tmpl.is_novel is None
        assert tmpl.baseline_count is None
        assert tmpl.tier not in {"T1", "T2"}
        assert tmpl.first_line is not None
    assert result.fingerprint_fine
    assert result.fingerprint_coarse


def test_jaccard_ignores_cluster_order() -> None:
    a = ["Exception: boom", "retrying in <:NUM:>s", "done"]
    b = list(reversed(a))
    assert jaccard(a, b) == 1.0
    assert jaccard([template_hash(x) for x in a], [template_hash(x) for x in b]) == 1.0
    assert fingerprint_fine(a) == fingerprint_fine(b)
    assert fingerprint_coarse(a[0]) != fingerprint_coarse(a[1])


def test_secret_absent_from_templates_vars_and_rep(tmp_path: Path) -> None:
    lines = [
        f"Authorization: {_SECRET}",
        "npm ERR! ERESOLVE could not resolve",
    ]
    result = novelty(lines, "CI_build", drain_dir=tmp_path, config_path=_INI)
    blob = result.report.model_dump_json()
    assert "sk-live-abc123" not in blob
    for tmpl in result.report.templates:
        assert "sk-live-abc123" not in tmpl.template
        if tmpl.representative_line:
            assert "sk-live-abc123" not in tmpl.representative_line
        for var in tmpl.variables:
            for value in var.values:
                assert "sk-live-abc123" not in value


def test_masking_config_hash_normalises_order_and_whitespace(tmp_path: Path) -> None:
    original = _INI.read_text(encoding="utf-8")
    base = masking_config_hash(str(_INI))
    shuffled = original.replace(
        'mask_prefix = <:\nmask_suffix = :>',
        'mask_suffix = :>\nmask_prefix = <:',
    )
    # extra whitespace in JSON array
    shuffled = shuffled.replace('masking = [', 'masking = [\n  ')
    other = tmp_path / "drain3.ini"
    other.write_text(shuffled, encoding="utf-8")
    assert masking_config_hash(str(other)) == base

    changed_th = original.replace("sim_th = 0.4", "sim_th = 0.5")
    th_path = tmp_path / "th.ini"
    th_path.write_text(changed_th, encoding="utf-8")
    assert masking_config_hash(str(th_path)) != base

    changed_re = original.replace('"mask_with":"NUM"', '"mask_with":"NUMBER"')
    re_path = tmp_path / "re.ini"
    re_path.write_text(changed_re, encoding="utf-8")
    assert masking_config_hash(str(re_path)) != base


def test_novelty_falls_back_to_same_workflow_bin(tmp_path: Path) -> None:
    healthy = [f"INFO processing record {i} of 200 [ok]" for i in range(1, 80)]
    train(
        healthy,
        "Test Failure Scenarios_baseline",
        drain_dir=tmp_path,
        config_path=_INI,
    )
    failing = healthy[-5:] + [
        "Exception: Connection refused to db:5432",
        "ERROR failed to flush buffer: connection reset by peer",
    ]
    result = novelty(
        failing,
        "Test Failure Scenarios_noisy",
        drain_dir=tmp_path,
        config_path=_INI,
        workflow="Test Failure Scenarios",
    )
    assert result.report.baseline_available is True
    assert result.fallback_file == "Test_Failure_Scenarios_baseline.bin"
    exact = novelty(
        failing,
        "Test Failure Scenarios_baseline",
        drain_dir=tmp_path,
        config_path=_INI,
        workflow="Test Failure Scenarios",
    )
    assert exact.fallback_file is None
    assert exact.report.baseline_available is True


def test_train_then_novelty_sets_novel_flags(tmp_path: Path) -> None:
    healthy = ["INFO processing record 1", "INFO processing record 2", "INFO processing record 3"]
    train(healthy, "CI_build", drain_dir=tmp_path, config_path=_INI)
    failing = [
        "INFO processing record 9",
        "npm ERR! ERESOLVE could not resolve",
        "##[error]Process completed with exit code 1",
    ]
    result = novelty(failing, "CI_build", drain_dir=tmp_path, config_path=_INI)
    assert result.report.baseline_available is True
    novels = [t for t in result.report.templates if t.is_novel]
    assert any(t.has_error_match for t in novels)
    assert all(t.baseline_count is not None for t in result.report.templates)
