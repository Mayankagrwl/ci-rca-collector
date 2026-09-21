"""Fetch tiny source hunks only when diagnose says the failure is code-implicated."""

from __future__ import annotations

from collections.abc import Callable
from typing import Sequence

from .config import CODE_HUNK_MAX_FILES, CODE_HUNK_MAX_LINES, CODE_HUNK_RADIUS
from .diagnose import (
    DeterministicVerdict,
    code_paths_for_fetch,
    is_blast_radius_skip,
    should_fetch_code_context,
)
from .models import CodeContext, CodeHunk, Summary
from .redact import redact_text

GetFile = Callable[[str, str, str], str | None]


def clip_hunk(
    text: str,
    *,
    center_line: int | None = None,
    radius: int = CODE_HUNK_RADIUS,
    max_lines: int = CODE_HUNK_MAX_LINES,
) -> tuple[str, int, int]:
    """Return (content, start_line, end_line) clipped around *center_line*."""
    lines = text.splitlines()
    if not lines:
        return "", 1, 1
    if center_line is None or center_line < 1:
        end = min(len(lines), max_lines)
        return "\n".join(lines[:end]), 1, end
    start = max(1, center_line - radius)
    end = min(len(lines), center_line + radius)
    if end - start + 1 > max_lines:
        extra = (end - start + 1) - max_lines
        left = extra // 2
        right = extra - left
        start = min(start + left, end)
        end = max(end - right, start)
    start = max(1, start)
    end = min(len(lines), end)
    return "\n".join(lines[start - 1 : end]), start, end


def resolve_source_ref(summary: Summary) -> tuple[str, str, str | None]:
    """Return (ref, basis, note). Prefer first_failing_sha over the PR tip."""
    head = summary.run.head_sha or ""
    first = summary.history.first_failing_sha if summary.history is not None else None
    if first:
        return first, "first_failing", None
    note = "first_failing_sha unknown; using head"
    return head, "head", note


def fetch_code_context(
    summary: Summary,
    verdict: DeterministicVerdict,
    *,
    get_file: GetFile,
    repo: str,
    ref: str | None = None,
    max_files: int = CODE_HUNK_MAX_FILES,
) -> CodeContext:
    """Download at most *max_files* paths. 404/403 are noted; never raises."""
    computed, basis, basis_note = resolve_source_ref(summary)
    primary = computed or ref or ""
    head = summary.run.head_sha or ref or primary

    if is_blast_radius_skip(summary) and not should_fetch_code_context(summary, verdict):
        classes = sorted(set((summary.changes.classes if summary.changes else []) or []))
        note = f"skipped source fetch: changes.classes={classes}"
        if note not in summary.collection_notes:
            summary.collection_notes.append(note)
        return CodeContext(
            skipped_reason="skipped_blast_radius",
            notes=[note],
            ref_sha=None,
            basis=None,
        )

    if not should_fetch_code_context(summary, verdict):
        why = "diagnose did not request source"
        if verdict.category in {
            "oom",
            "timeout",
            "disk_space",
            "image_pull",
            "auth",
            "network_dns",
            "dependency",
        }:
            why = f"skip source fetch for category {verdict.category}"
        classes = set((summary.changes.classes if summary.changes else []) or [])
        if classes and classes <= {
            "lockfile",
            "ci_config",
            "container",
            "dependency",
            "build_config",
        }:
            why = "skipped_blast_radius"
        return CodeContext(skipped_reason=why, ref_sha=primary or None, basis=basis)  # type: ignore[arg-type]

    notes: list[str] = []
    if basis_note:
        notes.append(basis_note)
    hunks: list[CodeHunk] = []
    for path, line in code_paths_for_fetch(summary, verdict)[:max_files]:
        used_ref = primary
        hunk_note: str | None = None
        try:
            raw = get_file(repo, path, primary) if primary else None
        except Exception as exc:  # noqa: BLE001 — collector must not fail
            notes.append(f"{path}: {exc}")
            hunks.append(CodeHunk(path=path, ref=primary, note=str(exc)))
            continue
        if raw is None and basis == "first_failing" and head and head != primary:
            try:
                raw = get_file(repo, path, head)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{path} at head: {exc}")
                raw = None
            if raw is not None:
                used_ref = head
                hunk_note = "not in first failing commit; using head"
                notes.append(f"{path}: {hunk_note}")
        if raw is None:
            note = "404/403"
            notes.append(f"{path}: {note}")
            hunks.append(CodeHunk(path=path, ref=used_ref, note=note))
            continue
        redacted, _n = redact_text(raw)
        content, start, end = clip_hunk(redacted, center_line=line)
        hunks.append(
            CodeHunk(
                path=path,
                ref=used_ref,
                start_line=start,
                end_line=end,
                content=content,
                note=hunk_note,
            )
        )
    return CodeContext(
        hunks=hunks,
        notes=notes,
        ref_sha=primary or None,
        basis=basis,  # type: ignore[arg-type]
    )


def paths_from_frames(texts: Sequence[str]) -> list[tuple[str, int | None]]:
    from .extract import extract_source_paths

    found: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for text in texts:
        for path, line in extract_source_paths(text):
            if path in seen:
                continue
            seen.add(path)
            found.append((path, line))
    return found
