"""Strip GitHub log chrome and join multi-line records.

Source-agnostic: operates on raw text only. Does not import GitHub clients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_NOISE_PREFIXES = (
    "##[group]",
    "##[endgroup]",
    "##[debug]",
    "Post job cleanup",
    "Cleaning up orphan processes",
    "Download action repository",
)

_CONTINUATION = re.compile(
    r"^(?:"
    r"\s+at "
    r"|\tat "
    r'|\s*File "'
    r"|\s*Caused by:"
    r"|\s+\.\.\. \d+ more"
    r")"
)

_IMAGE_LINE = re.compile(r"^Image:\s*(.+)\s*$")
_VERSION_LINE = re.compile(r"^Version:\s*(.+)\s*$")
_DISK_LINE = re.compile(
    r"(?:available disk space|disk space|disk free)[:\s]+(.+)",
    re.IGNORECASE,
)
_OS_GROUP = re.compile(r"^##\[group\]Operating System\s*$", re.IGNORECASE)
_ENDGROUP = re.compile(r"^##\[endgroup\]")
_POST_CLEANUP = re.compile(r"^Post job cleanup\b")


@dataclass(frozen=True)
class SetupFacts:
    image: str | None = None
    os: str | None = None
    disk_free_at_start: str | None = None


@dataclass(frozen=True)
class CleanResult:
    lines: list[str]
    lines_raw: int
    lines_clean: int
    bytes_raw: int
    setup: SetupFacts


def _physical_lines(raw: str) -> list[str]:
    # Split on newline only so CR progress redraws stay on one physical line.
    parts = raw.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _strip_timestamp(line: str) -> str:
    return _TIMESTAMP.sub("", line, count=1)


def _strip_ansi(line: str) -> str:
    return _ANSI.sub("", line)


def _strip_cr(line: str) -> str:
    if "\r" in line:
        return line.split("\r")[-1]
    return line


def _is_noise(line: str, *, keep_post_cleanup: bool) -> bool:
    stripped = line.lstrip()
    for prefix in _NOISE_PREFIXES:
        if stripped.startswith(prefix) or line.startswith(prefix):
            if keep_post_cleanup and prefix in (
                "Post job cleanup",
                "Cleaning up orphan processes",
            ):
                return False
            return True
    return False


def _is_continuation(line: str) -> bool:
    if line.startswith("\t"):
        return True
    return _CONTINUATION.match(line) is not None


def _extract_setup(lines: list[str]) -> SetupFacts:
    image_name: str | None = None
    image_version: str | None = None
    os_parts: list[str] = []
    in_os = False
    disk: str | None = None

    for line in lines:
        if _OS_GROUP.match(line):
            in_os = True
            continue
        if in_os:
            if _ENDGROUP.match(line):
                in_os = False
            elif line.strip():
                os_parts.append(line.strip())
            continue
        m = _IMAGE_LINE.match(line)
        if m:
            image_name = m.group(1).strip()
            continue
        m = _VERSION_LINE.match(line)
        if m and image_version is None:
            image_version = m.group(1).strip()
            continue
        m = _DISK_LINE.search(line)
        if m:
            disk = m.group(1).strip()

    image: str | None = None
    if image_name and image_version:
        image = f"{image_name}@{image_version}"
    elif image_name:
        image = image_name
    elif image_version:
        image = image_version

    os_name = " ".join(os_parts) if os_parts else None
    return SetupFacts(image=image, os=os_name, disk_free_at_start=disk)


def _drop_post_section(lines: list[str], *, keep_post_cleanup: bool) -> list[str]:
    if keep_post_cleanup:
        return lines
    out: list[str] = []
    for line in lines:
        if _POST_CLEANUP.match(line.lstrip()) or _POST_CLEANUP.match(line):
            break
        out.append(line)
    return out


def _join_records(lines: list[str]) -> list[str]:
    records: list[str] = []
    for line in lines:
        if records and _is_continuation(line):
            records[-1] = records[-1] + "\n" + line
        else:
            records.append(line)
    return records


def clean_log(raw: str, *, keep_post_cleanup: bool = False) -> CleanResult:
    """Clean a captured job log. Order is mandatory (spec Stage 4)."""
    bytes_raw = len(raw.encode("utf-8"))
    physical = _physical_lines(raw)
    stripped: list[str] = []
    for line in physical:
        line = _strip_timestamp(line)
        line = _strip_ansi(line)
        line = _strip_cr(line)
        stripped.append(line)

    setup = _extract_setup(stripped)
    stripped = _drop_post_section(stripped, keep_post_cleanup=keep_post_cleanup)

    kept: list[str] = []
    for line in stripped:
        if _is_noise(line, keep_post_cleanup=keep_post_cleanup):
            continue
        kept.append(line)

    records = _join_records(kept)
    return CleanResult(
        lines=records,
        lines_raw=len(physical),
        lines_clean=len(records),
        bytes_raw=bytes_raw,
        setup=setup,
    )
