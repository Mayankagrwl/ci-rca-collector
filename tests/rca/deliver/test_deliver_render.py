"""Step 14 AC1–AC5 — comment rendering (delivery spec v1.3 §7, §7.1–§7.3, §11)."""

from __future__ import annotations

import copy
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.rca.deliver._helpers import make_summary
from tools.eval.labels import VALID_CATEGORIES, load_goldens
from tools.rca.deliver import DeliveryContext, DeliveryInputs, SuppressionDecision
from tools.rca.deliver import render_comment as rc
from tools.rca.deliver.render_comment import (
    CATEGORY_WORDS,
    MAX_BODY_CHARS,
    UNVERIFIED_BANNER,
    render_comment,
)
from tools.rca.deliver.suppress import evaluate
from tools.rca.diagnose import apply_verdict, diagnose
from tools.rca.models import (
    AnalysisCitation,
    AnalysisRecord,
    AnalysisResult,
    DeterministicDiagnosis,
    FailedStepExcerpt,
    GroundingResult,
)

_ROOT = Path(__file__).resolve().parents[3]
_GOLDENS = {slug: s for slug, s, _l, _d in load_goldens(_ROOT / "tools/eval/goldens")}
_RULE_ID = re.compile(r"\bR\d+\b")
_SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _ctx(*, fork: bool = False, trigger: str = "push_default") -> DeliveryContext:
    return DeliveryContext(
        trigger=trigger,  # type: ignore[arg-type]
        pr_number=4 if trigger == "pull_request" else None,
        is_fork=fork,
        branch="main",
        default_branch="main",
        is_default_branch=True,
        actor="octocat",
        commit_sha="f" * 40,
    )


def _diagnosed(slug: str):
    """A golden as collect would hand it over: with the deterministic card applied."""
    summary = copy.deepcopy(_GOLDENS[slug])
    return apply_verdict(summary, diagnose(copy.deepcopy(summary)))


def _golden_body(slug: str) -> str:
    summary = _diagnosed(slug)
    decision = evaluate(summary, None, _ctx(), DeliveryInputs())
    return render_comment(summary, None, _ctx(), decision)


def _without_marker(body: str) -> str:
    return body.split("\n", 1)[1]


def _headline(body: str) -> str | None:
    lines = body.splitlines()
    heading = next(i for i, ln in enumerate(lines) if ln.startswith("### CI failure"))
    after = lines[heading + 2] if len(lines) > heading + 2 else ""
    return after[2:-2] if after.startswith("**") and after.endswith("**") else None


def _evidence(body: str) -> list[str]:
    if "<details>" not in body:
        return []
    block = body.split("<details><summary>Evidence</summary>", 1)[1].split("</details>", 1)[0]
    lines = [ln for ln in block.strip("\n").splitlines()]
    return lines[1:-1]  # drop the opening / closing fence


# ---- AC1: goldens ----------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(_GOLDENS))
def test_every_golden_renders_safely(slug: str) -> None:
    summary = _diagnosed(slug)
    body = _golden_body(slug)
    assert len(body) <= MAX_BODY_CHARS
    assert body.splitlines()[0] == f"<!-- rca-bot:fp={summary.fingerprint_coarse} -->"
    assert not _RULE_ID.search(_without_marker(body))
    words = CATEGORY_WORDS[summary.classification.category]
    assert f"### CI failure — {words}" in body
    for key in CATEGORY_WORDS:
        if "_" in key:
            assert key not in body, key
    assert "Seen " in body and "/resolved" in body


def test_artifactory_golden_heading_and_headline() -> None:  # delivery AC #34
    body = _golden_body("artifactory-version-exists")
    primary = _GOLDENS["artifactory-version-exists"].failed_jobs[0].primary_failure_line
    assert "### CI failure — release version already published" in body
    assert "unclassified" not in body
    assert _headline(body) == primary
    assert _evidence(body)[0] == primary


def test_java_compile_evidence_leads_with_the_cause() -> None:
    evidence = _evidence(_golden_body("java-compile-in-pipeline"))
    assert any("cannot find symbol" in line for line in evidence)
    assert "cannot find symbol" in evidence[0]
    assert not any("No services to build" in line for line in evidence)
    assert not any(line.lstrip().startswith("Using ") for line in evidence)


