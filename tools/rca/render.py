"""Render summary.md per spec §9. Omit empty sections. Tolerate missing drain/changes."""

from __future__ import annotations

from urllib.parse import urlparse

from .models import FailedJob, LogWindow, Summary

SECTION_ORDER = (
    "Verdict",
    "Failed Jobs",
    "Annotations",
    "Heuristic Classification",
    "First Error Window",
    "Tail Window",
    "Stack Traces",
    "Log Templates",
    "Artifacts",
    "Failed Tests",
    "Changes Since Last Green",
    "History",
    "Collection Notes",
)


def repository_from_html_url(html_url: str) -> str:
    path = urlparse(html_url).path.strip("/").split("/")
    if len(path) >= 2 and path[0] and path[1]:
        return f"{path[0]}/{path[1]}"
    return ""


def render_markdown(summary: Summary, *, repository: str | None = None) -> str:
    repo = repository or repository_from_html_url(summary.run.html_url)
    parts: list[str] = [_header(summary, repo), _verdict(summary)]
    failed = _failed_jobs(summary)
    if failed:
        parts.append(failed)
    annotations = _annotations(summary)
    if annotations:
        parts.append(annotations)
    parts.append(_classification(summary))
    for window_block in _windows(summary):
        parts.append(window_block)
    stacks = _stack_traces(summary)
    if stacks:
        parts.append(stacks)
    templates = _log_templates(summary)
    if templates:
        parts.append(templates)
    artifacts = _artifacts(summary)
    if artifacts:
        parts.append(artifacts)
    tests = _failed_tests(summary)
    if tests:
        parts.append(tests)
    changes = _changes(summary)
    if changes:
        parts.append(changes)
    history = _history(summary)
    if history:
        parts.append(history)
    notes = _notes(summary)
    if notes:
        parts.append(notes)
    return "\n\n".join(p for p in parts if p).rstrip() + "\n"


def _header(summary: Summary, repo: str) -> str:
    run = summary.run
    sha = run.head_sha[:7] if run.head_sha else ""
    collected = summary.collected_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_link = f"[#{run.run_id}]({run.html_url})" if run.html_url else f"#{run.run_id}"
    repo_bit = f"**Repository:** {repo} · " if repo else ""
    return "\n".join(
        [
            "# CI Failure Report",
            "",
            f"{repo_bit}**Workflow:** {run.workflow_name} · **Run:** {run_link} "
            f"(attempt {run.run_attempt})",
            f"**Branch:** `{run.head_branch}` · **Commit:** `{sha}` · "
            f"**Trigger:** {run.event} by @{run.actor}",
            f"**Collected:** {collected} · **Collector:** {summary.collector_version}",
        ]
    )


def _verdict(summary: Summary) -> str:
    c = summary.classification
    v = summary.verdict
    short = v.short_circuit or "no"
    flaky = "yes" if c.is_flaky else "no"
    fp = summary.fingerprint or "—"
    rows = [
        "| Field | Value |",
        "|---|---|",
        f"| Category | `{c.category}` (confidence: {c.confidence}) |",
        f"| Infra or code | `{c.is_infra_vs_code}` |",
        f"| Flaky | {flaky} |",
        f"| Short-circuited | {short} |",
        f"| Fingerprint | `{fp}` |",
    ]
    hist = summary.history
    if hist is not None:
        rows.append(_seen_before_row(hist))
        if hist.match == "exact" and hist.previous_resolution:
            rows.append(f"| Previously resolved by | {hist.previous_resolution} |")
    return "## Verdict\n\n" + "\n".join(rows)


def _seen_before_row(hist: object) -> str:
    match = getattr(hist, "match", "new")
    count = getattr(hist, "seen_count", 0)
    branches = getattr(hist, "branches_seen", []) or []
    branch_bit = f" (branches: {', '.join(f'`{b}`' for b in branches)})" if branches else ""
    if match == "similar":
        label = f"Similar failure seen {max(count, 1)}× (class match)"
        return f"| Seen before | {label} |"
    if match == "exact" and count:
        return f"| Seen before | Seen {count}×{branch_bit} |"
    return "| Seen before | First occurrence |"


