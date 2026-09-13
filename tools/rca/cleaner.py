"""Strip GitHub log chrome and join multi-line records.

Source-agnostic: operates on raw text only. Does not import GitHub clients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BOM = "\ufeff"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_NOISE_PREFIXES = (
    "##[group]",
    "##[endgroup]",
    "##[debug]",
    "Download action repository",
    "Cleaning up orphan processes",
)

_CONTINUATION = re.compile(
    r"^(?:"
    r"\s+at "  # V8 / JS, leading whitespace required
    r"|\tat "  # Java
    r'|\s+File "'  # Python; must not match a bare path
    r"|\s*Caused by:"
    r"|\s*\.\.\. \d+ more"
    r")"
)
_COMMAND_HEADER = re.compile(
    r"^(?:shell:|##\[command\]|##\[group\]Run\b)",
    re.IGNORECASE,
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
_RUN_START = re.compile(r"^##\[(?:group\]Run |command\])")
_SETUP_GROUPS = frozenset(
    {
        "operating system",
        "runner image",
        "runner image provisioner",
        "github_token permissions",
        "hosted compute agent",
        "set up job",
    }
)
_SETUP_LINE = re.compile(
    r"^(?:Current runner version:|Runner name:|Runner group name:|"
    r"Machine name:|Secret source:|Prepare workflow|Prepare all required|"
    r"Getting action download|Complete job name:|Azure Region:|"
    r"Hosted Compute Agent|Included Software:|Image Release:)",
    re.IGNORECASE,
)


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
    if raw.startswith(_BOM):
        raw = raw[1:]
    # Split on newline only so CR progress redraws stay on one physical line.
    parts = raw.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _strip_bom(line: str) -> str:
    if line.startswith(_BOM):
        return line[1:]
    return line


def _strip_timestamp(line: str) -> str:
    return _TIMESTAMP.sub("", line, count=1)


def _strip_ansi(line: str) -> str:
    return _ANSI.sub("", line)


def _strip_cr(line: str) -> str:
    if "\r" in line:
        return line.split("\r")[-1]
    return line


def _prefix_strip(line: str) -> str:
    """BOM and GitHub timestamp first, then ANSI / CR."""
    line = _strip_bom(line)
    line = _strip_timestamp(line)
    line = _strip_bom(line)
    line = _strip_ansi(line)
    line = _strip_cr(line)
    return line


def _is_noise(line: str, *, keep_post_cleanup: bool) -> bool:
    stripped = line.lstrip()
    for prefix in _NOISE_PREFIXES:
        if stripped.startswith(prefix) or line.startswith(prefix):
            return True
    if stripped.startswith("Post job cleanup") or line.startswith("Post job cleanup"):
        return not keep_post_cleanup
    return False


def _is_continuation(line: str, prev: str | None) -> bool:
    if _CONTINUATION.match(line) is None:
        return False
    if prev is not None and _COMMAND_HEADER.match(prev.lstrip()):
        return False
    return True


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


def _drop_setup_preamble(lines: list[str]) -> list[str]:
    """Keep image/OS/disk in SetupFacts; drop the rest of the Set up job chrome."""
    for index, line in enumerate(lines):
        if _RUN_START.match(line.lstrip()):
            return lines[index:]
    return _drop_named_setup_groups(lines)


def _drop_named_setup_groups(lines: list[str]) -> list[str]:
    out: list[str] = []
    skipping = False
    for line in lines:
        group = re.match(r"^##\[group\](.*)$", line.strip())
        if group:
            title = group.group(1).strip().lower()
            skipping = title in _SETUP_GROUPS
            continue
        if _ENDGROUP.match(line) or line.startswith("##[endgroup]"):
            skipping = False
            continue
        if skipping:
            continue
        if _SETUP_LINE.match(line.lstrip()):
            continue
        out.append(line)
    return out


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
        prev = records[-1].rsplit("\n", 1)[-1] if records else None
        if records and _is_continuation(line, prev):
            records[-1] = records[-1] + "\n" + line
        else:
            records.append(line)
    return records


def clean_log(raw: str, *, keep_post_cleanup: bool = False) -> CleanResult:
    """Clean a captured job log. Order is mandatory (spec Stage 4)."""
    bytes_raw = len(raw.encode("utf-8"))
    physical = _physical_lines(raw)
    stripped: list[str] = [_prefix_strip(line) for line in physical]

    setup = _extract_setup(stripped)
    stripped = _drop_setup_preamble(stripped)
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
