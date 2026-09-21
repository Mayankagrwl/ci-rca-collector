"""Code hunks: skip lockfile-only, clip around frame, note 404."""

from __future__ import annotations

from datetime import datetime, timezone

from tools.rca.code_context import clip_hunk, fetch_code_context
from tools.rca.diagnose import DeterministicVerdict, diagnose
from tools.rca.models import (
    BudgetReport,
    ChangeContext,
    Classification,
    CommitInfo,
    FailedJob,
    HistoryContext,
    LogWindow,
    RunMeta,
    StackTrace,
    Summary,
    Verdict,
)
from tools.rca.prompt import build_evidence
from tools.rca.render import render_markdown


def _verdict(**kwargs: object) -> DeterministicVerdict:
    payload = dict(
        category="compile",
        confidence="high",
        is_infra_vs_code="code",
        requires_analysis=True,
        rule_id="R18",
        one_liner="need a hunk",
        suspected_files=["src/foo.ts"],
    )
    payload.update(kwargs)
    return DeterministicVerdict(**payload)  # type: ignore[arg-type]


def _summary(**kwargs: object) -> Summary:
    defaults: dict[str, object] = dict(
        collector_version="0.1.0",
        collected_at=datetime.now(timezone.utc),
        run=RunMeta(
            run_id=1,
            run_attempt=1,
            workflow_name="CI",
            html_url="",
            event="push",
            actor="a",
            head_sha="deadbeef",
            head_branch="main",
            failed_job_total=1,
            failed_jobs_analysed=1,
        ),
        verdict=Verdict(requires_analysis=True),
        classification=Classification(
            category="compile",
            confidence="high",
            matched_pattern=None,
            matched_line=None,
            is_infra_vs_code="code",
        ),
        failed_jobs=[
            FailedJob(
                job_id=1,
                name="build",
                failed_step_name="Build",
                failed_step_number=1,
                exit_code=1,
                duration_seconds=10,
                windows=[
                    LogWindow(
                        label="first_error",
                        start_line=1,
                        end_line=2,
                        total_lines=2,
                        content='File "src/foo.ts", line 30\nerror TS2345',
                    )
                ],
                stack_traces=[
                    StackTrace(
                        headline="error TS2345",
                        content='File "src/foo.ts", line 30',
                        frame_count=1,
                    )
                ],
            )
        ],
        fingerprint="",
        fingerprint_coarse="",
        budget_report=BudgetReport(),
    )
    defaults.update(kwargs)
    return Summary(**defaults)  # type: ignore[arg-type]


def test_no_fetch_on_lockfile_only() -> None:
    summary = _summary(
        changes=ChangeContext(
            head_sha="deadbeef",
            range_basis="last_success",
            classes=["lockfile"],
            files=["package-lock.json"],
        )
    )
    calls: list[tuple[str, str, str]] = []

    def get_file(repo: str, path: str, ref: str) -> str | None:
        calls.append((repo, path, ref))
        return "should not fetch"

    ctx = fetch_code_context(
        summary,
        _verdict(category="dependency", suspected_files=["package-lock.json"]),
        get_file=get_file,
        repo="acme/widgets",
        ref="deadbeef",
    )
    assert calls == []
    assert ctx.skipped_reason
    assert "lockfile" in ctx.skipped_reason or ctx.skipped_reason == "skipped_blast_radius"
    assert ctx.hunks == []


def test_hunk_clipped_around_frame() -> None:
    lines = [f"line {i}" for i in range(1, 201)]
    source = "\n".join(lines)

    def get_file(repo: str, path: str, ref: str) -> str:
        assert path.endswith("foo.ts")
        return source

    summary = _summary(
        changes=ChangeContext(
            head_sha="deadbeef",
            range_basis="last_success",
            classes=["source"],
            files=["src/foo.ts"],
        )
    )
    ctx = fetch_code_context(
        summary,
        _verdict(),
        get_file=get_file,
        repo="acme/widgets",
        ref="deadbeef",
    )
    assert ctx.hunks
    hunk = ctx.hunks[0]
    assert hunk.start_line >= 1
    assert hunk.end_line - hunk.start_line + 1 <= 80
    assert "line 30" in hunk.content
    assert hunk.start_line > 1
    assert hunk.end_line < 200
    assert hunk.content.splitlines()[0] != "line 1"
    assert hunk.content.splitlines()[-1] != "line 200"