def test_docker_already_exists_is_never_cause_or_evidence() -> None:
    body = _golden_body("docker-already-exists-benign")
    assert "Already exists 0B" not in (_headline(body) or "")
    assert not any("Already exists" in line for line in _evidence(body))


# ---- AC2: headline precedence (v1.3 §7.1) ----------------------------------------------

_DET_LINE = "deterministic cause line"
_DET_FIX = "deterministic fix line"
_MODEL_LINE = "model root cause prose"


def _card_summary(confidence: str = "high"):
    summary = make_summary(confidence=confidence)
    summary.classification.category = "compile"
    summary.diagnosis = DeterministicDiagnosis(
        rule_id="R9", one_liner=_DET_LINE, fix_one_liner=_DET_FIX
    )
    return summary


def _model_record(*, status="ok", source="ai", confidence="high", rate=1.0):
    return AnalysisRecord(
        status=status,
        result=AnalysisResult(
            root_cause=_MODEL_LINE,
            suggested_fix="model fix prose",
            confidence=confidence,
            source=source,
        ),
        grounding=GroundingResult(grounded=rate >= 1.0, grounding_rate=rate),
    )


def _render(summary, record, *, threshold="medium", fork=False):
    ctx = _ctx(fork=fork)
    decision = evaluate(summary, record, ctx, DeliveryInputs(confidence_threshold=threshold))
    return render_comment(summary, record, ctx, decision)


def test_rule1_below_threshold_omits_root_cause_from_both_sources() -> None:  # AC #10
    body = _render(_card_summary(), _model_record(confidence="low"))
    assert _headline(body) is None
    assert _MODEL_LINE not in body and _DET_LINE not in body
    assert "Suggested fix" not in body
    assert "Confidence: low" in body
    det_only = _render(_card_summary(confidence="low"), None)
    assert _headline(det_only) is None and _DET_LINE not in det_only


@pytest.mark.parametrize("source", ["ai", "hybrid", "mixed"])
@pytest.mark.parametrize("status", ["ok", "cached"])
def test_rule2_model_answer(status, source) -> None:
    body = _render(_card_summary(), _model_record(status=status, source=source, confidence="medium"))
    assert _headline(body) == _MODEL_LINE
    assert "model fix prose" in body
    assert "Confidence: medium" in body
    assert _DET_LINE not in body


def test_rule3_deterministic_card() -> None:
    skipped = AnalysisRecord(
        status="skipped",
        result=AnalysisResult(
            root_cause="card", suggested_fix="card fix", confidence="high", source="deterministic"
        ),
    )
    for record in (None, skipped, _model_record(source="deterministic")):
        body = _render(_card_summary(), record)
        assert _headline(body) == _DET_LINE
        assert f"**Suggested fix** — {_DET_FIX}" in body
        assert _MODEL_LINE not in body
        assert UNVERIFIED_BANNER not in body


def test_rule4_needs_review_uses_banner_and_deterministic_text() -> None:  # AC #11
    record = _model_record(status="unvalidated", source="ai", confidence="high")
    body = _render(_card_summary(), record)
    assert UNVERIFIED_BANNER in body
    assert _headline(body) == _DET_LINE
    assert _MODEL_LINE not in body


def test_partial_grounding_banner_with_model_answer() -> None:
    body = _render(_card_summary(), _model_record(rate=0.8))
    assert UNVERIFIED_BANNER in body
    assert _headline(body) == _MODEL_LINE


# ---- AC3: cap ---------------------------------------------------------------------


def _huge_summary():
    summary = _card_summary()
    job = summary.failed_jobs[0]
    job.primary_failure_line = "E fatal: " + "x" * 400
    job.failed_step_excerpt = FailedStepExcerpt(
        name="Build",
        lines=[f"line {i} " + "y" * 280 + (f" token {_SECRET}" if i % 3 == 0 else "") for i in range(500)],
    )
    summary.diagnosis.one_liner = "h" * 280 + " " + _SECRET + " tail"
    return summary


def test_huge_excerpt_stays_under_cap_with_parts_intact() -> None:  # delivery AC #24
    summary = _huge_summary()
    body = _render(summary, None)
    assert len(body) <= MAX_BODY_CHARS
    assert body.startswith("<!-- rca-bot:fp=")
    assert "### CI failure — compilation error" in body
    assert _headline(body) is not None and _headline(body).startswith("hhh")
    assert body.rstrip().endswith("</sub>")
    assert _SECRET[:8] not in body and "ghp_" not in body


