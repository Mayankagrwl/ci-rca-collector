"""Stage 6 — error windows, stack traces, annotations, error_lines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .config import (
    FIRST_ERROR_CONTEXT_LINES,
    STACK_TRACE_BOTTOM_FRAMES,
    STACK_TRACE_TOP_FRAMES,
    TAIL_WINDOW_LINES,
)
from .models import ErrorLine, LogWindow, StackTrace

_WindowLabel = Literal["first_error", "tail", "merged"]

ERROR_LINE = re.compile(
    r"ERROR|FAIL|Exception|Traceback|panic:|##\[error\]|FATAL|"
    r"Segmentation fault|npm ERR!",
    re.IGNORECASE,
)
_FRAME = re.compile(
    r"^(?:"
    r"\s+at "
    r"|\tat "
    r'|\s*File "'
    r"|\s*Caused by:"
    r"|\s+\.\.\. \d+ more"
    r")"
)
_HEADLINE = re.compile(
    r"^(?:[A-Za-z_][\w.]*(?:Error|Exception)|Error|Exception|panic:|fatal error:)",
    re.IGNORECASE,
)
_ELIDE = "… {n} frames elided …"


@dataclass
class Extracted:
    windows: list[LogWindow] = field(default_factory=list)
    stack_traces: list[StackTrace] = field(default_factory=list)
    error_lines: list[ErrorLine] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)


def extract_from_lines(lines: list[str]) -> Extracted:
    """Operate on cleaned logical records. No GitHub API calls."""
    error_lines = _error_lines(lines)
    annotations = [
        line.strip()
        for line in _flatten(lines)
        if "##[error]" in line
    ]
    windows = _windows(lines)
    stacks = _stack_traces(lines)
    return Extracted(
        windows=windows,
        stack_traces=stacks,
        error_lines=error_lines,
        annotations=annotations,
    )


def _flatten(lines: list[str]) -> list[str]:
    out: list[str] = []
    for record in lines:
        out.extend(record.split("\n"))
    return out


def _error_lines(lines: list[str]) -> list[ErrorLine]:
    found: list[ErrorLine] = []
    for number, record in enumerate(lines, start=1):
        if ERROR_LINE.search(record):
            text = record.split("\n", 1)[0]
            found.append(ErrorLine(line_number=number, text=text))
    return found


def _windows(lines: list[str]) -> list[LogWindow]:
    if not lines:
        return []
    total = len(lines)
    error_index = next(
        (i for i, record in enumerate(lines) if ERROR_LINE.search(record)),
        None,
    )
    first: LogWindow | None = None
    if error_index is not None:
        start = max(1, error_index + 1 - FIRST_ERROR_CONTEXT_LINES)
        end = min(total, error_index + 1 + FIRST_ERROR_CONTEXT_LINES)
        first = _make_window(lines, start, end, "first_error")

    tail_start = max(1, total - TAIL_WINDOW_LINES + 1)
    tail = _make_window(lines, tail_start, total, "tail")

    if first is None:
        return [tail]
    if first.end_line >= tail.start_line:
        start = min(first.start_line, tail.start_line)
        end = max(first.end_line, tail.end_line)
        return [_make_window(lines, start, end, "merged")]
    return [first, tail]


def _make_window(lines: list[str], start: int, end: int, label: _WindowLabel) -> LogWindow:
    start = max(1, start)
    end = min(len(lines), end)
    return LogWindow(
        label=label,
        start_line=start,
        end_line=end,
        total_lines=len(lines),
        content="\n".join(lines[start - 1 : end]),
        truncated=False,
    )


def _stack_traces(lines: list[str]) -> list[StackTrace]:
    traces: list[StackTrace] = []
    frames: list[str] = []
    headline: str | None = None

    def flush() -> None:
        nonlocal frames, headline
        if not frames:
            headline = None
            return
        traces.append(_truncate(headline, frames))
        frames = []
        headline = None

    for record in lines:
        for physical in record.split("\n"):
            if _is_frame(physical):
                frames.append(physical)
                continue
            if frames:
                flush()
            if _HEADLINE.match(physical.strip()):
                headline = physical.strip()
    flush()
    return traces


def _is_frame(line: str) -> bool:
    return _FRAME.match(line) is not None


def _truncate(headline: str | None, frames: list[str]) -> StackTrace:
    top = STACK_TRACE_TOP_FRAMES
    bottom = STACK_TRACE_BOTTOM_FRAMES
    limit = top + bottom
    elided = 0
    kept = frames
    if len(frames) > limit:
        elided = len(frames) - limit
        kept = frames[:top] + [_ELIDE.format(n=elided)] + frames[-bottom:]
    parts = ([headline] if headline else []) + kept
    return StackTrace(
        headline=headline,
        content="\n".join(parts),
        frame_count=len(frames),
        elided_count=elided,
    )
