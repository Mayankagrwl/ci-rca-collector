"""Phase 2 prompt: evidence from summary.json only, wrapped in <EVIDENCE>."""

from __future__ import annotations

from .budget import trim_middle
from .config import PROMPT_VERSION, TOKEN_BUDGET_TOTAL
from .models import Summary

SYSTEM_PROMPT = (
    f"You are a CI root-cause assistant (prompt {PROMPT_VERSION}). "
    "Use only the text inside <EVIDENCE>. Do not invent log lines, file paths, or test names. "
    "Reply with ONLY a single JSON object. No markdown fences, no prose, no commentary. "
    "Keys: root_cause (string), suggested_fix (string), "
    "confidence (high|medium|low), "
    "citations (array of {quote, source, line}). "
    "Each citations[].quote MUST be a verbatim substring of <EVIDENCE>. "
    "source must be one of: first_error_window, tail_window, stack_traces, "
    "log_templates, junit, change_context, annotations, history, step_table."
)

_REPAIR = (
    "Your previous reply was not valid JSON: {reason}. "
    "Reply with ONLY the JSON object. No markdown fences, no prose, no commentary. "
    "Keys: root_cause, suggested_fix, confidence, citations. "
    "citations[].quote must be copied verbatim from <EVIDENCE>."
)


def build_evidence(summary: Summary, *, cap_tokens: int | None = None) -> str:
    """Render summary.json into a budgeted <EVIDENCE> block."""
    cap = TOKEN_BUDGET_TOTAL if cap_tokens is None else cap_tokens
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
                "Diagnose this CI failure. JSON only, citations from <EVIDENCE>."
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


def _evidence_lines(summary: Summary) -> list[str]:
    run = summary.run
    verdict = summary.verdict
    cls = summary.classification
    lines = [
        f"workflow: {run.workflow_name}",
        f"event: {run.event}",
        f"branch: {run.head_branch}",
        f"head_sha: {run.head_sha}",
        f"failed_jobs: {run.failed_jobs_analysed}/{run.failed_job_total}",
        f"verdict.requires_analysis: {verdict.requires_analysis}",
        f"verdict.short_circuit: {verdict.short_circuit}",
        f"classification: {cls.category} ({cls.confidence}) "
        f"infra_vs_code={cls.is_infra_vs_code} flaky={cls.is_flaky}",
        f"fingerprint: {summary.fingerprint}",
    ]
    if verdict.reason:
        lines.append(f"verdict.reason: {verdict.reason}")
    for job in summary.failed_jobs:
        lines.append(f"## job {job.name}")
        lines.append(f"failed_step: {job.failed_step_name} exit={job.exit_code}")
        if job.steps:
            lines.append("### step_table")
            for step in job.steps:
                lines.append(
                    f"{step.number}. {step.name} {step.conclusion} "
                    f"{step.duration_seconds}s cache_miss={step.suspected_cache_miss}"
                )
        for window in job.windows:
            label = (
                "first_error_window"
                if window.label in ("first_error", "merged")
                else "tail_window"
                if window.label == "tail"
                else window.label
            )
            lines.append(f"### {label}")
            lines.append(window.content)
        if job.stack_traces:
            lines.append("### stack_traces")
            for trace in job.stack_traces:
                lines.append(trace.content)
        if job.annotations:
            lines.append("### annotations")
            lines.extend(job.annotations)
    drain = summary.drain
    if drain is not None:
        lines.append("### log_templates")
        for tmpl in drain.templates:
            lines.append(f"[{tmpl.tier}] {tmpl.template} count={tmpl.count}")
            if tmpl.representative_line:
                lines.append(tmpl.representative_line)
    junit = summary.junit
    if junit is not None:
        lines.append("### junit")
        for failure in junit.failures:
            lines.append(f"{failure.classname}::{failure.name} {failure.message or ''}")
            if failure.body:
                lines.append(failure.body)
    changes = summary.changes
    if changes is not None:
        lines.append("### change_context")
        lines.append(
            f"range {changes.range_basis} {changes.base_sha}..{changes.head_sha} "
            f"classes={','.join(changes.classes)}"
        )
        for commit in changes.commits:
            lines.append(f"{commit.sha} {commit.subject}")
        if changes.diffstat:
            lines.append(changes.diffstat)
    history = summary.history
    if history is not None:
        lines.append("### history")
        lines.append(
            f"match={history.match} seen={history.seen_count} "
            f"last_success={history.last_success_sha}"
        )
        if history.previous_summary:
            lines.append(history.previous_summary)
        if history.previous_resolution:
            lines.append(f"resolution: {history.previous_resolution}")
    return [line for line in lines if line is not None]