def test_clip_hunk_direct() -> None:
    text = "\n".join(f"L{i}" for i in range(1, 101))
    content, start, end = clip_hunk(text, center_line=50, radius=25, max_lines=80)
    assert start == 25
    assert end == 75
    assert content.splitlines()[0] == "L25"
    assert end - start + 1 <= 80


def test_404_noted() -> None:
    summary = _summary(
        changes=ChangeContext(
            head_sha="deadbeef",
            range_basis="last_success",
            classes=["source"],
            files=["src/foo.ts"],
        )
    )

    def get_file(repo: str, path: str, ref: str) -> str | None:
        return None

    ctx = fetch_code_context(
        summary,
        _verdict(),
        get_file=get_file,
        repo="acme/widgets",
        ref="deadbeef",
    )
    assert ctx.hunks
    assert ctx.hunks[0].note == "404/403"
    assert any("404" in note for note in ctx.notes)
    assert ctx.hunks[0].content == ""


def test_skip_fetch_for_oom_category() -> None:
    calls: list[str] = []

    def get_file(repo: str, path: str, ref: str) -> str:
        calls.append(path)
        return "x"

    summary = _summary(
        failed_jobs=[
            FailedJob(
                job_id=1,
                name="build",
                failed_step_name="Build",
                failed_step_number=1,
                exit_code=137,
                duration_seconds=10,
                windows=[
                    LogWindow(
                        label="first_error",
                        start_line=1,
                        end_line=1,
                        total_lines=1,
                        content="JavaScript heap out of memory",
                    )
                ],
            )
        ]
    )
    ctx = fetch_code_context(
        summary,
        _verdict(category="oom", is_infra_vs_code="code"),
        get_file=get_file,
        repo="acme/widgets",
        ref="deadbeef",
    )
    assert calls == []
    assert ctx.skipped_reason


def _commit(sha: str, files: list[str]) -> CommitInfo:
    return CommitInfo(
        sha=sha[:7],
        subject="change",
        authored_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        files_changed=len(files),
        files=files,
    )


def test_blast_radius_lockfile_and_workflow_skips_get_file() -> None:
    summary = _summary(
        changes=ChangeContext(
            head_sha="deadbeef",
            range_basis="last_success",
            classes=["lockfile", "ci_config"],
            files=["package-lock.json", ".github/workflows/ci.yml"],
        ),
        failed_jobs=[
            FailedJob(
                job_id=1,
                name="build",
                failed_step_name="Install",
                failed_step_number=2,
                exit_code=1,
                duration_seconds=10,
                windows=[
                    LogWindow(
                        label="first_error",
                        start_line=1,
                        end_line=2,
                        total_lines=2,
                        content="npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE could not resolve",
                    )
                ],
            )
        ],
    )
    calls: list[tuple[str, str, str]] = []

    def get_file(repo: str, path: str, ref: str) -> str:
        calls.append((repo, path, ref))
        return "should not fetch"

    ctx = fetch_code_context(
        summary,
        _verdict(category="dependency", is_infra_vs_code="code", requires_analysis=True),
        get_file=get_file,
        repo="acme/widgets",
    )
    assert calls == []
    assert ctx.hunks == []
    assert ctx.skipped_reason == "skipped_blast_radius"
    assert any("changes.classes=" in note for note in summary.collection_notes)
    verdict = diagnose(summary)
    assert verdict.requires_analysis is False
    assert "Package install failed" in verdict.one_liner
    assert "R8" not in verdict.one_liner
    from tools.rca.diagnose import apply_verdict

    apply_verdict(summary, verdict)
    summary.code_context = ctx
    evidence = build_evidence(summary)
    assert "### code_context" not in evidence


