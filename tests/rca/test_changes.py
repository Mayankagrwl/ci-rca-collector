"""Compare range, path classes, revert flag, lockfile deltas."""

from __future__ import annotations

from datetime import datetime, timezone

from tools.rca.changes import (
    change_context_from_compare,
    classify_path,
    is_revert,
    lockfile_delta_from_patch,
    resolve_compare_base,
)
from tools.rca.models import (
    BudgetReport,
    ChangeContext,
    Classification,
    CommitInfo,
    FailedJob,
    HistoryContext,
    RunMeta,
    Summary,
    Verdict,
)
from tools.rca.render import render_markdown


def test_range_basis_fallback() -> None:
    assert resolve_compare_base("abc1234", "def5678") == ("abc1234", "last_success")
    assert resolve_compare_base(None, "def5678") == ("def5678", "merge_base")
    assert resolve_compare_base(None, None) == (None, "head_only")

    head_only = change_context_from_compare(
        None,
        head_sha="aabbccddeeff",
        range_basis="head_only",
        base_sha=None,
    )
    assert head_only.range_basis == "head_only"
    assert head_only.commits == []
    assert head_only.head_sha == "aabbccddeeff"


def test_revert_subject() -> None:
    assert is_revert('Revert "Bump typescript to 5.4.2"') is True
    assert is_revert("Bump typescript") is False
    assert is_revert("fix", "This reverts commit abc1234.") is True


def test_ci_config_classification() -> None:
    assert classify_path(".github/workflows/ci.yml") == "ci_config"
    assert classify_path(".github/workflows/rca-collect.yaml") == "ci_config"
    assert classify_path(".github/actions/setup/action.yml") == "ci_config"
    assert classify_path("src/parser/node.ts") == "source"
    assert classify_path("package-lock.json") == "lockfile"


def test_compare_commits_capped_and_truncated() -> None:
    commits = []
    for i in range(12):
        commits.append(
            {
                "sha": f"{i:040x}",
                "commit": {
                    "message": f"Commit {i}\n\nbody",
                    "author": {"name": "dev", "date": "2026-09-08T12:00:00Z"},
                },
                "author": {"login": "dev"},
                "parents": [{"sha": "p"}],
            }
        )
    commits[-1]["commit"]["message"] = 'Revert "Bump typescript to 5.4.2"'
    payload = {
        "total_commits": 260,
        "commits": commits,
        "files": [
            {
                "filename": ".github/workflows/ci.yml",
                "additions": 6,
                "deletions": 2,
                "patch": "@@ -1 +1 @@\n-on: push\n+on: [push, pull_request]\n",
            }
        ],
    }
    ctx = change_context_from_compare(
        payload,
        head_sha="fff",
        range_basis="last_success",
        base_sha="aaa",
    )
    assert ctx.range_truncated is True
    assert ctx.total_commits == 260
    assert len(ctx.commits) == 10
    assert ctx.commits[0].is_revert is True
    assert ctx.commits[0].subject.startswith("Revert ")
    assert "ci_config" in ctx.classes


def test_lockfile_delta_major_first_not_embedded() -> None:
    patch = """\
@@ -10,6 +10,8 @@
-    "typescript": "5.3.3",
+    "typescript": "5.4.2",
-    "left-pad": "1.3.0",
+    "node-gyp": "10.1.0",
-    "widget": "1.4.0",
+    "widget": "2.0.0",
"""
    rows = lockfile_delta_from_patch("package-lock.json", patch)
    assert any(r.startswith("widget ") and "(major)" in r for r in rows)
    assert rows[0].startswith("widget ")
    assert not any("@@" in r for r in rows)
    joined = "\n".join(rows)
    assert "left-pad" in joined
    assert len(joined) < 500


def test_render_surfaces_ci_config() -> None:
    summary = Summary(
        collector_version="0.1.0",
        collected_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
        run=RunMeta(
            run_id=1,
            run_attempt=1,
            workflow_name="CI",
            html_url="https://example.invalid/acme/widgets/actions/runs/1",
            event="push",
            actor="a",
            head_sha="aabbccdd",
            head_branch="main",
            failed_job_total=1,
            failed_jobs_analysed=1,
        ),
        verdict=Verdict(requires_analysis=True),
        classification=Classification(
            category="unknown",
            confidence="low",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="unknown",
        ),
        failed_jobs=[
            FailedJob(
                job_id=1,
                name="build",
                failed_step_name="x",
                failed_step_number=1,
                exit_code=1,
                duration_seconds=1,
            )
        ],
        changes=ChangeContext(
            base_sha="1111111",
            head_sha="aabbccdd",
            range_basis="last_success",
            total_commits=2,
            files_changed=1,
            classes=["ci_config", "source"],
            commits=[
                CommitInfo(
                    sha="aabbccd",
                    subject='Revert "tmp"',
                    authored_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
                    files_changed=1,
                    is_revert=True,
                )
            ],
        ),
        history=HistoryContext(
            last_success_sha="1111111aaaa",
            last_success_age_hours=4.0,
            recent_outcomes="✓✓✗",
            first_failing_sha="aabbccdd",
        ),
        fingerprint="",
        fingerprint_coarse="",
        kubernetes=None,
        budget_report=BudgetReport(),
    )
    md = render_markdown(summary)
    headings = [ln[3:] for ln in md.splitlines() if ln.startswith("## ")]
    assert "Changes Since Last Green" in headings
    assert "History" in headings
    assert headings.index("Changes Since Last Green") < headings.index("History")
    assert "ci_config" in md
    assert "First failing commit" in md
    assert "github.st.com" not in md
