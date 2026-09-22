"""Token estimation and middle-trim. Source-agnostic."""

from __future__ import annotations

from .config import SECTION_TOKEN_CAPS, TOKEN_BUDGET_TOTAL
from .extract import _first_error_index
from .models import BudgetReport, LogWindow, StackTrace, Summary

_MIDDLE = "\n… middle elided …\n"
_ELIDED = "… elided …"


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


def trim_around(
    text: str,
    cap_tokens: int,
    *,
    anchor_index: int | None = None,
) -> tuple[str, bool, int, int]:
    """Keep a cap-sized band of lines centered on the error anchor.

    For a first_error / merged window the causal line can sit in the middle,
    where head+tail trimming would drop it. This keeps the lines around the
    anchor instead. Falls back to ``trim_middle`` when no anchor is found.
    Returns (text, trimmed, before, after). Source-agnostic.
    """
    before = token_count(text)
    if cap_tokens <= 0 or before <= cap_tokens:
        return text, False, before, before
    lines = text.split("\n")
    if anchor_index is None:
        anchor_index = _first_error_index(lines)
    if anchor_index is None or not 0 <= anchor_index < len(lines):
        return trim_middle(text, cap_tokens)

    keep_chars = cap_tokens * 4
    lo = hi = anchor_index
    size = len(lines[anchor_index])
    while True:
        grew = False
        if hi + 1 < len(lines) and size + 1 + len(lines[hi + 1]) <= keep_chars:
            hi += 1
            size += 1 + len(lines[hi])
            grew = True
        if lo - 1 >= 0 and size + 1 + len(lines[lo - 1]) <= keep_chars:
            lo -= 1
            size += 1 + len(lines[lo])
            grew = True
        if not grew:
            break

    parts: list[str] = []
    if lo > 0:
        parts.append(_ELIDED)
    parts.extend(lines[lo : hi + 1])
    if hi + 1 < len(lines):
        parts.append(_ELIDED)
    trimmed = "\n".join(parts)
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
            if window.label == "tail":
                key = "tail_window"
                cap = caps.get(key, 1000)
                content, trimmed, before, after = trim_middle(window.content, cap)
                elision = "middle elided"
            else:
                # first_error / merged: keep the band around the causal line.
                key = "first_error_window"
                cap = caps.get(key, 1000)
                content, trimmed, before, after = trim_around(window.content, cap)
                elision = "error-centered"
            if trimmed:
                window.truncated = True
                notes.append(f"{key} from {before} → {after} tokens ({elision})")
            window.content = content
            section_used[key] = section_used.get(key, 0) + after
            new_windows.append(window)
        job.windows = new_windows
        # failed_step_excerpt is mandatory STGPT input and is never middle-trimmed.

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
            if window.label in {"first_error", "merged"}:
                content, trimmed, before, after = trim_around(window.content, pipe_cap)
                elision = "error-centered"
            else:
                content, trimmed, before, after = trim_middle(window.content, pipe_cap)
                elision = "middle elided"
            if trimmed:
                window.truncated = True
                notes.append(
                    f"pipeline_logs from {before} → {after} tokens ({elision})"
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
