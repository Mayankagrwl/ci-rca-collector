"""Phase 2 prompt: evidence from summary.json only, wrapped in <EVIDENCE>."""

from __future__ import annotations

from .budget import trim_middle
from .config import PROMPT_VERSION, TOKEN_BUDGET_ANALYZE
from .models import PipelineLogStream, Summary

SYSTEM_PROMPT = (
    f"You are a CI root-cause assistant (prompt {PROMPT_VERSION}). "
    "DETERMINISTIC_HINT is collector input + fallback, not the user-facing answer. "
    "Use DETERMINISTIC_HINT when it matches the first_error_window. "
    "If the log clearly names another cause (Artifactory version already exists, "
    "missing package, compile file:line, ERESOLVE, JUnit failure), "
    "PREFER the log and ignore a generic ci_config / workflow-changed hint. "
    "When short_circuit is infra_runner, infra_widespread, or flake_same_sha_passed, "
    "do not invent an application code bug. "
    "Prefer pipeline_logs over job log when both exist. "
    "Use only the text inside <EVIDENCE>. Do not invent log lines, file paths, or test names. "
    "Quote only from <EVIDENCE>. Reply with ONLY a single JSON object. "
    "No markdown fences, no prose, no commentary. "
    "Keys: root_cause (string), suggested_fix (string), "
    "confidence (high|medium|low), "
    "citations (array of {quote, source, line}), "
    "cannot_determine (bool), suspected_files (array of string), "
    "suspected_stage (string or null), infra_or_code (infra|code|unknown), "
    "used_deterministic_rule (string or null). "
    "Each citations[].quote MUST be a verbatim substring of <EVIDENCE>. "
    "source must be one of: first_error_window, tail_window, stack_traces, "
    "log_templates, junit, change_context, annotations, history, step_table, "
    "pipeline_logs, code_context, last_green_compare, deterministic_rule. "
    "If evidence is insufficient, cannot_determine=true, confidence=low — do not invent files."
)

_REPAIR = (
    "Your previous reply was not valid JSON: {reason}. "
    "Reply with ONLY the JSON object. No markdown fences, no prose, no commentary. "
    "Keys: root_cause, suggested_fix, confidence, citations. "
    "citations[].quote must be copied verbatim from <EVIDENCE>."
)


def build_evidence(summary: Summary, *, cap_tokens: int | None = None) -> str:
    """Render a MINIMAL evidence packet. Analyze cap is 2500 tokens."""
    cap = TOKEN_BUDGET_ANALYZE if cap_tokens is None else cap_tokens
    body = "\n".join(_evidence_lines(summary))
    trimmed, _, _, _ = trim_middle(body, cap)
    return f"<EVIDENCE>\n{trimmed}\n</EVIDENCE>"


def build_messages(
    evidence: str,
    *,
    prior_completion: str | None = None,
    repair_reason: str | None = None,
) -> list[dict[str, str]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{evidence}\n\n"
                "Use DETERMINISTIC_HINT when it matches first_error_window. "
                "If the log names a more specific cause, prefer the log. "
                "JSON only, citations from <EVIDENCE>."
            ),
        },
    ]
    if prior_completion:
        messages.append({"role": "assistant", "content": prior_completion})
        messages.append(
            {
                "role": "user",
                "content": _REPAIR.format(reason=repair_reason or "validation failed"),
            }
        )
    return messages


_INFRA_FLAKE_SHORT = frozenset(
    {"infra_runner", "infra_widespread", "flake_same_sha_passed"}
)


def _deterministic_hint_lines(summary: Summary) -> list[str]:
    """Always-on compact hint. Deterministic output is input + fallback."""
    diag = summary.diagnosis
    verdict = summary.verdict
    cls = summary.classification
    rule = (diag.rule_id if diag is not None else "") or (verdict.reason or "")
    one = (diag.one_liner if diag is not None else "") or ""
    fix = (diag.fix_one_liner if diag is not None else None) or ""
    stage = (diag.suspected_stage if diag is not None else None) or ""
    files = ",".join(list(diag.suspected_files if diag is not None else [])[:8])
    short = verdict.short_circuit or ""
    lines = [
        "DETERMINISTIC_HINT:",
        f"  rule_id: {rule}",
        f"  category: {cls.category}",
        f"  one_liner: {one}",
        f"  suggested_fix: {fix}",
        f"  suspected_stage: {stage}",
        f"  suspected_files: {files}",
        f"  requires_analysis: {str(bool(verdict.requires_analysis)).lower()}",
        f"  short_circuit: {short}",
    ]
    if short in _INFRA_FLAKE_SHORT:
        lines.append(
            "  instruction: High-confidence infrastructure/flake verdict; "
            "do not invent a code bug."
        )
    return lines


