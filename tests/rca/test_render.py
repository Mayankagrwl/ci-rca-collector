"""summary.md section order and omitted-empty-sections."""

from __future__ import annotations

from datetime import datetime, timezone

from tools.rca.models import (
    BudgetReport,
    Classification,
    FailedJob,
    LogWindow,
    RunMeta,
    StepInfo,
    Summary,
    Verdict,
)
from tools.rca.render import SECTION_ORDER, render_markdown


def _summary(**kwargs: object) -> Summary:
    defaults: dict[str, object] = dict(
        collector_version="0.1.0",
        collected_at=datetime(2026, 9, 8, 14, 22, 10, tzinfo=timezone.utc),
        run=RunMeta(
            run_id=4821,
            run_attempt=1,
            workflow_name="CI",
            html_url="https://example.invalid/acme/widgets/actions/runs/4821",
            event="pull_request",
            actor="dsharma",
            head_sha="a3f9c21deadbeef",
            head_branch="feature/parser-rewrite",
            failed_job_total=1,
            failed_jobs_analysed=1,
        ),
        verdict=Verdict(requires_analysis=True),
        classification=Classification(
            category="dependency",
            confidence="high",
            matched_pattern="ERESOLVE",
            matched_line=2,
            is_infra_vs_code="code",
        ),
        failed_jobs=[
            FailedJob(
                job_id=77,
                name="build (node-20)",
                failed_step_name="Install dependencies",
                failed_step_number=3,
                exit_code=1,
                duration_seconds=134,
                queue_seconds=8,
                steps=[
                    StepInfo(number=1, name="Set up job", conclusion="success", duration_seconds=3),
                    StepInfo(
                        number=3,
                        name="Install dependencies",
                        conclusion="failure",
                        duration_seconds=128,
                    ),
                ],
                windows=[
                    LogWindow(
                        label="first_error",
                        start_line=10,
                        end_line=20,
                        total_lines=40,
                        content="npm ERR! ERESOLVE could not resolve",
                    )
                ],
                annotations=["##[error] Process completed with exit code 1"],
            )
        ],
        drain=None,
        changes=None,
        history=None,
        junit=None,
        artifacts=[],
        fingerprint="",
        fingerprint_coarse="",
        kubernetes=None,
        budget_report=BudgetReport(),
        collection_notes=["Drain3, history, and changes are not wired yet."],
    )
    defaults.update(kwargs)
    return Summary(**defaults)  # type: ignore[arg-type]


def _h2(md: str) -> list[str]:
    return [line[3:] for line in md.splitlines() if line.startswith("## ")]


def test_section_order_and_omitted_empty() -> None:
    md = render_markdown(_summary())
    headings = _h2(md)
    assert headings == [
        "Verdict",
        "Failed Jobs",
        "Annotations",
        "Heuristic Classification",
        "First Error Window — `build (node-20)` / `Install dependencies`",
        "Collection Notes",
    ]
    present = [h.split(" — ")[0] for h in headings]
    allowed = list(SECTION_ORDER)
    assert [h for h in present if h in allowed] == present
    assert allowed.index("Verdict") < allowed.index("Failed Jobs")
    assert allowed.index("Failed Jobs") < allowed.index("Annotations")
    assert allowed.index("Annotations") < allowed.index("Heuristic Classification")
    assert "Log Templates" not in md
    assert "Changes Since Last Green" not in md
    assert "## History" not in md
    assert "## Artifacts" not in md
    assert "## Failed Tests" not in md
    assert "## Tail Window" not in md
    assert "## Stack Traces" not in md


def test_omits_failed_jobs_when_empty() -> None:
    md = render_markdown(
        _summary(
            failed_jobs=[],
            verdict=Verdict(
                short_circuit="no_failed_jobs",
                requires_analysis=False,
                reason="no failed jobs",
            ),
        )
    )
    assert "## Failed Jobs" not in md
    assert "## Verdict" in md
    assert "no_failed_jobs" in md


def test_header_uses_html_url_path_not_hardcoded_host() -> None:
    md = render_markdown(_summary())
    assert "acme/widgets" in md
    assert "github.st.com" not in md
