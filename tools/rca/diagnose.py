"""Deterministic RCA rules R1–R18. Pure functions on Summary. No HTTP, no LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .classify import (
    ClassificationHit,
    _has_timeout_signature,
    _near_workflow_timeout,
    _side,
    classify_lines,
    classify_union,
)
from .config import CLASSIFY_RULES, RUNNER_FAILURE_PATTERNS
from .extract import extract_source_paths
from .models import (
    ChangeContext,
    DeterministicDiagnosis,
    FailedJob,
    LogTemplate,
    PipelineLogStream,
    Summary,
    Verdict,
)

SKIP_CODE_FETCH_CATEGORIES = frozenset(
    {
        "oom",
        "timeout",
        "disk_space",
        "image_pull",
        "auth",
        "network_dns",
        "dependency",
    }
)
SKIP_CODE_FETCH_CLASSES = frozenset(
    {"lockfile", "ci_config", "container", "dependency", "build_config"}
)
BLAST_RADIUS_CLASSES = SKIP_CODE_FETCH_CLASSES
APP_SOURCE_DIRS = frozenset({"src", "lib", "app", "pkg"})
_ROLLBACK_PRIORITY = (
    "lockfile",
    "dependency",
    "ci_config",
    "container",
    "build_config",
)
_ROLLBACK_ONE_LINERS = {
    "lockfile": "Roll back the lockfile / pin the bumped package; do not chase app code.",
    "dependency": "Manifest/lockfile resolution failed; revert the dependency bump.",
    "ci_config": "Inspect workflow/action changes in this range; revert the pipeline edit.",
    "container": "Base image / Dockerfile changed; compare digest to last green.",
    "build_config": "Inspect workflow/action changes in this range; revert the pipeline edit.",
}
_INFRA_CONFIG_CATEGORIES = frozenset({"dependency", "image_pull", "auth"})
_FIX_BY_RULE = {
    "R1": "Retry the job; if this is widespread, check runner/GitHub status.",
    "R2": "Treat as infrastructure; wait or retry after checking GitHub/runner health.",
    "R3": "Re-run the job; the same SHA previously succeeded.",
    "R4": "Raise timeout-minutes or fix the hang/retry loop.",
    "R5": "Raise the job/container memory limit; this is not a runner infra OOM.",
    "R6": "Free disk space or use a larger runner disk.",
    "R7": "Fix image name/registry auth; compare digest to last green.",
    "R8": "Roll back the lockfile / pin the bumped package; do not chase app code.",
    "R9": "Fix the compile error in the suspected source file(s).",
    "R10": "Fix the failing test assertion.",
    "R11": "Stop the retry storm; check for a hang or flaky network.",
    "R12": "Investigate the novel error template that replaced healthy traffic.",
    "R13": "Diff the novel pipeline/docker template against last green.",
    "R14": "Inspect workflow/action or Dockerfile changes; revert the pipeline edit.",
    "R15": "Reuse the previous resolution recorded in history.",
    "R16": "Treat as flaky; the same fingerprint failed on another branch.",
    "R17": "Diagnose from the pipeline/docker log, not the job tail.",
    "R18": "Inspect the collector evidence bundle and compare with last green.",
}
_FIX_PREFIX = (
    "roll back",
    "inspect ",
    "revert ",
    "raise ",
    "retry ",
    "fix ",
    "treat ",
    "re-run",
    "stop ",
    "diff ",
    "free ",
    "pin ",
    "reuse ",
)
_LOCK_OR_MANIFEST = frozenset({"lockfile", "dependency"})
_CONFIG_ONLY = frozenset({"ci_config", "container"})
_SHORT_CIRCUITS = frozenset(
    {"infra_runner", "infra_widespread", "flake_same_sha_passed", "no_failed_jobs"}
)
_RETRY_RE = re.compile(
    r"retry|attempt|waiting|reconnect|backoff|hang",
    re.IGNORECASE,
)
_GENERIC_ASSERT_RE = re.compile(
    r"(expected\s+(true|false)\s+to\s+be\s+(true|false)"
    r"|assert(?:ion)?(?:\s*error)?\s*$"
    r"|expected true to be false"
    r"|expected false to be true)",
    re.IGNORECASE,
)
_EXIT_ONLY_RE = re.compile(
    r"(?:##\[error\])?\s*Process completed with exit code \d+\.?\s*$",
    re.IGNORECASE,
)
_NOISE_LINE_RE = re.compile(
    r"^(?:Post job cleanup|Cleaning up orphan processes|Terminate orphan process|"
    r"##\[endgroup\]|##\[group\].*)\s*$",
    re.IGNORECASE,
)
_IMAGE_OR_WORKFLOW_RE = re.compile(
    r"image|docker|container|workflow|registry|manifest|pull access",
    re.IGNORECASE,
)
_RUNNER_RES = [re.compile(p, re.IGNORECASE) for p in RUNNER_FAILURE_PATTERNS]
_RULE_RES = [
    (rule["category"], re.compile(rule["pattern"], re.IGNORECASE), rule["confidence"])
    for rule in CLASSIFY_RULES
]
_OOM_RE = [
    compiled
    for category, compiled, _conf in _RULE_RES
    if category == "oom"
]


@dataclass
class DeterministicVerdict:
    category: str
    confidence: str
    is_infra_vs_code: str
    requires_analysis: bool
    rule_id: str
    one_liner: str
    suspected_stage: str | None = None
    suspected_files: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    is_flaky: bool = False
    short_circuit: str | None = None
    matched_stream: str | None = None
    winning_stream_id: str | None = None
    matched_pattern: str | None = None
    matched_line: int | None = None
    other_matches: list[str] = field(default_factory=list)
    fix_one_liner: str | None = None


def diagnose(summary: Summary) -> DeterministicVerdict:
    """First matching rule wins. R18 is the residual that still needs STGPT."""
    ignore_job = _job_logs_are_exit_only(summary) and _pipeline_has_first_error(summary)
    hit = _union_hit(summary, ignore_job=ignore_job)
    stage = _suspected_stage(summary, hit)
    files_from_logs = _error_paths(summary, ignore_job=ignore_job)

    def finish(verdict: DeterministicVerdict) -> DeterministicVerdict:
        return enrich_display(summary, refine_blast_radius(summary, verdict))

    r1 = _rule_short_circuits(summary)
    if r1 is not None:
        return finish(r1)

    r4 = _rule_timeout(summary, hit, ignore_job=ignore_job)
    if r4 is not None:
        return finish(r4)

    r5 = _rule_oom(summary, hit, ignore_job=ignore_job)
    if r5 is not None:
        return finish(r5)

    r6 = _rule_disk(summary, hit)
    if r6 is not None:
        return finish(r6)

    r7 = _rule_image_pull(summary, hit)
    if r7 is not None:
        return finish(r7)

    r8 = _rule_dependency_lockfile(summary, hit)
    if r8 is not None:
        return finish(r8)

    r9 = _rule_compile(summary, hit, files_from_logs, ignore_job=ignore_job)
    if r9 is not None:
        return finish(r9)

    r10 = _rule_junit(summary)
    if r10 is not None:
        return finish(r10)

    r11 = _rule_flooding(summary, hit)
    if r11 is not None:
        return finish(r11)

    r12 = _rule_depleted(summary, hit)
    if r12 is not None:
        return finish(r12)

    r13 = _rule_last_green(summary)
    if r13 is not None:
        return finish(r13)

    r14 = _rule_config_only(summary, hit)
    if r14 is not None:
        return finish(r14)

    r15 = _rule_history_resolution(summary, hit)
    if r15 is not None:
        return finish(r15)

    r16 = _rule_history_cross_branch(summary, hit)
    if r16 is not None:
        return finish(r16)

    r17 = _rule_pipeline_over_job(summary, hit, ignore_job=ignore_job, stage=stage)
    if r17 is not None:
        return finish(r17)

    return finish(_rule_r18(summary, hit, stage, files_from_logs))


def apply_verdict(summary: Summary, verdict: DeterministicVerdict) -> Summary:
    """Copy a verdict onto Summary.verdict / classification / diagnosis."""
    short = verdict.short_circuit if verdict.short_circuit in _SHORT_CIRCUITS else None
    summary.verdict = Verdict(
        short_circuit=short,  # type: ignore[arg-type]
        requires_analysis=verdict.requires_analysis,
        reason=f"{verdict.rule_id}: {verdict.one_liner}",
    )
    confidence = verdict.confidence if verdict.confidence in ("high", "medium", "low") else "low"
    side = (
        verdict.is_infra_vs_code
        if verdict.is_infra_vs_code in ("infra", "code", "unknown")
        else "unknown"
    )
    summary.classification.category = verdict.category
    summary.classification.confidence = confidence  # type: ignore[assignment]
    summary.classification.is_infra_vs_code = side  # type: ignore[assignment]
    summary.classification.is_flaky = bool(verdict.is_flaky or summary.classification.is_flaky)
    if verdict.matched_pattern is not None:
        summary.classification.matched_pattern = verdict.matched_pattern
    if verdict.matched_line is not None:
        summary.classification.matched_line = verdict.matched_line
    if verdict.other_matches:
        summary.classification.other_matches = list(verdict.other_matches)
    summary.classification.matched_stream = verdict.matched_stream
    summary.diagnosis = DeterministicDiagnosis(
        rule_id=verdict.rule_id,
        one_liner=verdict.one_liner,
        suspected_stage=verdict.suspected_stage,
        suspected_files=list(verdict.suspected_files),
        citations=list(verdict.citations),
        winning_stream_id=verdict.winning_stream_id,
        fix_one_liner=verdict.fix_one_liner,
    )
    if is_blast_radius_skip(summary):
        classes = sorted(set(summary.changes.classes or []))  # type: ignore[union-attr]
        note = f"skipped source fetch: changes.classes={classes}"
        if note not in summary.collection_notes:
            summary.collection_notes.append(note)
    return summary


def fix_for_rule(rule_id: str | None) -> str:
    if not rule_id:
        return "Inspect the collector evidence; compare with last green."
    return _FIX_BY_RULE.get(
        rule_id, "Inspect the collector evidence; compare with last green."
    )


def looks_like_fix(text: str | None) -> bool:
    lowered = (text or "").strip().lower()
    return bool(lowered) and any(lowered.startswith(prefix) for prefix in _FIX_PREFIX)


def enrich_display(summary: Summary, verdict: DeterministicVerdict) -> DeterministicVerdict:
    """Fill fix_one_liner and citation quotes without changing the winning rule."""
    if not verdict.fix_one_liner:
        if looks_like_fix(verdict.one_liner):
            verdict.fix_one_liner = verdict.one_liner
        else:
            verdict.fix_one_liner = _FIX_BY_RULE.get(verdict.rule_id) or (
                "Inspect the collector evidence; compare with last green."
            )
    if not verdict.citations:
        verdict.citations = display_citation_quotes(summary, extra=verdict.one_liner)
    return verdict


def display_citation_quotes(summary: Summary, *, extra: str | None = None) -> list[str]:
    """Quotes already present on the summary; never invent file paths."""
    quotes: list[str] = []

    def _add(text: str | None) -> None:
        item = (text or "").strip()
        if not item or item in quotes:
            return
        quotes.append(item[:240])

    _add(extra)
    if summary.diagnosis is not None and summary.diagnosis.one_liner:
        _add(summary.diagnosis.one_liner)
    for job in summary.failed_jobs:
        for err in job.error_lines[:2]:
            _add(err.text)
        for window in job.windows:
            if window.label in {"first_error", "merged"} and window.content:
                line = next((ln for ln in window.content.splitlines() if ln.strip()), "")
                _add(line)
                break
    for stream in summary.pipeline_logs:
        for window in stream.windows:
            if window.content:
                line = next((ln for ln in window.content.splitlines() if ln.strip()), "")
                _add(line)
                break
    if summary.changes is not None:
        from .changes import classify_path

        for path in summary.changes.files:
            if classify_path(path) in {"lockfile", "dependency", "ci_config", "container"}:
                _add(path)
    if summary.history is not None and summary.history.last_success_sha:
        _add(summary.history.last_success_sha)
    if summary.last_green_compare is not None:
        for tmpl in summary.last_green_compare.novel_templates[:2]:
            _add(tmpl)
    return quotes[:6]


def refine_blast_radius(
    summary: Summary, verdict: DeterministicVerdict
) -> DeterministicVerdict:
    """Rewrite rollback one-liners when the diff is only config/deps/image."""
    if not is_blast_radius_skip(summary):
        return verdict
    classes = set((summary.changes.classes if summary.changes else []) or [])
    message = rollback_one_liner(classes)
    if message:
        verdict.one_liner = message
    if verdict.category in _INFRA_CONFIG_CATEGORIES:
        verdict.requires_analysis = False
    return verdict


def rollback_one_liner(classes: set[str]) -> str | None:
    for name in _ROLLBACK_PRIORITY:
        if name in classes:
            return _ROLLBACK_ONE_LINERS[name]
    return None


def is_blast_radius_skip(summary: Summary) -> bool:
    if summary.changes is None:
        return False
    classes = set(summary.changes.classes or [])
    if not classes or not classes <= BLAST_RADIUS_CLASSES:
        return False
    return not application_stack_in_diff(summary)


def is_app_source_path(path: str) -> bool:
    posix = path.replace("\\", "/").lstrip("./")
    parts = posix.split("/")
    return any(part in APP_SOURCE_DIRS for part in parts[:-1])


def application_stack_in_diff(summary: Summary) -> list[tuple[str, int | None]]:
    changed = _changed_files(summary.changes)
    if not changed:
        return []
    hits: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for path, line in _stack_paths(summary):
        if not is_app_source_path(path) or not _intersects(path, changed):
            continue
        key = path.replace("\\", "/")
        if key in seen:
            continue
        seen.add(key)
        hits.append((path, line))
    return hits


def stack_names_source(summary: Summary) -> bool:
    from .changes import classify_path

    for path, _line in _stack_paths(summary):
        if is_app_source_path(path) or classify_path(path) in {"source", "test"}:
            return True
    return False


def should_fetch_code_context(summary: Summary, verdict: DeterministicVerdict) -> bool:
    if application_stack_in_diff(summary):
        if verdict.category in SKIP_CODE_FETCH_CATEGORIES and not stack_names_source(summary):
            return False
        return True
    if is_blast_radius_skip(summary):
        return False
    if verdict.category in SKIP_CODE_FETCH_CATEGORIES and not stack_names_source(summary):
        return False
    if verdict.is_infra_vs_code != "code":
        return False
    if not verdict.requires_analysis:
        return False
    return bool(code_paths_for_fetch(summary, verdict))


def first_failing_commit_files(summary: Summary) -> list[str]:
    hist = summary.history
    if hist is None or not hist.first_failing_sha or summary.changes is None:
        return []
    target = hist.first_failing_sha
    for commit in summary.changes.commits:
        if _sha_match(commit.sha, target):
            return list(commit.files or [])
    return []


def _sha_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    a, b = left.lower(), right.lower()
    return a == b or a.startswith(b) or b.startswith(a)


def code_paths_for_fetch(
    summary: Summary, verdict: DeterministicVerdict | None = None
) -> list[tuple[str, int | None]]:
    """Prefer first-failing-commit files, then the full compare range. Max 3 later."""
    app_hits = application_stack_in_diff(summary)
    classes = set((summary.changes.classes if summary.changes else []) or [])
    if app_hits and classes <= BLAST_RADIUS_CLASSES:
        return app_hits

    ordered: list[tuple[str, int | None]] = []
    seen: set[str] = set()

    def _add(path: str, line: int | None) -> None:
        key = path.replace("\\", "/").lstrip("./")
        if not key or key in seen:
            return
        seen.add(key)
        ordered.append((key, line))

    first_files = first_failing_commit_files(summary)
    stack = _stack_paths(summary)
    errors = _error_paths(summary, ignore_job=False)
    changed = _changed_files(summary.changes)
    pool = first_files or changed

    for path, line in stack:
        if not pool or _intersects(path, pool):
            _add(path, line)
    for path, line in errors:
        if first_files and _intersects(path, first_files):
            _add(path, line)
    if verdict is not None:
        for path in verdict.suspected_files:
            if first_files and _intersects(path, first_files):
                _add(path, None)
            elif not first_files:
                _add(path, None)
    for path in first_files:
        _add(path, None)
    if not ordered:
        for path, line in errors:
            if changed and _intersects(path, changed):
                _add(path, line)
        if verdict is not None:
            for path in verdict.suspected_files:
                _add(path, None)
        for path in changed:
            _add(path, None)
    return ordered


def _v(
    rule_id: str,
    one_liner: str,
    *,
    category: str,
    confidence: str = "high",
    is_infra_vs_code: str = "unknown",
    requires_analysis: bool = False,
    short_circuit: str | None = None,
    is_flaky: bool = False,
    suspected_stage: str | None = None,
    suspected_files: Sequence[str] | None = None,
    citations: Sequence[str] | None = None,
    matched_stream: str | None = None,
    winning_stream_id: str | None = None,
    matched_pattern: str | None = None,
    matched_line: int | None = None,
    other_matches: Sequence[str] | None = None,
) -> DeterministicVerdict:
    return DeterministicVerdict(
        category=category,
        confidence=confidence,
        is_infra_vs_code=is_infra_vs_code,
        requires_analysis=requires_analysis,
        rule_id=rule_id,
        one_liner=one_liner,
        suspected_stage=suspected_stage,
        suspected_files=list(suspected_files or []),
        citations=list(citations or []),
        is_flaky=is_flaky,
        short_circuit=short_circuit,
        matched_stream=matched_stream,
        winning_stream_id=winning_stream_id or matched_stream,
        matched_pattern=matched_pattern,
        matched_line=matched_line,
        other_matches=list(other_matches or []),
    )


def _from_hit(
    rule_id: str,
    one_liner: str,
    hit: ClassificationHit,
    **kwargs: object,
) -> DeterministicVerdict:
    payload = dict(
        category=hit.category,
        confidence=hit.confidence,
        is_infra_vs_code=hit.is_infra_vs_code,
        matched_stream=hit.matched_stream,
        winning_stream_id=hit.matched_stream,
        matched_pattern=hit.matched_pattern,
        matched_line=hit.matched_line,
        other_matches=list(hit.other_matches),
        is_flaky=hit.is_flaky,
    )
    payload.update(kwargs)
    return _v(rule_id, one_liner, **payload)  # type: ignore[arg-type]


_R1_OVERLAP = ("no space left", "pull access denied", "enospc")


def _rule_short_circuits(summary: Summary) -> DeterministicVerdict | None:
    v = summary.verdict
    c = summary.classification
    if v.short_circuit == "no_failed_jobs":
        return _v(
            "no_failed_jobs",
            v.reason or "no failed jobs",
            category="unknown",
            confidence="low",
            requires_analysis=False,
            short_circuit="no_failed_jobs",
        )
    if v.short_circuit == "infra_runner" or c.category == "infra_runner":
        pattern = (c.matched_pattern or v.reason or "").lower()
        if any(token in pattern for token in _R1_OVERLAP):
            return None
        if _search_category(summary, "disk_space") or _search_category(summary, "image_pull"):
            return None
        return _v(
            "R1",
            v.reason or "runner infrastructure failure",
            category="infra_runner",
            confidence="high",
            is_infra_vs_code="infra",
            requires_analysis=False,
            short_circuit="infra_runner",
            matched_pattern=c.matched_pattern,
            matched_line=c.matched_line,
        )
    if v.short_circuit == "infra_widespread" or _widespread(summary):
        return _v(
            "R2",
            "failures across multiple workflows and branches",
            category="infra_runner",
            confidence="high",
            is_infra_vs_code="infra",
            requires_analysis=False,
            short_circuit="infra_widespread",
        )
    if v.short_circuit == "flake_same_sha_passed":
        return _v(
            "R3",
            v.reason or "same SHA previously succeeded for this job",
            category=c.category or "unknown",
            confidence="high",
            is_infra_vs_code=c.is_infra_vs_code or "unknown",
            requires_analysis=False,
            short_circuit="flake_same_sha_passed",
            is_flaky=True,
            matched_pattern=c.matched_pattern,
            matched_line=c.matched_line,
        )
    return None


def _widespread(summary: Summary) -> bool:
    run = summary.run
    branches = [b for b in (run.concurrent_branches or []) if b]
    return run.concurrent_failures >= 3 and len(set(branches)) >= 2


def _rule_timeout(
    summary: Summary, hit: ClassificationHit, *, ignore_job: bool
) -> DeterministicVerdict | None:
    if hit.category == "timeout":
        return _from_hit(
            "R4",
            _one_liner_from_hit(hit, "job or pipeline log matched a timeout signature"),
            hit,
            is_infra_vs_code="infra",
            requires_analysis=False,
            suspected_stage=_suspected_stage(summary, hit),
        )
    for job in summary.failed_jobs:
        raw = "" if ignore_job else _job_text(job)
        if _has_timeout_signature(
            raw,
            conclusion=None,
            step_conclusion=None,
            duration_seconds=job.duration_seconds,
            timeout_minutes=job.timeout_minutes,
        ) or _near_workflow_timeout(job.duration_seconds, job.timeout_minutes):
            return _v(
                "R4",
                "job duration reached timeout-minutes (or timeout signature in the log)",
                category="timeout",
                confidence="high",
                is_infra_vs_code="infra",
                requires_analysis=False,
                suspected_stage=job.failed_step_name,
                winning_stream_id=_job_stream_id(job),
            )
    return None


def _rule_oom(
    summary: Summary, hit: ClassificationHit, *, ignore_job: bool
) -> DeterministicVerdict | None:
    if hit.category != "oom" and not _text_matches(_all_text(summary, ignore_job), _OOM_RE):
        return None
    if _runner_patterns_present(summary, ignore_job=ignore_job):
        return None
    stream = hit.matched_stream if hit.category == "oom" else _first_pipeline_stream_id(summary)
    return _v(
        "R5",
        _one_liner_from_hit(hit, "out-of-memory in pipeline/job log; runner patterns absent")
        if hit.category == "oom"
        else "out-of-memory in pipeline/job log; runner patterns absent",
        category="oom",
        confidence="high",
        is_infra_vs_code="code",
        requires_analysis=False,
        suspected_stage=_suspected_stage(summary, hit),
        matched_stream=stream,
        winning_stream_id=stream,
        matched_pattern=hit.matched_pattern if hit.category == "oom" else None,
    )


def _rule_disk(summary: Summary, hit: ClassificationHit) -> DeterministicVerdict | None:
    if hit.category != "disk_space" and not _search_category(summary, "disk_space"):
        return None
    return _from_hit(
        "R6",
        _one_liner_from_hit(hit, "disk full (ENOSPC / no space left on device)"),
        hit if hit.category == "disk_space" else ClassificationHit(
            category="disk_space",
            confidence="high",
            is_infra_vs_code="infra",
        ),
        category="disk_space",
        is_infra_vs_code="infra",
        requires_analysis=False,
        suspected_stage=_suspected_stage(summary, hit),
    )


def _rule_image_pull(summary: Summary, hit: ClassificationHit) -> DeterministicVerdict | None:
    dockerish = _docker_stream_hit(summary) or _text_is_registry(summary)
    if hit.category == "image_pull" or (
        hit.category == "auth" and dockerish
    ) or _search_category(summary, "image_pull"):
        category = "image_pull"
        stream = hit.matched_stream
        if stream is None:
            stream = _first_docker_stream_id(summary)
        return _v(
            "R7",
            _one_liner_from_hit(hit, "image pull / registry 401/403 in docker log"),
            category=category,
            confidence="high",
            is_infra_vs_code="infra",
            requires_analysis=False,
            suspected_stage=_suspected_stage(summary, hit) or "build",
            matched_stream=stream,
            winning_stream_id=stream,
            matched_pattern=hit.matched_pattern,
            matched_line=hit.matched_line,
        )
    return None


def _rule_dependency_lockfile(
    summary: Summary, hit: ClassificationHit
) -> DeterministicVerdict | None:
    if hit.category != "dependency" and not _search_category(summary, "dependency"):
        return None
    classes = set((summary.changes.classes if summary.changes else []) or [])
    if not (classes & _LOCK_OR_MANIFEST):
        return None
    files = [
        path
        for path in _changed_files(summary.changes)
        if _path_class(path) in _LOCK_OR_MANIFEST
    ]
    return _from_hit(
        "R8",
        _one_liner_from_hit(
            hit,
            "dependency resolution failed and a lockfile/manifest changed",
        ),
        hit if hit.category == "dependency" else ClassificationHit(
            category="dependency",
            confidence="high",
            is_infra_vs_code="code",
        ),
        category="dependency",
        is_infra_vs_code="code",
        requires_analysis=False,
        suspected_files=files,
        suspected_stage=_suspected_stage(summary, hit),
    )


def _rule_compile(
    summary: Summary,
    hit: ClassificationHit,
    files_from_logs: list[tuple[str, int | None]],
    *,
    ignore_job: bool,
) -> DeterministicVerdict | None:
    compile_hit = hit.category == "compile" or _search_category(summary, "compile")
    if not compile_hit:
        return None
    in_build_pipeline = any(
        (stream.stage == "build") and _stream_has_compile(stream)
        for stream in summary.pipeline_logs
    )
    if not in_build_pipeline:
        return None
    changed = _changed_files(summary.changes)
    log_paths = [path for path, _line in files_from_logs]
    if changed:
        intersection = [path for path in log_paths if _intersects(path, changed)]
    else:
        intersection = list(dict.fromkeys(log_paths))
    stack = _stack_paths(summary)
    single_stack = len({path for path, _line in stack}) == 1
    if in_build_pipeline and changed and not intersection:
        return None
    suspected = intersection or [path for path, _line in stack] or log_paths
    suspected = list(dict.fromkeys(suspected))
    needs_ai = len(suspected) > 1 and not single_stack
    stream = hit.matched_stream
    if in_build_pipeline:
        stream = stream or _first_build_stream_id(summary)
    return _v(
        "R9",
        _one_liner_from_hit(hit, "compile error intersects changed source"),
        category="compile",
        confidence="high" if hit.confidence == "high" or in_build_pipeline else "medium",
        is_infra_vs_code="code",
        requires_analysis=needs_ai,
        suspected_files=suspected,
        suspected_stage="build",
        matched_stream=stream,
        winning_stream_id=stream,
        matched_pattern=hit.matched_pattern if hit.category == "compile" else None,
        citations=suspected[:3],
    )


def _rule_junit(summary: Summary) -> DeterministicVerdict | None:
    report = summary.junit
    if report is None or not report.failures:
        return None
    if report.total_failures > 5 and len(report.failures) > 5:
        return None
    if len(report.failures) > 5:
        return None
    if not any((item.message or item.body) for item in report.failures):
        return None
    names = [item.name for item in report.failures if item.name]
    messages = [(item.message or item.body or "").strip() for item in report.failures]
    generic = all(_GENERIC_ASSERT_RE.search(msg or "") or not msg for msg in messages)
    specific = (not generic) and any(messages)
    hunks = bool(summary.code_context and summary.code_context.hunks)
    if specific or (generic and not hunks):
        one = messages[0] if len(messages) == 1 else f"{len(report.failures)} tests failed"
        if names and len(names) == 1 and messages[0]:
            one = f"{names[0]}: {messages[0]}"
        return _v(
            "R10",
            one,
            category="test_failure",
            confidence="high",
            is_infra_vs_code="code",
            requires_analysis=False,
            suspected_stage="test",
            citations=names[:3],
        )
    if generic and hunks:
        return _v(
            "R10",
            "generic assertion; inspect code_context hunk",
            category="test_failure",
            confidence="medium",
            is_infra_vs_code="code",
            requires_analysis=True,
            suspected_stage="test",
            suspected_files=[hunk.path for hunk in summary.code_context.hunks],  # type: ignore[union-attr]
        )
    return None


def _rule_flooding(summary: Summary, hit: ClassificationHit) -> DeterministicVerdict | None:
    flooding = [
        tmpl
        for tmpl in _all_templates(summary)
        if tmpl.anomaly == "flooding"
        or (
            tmpl.baseline_count is not None
            and tmpl.baseline_count > 0
            and tmpl.count > tmpl.baseline_count * 10
            and tmpl.count >= 50
        )
    ]
    if not flooding:
        return None
    retryish = any(_RETRY_RE.search(tmpl.template or "") for tmpl in flooding) or any(
        _RETRY_RE.search(tmpl.representative_line or "") for tmpl in flooding
    )
    if not retryish:
        return None
    category = "timeout" if hit.category == "timeout" else "crash"
    if hit.category == "timeout":
        category = "timeout"
    return _v(
        "R11",
        "Drain3 flooding with retry templates (hang / retry storm)",
        category=category,
        confidence="high",
        is_infra_vs_code="code" if category == "crash" else "infra",
        requires_analysis=False,
        citations=[tmpl.template for tmpl in flooding[:3]],
        suspected_stage=_suspected_stage(summary, hit),
    )


def _rule_depleted(summary: Summary, hit: ClassificationHit) -> DeterministicVerdict | None:
    templates = _all_templates(summary)
    depleted = [
        tmpl
        for tmpl in templates
        if tmpl.anomaly in {"depleted", "missing"}
    ]
    novel_error = [
        tmpl
        for tmpl in templates
        if tmpl.is_novel is True and (tmpl.has_error_match or tmpl.tier == "T1")
    ]
    if not depleted or not novel_error:
        return None
    sample = novel_error[0].representative_line or novel_error[0].template
    classified = classify_lines([sample]) if sample else hit
    category = classified.category if classified.category != "unknown" else (
        hit.category if hit.category != "unknown" else "crash"
    )
    return _v(
        "R12",
        f"healthy-traffic template depleted; novel error: {sample[:180]}",
        category=category,
        confidence="high" if classified.confidence == "high" else "medium",
        is_infra_vs_code=_side(category),
        requires_analysis=False,
        citations=[tmpl.template for tmpl in novel_error[:3]],
        matched_pattern=classified.matched_pattern,
        suspected_stage=_suspected_stage(summary, hit),
        winning_stream_id=hit.matched_stream,
    )


def _rule_last_green(summary: Summary) -> DeterministicVerdict | None:
    compare = summary.last_green_compare
    if compare is None or not compare.available or not compare.novel_templates:
        return None
    for template in compare.novel_templates:
        classified = classify_lines([template])
        if classified.category == "unknown":
            continue
        high = classified.confidence == "high"
        return _from_hit(
            "R13",
            f"novel docker/pipeline template matched {classified.category}",
            classified,
            requires_analysis=not high,
            suspected_stage=_stage_from_artifact(compare.artifact_name),
            citations=[template],
            winning_stream_id=(
                f"artifact:{compare.artifact_name}" if compare.artifact_name else None
            ),
        )
    return None


def _rule_config_only(summary: Summary, hit: ClassificationHit) -> DeterministicVerdict | None:
    classes = set((summary.changes.classes if summary.changes else []) or [])
    if not classes or not classes <= _CONFIG_ONLY:
        return None
    blob = _all_text(summary, ignore_job=False)
    related = (
        hit.category in {"image_pull", "auth", "unknown"}
        or bool(_IMAGE_OR_WORKFLOW_RE.search(blob))
    )
    if not related:
        return None
    category = hit.category if hit.category != "unknown" else "image_pull"
    return _v(
        "R14",
        "only ci_config/container changed and the error is image/workflow related",
        category=category,
        confidence="high",
        is_infra_vs_code="infra",
        requires_analysis=False,
        suspected_files=list(_changed_files(summary.changes)),
        suspected_stage=_suspected_stage(summary, hit),
        matched_stream=hit.matched_stream,
        winning_stream_id=hit.matched_stream,
    )


def _rule_history_resolution(
    summary: Summary, hit: ClassificationHit
) -> DeterministicVerdict | None:
    hist = summary.history
    if hist is None or hist.match != "exact" or not hist.previous_resolution:
        return None
    return _from_hit(
        "R15",
        f"exact fingerprint match; reuse resolution: {hist.previous_resolution}",
        hit,
        requires_analysis=False,
        citations=["history"],
        is_flaky=hit.is_flaky or hist.cross_branch,
    )


def _rule_history_cross_branch(
    summary: Summary, hit: ClassificationHit
) -> DeterministicVerdict | None:
    hist = summary.history
    if hist is None or hist.match != "exact" or not hist.cross_branch:
        return None
    return _from_hit(
        "R16",
        "exact fingerprint seen on another branch (flaky)",
        hit,
        requires_analysis=False,
        is_flaky=True,
        citations=["history"],
    )


def _rule_pipeline_over_job(
    summary: Summary,
    hit: ClassificationHit,
    *,
    ignore_job: bool,
    stage: str | None,
) -> DeterministicVerdict | None:
    if not ignore_job:
        return None
    if hit.category == "unknown" or hit.confidence == "low":
        return None
    stream = hit.matched_stream or _first_pipeline_stream_id(summary)
    return _from_hit(
        "R17",
        _one_liner_from_hit(
            hit,
            "job log is only exit code 1; diagnosing from pipeline stream",
        ),
        hit,
        requires_analysis=False,
        suspected_stage=stage or _suspected_stage(summary, hit),
        matched_stream=stream,
        winning_stream_id=stream,
    )


def _rule_r18(
    summary: Summary,
    hit: ClassificationHit,
    stage: str | None,
    files_from_logs: list[tuple[str, int | None]],
) -> DeterministicVerdict:
    files = list(dict.fromkeys(path for path, _line in files_from_logs))
    return _from_hit(
        "R18",
        "insufficient deterministic evidence; packaging a minimal bundle",
        hit,
        requires_analysis=True,
        suspected_stage=stage,
        suspected_files=files[:5],
        is_infra_vs_code=hit.is_infra_vs_code or "unknown",
    )


def _union_hit(summary: Summary, *, ignore_job: bool) -> ClassificationHit:
    streams: list[tuple[str, list[str]]] = []
    for stream in _sorted_pipeline_streams(summary):
        streams.append((_stream_id(stream), _stream_lines(stream)))
    if not ignore_job:
        for job in summary.failed_jobs:
            streams.append((_job_stream_id(job), _job_lines(job)))
    if not streams:
        existing = summary.classification
        return ClassificationHit(
            category=existing.category,
            confidence=existing.confidence,
            matched_pattern=existing.matched_pattern,
            matched_line=existing.matched_line,
            other_matches=list(existing.other_matches),
            is_infra_vs_code=existing.is_infra_vs_code,
            is_flaky=existing.is_flaky,
            matched_stream=existing.matched_stream,
        )
    hit = classify_union(streams)
    if summary.classification.is_flaky:
        hit.is_flaky = True
    return hit


def _sorted_pipeline_streams(summary: Summary) -> list[PipelineLogStream]:
    failed = None
    if summary.failed_jobs:
        failed = _stage_from_step(summary.failed_jobs[0].failed_step_name)
    order = {"build": 0, "test": 1, "e2e": 2, "lint": 3}

    def _key(stream: PipelineLogStream) -> tuple[int, int, str]:
        stage = stream.stage
        preferred = 0 if failed and stage == failed else 1
        return (preferred, order.get(stage or "", 9), stream.file)

    return sorted(summary.pipeline_logs, key=_key)


def _stream_id(stream: PipelineLogStream) -> str:
    return f"artifact:{stream.artifact_name}:{stream.file}"


def _job_stream_id(job: FailedJob) -> str:
    return f"job:{job.name}" if job.name else "job"


def _first_pipeline_stream_id(summary: Summary) -> str | None:
    streams = _sorted_pipeline_streams(summary)
    return _stream_id(streams[0]) if streams else None


def _first_build_stream_id(summary: Summary) -> str | None:
    for stream in summary.pipeline_logs:
        if stream.stage == "build":
            return _stream_id(stream)
    return _first_pipeline_stream_id(summary)


def _first_docker_stream_id(summary: Summary) -> str | None:
    for stream in summary.pipeline_logs:
        lowered = stream.artifact_name.lower()
        if "docker" in lowered or "container" in lowered:
            return _stream_id(stream)
    return _first_pipeline_stream_id(summary)


def _stream_lines(stream: PipelineLogStream) -> list[str]:
    lines: list[str] = []
    for window in stream.windows:
        lines.extend(window.content.splitlines())
    for err in stream.error_lines:
        lines.append(err.text)
    return lines


def _job_lines(job: FailedJob) -> list[str]:
    lines: list[str] = []
    for window in job.windows:
        lines.extend(window.content.splitlines())
    for err in job.error_lines:
        lines.append(err.text)
    return lines


def _job_text(job: FailedJob) -> str:
    return "\n".join(_job_lines(job))


def _all_text(summary: Summary, ignore_job: bool) -> str:
    parts: list[str] = []
    for stream in summary.pipeline_logs:
        parts.append("\n".join(_stream_lines(stream)))
    if not ignore_job:
        for job in summary.failed_jobs:
            parts.append(_job_text(job))
    return "\n".join(parts)


def _job_logs_are_exit_only(summary: Summary) -> bool:
    if not summary.failed_jobs:
        return False
    for job in summary.failed_jobs:
        lines = [ln.strip() for ln in _job_lines(job) if ln.strip()]
        if not lines:
            continue
        informative = [
            ln
            for ln in lines
            if not _EXIT_ONLY_RE.match(ln) and not _NOISE_LINE_RE.match(ln)
        ]
        if informative:
            return False
    return any(summary.failed_jobs)


def _pipeline_has_first_error(summary: Summary) -> bool:
    for stream in summary.pipeline_logs:
        for window in stream.windows:
            if window.label in {"first_error", "merged"} and window.content.strip():
                return True
        if stream.error_lines:
            return True
    return False


def _runner_patterns_present(summary: Summary, *, ignore_job: bool) -> bool:
    blob = _all_text(summary, ignore_job)
    return any(compiled.search(blob) for compiled in _RUNNER_RES)


def _text_matches(text: str, compileds: Sequence[re.Pattern[str]]) -> bool:
    return any(compiled.search(text) for compiled in compileds)


def _search_category(summary: Summary, category: str) -> bool:
    blob = _all_text(summary, ignore_job=False)
    for cat, compiled, _conf in _RULE_RES:
        if cat == category and compiled.search(blob):
            return True
    return False


def _docker_stream_hit(summary: Summary) -> bool:
    for stream in summary.pipeline_logs:
        lowered = f"{stream.artifact_name} {stream.file}".lower()
        if "docker" in lowered or "container" in lowered:
            return True
    return False


def _text_is_registry(summary: Summary) -> bool:
    blob = _all_text(summary, ignore_job=False)
    return bool(
        re.search(r"pull access denied|manifest unknown|denied: requested access", blob, re.I)
    )


def _stream_has_compile(stream: PipelineLogStream) -> bool:
    blob = "\n".join(_stream_lines(stream))
    for cat, compiled, _conf in _RULE_RES:
        if cat == "compile" and compiled.search(blob):
            return True
    return False


def _one_liner_from_hit(hit: ClassificationHit, fallback: str) -> str:
    if hit.matched_pattern:
        return f"{hit.category} matched {hit.matched_pattern}"
    return fallback


def _suspected_stage(summary: Summary, hit: ClassificationHit) -> str | None:
    if hit.matched_stream and hit.matched_stream.startswith("artifact:"):
        for stream in summary.pipeline_logs:
            if _stream_id(stream) == hit.matched_stream:
                return stream.stage
    for stream in _sorted_pipeline_streams(summary):
        if stream.stage:
            return stream.stage
    if summary.failed_jobs:
        return _stage_from_step(summary.failed_jobs[0].failed_step_name)
    return None


def _stage_from_step(name: str | None) -> str | None:
    if not name:
        return None
    lowered = name.lower()
    if any(token in lowered for token in ("build", "compile", "package")):
        return "build"
    if any(token in lowered for token in ("unit", "test")):
        return "test"
    if any(token in lowered for token in ("e2e", "integration")):
        return "e2e"
    if "lint" in lowered:
        return "lint"
    return None


def _stage_from_artifact(name: str | None) -> str | None:
    if not name:
        return None
    lowered = name.lower()
    for token, stage in (
        ("build", "build"),
        ("compile", "build"),
        ("package", "build"),
        ("unit-test", "test"),
        ("test", "test"),
        ("e2e", "e2e"),
        ("lint", "lint"),
    ):
        if token in lowered:
            return stage
    if "docker" in lowered or "container" in lowered:
        return "build"
    return None


def _error_paths(
    summary: Summary, *, ignore_job: bool
) -> list[tuple[str, int | None]]:
    found: list[tuple[str, int | None]] = []
    seen: set[str] = set()

    def _consume(text: str) -> None:
        for path, line in extract_source_paths(text):
            key = f"{path}:{line}"
            if key in seen:
                continue
            seen.add(key)
            found.append((path, line))

    for stream in summary.pipeline_logs:
        _consume("\n".join(_stream_lines(stream)))
        for trace in stream.stack_traces:
            _consume(trace.content)
    if not ignore_job:
        for job in summary.failed_jobs:
            _consume(_job_text(job))
            for trace in job.stack_traces:
                _consume(trace.content)
    return found


def _stack_paths(summary: Summary) -> list[tuple[str, int | None]]:
    found: list[tuple[str, int | None]] = []
    for job in summary.failed_jobs:
        for trace in job.stack_traces:
            found.extend(extract_source_paths(trace.content))
    for stream in summary.pipeline_logs:
        for trace in stream.stack_traces:
            found.extend(extract_source_paths(trace.content))
    return found


def _changed_files(changes: ChangeContext | None) -> list[str]:
    if changes is None:
        return []
    if changes.files:
        return list(changes.files)
    if not changes.diffstat:
        return []
    files: list[str] = []
    for line in changes.diffstat.splitlines():
        name = line.split("|", 1)[0].strip()
        if name and name != "…":
            files.append(name)
    return files


def _intersects(path: str, changed: Sequence[str]) -> bool:
    posix = path.replace("\\", "/").lstrip("./")
    for item in changed:
        other = item.replace("\\", "/").lstrip("./")
        if posix == other or posix.endswith(other) or other.endswith(posix):
            return True
    return False


def _path_class(path: str) -> str:
    from .changes import classify_path

    return classify_path(path)


def _all_templates(summary: Summary) -> list[LogTemplate]:
    out: list[LogTemplate] = []
    if summary.drain is not None:
        out.extend(summary.drain.templates)
    for stream in summary.pipeline_logs:
        out.extend(stream.templates)
    return out