def _evidence_lines(summary: Summary) -> list[str]:
    run = summary.run
    verdict = summary.verdict
    cls = summary.classification
    diagnosis = summary.diagnosis
    lines = [
        f"workflow: {run.workflow_name}",
        f"event: {run.event}",
        f"branch: {run.head_branch}",
        f"head_sha: {run.head_sha}",
        f"failed_jobs: {run.failed_jobs_analysed}/{run.failed_job_total}",
        f"classification: {cls.category} ({cls.confidence}) "
        f"infra_vs_code={cls.is_infra_vs_code} flaky={cls.is_flaky}",
    ]
    lines.extend(_deterministic_hint_lines(summary))
    if diagnosis is not None:
        lines.append(f"rule_id: {diagnosis.rule_id}")
        lines.append(f"one_liner: {diagnosis.one_liner}")
        if diagnosis.suspected_stage:
            lines.append(f"suspected_stage: {diagnosis.suspected_stage}")
        if diagnosis.suspected_files:
            lines.append("suspected_files: " + ", ".join(diagnosis.suspected_files[:8]))
        lines.append(f"winning_stream: {diagnosis.winning_stream_id or ''}")
    elif verdict.reason:
        lines.append(f"verdict.reason: {verdict.reason}")
    if diagnosis and diagnosis.suspected_stage:
        pass
    elif summary.failed_jobs:
        lines.append(f"suspected_stage: {summary.failed_jobs[0].failed_step_name or ''}")

    window_lines, stack_lines, _source = _winning_stream_evidence(summary)
    lines.extend(window_lines)
    lines.extend(stack_lines)

    t1 = _t1_templates(summary)
    if t1:
        lines.append("### log_templates")
        for tmpl in t1[:3]:
            lines.append(f"[T1] {tmpl.template} count={tmpl.count}")
            if tmpl.representative_line:
                lines.append(tmpl.representative_line)
            for var in tmpl.variables[:4]:
                if var.values:
                    lines.append(f"  {var.mask}=" + ", ".join(var.values[:5]))

    junit = summary.junit
    if junit is not None and junit.failures:
        lines.append("### junit")
        for failure in junit.failures[:3]:
            lines.append(f"{failure.classname}::{failure.name} {failure.message or ''}")
            if failure.body:
                lines.append("\n".join(failure.body.splitlines()[:10]))

    changes = summary.changes
    if changes is not None:
        lines.append("### change_context")
        lines.append(
            f"range {changes.range_basis} {changes.base_sha}..{changes.head_sha} "
            f"classes={','.join(changes.classes)}"
        )
        for commit in changes.commits[:5]:
            lines.append(f"{commit.sha} {commit.subject}")
        suspected = list((diagnosis.suspected_files if diagnosis else []) or [])
        if changes.diffstat and suspected:
            keep = [
                row
                for row in changes.diffstat.splitlines()
                if any(name in row for name in suspected)
            ]
            if keep:
                lines.extend(keep)
        elif changes.diffstat:
            lines.extend(changes.diffstat.splitlines()[:8])

    if summary.code_context and summary.code_context.hunks:
        lines.append("### code_context")
        for hunk in summary.code_context.hunks[:3]:
            loc = f"{hunk.path}:{hunk.start_line}-{hunk.end_line}"
            if hunk.note:
                lines.append(f"{loc} {hunk.note}")
            else:
                lines.append(loc)
                lines.append(hunk.content)

    compare = summary.last_green_compare
    if compare is not None and compare.novel_templates:
        lines.append("### last_green_compare")
        for name in compare.novel_templates[:8]:
            lines.append(name)

    history = summary.history
    if history is not None:
        lines.append("### history")
        lines.append(f"match={history.match} seen={history.seen_count}")
        if history.previous_resolution:
            lines.append(f"resolution: {history.previous_resolution}")

    return [line for line in lines if line is not None]


def _winning_stream_evidence(summary: Summary) -> tuple[list[str], list[str], str]:
    sid = summary.diagnosis.winning_stream_id if summary.diagnosis else None
    pipeline = _pick_pipeline_stream(summary, sid)
    if pipeline is not None:
        windows = _first_error_windows(pipeline.windows)
        traces = pipeline.stack_traces[:1]
        source = "pipeline_logs"
        lines = ["### first_error_window", *[w.content for w in windows]]
        if not windows:
            tails = [w for w in pipeline.windows if w.label == "tail"]
            if tails:
                lines = ["### tail_window", tails[0].content]
        stack_lines: list[str] = []
        if traces:
            stack_lines = ["### stack_traces", traces[0].content]
        return lines, stack_lines, source

    lines: list[str] = []
    stack_lines = []
    for job in summary.failed_jobs[:1]:
        first = [w for w in job.windows if w.label in {"first_error", "merged"}]
        if first:
            lines = ["### first_error_window", first[0].content]
        elif job.windows:
            lines = ["### tail_window", job.windows[0].content]
        if job.stack_traces:
            stack_lines = ["### stack_traces", job.stack_traces[0].content]
        break
    return lines, stack_lines, "job"


def _pick_pipeline_stream(
    summary: Summary, sid: str | None
) -> PipelineLogStream | None:
    if not summary.pipeline_logs:
        return None
    if sid:
        for stream in summary.pipeline_logs:
            ident = f"artifact:{stream.artifact_name}:{stream.file}"
            if ident == sid or sid == f"artifact:{stream.artifact_name}":
                return stream
    return summary.pipeline_logs[0]


def _first_error_windows(windows: list) -> list:
    first = [w for w in windows if w.label in {"first_error", "merged"}]
    return first[:1]


def _t1_templates(summary: Summary):
    templates = []
    if summary.drain is not None:
        templates.extend(summary.drain.templates)
    for stream in summary.pipeline_logs:
        templates.extend(stream.templates)
    t1 = [t for t in templates if t.tier == "T1"]
    if t1:
        return t1
    return [t for t in templates if t.has_error_match or t.tier == "T3"]