def _failed_jobs(summary: Summary) -> str:
    if not summary.failed_jobs:
        return ""
    lines = [
        "## Failed Jobs",
        "",
        "| Job | Failed step | Exit code | Duration | Queued | Runner |",
        "|---|---|---|---|---|---|",
    ]
    for job in summary.failed_jobs:
        runner = _runner_label(job)
        lines.append(
            f"| `{job.name}` | {job.failed_step_name or ''} | "
            f"{job.exit_code if job.exit_code is not None else ''} | "
            f"{_fmt_dur(job.duration_seconds)} | {_fmt_dur(job.queue_seconds)} | {runner} |"
        )
        extra = _runner_facts(job)
        if extra:
            lines += ["", extra]
        if job.steps:
            lines += [
                "",
                f"### Steps — `{job.name}`",
                "",
                "| # | Step | Result | Time |",
                "|---|---|---|---|",
            ]
            for step in job.steps:
                mark = _step_mark(step.conclusion)
                flag = " ⚠ likely miss" if step.suspected_cache_miss else ""
                lines.append(
                    f"| {step.number} | {step.name} | {mark}{flag} | {_fmt_dur(step.duration_seconds)} |"
                )
            lines += [
                "",
                f"Log volume: {job.log_lines_clean} lines "
                f"({job.log_lines_raw} raw), {job.log_bytes} bytes.",
            ]
    return "\n".join(lines)


def _runner_label(job: FailedJob) -> str:
    if not job.runner:
        return ""
    bits = [job.runner.name or "", job.runner.group or ""]
    joined = " / ".join(b for b in bits if b)
    return f"`{joined}`" if joined else ""


def _runner_facts(job: FailedJob) -> str:
    if not job.runner:
        return ""
    bits = []
    if job.runner.image:
        bits.append(f"Runner image `{job.runner.image}`")
    if job.runner.os:
        bits.append(job.runner.os)
    if job.runner.disk_free_at_start:
        bits.append(f"{job.runner.disk_free_at_start} free at start")
    return " · ".join(bits)


def _step_mark(conclusion: str) -> str:
    if conclusion == "success":
        return "✓"
    if conclusion == "failure":
        return "✗"
    if conclusion == "skipped":
        return "– skipped"
    return conclusion


