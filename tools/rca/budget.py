"""Token estimation and middle-trim. Source-agnostic."""

from __future__ import annotations

from .config import SECTION_TOKEN_CAPS, TOKEN_BUDGET_TOTAL
from .models import BudgetReport, LogWindow, StackTrace, Summary

_MIDDLE = "\n… middle elided …\n"


def token_count(text: str) -> int:
    return len(text) // 4


def trim_middle(text: str, cap_tokens: int) -> tuple[str, bool, int, int]:
    """Keep the ends of a log block. Returns (text, trimmed, before, after)."""
    before = token_count(text)
    if cap_tokens <= 0 or before <= cap_tokens:
        return text, False, before, before
    keep_chars = cap_tokens * 4
    half = max(1, keep_chars // 2)
    if len(text) <= keep_chars:
        return text, False, before, before
    trimmed = text[:half] + _MIDDLE + text[-half:]
    return trimmed, True, before, token_count(trimmed)


def apply_budget(
    summary: Summary,
    *,
    total_cap: int | None = None,
) -> Summary:
    """Enforce per-section caps from §8. Surplus is not redistributed."""
    caps = dict(SECTION_TOKEN_CAPS)
    cap_total = int(total_cap) if total_cap else TOKEN_BUDGET_TOTAL
    notes: list[str] = list(summary.budget_report.trimmed)
    section_used: dict[str, int] = dict(summary.budget_report.section_used)

    for job in summary.failed_jobs:
        new_windows: list[LogWindow] = []
        for window in job.windows:
            key = (
                "tail_window"
                if window.label == "tail"
                else "first_error_window"
            )
            cap = caps.get(key, 1000)
            content, trimmed, before, after = trim_middle(window.content, cap)
            if trimmed:
                window.truncated = True
                notes.append(f"{key} from {before} → {after} tokens (middle elided)")
            window.content = content
            section_used[key] = section_used.get(key, 0) + after
            new_windows.append(window)
        job.windows = new_windows

        stack_cap = caps.get("stack_traces", 600)
        new_traces: list[StackTrace] = []
        for trace in job.stack_traces:
            content, trimmed, before, after = trim_middle(trace.content, stack_cap)
            if trimmed:
                notes.append(
                    f"stack_traces from {before} → {after} tokens (middle elided)"
                )
            trace.content = content
            section_used["stack_traces"] = section_used.get("stack_traces", 0) + after
            new_traces.append(trace)
        job.stack_traces = new_traces

        ann_cap = caps.get("annotations", 200)
        joined = "\n".join(job.annotations)
        if joined:
            content, trimmed, before, after = trim_middle(joined, ann_cap)
            if trimmed:
                notes.append(
                    f"annotations from {before} → {after} tokens (middle elided)"
                )
                job.annotations = content.split("\n")
            section_used["annotations"] = section_used.get("annotations", 0) + after

    pipe_cap = caps.get("pipeline_logs", 1000)
    for stream in summary.pipeline_logs:
        new_windows = []
        for window in stream.windows:
            content, trimmed, before, after = trim_middle(window.content, pipe_cap)
            if trimmed:
                window.truncated = True
                notes.append(
                    f"pipeline_logs from {before} → {after} tokens (middle elided)"
                )
            window.content = content
            section_used["pipeline_logs"] = section_used.get("pipeline_logs", 0) + after
            new_windows.append(window)
        stream.windows = new_windows

    if summary.code_context and summary.code_context.hunks:
        hunk_cap = caps.get("code_context", 400)
        for hunk in summary.code_context.hunks:
            content, trimmed, before, after = trim_middle(hunk.content, hunk_cap)
            if trimmed:
                notes.append(
                    f"code_context from {before} → {after} tokens (middle elided)"
                )
            hunk.content = content
            section_used["code_context"] = section_used.get("code_context", 0) + after

    used = sum(section_used.values())
    summary.budget_report = BudgetReport(
        total_cap_tokens=cap_total,
        total_used_tokens=used,
        section_used=section_used,
        trimmed=notes,
        t1_overrun=summary.budget_report.t1_overrun,
        t1_raised_to=summary.budget_report.t1_raised_to,
    )
    if notes:
        for note in notes:
            tagged = f"Trimmed: {note}"
            if tagged not in summary.collection_notes:
                summary.collection_notes.append(tagged)
    return summary