@pytest.mark.parametrize("cap", range(700, 3200, 97))
def test_structural_truncation_never_splits_a_redaction(monkeypatch, cap) -> None:
    monkeypatch.setattr(rc, "MAX_BODY_CHARS", cap)
    body = _render(_huge_summary(), None)
    assert "ghp_" not in body and _SECRET[4:12] not in body
    assert body.startswith("<!-- rca-bot:fp=")
    assert "### CI failure" in body and body.rstrip().endswith("</sub>")
    assert _headline(body) is not None
    if len(body) > cap:  # only when even the fixed parts exceed a tiny cap
        assert "<details>" not in body


def test_details_dropped_before_touching_fixed_parts(monkeypatch) -> None:
    monkeypatch.setattr(rc, "MAX_BODY_CHARS", 900)
    body = _render(_huge_summary(), None)
    assert "<details>" not in body
    assert _headline(body) is not None and "/resolved" in body


# ---- AC4: safety -------------------------------------------------------------------


def test_backtick_run_cannot_break_the_fence() -> None:
    summary = _card_summary()
    summary.failed_jobs[0].primary_failure_line = "boom ```` </details> injected ``` tail"
    body = _render(summary, None)
    assert "`````text" in body  # fence longer than the 4-backtick run
    block = body.split("`````text\n", 1)[1]
    evidence, closing = block.split("\n`````\n", 1)
    assert "injected" in evidence
    assert closing.startswith("</details>")


def test_mentions_are_neutralised_in_model_and_log_text() -> None:
    summary = _card_summary()
    summary.failed_jobs[0].primary_failure_line = "failed, blame @bob-dev"
    record = _model_record()
    record.result.root_cause = "ask @alice about it"
    body = _render(summary, record)
    assert "@alice" not in body and "@​alice" in body
    assert "@bob-dev" not in body and "@​bob-dev" in body


def test_fork_escapes_html_outside_the_fence() -> None:  # delivery AC #18 spirit
    record = _model_record()
    record.result.root_cause = "<script>alert(1)</script> broke it"
    body = _render(_card_summary(), record, fork=True)
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; broke it" in body


def test_rule_ids_are_defused_everywhere() -> None:
    record = _model_record()
    record.result.root_cause = "rule R8 and R19 fired"
    summary = _card_summary()
    summary.failed_jobs[0].primary_failure_line = "log mentions R14 too"
    body = _render(summary, record)
    assert not _RULE_ID.search(_without_marker(body))


def test_run_link_attempt_and_seen_count() -> None:
    summary = _card_summary()
    summary.run.run_attempt = 2
    summary.history.seen_count = 4
    body = _render(summary, None)
    assert f"[run {summary.run.run_id}]({summary.run.html_url}) (attempt 2)" in body
    assert "Seen 5×" in body


def test_only_anchored_citations_become_evidence() -> None:
    summary = _card_summary()
    job = summary.failed_jobs[0]
    job.primary_failure_line = "the anchored cause"
    record = _model_record()
    record.result.citations = [
        AnalysisCitation(quote="a drain template from another step", source="log_templates"),
        AnalysisCitation(quote="the anchored cause", source="failed_step_excerpt"),
    ]
    body = _render(summary, record)
    assert "a drain template from another step" not in body


# ---- AC5: every category has words ---------------------------------------------------


def test_every_category_has_human_words() -> None:
    for category in set(VALID_CATEGORIES) | {"unknown"}:
        assert category in CATEGORY_WORDS, category
        assert CATEGORY_WORDS[category] != category
    assert rc.category_words("brand_new_category") == "unrecognised failure type"


def test_unknown_category_never_renders_raw_key() -> None:
    summary = _card_summary()
    summary.classification.category = "brand_new_category"
    assert "brand_new_category" not in _render(summary, None)


def test_render_comment_is_import_pure() -> None:
    code = (
        "import sys\nimport tools.rca.deliver.render_comment\n"
        "print(','.join(m for m in sys.modules if m == 'httpx' or m.endswith('github_api')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=_ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_suppression_decision_type_is_reused() -> None:
    assert isinstance(evaluate(_card_summary(), None, _ctx(), DeliveryInputs()), SuppressionDecision)