def _fmt_dur(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if rem == 0:
        return f"{minutes}m"
    return f"{minutes}m {rem:02d}s"


def _annotations(summary: Summary) -> str:
    items: list[str] = []
    for job in summary.failed_jobs:
        items.extend(job.annotations)
    if not items:
        return ""
    lines = ["## Annotations", ""]
    lines.extend(f"- `{item}`" if not item.startswith("`") else f"- {item}" for item in items)
    return "\n".join(lines)


def _classification(summary: Summary) -> str:
    c = summary.classification
    job_name = summary.failed_jobs[0].name if summary.failed_jobs else "unknown"
    pattern = c.matched_pattern or ""
    line = c.matched_line
    loc = f" at line {line} of `{job_name}`" if line is not None else ""
    text = (
        f"## Heuristic Classification\n\n"
        f"Matched rule `{c.category}` on pattern `{pattern}`{loc}."
    )
    if c.other_matches:
        text += f"\nOther rules matched: {', '.join(f'`{m}`' for m in c.other_matches)}."
    return text


def _windows(summary: Summary) -> list[str]:
    blocks: list[str] = []
    for job in summary.failed_jobs:
        for window in job.windows:
            blocks.append(_window_block(job, window))
    return blocks


def _window_block(job: FailedJob, window: LogWindow) -> str:
    step = job.failed_step_name or ""
    where = f"`{job.name}`"
    if step:
        where += f" / `{step}`"
    if window.label == "tail":
        title = f"## Tail Window — {where}"
        intro = (
            f"Last {window.end_line - window.start_line + 1} lines of the failed step. "
            "This is usually the symptom, not the cause."
        )
    elif window.label == "merged":
        title = f"## First Error Window — {where}"
        intro = (
            f"Lines {window.start_line}–{window.end_line} of {window.total_lines} "
            "(merged with tail). Earliest error, usually closest to the cause."
        )
    else:
        title = f"## First Error Window — {where}"
        intro = (
            f"Lines {window.start_line}–{window.end_line} of {window.total_lines}. "
            "This is the earliest error in the log and is usually closest to the cause."
        )
    return f"{title}\n\n{intro}\n\n```text\n{window.content}\n```"


def _stack_traces(summary: Summary) -> str:
    traces = [t for job in summary.failed_jobs for t in job.stack_traces]
    if not traces:
        return ""
    lines = ["## Stack Traces"]
    for i, trace in enumerate(traces, start=1):
        head = trace.headline or f"Trace {i}"
        lines += ["", f"### Trace {i} — `{head}`", "", "```text", trace.content, "```"]
    return "\n".join(lines)


def _log_templates(summary: Summary) -> str:
    drain = summary.drain
    if drain is None or not drain.templates:
        return ""
    intro = (
        f"Clustered by Drain3. Baseline: {drain.baseline_template_count or 0} templates."
        if drain.baseline_available
        else "No Drain3 baseline (novelty is unknown; T1/T2 not assigned)."
    )
    lines = [
        "## Log Templates",
        "",
        intro,
        "",
        "| # | New | Count | Base | Line | Template |",
        "|---|-----|-------|------|------|----------|",
    ]
    for tmpl in drain.templates:
        if tmpl.is_novel is True:
            novel = "●"
        elif tmpl.is_novel is False:
            novel = ""
        else:
            novel = "—"
        base = "" if tmpl.baseline_count is None else str(tmpl.baseline_count)
        lines.append(
            f"| {tmpl.template_id} | {novel} | {tmpl.count} | {base} | "
            f"{tmpl.first_line} | `{tmpl.template}` |"
        )
    var_lines: list[str] = []
    for tmpl in drain.templates:
        if tmpl.tier in {"T1", "T2", "T3"} and tmpl.variables:
            bits = []
            for var in tmpl.variables:
                if var.kind == "numeric":
                    bits.append(
                        f"{var.mask}=[{var.minimum}..{var.maximum}], p50 {var.median}"
                    )
                elif var.kind == "sequence" and var.sequence_rle:
                    bits.append(f"{var.mask}={var.sequence_rle}")
                elif var.values:
                    bits.append(f"{var.mask}=" + ", ".join(var.values))
            if bits:
                var_lines.append(f"- **#{tmpl.template_id}** — " + "; ".join(bits))
    if var_lines:
        lines += ["", "**Variables**", ""]
        lines.extend(var_lines)
    for tmpl in drain.templates:
        if tmpl.anomaly:
            ratio = f" ({tmpl.anomaly_ratio:.0%})" if tmpl.anomaly_ratio is not None else ""
            lines += [
                "",
                f"⚠ **{tmpl.anomaly.capitalize()}** — template #{tmpl.template_id}{ratio}.",
            ]
    if drain.omitted_count:
        extra = f" {drain.omitted_criteria}" if drain.omitted_criteria else ""
        lines += ["", f"{drain.omitted_count} further templates omitted.{extra}"]
    reps = [
        t
        for t in drain.templates
        if t.tier in {"T1", "T2"} and t.representative_line
    ]
    if reps:
        lines += ["", "### Representative lines", ""]
        for tmpl in reps:
            lines.append(
                f"- **#{tmpl.template_id}** (L{tmpl.first_line}) `{tmpl.representative_line}`"
            )
    return "\n".join(lines)


def _artifacts(summary: Summary) -> str:
    if not summary.artifacts:
        return ""
    lines = [
        "## Artifacts",
        "",
        "| Name | Size | Parsed |",
        "|---|---|---|",
    ]
    for art in summary.artifacts:
        parsed = "✓" if art.parsed else "—"
        lines.append(f"| `{art.name}` | {art.size_bytes} | {parsed} |")
    return "\n".join(lines)


def _failed_tests(summary: Summary) -> str:
    report = summary.junit
    if report is None or not report.failures:
        return ""
    total = report.total_tests
    shown = len(report.failures)
    headline = f"{report.total_failures} tests failed. Showing {shown}."
    if total is not None:
        headline = f"{report.total_failures} of {total} tests failed. Showing {shown}."
    lines = ["## Failed Tests", "", headline]
    for fail in report.failures:
        title = f"{fail.classname} › {fail.name}" if fail.classname else fail.name
        body = fail.body or fail.message or ""
        lines += ["", f"### `{title}`", "```text", body, "```"]
    return "\n".join(lines)


def _changes(summary: Summary) -> str:
    ctx = summary.changes
    if ctx is None:
        return ""
    base = (ctx.base_sha or "?")[:7]
    head = (ctx.head_sha or "")[:7]
    lines = [
        "## Changes Since Last Green",
        "",
        f"Comparing `{base}..{head}` — {ctx.total_commits} commits, "
        f"{ctx.files_changed} files (+{ctx.additions} / −{ctx.deletions}). "
        f"Base is `{ctx.range_basis}`.",
    ]
    if ctx.range_truncated:
        lines += ["", "⚠ Compare range truncated by GitHub (250 commits / 300 files)."]
    hot = [c for c in ctx.classes if c in {"ci_config", "container", "lockfile"}]
    if hot:
        lines += ["", f"⚠ **Pipeline configuration changed in this range** (`{'`, `'.join(hot)}`)."]
    if ctx.commits:
        lines += [
            "",
            "| Commit | Author | Subject | Files |",
            "|---|---|---|---|",
        ]
        for commit in ctx.commits:
            lines.append(
                f"| `{commit.sha}` | {commit.author or ''} | {commit.subject} | "
                f"{commit.files_changed} |"
            )
    if ctx.pr_title:
        labels = ", ".join(ctx.pr_labels) if ctx.pr_labels else ""
        draft = " · draft" if ctx.pr_is_draft else ""
        extra = f" · labels: {labels}" if labels else ""
        lines += ["", f"**PR** — *{ctx.pr_title}*{extra}{draft}"]
    if ctx.diffstat:
        lines += ["", "```text", ctx.diffstat, "```"]
    for name, rows in ctx.lockfile_deltas.items():
        lines += ["", f"### Lockfile changes — `{name}`", "", "```text", *rows, "```"]
    return "\n".join(lines)


def _history(summary: Summary) -> str:
    hist = summary.history
    if hist is None:
        return ""
    lines = ["## History"]
    if hist.last_success_sha:
        age = (
            f", {hist.last_success_age_hours:.0f} hours ago"
            if hist.last_success_age_hours is not None
            else ""
        )
        ident = f" run #{hist.last_success_run_id}" if hist.last_success_run_id else ""
        lines.append(f"- Last success on this branch:{ident}, commit `{hist.last_success_sha}`{age}.")
    if hist.blame_range:
        lines.append(f"- Blame range: `{hist.blame_range}`.")
    if hist.first_failing_sha:
        lines.append(f"- **First failing commit:** `{hist.first_failing_sha}`.")
    if hist.recent_outcomes:
        lines.append(f"- Recent outcomes: `{hist.recent_outcomes}`")
    if hist.same_sha_runs:
        if len(hist.same_sha_runs) <= 1:
            lines.append("- Same SHA has not been run before.")
        else:
            lines.append(f"- Same SHA runs: {', '.join(f'#{i}' for i in hist.same_sha_runs)}.")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _notes(summary: Summary) -> str:
    if not summary.collection_notes:
        return ""
    lines = ["## Collection Notes", ""]
    lines.extend(f"- {note}" for note in summary.collection_notes)
    return "\n".join(lines)
