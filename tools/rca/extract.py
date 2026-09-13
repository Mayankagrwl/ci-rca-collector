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
    r"Segmentation fault|npm ERR!|SyntaxError|IndentationError|"
    r'TypeError|NameError|File "',
    re.IGNORECASE,
)
# Center the first-error window on executed output, not the step script listing.
_WINDOW_ERROR = re.compile(
    r"Exception:|(?<![A-Za-z])ERROR(?![A-Za-z])|panic:|##\[error\]|fatal error:",
    re.IGNORECASE,
)
_SHELL_LINE = re.compile(r"^shell:\s", re.IGNORECASE)
_ENV_HEADER = re.compile(r"^env:\s*$", re.IGNORECASE)
_EXIT_CODE = re.compile(r"Process completed with exit code (\d+)", re.IGNORECASE)
_FRAME = re.compile(
    r"^(?:"
    r"\s+at "
    r"|\tat "
    r'|\s+File "'
    r"|\s*Caused by:"
    r"|\s*\.\.\. \d+ more"
    r"|Traceback \(most recent call last\):"
    r")"
)
_CARET = re.compile(r"^\s+\^+\s*$")
_HEADLINE = re.compile(
    r"^(?:[A-Za-z_][\w.]*(?:Error|Exception)|Error|Exception|panic:|fatal error:)",
    re.IGNORECASE,
)
_PIP_ERROR = re.compile(r"^ERROR:", re.IGNORECASE)
_TRACEBACK_HINT = re.compile(r'File "|Traceback|(?:^|\s)at\s')
_ELIDE = "… {n} frames elided …"


@dataclass
class Extracted:
    windows: list[LogWindow] = field(default_factory=list)
    stack_traces: list[StackTrace] = field(default_factory=list)
    error_lines: list[ErrorLine] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    exit_code: int | None = None


def parse_exit_code(text: str | list[str]) -> int | None:
    blob = text if isinstance(text, str) else "\n".join(text)
    match = _EXIT_CODE.search(blob)
    if not match:
        return None
    return int(match.group(1))


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
        exit_code=parse_exit_code(lines),
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


def _skip_script_preamble(lines: list[str]) -> int:
    """Index of the first executed output line (after `shell:` / env block)."""
    for index, record in enumerate(lines):
        head = record.split("\n", 1)[0].strip()
        if not _SHELL_LINE.match(head):
            continue
        start = index + 1
        if start < len(lines) and _ENV_HEADER.match(lines[start].split("\n", 1)[0].strip()):
            start += 1
            while start < len(lines):
                nxt = lines[start].split("\n", 1)[0]
                if nxt.startswith(" ") or nxt.startswith("\t") or not nxt.strip():
                    start += 1
                    continue
                break
        return start
    return 0


def _first_error_index(lines: list[str]) -> int | None:
    start = _skip_script_preamble(lines)
    for index in range(start, len(lines)):
        if _WINDOW_ERROR.search(lines[index]):
            return index
    for index in range(start, len(lines)):
        if ERROR_LINE.search(lines[index]):
            return index
    return None


def _windows(lines: list[str]) -> list[LogWindow]:
    if not lines:
        return []
    total = len(lines)
    error_index = _first_error_index(lines)
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
    in_python = False

    def flush() -> None:
        nonlocal frames, headline, in_python
        if not frames and not headline:
            return
        traces.append(_truncate(headline, frames))
        frames = []
        headline = None
        in_python = False

    for record in lines:
        for physical in record.split("\n"):
            if _is_frame(physical):
                frames.append(physical)
                in_python = 'File "' in physical or physical.strip().startswith("Traceback")
                continue
            if frames and in_python and (
                _CARET.match(physical) or physical.startswith(" ") or physical.startswith("\t")
            ):
                frames.append(physical)
                continue
            if _is_stack_headline(physical):
                headline = physical.strip()
                if frames:
                    frames.append(physical)
                flush()
                continue
            if frames:
                flush()
    flush()
    return traces


def _is_frame(line: str) -> bool:
    return _FRAME.match(line) is not None


def _is_stack_headline(line: str) -> bool:
    stripped = line.strip()
    if not _HEADLINE.match(stripped):
        return False
    # pip "ERROR: No matching distribution..." is not a traceback.
    if _PIP_ERROR.match(stripped) and not _TRACEBACK_HINT.search(stripped):
        return False
    return True


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