def test_stack_in_src_and_diff_still_fetches_foo() -> None:
    summary = _summary(
        changes=ChangeContext(
            head_sha="deadbeef",
            range_basis="last_success",
            classes=["lockfile"],
            files=["package-lock.json", "src/foo.ts"],
        ),
        history=HistoryContext(first_failing_sha="c81b40eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    )
    calls: list[tuple[str, str]] = []

    def get_file(repo: str, path: str, ref: str) -> str:
        calls.append((path, ref))
        return "export const x = 1\n"

    ctx = fetch_code_context(
        summary,
        _verdict(),
        get_file=get_file,
        repo="acme/widgets",
    )
    assert calls
    assert all(path.endswith("foo.ts") for path, _ref in calls)
    assert ctx.hunks
    assert ctx.hunks[0].path.endswith("foo.ts")


def test_get_file_uses_first_failing_sha_not_tip() -> None:
    middle = "c81b40e1111111111111111111111111111111"
    tip = "a3f9c21deadbeef00000000000000000000000"
    summary = _summary(
        run=RunMeta(
            run_id=1,
            run_attempt=1,
            workflow_name="CI",
            html_url="",
            event="push",
            actor="a",
            head_sha=tip,
            head_branch="main",
            failed_job_total=1,
            failed_jobs_analysed=1,
        ),
        history=HistoryContext(first_failing_sha=middle),
        changes=ChangeContext(
            head_sha=tip,
            range_basis="last_success",
            classes=["source"],
            files=["README.md", "src/foo.ts", "docs/a.md"],
            commits=[
                _commit(tip, ["README.md"]),
                _commit(middle, ["src/foo.ts"]),
                _commit("bbbbbbb2222222222222222222222222222222", ["docs/a.md"]),
            ],
        ),
    )
    refs: list[str] = []

    def get_file(repo: str, path: str, ref: str) -> str | None:
        refs.append(ref)
        if ref.startswith("c81b40e") and path.endswith("foo.ts"):
            return "\n".join(f"line {i}" for i in range(1, 40))
        return None

    ctx = fetch_code_context(
        summary,
        _verdict(),
        get_file=get_file,
        repo="acme/widgets",
        ref=tip,
    )
    assert refs
    assert all(item.startswith("c81b40e") for item in refs)
    assert not any(item.startswith("a3f9c21") for item in refs)
    assert ctx.ref_sha == middle
    assert ctx.basis == "first_failing"
    md = render_markdown(summary.model_copy(update={"code_context": ctx}))
    assert "Code at first failing commit `c81b40e`" in md
    assert "not PR tip `a3f9c21`" in md


def test_first_failing_sha_none_uses_head() -> None:
    tip = "a3f9c21deadbeef00000000000000000000000"
    summary = _summary(
        run=RunMeta(
            run_id=1,
            run_attempt=1,
            workflow_name="CI",
            html_url="",
            event="push",
            actor="a",
            head_sha=tip,
            head_branch="main",
            failed_job_total=1,
            failed_jobs_analysed=1,
        ),
        history=HistoryContext(first_failing_sha=None),
        changes=ChangeContext(
            head_sha=tip,
            range_basis="last_success",
            classes=["source"],
            files=["src/foo.ts"],
        ),
    )
    refs: list[str] = []

    def get_file(repo: str, path: str, ref: str) -> str:
        refs.append(ref)
        return "const x = 1\n"

    ctx = fetch_code_context(
        summary,
        _verdict(),
        get_file=get_file,
        repo="acme/widgets",
        ref="should-not-win",
    )
    assert refs == [tip]
    assert ctx.ref_sha == tip
    assert ctx.basis == "head"
    assert any("first_failing_sha unknown" in note for note in ctx.notes)
