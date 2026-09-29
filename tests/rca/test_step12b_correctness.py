"""Step 12b — pre-delivery correctness fixes, driven by the committed goldens.

D1 terminal cause → category gap-fill (R19, new ``release`` category),
D2 headline cites the failed step's own line, D3 source labelling,
D4 AGENTS.md reflects the current phases.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from tests.rca.test_diagnose import _summary
from tools.eval.labels import VALID_CATEGORIES, load_goldens
from tools.rca.analyze import decide_stgpt_call
from tools.rca.classify import _side
from tools.rca.config import (
    EVIDENCE_PROFILES,
    TERMINAL_CAUSE_CATEGORIES,
    TERMINAL_CAUSE_PATTERNS,
    TERMINAL_CAUSE_RULES,
)
from tools.rca.diagnose import (
    SIGNATURE_CATEGORIES,
    DeterministicVerdict,
    _contains_rule_id,
    _FIX_BY_RULE,
    apply_verdict,
    diagnose,
    specific_log_cause,
    user_facing,
)
from tools.rca.models import AnalysisRecord, AnalysisResult
from tools.rca.outputs import analysis_outputs, diagnosis_source

_ROOT = Path(__file__).resolve().parents[2]
_GOLDENS = {slug: summary for slug, summary, _label, _dir in load_goldens(_ROOT / "tools/eval/goldens")}
_RULE_ID = re.compile(r"\bR\d+\b")

# Verdicts of every golden decided by a category rule before Step 12b. The
# gap-fill must leave these byte-for-byte unchanged in rule/category/confidence.
_CATEGORY_RULE_GOLDENS = {
    "docker-already-exists-benign": ("R17", "test_failure", "high"),
    "flake-same-sha": ("R3", "test_failure", "high"),
    "image-pull": ("R7", "image_pull", "high"),
    "infra-runner": ("R1", "infra_runner", "high"),
    "java-compile-in-pipeline": ("R9", "compile", "high"),
    "npm-eresolve": ("R8", "dependency", "high"),
    "oom": ("R5", "oom", "high"),
    "test-failure-junit": ("R10", "test_failure", "high"),
    "timeout": ("R4", "timeout", "high"),
}


def _golden(slug: str):
    return copy.deepcopy(_GOLDENS[slug])


# --- table wiring ----------------------------------------------------------


def test_terminal_patterns_are_derived_and_unchanged() -> None:
    assert TERMINAL_CAUSE_PATTERNS == [rule["pattern"] for rule in TERMINAL_CAUSE_RULES]
    # Pre-12b list, verbatim: every existing consumer sees the same patterns.
    assert TERMINAL_CAUSE_PATTERNS == [
        r"(?:release|version|tag|artifact|image|package)\b[^\n]*already exists",
        r"already exists[^\n]*(?:you need to update|update (?:the )?package|overwrite|on\s+\w+)",
        r"version exists",
        r"must update",
        r"you need to update",
        r"ERESOLVE",
        r"No matching distribution",
        r"Could not find a version",
        r"ModuleNotFoundError",
        r"Cannot find module",
        r"error TS\d+",
        r"cannot find symbol",
        r"AssertionError",
        r"\bFAILED\s+\S+",
        r"ENOSPC",
        r"quality gate (?:failed|not passed)",
        r"coverage .*(?:below|threshold|did not meet)",
    ]


def test_release_category_is_wired_everywhere() -> None:
    assert "release" in TERMINAL_CAUSE_CATEGORIES
    assert _side("release") == "code"
    assert "release" in SIGNATURE_CATEGORIES
    assert "release" in EVIDENCE_PROFILES
    assert "log_templates" not in EVIDENCE_PROFILES["release"]
    assert "change_context" in EVIDENCE_PROFILES["release"]
    assert "release" in VALID_CATEGORIES
    assert "R19" in _FIX_BY_RULE
    action = (_ROOT / "action.yml").read_text(encoding="utf-8")
    assert "release" in action.split("  category:", 1)[1].split("value:", 1)[0]


# --- AC 1: artifactory golden → release, high, no analysis ------------------


def test_artifactory_golden_becomes_release_verdict() -> None:
    summary = _golden("artifactory-version-exists")
    primary = summary.failed_jobs[0].primary_failure_line
    verdict = diagnose(copy.deepcopy(summary))
    assert verdict.category == "release"
    assert verdict.confidence == "high"
    assert verdict.requires_analysis is False
    assert verdict.is_infra_vs_code == "code"
    assert verdict.one_liner == primary
    assert not verdict.one_liner.startswith("Checking if version")


# --- AC 2: the model is no longer called for it -----------------------------


def test_artifactory_golden_is_deterministic_sufficient() -> None:
    summary = _golden("artifactory-version-exists")
    applied = apply_verdict(summary, diagnose(copy.deepcopy(summary)))
    assert decide_stgpt_call(applied, stgpt_key_present=True) == (
        False,
        "deterministic_sufficient",
    )


# --- AC 3: no rule id leaks -------------------------------------------------


def test_new_rule_id_never_reaches_user_text() -> None:
    summary = _golden("artifactory-version-exists")
    verdict = diagnose(copy.deepcopy(summary))
    card = user_facing(copy.deepcopy(verdict), summary)
    for text in (card.root_cause, card.suggested_fix, verdict.one_liner, verdict.fix_one_liner):
        assert not _RULE_ID.search(text or ""), text
    assert all(not _RULE_ID.search(c.quote) for c in card.citations)


def test_rule_id_regex_covers_any_number() -> None:
    for rid in ("R1", "R9", "R18", "R19", "R20", "R123"):
        assert _contains_rule_id(f"see {rid} here"), rid
    summary = _summary(job_text="nothing recognisable happened here")
    verdict = DeterministicVerdict(
        category="unknown",
        confidence="low",
        is_infra_vs_code="unknown",
        requires_analysis=True,
        rule_id="R19",
        one_liner="R19: something opaque happened",
        fix_one_liner="R27: look at it",
    )
    card = user_facing(verdict, summary)
    assert card.root_cause == "something opaque happened"
    assert card.suggested_fix == "look at it"


# --- AC 4: gap-filling only -------------------------------------------------


@pytest.mark.parametrize("slug", sorted(_CATEGORY_RULE_GOLDENS))
def test_category_rule_goldens_are_unchanged(slug: str) -> None:
    verdict = diagnose(_golden(slug))
    assert (verdict.rule_id, verdict.category, verdict.confidence) == _CATEGORY_RULE_GOLDENS[slug]


def test_benign_docker_already_exists_never_becomes_release() -> None:
    assert diagnose(_golden("docker-already-exists-benign")).category == "test_failure"
    assert diagnose(_golden("java-compile-in-pipeline")).category == "compile"
    summary = _summary(job_text="a1b2c3d4e5f6: Already exists 0B\nexit status 1")
    summary.failed_jobs[0].primary_failure_line = "a1b2c3d4e5f6: Already exists 0B"
    verdict = diagnose(summary)
    assert verdict.category != "release"
    assert verdict.rule_id != "R19"


def test_classified_residual_is_not_gap_filled() -> None:
    # "failed for" is a case-insensitive terminal match, but not a pytest
    # "FAILED <name>" line: it must not be gap-filled into test_failure.
    verdict = diagnose(_summary(job_text="build failed for an unspecified reason"))
    assert verdict.rule_id == "R18"
    assert verdict.category == "unknown"


def test_terminal_line_without_category_is_not_gap_filled() -> None:
    summary = _summary(job_text="Quality gate failed: coverage below 80%")
    summary.failed_jobs[0].primary_failure_line = "Quality gate failed: coverage below 80%"
    verdict = diagnose(summary)
    assert verdict.rule_id != "R19"


def test_gap_fill_generalises_beyond_the_golden() -> None:
    # (No "Error:" prefix: that alone matches a generic classify rule, and a
    # residual that already carries a category is never gap-filled.)
    line = "Publish rejected: tag v2.0.0 already exists; you need to update the version"
    summary = _summary(job_text=f"Publishing v2.0.0\n{line}\nexit status 1")
    summary.failed_jobs[0].primary_failure_line = line
    verdict = diagnose(summary)
    assert (verdict.rule_id, verdict.category, verdict.confidence) == ("R19", "release", "high")
    assert verdict.one_liner == line


# --- AC 5: headline line ----------------------------------------------------


def test_specific_log_cause_prefers_primary_failure_line() -> None:
    summary = _golden("artifactory-version-exists")
    assert specific_log_cause(summary) == summary.failed_jobs[0].primary_failure_line


def test_echo_line_mentioning_registry_is_not_a_cause() -> None:
    summary = _summary(
        job_text="Checking if version 3.1.21 exists in Artifactory\nexit status 1"
    )
    assert specific_log_cause(summary) is None


# --- AC 6: diagnosis-source follows the shown card --------------------------


def test_diagnosis_source_follows_result_source() -> None:
    summary = _summary()
    det = AnalysisRecord(
        status="ok",
        result=AnalysisResult(
            root_cause="x", suggested_fix="y", confidence="medium", source="deterministic"
        ),
    )
    assert diagnosis_source(summary, det) == "deterministic"
    assert analysis_outputs(det)["diagnosis-source"] == "deterministic"
    for src in ("ai", "hybrid", "mixed"):
        rec = det.model_copy(update={"result": det.result.model_copy(update={"source": src})})
        assert diagnosis_source(summary, rec) == "ai"
    gated = AnalysisRecord(status="gated", notes=["skipped: requires_analysis is false"])
    assert diagnosis_source(summary, gated) == "gated"


# --- AC 7: AGENTS.md --------------------------------------------------------


def test_agents_md_reflects_current_phases() -> None:
    text = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "phase 1 only" not in lowered
    assert "no pr comments" not in lowered
    assert "`actions: read`, `contents: read`" in text
    assert "tools/rca/deliver/" in text
    assert "separate delivery job" in text
    for module in (
        "cleaner.py",
        "drain_index.py",
        "budget.py",
        "redact.py",
        "history.py",
        "extract.py",
        "classify.py",
        "pipeline_logs.py",
    ):
        assert f"`{module}`" in text, module
    assert "pyyaml" in text
    assert "tools/eval/" in text
    assert "R<n>" in text
