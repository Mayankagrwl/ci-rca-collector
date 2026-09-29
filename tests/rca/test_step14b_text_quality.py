"""Step 14b — sharper headlines, cleaner evidence, Maven / file:// source locations.

Text quality only: categories, confidence and gate decisions are pinned unchanged.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from tests.rca.test_diagnose import _summary
from tools.eval.labels import load_goldens
from tools.rca.analyze import decide_stgpt_call
from tools.rca.deliver import DeliveryContext, DeliveryInputs
from tools.rca.deliver.render_comment import MAX_BODY_CHARS, evidence_lines, render_comment
from tools.rca.deliver.suppress import evaluate
from tools.rca.diagnose import (
    DeterministicVerdict,
    apply_verdict,
    diagnose,
    headline_cause_line,
    user_facing,
)
from tools.rca.extract import extract_source_paths, is_exit_code_line
from tools.rca.models import JUnitFailure, JUnitReport

_ROOT = Path(__file__).resolve().parents[2]
_GOLDENS = {slug: s for slug, s, _l, _d in load_goldens(_ROOT / "tools/eval/goldens")}
_RULE_ID = re.compile(r"\bR\d+\b")
_CTX = DeliveryContext(
    trigger="push_default",
    branch="main",
    default_branch="main",
    is_default_branch=True,
    actor="octocat",
    commit_sha="f" * 40,
)

# Verdicts before Step 14b — text changes must never move any of these.
_LOCKED = {
    "artifactory-version-exists": ("R19", "release", "high", False, (False, "deterministic_sufficient")),
    # Pre-14b these two already called the model (not deterministic-sufficient); a
    # sharper headline / a suspected file must not flip that.
    "docker-already-exists-benign": ("R17", "test_failure", "high", False, (True, None)),
    "java-compile-in-pipeline": ("R9", "compile", "high", False, (True, None)),
    "npm-eresolve": ("R8", "dependency", "high", False, (False, "deterministic_sufficient")),
}


def _diagnosed(slug: str):
    summary = copy.deepcopy(_GOLDENS[slug])
    verdict = diagnose(copy.deepcopy(summary))
    return apply_verdict(summary, copy.deepcopy(verdict)), verdict


def _body(slug: str) -> str:
    summary, _ = _diagnosed(slug)
    return render_comment(summary, None, _CTX, evaluate(summary, None, _CTX, DeliveryInputs()))


# ---- AC1: source locations ----------------------------------------------------------


def test_maven_and_kotlin_locations() -> None:
    assert extract_source_paths(
        "[ERROR] /work/src/test/TestConfig.java:[19,40] cannot find symbol"
    ) == [("/work/src/test/TestConfig.java", 19)]
    assert extract_source_paths("[ERROR] src/Foo.java:[7] ';' expected") == [("src/Foo.java", 7)]
    assert extract_source_paths(
        "e: file:///home/ci/app/src/main/kotlin/Foo.kt:12:5 Unresolved reference: bar"
    ) == [("/home/ci/app/src/main/kotlin/Foo.kt", 12)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('File "a/b.py", line 3, in f', [("a/b.py", 3)]),
        ("src/a.ts(12,3): error TS2304", [("src/a.ts", 12)]),
        ("src/b.ts:4:2 - error", [("src/b.ts", 4)]),
        ("\tat com.x.Foo.bar(Foo.java:9)", [("Foo.java", 9)]),
        ("see https://example.invalid/a.js:10", []),
    ],
)
def test_existing_forms_unchanged(text, expected) -> None:
    assert extract_source_paths(text) == expected


def test_is_exit_code_line() -> None:
    for line in (
        "Process completed with exit code 1.",
        "##[error]Process completed with exit code 2.",
        "Error: Process completed with exit code 137",
    ):
        assert is_exit_code_line(line), line
    assert not is_exit_code_line("AssertionError: exit code was 1")
    assert not is_exit_code_line("")


# ---- AC2: java golden ----------------------------------------------------------------


def test_java_golden_names_the_file_and_stays_compile() -> None:
    summary, verdict = _diagnosed("java-compile-in-pipeline")
    assert verdict.category == "compile"
    assert "/work/src/test/TestConfig.java" in verdict.suspected_files
    assert "/work/src/test/TestConfig.java" in verdict.one_liner
    assert summary.diagnosis.suspected_files == verdict.suspected_files


# ---- AC3: docker golden -----------------------------------------------------------------


def test_docker_golden_evidence_and_headline() -> None:
    summary, verdict = _diagnosed("docker-already-exists-benign")
    evidence = evidence_lines(summary, None)
    assert evidence and "AssertionError" in evidence[0]
    assert not any(is_exit_code_line(line) for line in evidence)
    assert verdict.one_liner == "A test assertion failed — AssertionError: response body mismatch"
    body = _body("docker-already-exists-benign")
    assert "**A test assertion failed — AssertionError: response body mismatch**" in body
    assert "exit code" not in body


# ---- AC4: artifactory headline pinned -------------------------------------------------------


def test_artifactory_headline_byte_identical() -> None:
    _summary_, verdict = _diagnosed("artifactory-version-exists")
    assert verdict.one_liner == (
        "This release already exists on Artifactory. You need to update package.json"
    )


# ---- AC5 + constraints: every golden -------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(_GOLDENS))
def test_goldens_render_clean(slug: str) -> None:
    body = _body(slug)
    assert len(body) <= MAX_BODY_CHARS
    assert not _RULE_ID.search(body.split("\n", 1)[1])
    summary, _ = _diagnosed(slug)
    assert not any(is_exit_code_line(line) for line in evidence_lines(summary, None))


@pytest.mark.parametrize("slug", sorted(_LOCKED))
def test_verdicts_and_gate_unchanged(slug: str) -> None:
    summary, verdict = _diagnosed(slug)
    gate = decide_stgpt_call(summary, stgpt_key_present=True)
    assert (
        verdict.rule_id,
        verdict.category,
        verdict.confidence,
        verdict.requires_analysis,
        gate,
    ) == _LOCKED[slug]


# ---- F3 unit behaviour ----------------------------------------------------------------------


def _verdict(category: str) -> DeterministicVerdict:
    return DeterministicVerdict(
        category=category,
        confidence="high",
        is_infra_vs_code="code",
        requires_analysis=False,
        rule_id="R9",
        one_liner="",
    )


def _with_primary(line: str | None, text: str = "boom"):
    summary = _summary(job_text=text)
    summary.failed_jobs[0].primary_failure_line = line
    return summary


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("##[error]Error: widget is not defined", "widget is not defined"),
        ("[ERROR] Failed to execute goal x", "Failed to execute goal x"),
        ("ERROR: cannot open thing", "cannot open thing"),
        ("E       AssertionError: 1 != 2", "AssertionError: 1 != 2"),
        ("Elephant in the room", "Elephant in the room"),
    ],
)
def test_prefix_strip_table(raw, cleaned) -> None:
    assert headline_cause_line(_with_primary(raw)) == cleaned


@pytest.mark.parametrize(
    ("category", "sentence"),
    [
        ("compile", "Compilation failed"),
        ("test_failure", "A test assertion failed"),
        ("dependency", "Package install failed: a package was not found (or could not be resolved)"),
    ],
)
def test_generic_branches_append_the_cause(category, sentence) -> None:
    summary = _with_primary("E   something specific broke")
    card = user_facing(_verdict(category), summary)
    assert card.root_cause == f"{sentence} — something specific broke"


def test_branches_with_detail_are_unchanged() -> None:
    summary = _with_primary("E   AssertionError: nope")
    summary.junit = JUnitReport(
        total_failures=1, failures=[JUnitFailure(classname="tests.t", name="test_x")]
    )
    assert user_facing(_verdict("test_failure"), summary).root_cause == "Test failed: tests.t::test_x."
    compile_v = _verdict("compile")
    compile_v.suspected_files = ["src/a.ts"]
    assert user_facing(compile_v, summary).root_cause == "Compilation failed in src/a.ts."


@pytest.mark.parametrize(
    "line",
    [
        None,
        "",
        "##[error]Process completed with exit code 1.",
        "a1b2c3d4e5f6 Already exists 0B",
        "rule R8 says hello",
    ],
    ids=["none", "empty", "exit-code", "benign", "rule-id"],
)
def test_unusable_cause_lines_leave_the_generic_sentence(line) -> None:
    summary = _with_primary(line)
    assert user_facing(_verdict("compile"), summary).root_cause == "Compilation failed."


def test_cause_already_in_sentence_is_not_repeated() -> None:
    summary = _with_primary("compilation failed")
    assert user_facing(_verdict("compile"), summary).root_cause == "Compilation failed."


def test_headline_is_capped() -> None:
    summary = _with_primary("E   " + "x" * 600)
    root = user_facing(_verdict("compile"), summary).root_cause
    assert len(root) <= 300 and root.endswith("…")
    assert root.startswith("Compilation failed — xxx")
