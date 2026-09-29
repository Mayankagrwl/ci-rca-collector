"""Stage 6 — error windows, stack traces, annotations, error_lines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .config import (
    BENIGN_LINE_PATTERNS,
    FAILED_STEP_EXCERPT_LINES,
    FIRST_ERROR_CONTEXT_LINES,
    STACK_TRACE_BOTTOM_FRAMES,
    STACK_TRACE_TOP_FRAMES,
    TAIL_WINDOW_LINES,
)
from .models import ErrorLine, LogWindow, StackTrace

_BENIGN_RES = [re.compile(pattern, re.IGNORECASE) for pattern in BENIGN_LINE_PATTERNS]


def is_benign_line(line: str) -> bool:
    """True for normal/informational output that must never be a failure cause.

    Docker/registry pull progress, image-up-to-date/loaded, orchestration
    warnings/echoes, and normal completions. The timestamp prefix is stripped
    first so line-anchored patterns match. Source-agnostic; never raises.
    """
    head = _strip_ts((line or "").split("\n", 1)[0])
    return any(rx.search(head) for rx in _BENIGN_RES)

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
# Cause lines that are not tagged ERROR / ##[error] (Artifactory version, npm, …).
_SEMANTIC_CAUSE = re.compile(
    r"already exists|must update|version exists|not found|"
    r"ERESOLVE|you need to update",
    re.IGNORECASE,
)
_TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
_GROUP_RUN = re.compile(r"^##\[group\]Run\s+(.*)$", re.IGNORECASE)
_GROUP_ANY = re.compile(r"^##\[group\](.*)$", re.IGNORECASE)
_ENDGROUP = re.compile(r"^##\[endgroup\]")
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
    excerpt_lines: list[str] = field(default_factory=list)
    primary_failure_line: str | None = None


_SOURCE_PATHS = (
    re.compile(r'File "([^"]+)", line (\d+)'),
    re.compile(
        r"(?:^|[\s'`\"(])([A-Za-z0-9_./\\-]+\.[A-Za-z][A-Za-z0-9]*)\((\d+),\d+\):"
    ),
    re.compile(
        r"(?:^|[\s'`\"(])([A-Za-z0-9_./\\-]+\.[A-Za-z][A-Za-z0-9]*):(\d+)(?::\d+)?"
    ),
    re.compile(r"\(([^():]+):(\d+)(?::\d+)?\)"),
    # Maven / javac: "/work/src/Foo.java:[19,40]".
    re.compile(
        r"(?:^|[\s'`\"(])([A-Za-z0-9_./\\-]+\.[A-Za-z][A-Za-z0-9]*):\[(\d+)(?:,\d+)?\]"
    ),
    # Kotlin / Gradle: "e: file:///work/src/Foo.kt:12:5" — the path without the scheme.
    re.compile(r"\bfile://([A-Za-z0-9_./\\-]*/[A-Za-z0-9_.\\-]+\.[A-Za-z][A-Za-z0-9]*):(\d+)"),
)


def is_exit_code_line(line: str) -> bool:
    """True for a bare "Process completed with exit code N" line (any prefix).

    Such lines never explain a failure; the same rule _primary_failure_line applies.
    """
    return bool(_EXIT_CODE.search(line or ""))


def extract_source_paths(text: str) -> list[tuple[str, int | None]]:
    """Paths (and optional line numbers) mentioned in log or stack text."""
    found: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for compiled in _SOURCE_PATHS:
        for match in compiled.finditer(text or ""):
            path = (match.group(1) or "").replace("\\", "/").strip()
            if not path or path in seen:
                continue
            if path.startswith("http:") or path.startswith("https:"):
                continue
            line: int | None = None
            if match.lastindex and match.lastindex >= 2:
                try:
                    line = int(match.group(2))
                except (TypeError, ValueError):
                    line = None
            seen.add(path)
            found.append((path, line))
    return found


def order_stack_traces_by_paths(
    traces: list[StackTrace],
    changed_paths: list[str],
) -> list[StackTrace]:
    """Stable-order traces so any naming a changed source path leads. No drops.

    Source-agnostic: ``changed_paths`` is a plain list (the collect layer passes
    ``changes.files``). Relevance is a lenient path match against the paths the
    trace text mentions.
    """
    if not traces or not changed_paths:
        return list(traces)
    changed = [p.replace("\\", "/").strip() for p in changed_paths if p]

    def _touches_changed(trace: StackTrace) -> bool:
        text = f"{trace.headline or ''}\n{trace.content or ''}"
        for path, _line in extract_source_paths(text):
            tp = path.strip()
            tp_base = tp.rsplit("/", 1)[-1]
            for cp in changed:
                cp_base = cp.rsplit("/", 1)[-1]
                if tp == cp or tp.endswith("/" + cp) or cp.endswith("/" + tp):
                    return True
                if tp_base and tp_base == cp_base:
                    return True
        return False

    relevant: list[StackTrace] = []
    others: list[StackTrace] = []
    for trace in traces:
        (relevant if _touches_changed(trace) else others).append(trace)
    return relevant + others


def parse_exit_code(text: str | list[str]) -> int | None:
    blob = text if isinstance(text, str) else "\n".join(text)
    match = _EXIT_CODE.search(blob)
    if not match:
        return None
    return int(match.group(1))


def extract_from_lines(
    lines: list[str],
    *,
    failed_step_name: str | None = None,
) -> Extracted:
    """Operate on cleaned logical records. No GitHub API calls.

    When ``failed_step_name`` resolves to a GitHub log group, the first-error
    window, the error-line scan, and ``primary_failure_line`` are anchored
    inside that group; otherwise they fall back to whole-log behaviour.
    """
    lo, hi = _failed_group_bounds(lines, failed_step_name)
    error_lines = _error_lines(lines, lo, hi)
    annotations = [
        line.strip()
        for line in _flatten(lines)
        if "##[error]" in line
    ]
    windows = _windows(lines, lo=lo, hi=hi)
    stacks = _stack_traces(lines)
    excerpt = failed_step_excerpt_lines(lines, step_name=failed_step_name)
    primary = _primary_failure_line(lines, lo, hi)
    return Extracted(
        windows=windows,
        stack_traces=stacks,
        error_lines=error_lines,
        annotations=annotations,
        exit_code=parse_exit_code(lines),
        excerpt_lines=excerpt,
        primary_failure_line=primary,
    )


def _failed_group_bounds(
    lines: list[str],
    failed_step_name: str | None,
) -> tuple[int, int]:
    """Bounds of the GitHub failed-step group, or the whole log if none resolves.

    Only anchors when GitHub named a failed step; with no step name we keep
    today's whole-log behaviour. When a step name is present, the group is
    resolved against the same stripped view ``failed_step_excerpt_lines`` uses,
    so the window/error-line/primary-line anchoring agrees with the excerpt.
    """
    if not (failed_step_name or "").strip():
        return 0, len(lines)
    stripped = [_strip_ts(record.split("\n", 1)[0]) for record in lines]
    group = _pick_failed_group(stripped, failed_step_name)
    if group is not None:
        return group
    return 0, len(lines)


def _primary_failure_line(lines: list[str], lo: int, hi: int) -> str | None:
    """First cause line inside [lo:hi]: semantic, then error window, then grep.

    Benign progress lines and bare exit-code lines are never selected.
    """
    for pattern in (_SEMANTIC_CAUSE, _WINDOW_ERROR, ERROR_LINE):
        for index in range(lo, hi):
            line = lines[index]
            if is_benign_line(line) or is_exit_code_line(line):
                continue
            if pattern.search(line):
                return _strip_ts(line.split("\n", 1)[0]).strip() or None
    return None


def _flatten(lines: list[str]) -> list[str]:
    out: list[str] = []
    for record in lines:
        out.extend(record.split("\n"))
    return out


def _error_lines(
    lines: list[str],
    lo: int = 0,
    hi: int | None = None,
) -> list[ErrorLine]:
    hi = len(lines) if hi is None else hi
    found: list[ErrorLine] = []
    for index in range(lo, hi):
        record = lines[index]
        if ERROR_LINE.search(record) or _SEMANTIC_CAUSE.search(record):
            text = record.split("\n", 1)[0]
            found.append(ErrorLine(line_number=index + 1, text=text))
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


def _first_error_index(
    lines: list[str],
    lo: int = 0,
    hi: int | None = None,
) -> int | None:
    """Absolute index of the first-error anchor, optionally scoped to [lo:hi]."""
    hi = len(lines) if hi is None else hi
    start = lo + _skip_script_preamble(lines[lo:hi])
    for index in range(start, hi):
        if _SEMANTIC_CAUSE.search(lines[index]) and not is_benign_line(lines[index]):
            return index
    for index in range(start, hi):
        if _WINDOW_ERROR.search(lines[index]) and not is_benign_line(lines[index]):
            return index
    for index in range(start, hi):
        if ERROR_LINE.search(lines[index]) and not is_benign_line(lines[index]):
            return index
    for index in range(start, hi):
        if _EXIT_CODE.search(lines[index]):
            for back in range(index, start - 1, -1):
                if is_benign_line(lines[back]):
                    continue
                if _SEMANTIC_CAUSE.search(lines[back]) or ERROR_LINE.search(lines[back]):
                    return back
            return index
    return None


def _strip_ts(line: str) -> str:
    text = line.lstrip("\ufeff")
    return _TIMESTAMP_PREFIX.sub("", text, count=1)


def failed_step_excerpt_lines(
    lines: list[str],
    *,
    step_name: str | None = None,
    max_lines: int = FAILED_STEP_EXCERPT_LINES,
) -> list[str]:
    """Last `max_lines` of the first failed step (GitHub group when present)."""
    stripped = [_strip_ts(record.split("\n", 1)[0]) for record in lines]
    group = _pick_failed_group(stripped, step_name)
    if group is not None:
        start, end = group
        chunk = [line for line in stripped[start:end] if line.strip()]
        return chunk[-max_lines:]
    start = _skip_script_preamble(stripped)
    error_index = _first_error_index(stripped)
    if error_index is None:
        chunk = [line for line in stripped[start:] if line.strip()]
        return chunk[-max_lines:]
    end = min(len(stripped), error_index + 2)
    begin = max(start, end - max_lines)
    chunk = [line for line in stripped[begin:end] if line.strip()]
    return chunk[-max_lines:]


def _pick_failed_group(
    lines: list[str],
    step_name: str | None,
) -> tuple[int, int] | None:
    groups = _github_groups(lines)
    if not groups:
        return None
    wanted = (step_name or "").strip().lower()
    if wanted:
        for title, start, end in groups:
            lowered = title.lower()
            if wanted in lowered or lowered in wanted:
                return start, end
    for title, start, end in groups:
        blob = "\n".join(lines[start:end])
        if (
            _SEMANTIC_CAUSE.search(blob)
            or _WINDOW_ERROR.search(blob)
            or ERROR_LINE.search(blob)
            or _EXIT_CODE.search(blob)
        ):
            return start, end
    return None


def _github_groups(lines: list[str]) -> list[tuple[str, int, int]]:
    groups: list[tuple[str, int, int]] = []
    title: str | None = None
    start: int | None = None
    for index, line in enumerate(lines):
        head = line.strip()
        match = _GROUP_RUN.match(head) or _GROUP_ANY.match(head)
        if match:
            if title is not None and start is not None:
                groups.append((title, start, index))
            title = (match.group(1) or "").strip()
            start = index + 1
            continue
        if _ENDGROUP.match(head) and title is not None and start is not None:
            groups.append((title, start, index))
            title = None
            start = None
    if title is not None and start is not None:
        groups.append((title, start, len(lines)))
    return groups


def _windows(
    lines: list[str],
    *,
    lo: int = 0,
    hi: int | None = None,
) -> list[LogWindow]:
    if not lines:
        return []
    total = len(lines)
    # first_error is anchored inside the failed-step group; tail stays whole-log.
    error_index = _first_error_index(lines, lo, hi)
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
