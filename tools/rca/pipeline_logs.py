"""Unpack pipeline/docker log artifacts into structured facts. No GitHub imports."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

from .cleaner import clean_log
from .config import (
    ARTIFACT_DOWNLOAD_MAX_BYTES,
    MAX_PIPELINE_LOG_ARTIFACTS,
    PIPELINE_ARTIFACT_NAME_RE,
    PIPELINE_PHASE_TOKENS,
    PIPELINE_STAGE_MAP,
    PIPELINE_STREAM_MAX_BYTES,
    PIPELINE_STREAM_MAX_LINES,
)
from .drain_index import novelty_in_memory
from .extract import extract_from_lines
from .models import ArtifactInfo, LastGreenCompare, LogTemplate, PipelineLogStream
from .redact import redact_text

_NAME_RE = re.compile(PIPELINE_ARTIFACT_NAME_RE, re.IGNORECASE)
_TEXT_SUFFIXES = {".log", ".txt"}
_SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".webm",
    ".mov",
    ".avi",
    ".har",
    ".trace",
    ".zip",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".wasm",
    ".woff",
    ".woff2",
    ".ttf",
    ".pdf",
    ".pyc",
    ".whl",
    ".jar",
    ".mp3",
    ".wav",
    ".pb",
}
_STAGE_PRIORITY = {"build": 0, "test": 1, "e2e": 2, "lint": 3}
_PHASE_SPLIT = re.compile(r"[^a-z0-9]+")


def pipeline_phase(file_name: str | None, artifact_name: str | None = None) -> str | None:
    """Coarse phase of a docker sub-step log from its file (then artifact) name.

    setup / teardown when a known token appears, "run" for any other named
    stream, None for an empty/odd name. Pure string work; never raises.
    """
    for candidate in (file_name, artifact_name):
        base = (candidate or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if "." in base:
            base = base.rsplit(".", 1)[0]
        tokens = [token for token in _PHASE_SPLIT.split(base) if token]
        if not tokens:
            continue
        for token in tokens:
            phase = PIPELINE_PHASE_TOKENS.get(token)
            if phase:
                return phase
        return "run"
    return None


def match_pipeline_artifact_name(name: str) -> tuple[str, str | None] | None:
    """Return (kind, mapped_stage) when *name* is a pipeline/docker log artifact."""
    stem = (name or "").strip()
    match = _NAME_RE.match(stem)
    if not match:
        return None
    raw_stage = match.groupdict().get("stage")
    lowered = stem.lower()
    if lowered in {"docker-logs", "container-logs"}:
        return "pipeline_logs", None
    stage = PIPELINE_STAGE_MAP.get((raw_stage or "").lower()) if raw_stage else None
    return "pipeline_logs", stage


def artifact_kind_and_stage(name: str) -> tuple[str, str | None]:
    matched = match_pipeline_artifact_name(name)
    if matched is not None:
        return matched
    return "other", None


def rank_pipeline_artifacts(
    names: Sequence[str],
    *,
    failed_stage: str | None = None,
) -> list[str]:
    """Failed-stage first, then build/test/e2e/lint, then name."""

    def _key(name: str) -> tuple[int, int, int, str]:
        matched = match_pipeline_artifact_name(name)
        stage = matched[1] if matched else None
        teardown = 1 if pipeline_phase(name) == "teardown" else 0
        preferred = 0 if failed_stage and stage == failed_stage else 1
        return (teardown, preferred, _STAGE_PRIORITY.get(stage or "", 9), name.lower())

    matched = [name for name in names if match_pipeline_artifact_name(name)]
    return sorted(matched, key=_key)


def is_text_member(path: str) -> bool:
    posix = path.replace("\\", "/")
    base = posix.rsplit("/", 1)[-1]
    lowered = base.lower()
    suffix = ""
    if "." in lowered:
        suffix = "." + lowered.rsplit(".", 1)[-1]
    if suffix in _SKIP_SUFFIXES:
        return False
    if suffix in _TEXT_SUFFIXES:
        return True
    if lowered.startswith("docker") and lowered.endswith(".log"):
        return True
    parts = posix.lower().split("/")
    if "logs" in parts and suffix not in _SKIP_SUFFIXES:
        return True
    return False


def parse_pipeline_zip(
    blob: bytes,
    *,
    artifact_name: str,
    stage: str | None = None,
) -> list[PipelineLogStream]:
    """Bytes in, structured streams out. Skips binaries; caps size/lines."""
    streams: list[PipelineLogStream] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return streams
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if not is_text_member(name):
                continue
            try:
                raw = archive.read(info)
            except Exception:  # noqa: BLE001
                continue
            if b"\x00" in raw[:1024]:
                continue
            if len(raw) > PIPELINE_STREAM_MAX_BYTES:
                raw = raw[:PIPELINE_STREAM_MAX_BYTES]
            try:
                text = raw.decode("utf-8-sig", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            if len(text) > PIPELINE_STREAM_MAX_BYTES:
                text = text[:PIPELINE_STREAM_MAX_BYTES]
            stream = _stream_from_text(text, artifact_name=artifact_name, stage=stage, file=name)
            if stream is not None:
                streams.append(stream)
    return streams


def _stream_from_text(
    text: str,
    *,
    artifact_name: str,
    stage: str | None,
    file: str,
) -> PipelineLogStream | None:
    capped = "\n".join(
        line[:4000] for line in text.splitlines()[: PIPELINE_STREAM_MAX_LINES * 2]
    )
    redacted, _n = redact_text(capped)
    cleaned = clean_log(redacted).lines[:PIPELINE_STREAM_MAX_LINES]
    if not cleaned:
        return None
    extracted = extract_from_lines(cleaned)
    novelty = novelty_in_memory(cleaned)
    templates = _drain_subset(novelty.report.templates)
    step_name = file.replace("\\", "/").rsplit("/", 1)[-1] or None
    return PipelineLogStream(
        artifact_name=artifact_name,
        stage=stage,
        file=file,
        phase=pipeline_phase(file, artifact_name),
        step_name=step_name,
        windows=extracted.windows,
        error_lines=extracted.error_lines[:50],
        stack_traces=extracted.stack_traces,
        templates=templates,
    )


def _drain_subset(templates: Sequence[LogTemplate], *, cap: int = 12) -> list[LogTemplate]:
    preferred = [
        item
        for item in templates
        if item.tier in {"T1", "T2", "T3"} or item.has_error_match or item.is_novel
    ]
    chosen = preferred or list(templates)
    return list(chosen[:cap])


def compare_green_fail(
    green_lines: Sequence[str],
    fail_lines: Sequence[str],
    *,
    artifact_name: str | None = None,
) -> LastGreenCompare:
    """In-memory Drain3: train on green, novelty on fail. Not written to .drain."""
    if not green_lines:
        return LastGreenCompare(
            artifact_name=artifact_name,
            available=False,
            skipped_reason="green artifact missing",
        )
    fail_clean = [redact_text(str(line))[0] for line in fail_lines]
    green_clean = [redact_text(str(line))[0] for line in green_lines]
    result = novelty_in_memory(fail_clean, green_lines=green_clean)
    novel = [
        item.template
        for item in result.report.templates
        if item.is_novel is True
    ]
    missing = [
        item.template
        for item in result.report.templates
        if item.anomaly in {"missing", "depleted"} or item.count == 0
    ]
    present = [
        item.template
        for item in result.report.templates
        if item.baseline_count
    ]
    return LastGreenCompare(
        artifact_name=artifact_name,
        novel_templates=novel,
        missing_on_fail=missing,
        present_on_green=present,
        available=True,
    )


def parse_pipeline_zip_file(path: Path, *, artifact_name: str | None = None) -> list[PipelineLogStream]:
    name = artifact_name or path.stem
    matched = match_pipeline_artifact_name(name)
    stage = matched[1] if matched else None
    return parse_pipeline_zip(path.read_bytes(), artifact_name=name, stage=stage)


def select_pipeline_names(
    artifacts: Iterable[ArtifactInfo | dict[str, object]],
    *,
    failed_stage: str | None = None,
    limit: int = MAX_PIPELINE_LOG_ARTIFACTS,
) -> list[str]:
    names: list[str] = []
    for item in artifacts:
        if isinstance(item, ArtifactInfo):
            name, size, expired = item.name, item.size_bytes, item.expired
        else:
            name = str(item.get("name") or "")
            size = int(item.get("size_bytes") or item.get("size_in_bytes") or 0)
            expired = bool(item.get("expired"))
        if not name or expired:
            continue
        if match_pipeline_artifact_name(name) is None:
            continue
        if size > ARTIFACT_DOWNLOAD_MAX_BYTES:
            continue
        names.append(name)
    return rank_pipeline_artifacts(names, failed_stage=failed_stage)[:limit]
