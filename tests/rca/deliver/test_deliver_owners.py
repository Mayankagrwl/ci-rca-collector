"""Step 17 AC1–AC6 — CODEOWNERS parsing / matching and the v1.3 §9 owner precedence (pure)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.rca.deliver._helpers import make_summary
from tools.rca.deliver import DeliveryContext, DeliveryInputs
from tools.rca.deliver.owners import (
    assignable_logins,
    owners_for,
    parse_codeowners,
    repo_relative_candidates,
    resolve_owner,
    resolve_path_owners,
)
from tools.rca.models import (
    AnalysisRecord,
    AnalysisResult,
    ChangeContext,
    DeterministicDiagnosis,
)

_ROOT = Path(__file__).resolve().parents[3]


def _rules(text: str):
    rules, _notes = parse_codeowners(text)
    return rules


def _owners(path: str, text: str):
    match = owners_for(path, _rules(text))
    return match.owners if match is not None else None


# ---- AC1 / AC2 -------------------------------------------------------------------------


def test_last_match_wins() -> None:  # delivery AC #16
    text = "* @a\n/src/ @b\n*.java @c\n"
    assert _owners("src/X.java", text) == ("@c",)
    assert _owners("src/x.py", text) == ("@b",)
    assert _owners("README.md", text) == ("@a",)
    match = owners_for("README.md", _rules(text))
    assert match.catch_all and match.line_no == 1


def test_explicitly_unowned_rule() -> None:
    text = "/src/ @b\n/src/gen/\n"
    match = owners_for("src/gen/x.py", _rules(text))
    assert match is not None and match.owners == () and not match.catch_all
    assert _owners("src/a.py", text) == ("@b",)


# ---- AC3: pattern semantics + parser ---------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("/build/logs/", "build/logs/a/b.txt", True),  # leading /: anchored
        ("/build/logs/", "x/build/logs/a", False),
        ("docs/api", "docs/api/v1.md", True),  # middle /: anchored
        ("docs/api", "x/docs/api/v1.md", False),
        ("apps/", "deep/apps/y.js", True),  # trailing /: any depth, recursive
        ("apps/", "apps", False),  # ...a directory, not a file named apps
        ("logs", "a/b/logs/x.txt", True),  # slash-less: any depth
        ("*.js", "a/b/c.js", True),
        ("src/*.js", "src/a/b.js", False),  # * does not cross /
        ("src/**/x.py", "src/x.py", True),  # ** crosses /
        ("src/**/x.py", "src/a/b/x.py", True),
        ("/scripts/**", "scripts/a/b", True),
        ("docs/*", "docs/a.md", True),  # dir/*: direct children only
        ("docs/*", "docs/a/b.md", False),
        ("**/logs", "deep/down/logs/x", True),
        ("Src/", "src/a.py", False),  # case-sensitive
    ],
)
def test_pattern_semantics(pattern, path, matches) -> None:
    assert (_owners(path, f"{pattern} @x") is not None) is matches


def test_parser_syntax_and_skips() -> None:
    text = (
        "\ufeff# comment\r\n"
        "\r\n"
        "*.md @docs # inline comment\r\n"
        "\\#weird @hash\r\n"
        "!negated @n\r\n"
        "[ab].txt @r\r\n"
        "*.py not-an-owner\r\n"
        "*.go @org/go-team dev@example.com\r\n"
    )
    rules, notes = parse_codeowners(text)
    assert [(r.pattern, r.owners) for r in rules] == [
        ("*.md", ("@docs",)),
        ("#weird", ("@hash",)),
        ("*.go", ("@org/go-team", "dev@example.com")),
    ]
    assert any("'!negated'" in n for n in notes)
    assert any("'[ab].txt'" in n for n in notes)
    assert any("not-an-owner" in n for n in notes)
    assert _owners("#weird", text) == ("@hash",)


def test_empty_and_none_codeowners() -> None:
    assert parse_codeowners("") == ([], [])
    assert parse_codeowners(None) == ([], [])


# ---- AC4: runner paths --------------------------------------------------------------------------


def test_repo_relative_candidates() -> None:
    assert repo_relative_candidates("/work/src/test/A.java", workspace=None) == [
        "work/src/test/A.java", "src/test/A.java", "test/A.java", "A.java"]
    assert repo_relative_candidates("/work/src/test/A.java", workspace="/work/") == ["src/test/A.java"]
    assert repo_relative_candidates("file:///w/x/A.kt", workspace=None) == ["w/x/A.kt", "x/A.kt", "A.kt"]
    assert repo_relative_candidates("src/a.py", workspace=None) == ["src/a.py"]
    assert repo_relative_candidates("./src/a.py", workspace=None) == ["src/a.py"]
    assert repo_relative_candidates("C:/w/src/a.py", workspace=None)[0] == "w/src/a.py"
    assert repo_relative_candidates(r"D:\w\src\a.py", workspace="D:/w") == ["src/a.py"]


def test_absolute_runner_path_resolution() -> None:
    rules = _rules("* @everyone\n/src/ @b\n")
    path = "/work/src/test/TestConfig.java"
    assert resolve_path_owners(path, rules, workspace=None).owners == ("@b",)
    via_ws = resolve_path_owners(path, rules, workspace="/work")
    assert via_ws.owners == ("@b",) and via_ws.path == "src/test/TestConfig.java"
    known = resolve_path_owners(path, rules, workspace=None, known_files=["src/test/TestConfig.java"])
    assert known.path == "src/test/TestConfig.java"


def test_unanchored_pattern_uses_longest_candidate_and_catch_all_is_last() -> None:
    assert resolve_path_owners("/w/src/A.java", _rules("*.java @c"), workspace=None).path == "w/src/A.java"
    only_catch_all = resolve_path_owners("/w/src/A.java", _rules("* @all"), workspace=None)
    assert only_catch_all.owners == ("@all",) and only_catch_all.catch_all


# ---- AC5 / AC6: resolver precedence ------------------------------------------------------------------


def _ctx(*, trigger="push_default", actor="octocat") -> DeliveryContext:
    return DeliveryContext(
        trigger=trigger, branch="main", default_branch="main", is_default_branch=True,
        actor=actor, commit_sha="f" * 40,
    )


def _summary(*, infra=False, short_circuit=None, diag_files=(), changed=()):
    summary = make_summary(short_circuit=short_circuit)
    summary.classification.is_infra_vs_code = "infra" if infra else "code"
    summary.diagnosis = DeterministicDiagnosis(
        rule_id="R9", one_liner="x", suspected_files=list(diag_files)
    )
    summary.changes = ChangeContext(head_sha="f" * 40, files=list(changed)) if changed else None
    return summary


def _analysis(files, status="ok"):
    return AnalysisRecord(
        status=status,
        result=AnalysisResult(root_cause="r", suggested_fix="f", confidence="high",
                              suspected_files=list(files), source="ai"),
    )


RULES = _rules("* @catchall\n/ai/ @ai-owner\n/diag/ @diag-owner\n/changed/ @org/changers\n")


def _resolve(summary, analysis=None, *, ctx=None, rules=RULES, **inputs):
    return resolve_owner(summary, analysis, ctx or _ctx(), DeliveryInputs(**inputs), rules, workspace=None)


@pytest.mark.parametrize("infra_kwargs", [{"infra": True}, {"short_circuit": "infra_runner"},
                                          {"short_circuit": "infra_widespread"}])
def test_infra_goes_to_platform_team_only(infra_kwargs) -> None:  # delivery AC #15
    summary = _summary(diag_files=["diag/a.py"], changed=["changed/b.py"], **infra_kwargs)
    got = _resolve(summary, platform_team="@org/platform")
    assert (got.owners, got.resolved_by) == (["@org/platform"], "platform_team")
    no_team = _resolve(summary, default_notify="@lead")
    assert (no_team.owners, no_team.resolved_by) == (["@lead"], "default_notify")
    nothing = _resolve(summary)
    assert (nothing.owners, nothing.resolved_by) == ([], "none")  # never the actor


def test_platform_team_splits_on_commas_and_spaces() -> None:
    got = _resolve(_summary(infra=True), platform_team="@org/a, @org/b  @c")
    assert got.owners == ["@org/a", "@org/b", "@c"]


def test_precedence_chain() -> None:
    full = _summary(diag_files=["diag/a.py"], changed=["changed/b.py"])
    # Step 2 unions trusted analysis files, then diagnosis files, in that order.
    assert _resolve(full, _analysis(["ai/x.py"])).owners == ["@ai-owner", "@diag-owner"]
    assert _resolve(full, _analysis(["ai/x.py"])).resolved_by == "codeowners_suspected"
    for untrusted in ("unvalidated", "failed"):
        got = _resolve(full, _analysis(["ai/x.py"], status=untrusted))
        assert got.owners == ["@diag-owner"], untrusted
    changes = _resolve(_summary(changed=["changed/b.py"]))
    assert (changes.owners, changes.resolved_by) == (["@org/changers"], "codeowners_changes")
    assert any("no change counts" in n for n in changes.notes)
    unowned_rules = _rules("/other/ @x\n")
    actor = _resolve(_summary(diag_files=["diag/a.py"]), rules=unowned_rules)
    assert (actor.owners, actor.resolved_by) == (["@octocat"], "actor")
    default = _resolve(_summary(), ctx=_ctx(actor="github-actions[bot]"), rules=unowned_rules,
                       default_notify="@lead")
    assert (default.owners, default.resolved_by) == (["@lead"], "default_notify")
    cron = _resolve(_summary(), ctx=_ctx(trigger="schedule"), rules=unowned_rules)
    assert (cron.owners, cron.resolved_by) == ([], "none")


def test_catch_all_counts_for_suspected_files() -> None:
    got = _resolve(_summary(diag_files=["elsewhere/z.py"]))
    assert (got.owners, got.resolved_by) == (["@catchall"], "codeowners_suspected")


def test_explicitly_unowned_file_contributes_nothing_and_moves_on() -> None:
    rules = _rules("/src/ @b\n/src/gen/\n")
    got = _resolve(_summary(diag_files=["src/gen/x.py", "src/real.py"]), rules=rules)
    assert got.owners == ["@b"]
    only_gen = _resolve(_summary(diag_files=["src/gen/x.py"]), rules=rules)
    assert only_gen.resolved_by == "actor"


def test_union_dedupe_and_caps() -> None:
    rules = _rules("\n".join(f"/d{i}/ @u{i} @shared" for i in range(15)))
    files = [f"d{i}/f.py" for i in range(15)]
    got = _resolve(_summary(diag_files=files), rules=rules)
    assert got.owners[:3] == ["@u0", "@shared", "@u1"]
    assert len(got.owners) == 10


def test_no_codeowners_skips_file_steps() -> None:
    got = _resolve(_summary(diag_files=["diag/a.py"]), rules=None)
    assert got.resolved_by == "actor"
    assert any("CODEOWNERS unavailable" in n for n in got.notes)


def test_assignable_logins() -> None:
    owners = ["@alice", "@org/team", "dev@example.com", "@bob", "@alice"]
    assert assignable_logins(owners) == ["alice", "bob"]
    assert len(assignable_logins([f"@u{i}" for i in range(20)])) == 10


def test_owners_module_is_import_pure() -> None:
    code = (
        "import sys\nimport tools.rca.deliver.owners\n"
        "print(','.join(m for m in sys.modules if m == 'httpx' or m.endswith('github_api')))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
    source = (_ROOT / "tools" / "rca" / "deliver" / "owners.py").read_text(encoding="utf-8")
    assert "open(" not in source and "github_api" not in source
