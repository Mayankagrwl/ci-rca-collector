"""Phase 2 prompt: evidence from summary.json only, wrapped in <EVIDENCE>."""

from __future__ import annotations

import re

from .budget import token_count, trim_middle
from .config import (
    ANALYZE_EVIDENCE_CAP,
    EVIDENCE_PROFILE_DEFAULT,
    EVIDENCE_PROFILES,
    JUNIT_EVIDENCE_CAP,
    PROMPT_VERSION,
    SECTION_TOKEN_CAPS,
)
from .models import FailedJob, Summary

SYSTEM_PROMPT = (
    f"You are a CI root-cause assistant (prompt {PROMPT_VERSION}). "
    "The job failed at failed_step. Cite that step's excerpt first. "
    "Do not treat a later permission/403/template as the root cause if an earlier "
    "failed step already explained the exit (version exists, tests failed, compile error). "
    "Follow-on errors after the failed step are symptoms. "
    "DETERMINISTIC_HINT is collector input + fallback, not the user-facing answer. "
    "Use DETERMINISTIC_HINT when it matches the first_error_window or failed_step_excerpt. "
    "If the log clearly names another cause (Artifactory version already exists, "
    "missing package, compile file:line, ERESOLVE, JUnit failure), "
    "PREFER the log and ignore a generic ci_config / workflow-changed hint. "
    "When short_circuit is infra_runner, infra_widespread, or flake_same_sha_passed, "
    "do not invent an application code bug. "
    "Prefer the job log failed_step_excerpt and first_error_window over pipeline_logs. "
    "Use only the text inside <EVIDENCE>. Do not invent log lines, file paths, or test names. "
    "Quote only from <EVIDENCE>. Reply with ONLY a single JSON object. "
    "No markdown fences, no prose, no commentary. "
    "Keys: root_cause (string), suggested_fix (string), "
    "confidence (high|medium|low), "
    "citations (array of {quote, source, line}), "
    "cannot_determine (bool), suspected_files (array of string), "
    "suspected_stage (string or null), infra_or_code (infra|code|unknown), "
    "used_deterministic_rule (string or null). "
    "Each citations[].quote MUST be a verbatim substring of <EVIDENCE>. "
    "source must be one of: first_error_window, tail_window, stack_traces, "
    "log_templates, junit, change_context, annotations, history, step_table, "
    "pipeline_logs, code_context, last_green_compare, deterministic_rule, "
    "failed_step_excerpt. "
    "If evidence is insufficient, cannot_determine=true, confidence=low — do not invent files."
)

_REPAIR = (
    "Your previous reply was not valid JSON: {reason}. "
    "Reply with ONLY the JSON object. No markdown fences, no prose, no commentary. "
    "Keys: root_cause, suggested_fix, confidence, citations. "
    "citations[].quote must be copied verbatim from <EVIDENCE>."
)

_INFRA_FLAKE_SHORT = frozenset(
    {"infra_runner", "infra_widespread", "flake_same_sha_passed"}
)
_PIPELINE_STEP = re.compile(
    r"docker|container|build|compile|package|test|pytest|lint|e2e|integration",
    re.IGNORECASE,
)


def build_evidence(
    summary: Summary,
    *,
    cap_tokens: int | None = None,
    exclude: set[str] | None = None,
) -> str:
    """Priority-packed evidence. failed_step_excerpt is mandatory and untrimmed.

    ``exclude`` drops named sections (by key) before packing; the default
    (non-focused) ordering and output are unchanged when it is omitted.
    """
    cap = ANALYZE_EVIDENCE_CAP if cap_tokens is None else cap_tokens
    skip = set(exclude or set())
    if _terminal_cause_present(summary):
        # A terminal cause at the failed step: drop symptom-prone sections up
        # front so the first model call can't wander to a follow-on symptom.
        skip |= {"log_templates", "pipeline_logs"}
    packed: list[str] = []
    used = 0
    for key, mandatory, lines in _priority_sections(summary):
        if key in skip:
            continue
        block = "\n".join(line for line in lines if line is not None and str(line) != "")
        if not block.strip():
            continue
        tokens = token_count(block)
        if not mandatory and used + tokens > cap:
            continue
        packed.append(block)
        used += tokens
    return "<EVIDENCE>\n" + "\n".join(packed) + "\n</EVIDENCE>"


def _terminal_cause_present(summary: Summary) -> bool:
    """Lazy bridge to the deterministic terminal-cause check; never raises."""
    try:
        from .diagnose import terminal_cause_present

        return terminal_cause_present(summary)
    except Exception:  # noqa: BLE001 — evidence packing must not crash
        return False


def focused_evidence(summary: Summary, *, cap_tokens: int | None = None) -> str:
    """Evidence with log_templates and pipeline_logs removed (grounding re-ask)."""
    return build_evidence(
        summary,
        cap_tokens=cap_tokens,
        exclude={"log_templates", "pipeline_logs"},
    )


def failed_step_anchor_text(summary: Summary) -> str:
    """Failed-step evidence a grounded answer must quote from (Step 2 gate).

    primary_failure_line + failed_step_excerpt + the first_error/merged window +
    the primary job's stack traces (Step 6: a crash/compile frame is the failed
    step's own output). This is what the grounding check greps against.
    """
    parts: list[str] = []
    parts.extend(_primary_failure_line_lines(summary))
    parts.extend(_excerpt_lines(summary))
    parts.extend(_job_first_error_lines(summary))
    parts.extend(_stack_trace_lines(summary))
    return "\n".join(part for part in parts if part)


def build_messages(
    evidence: str,
    *,
    prior_completion: str | None = None,
    repair_reason: str | None = None,
) -> list[dict[str, str]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{evidence}\n\n"
                "The job failed at failed_step. Cite that excerpt first. "
                "Follow-on permission/403 lines after the failed step are symptoms. "
                "JSON only, citations from <EVIDENCE>."
            ),
        },
    ]
    if prior_completion:
        messages.append({"role": "assistant", "content": prior_completion})
        messages.append(
            {
                "role": "user",
                "content": _REPAIR.format(reason=repair_reason or "validation failed"),
            }
        )
    return messages


_MANDATORY_SECTIONS = {"primary_failure_line", "failed_step_excerpt"}


def _section_builders() -> dict[str, "callable"]:  # type: ignore[type-arg]
    return {
        "primary_failure_line": _primary_failure_line_lines,
        "failed_step_excerpt": _excerpt_lines,
        "first_error_window": _job_first_error_lines,
        "stack_traces": _stack_trace_lines,
        "junit": _junit_lines,
        "code_context": _code_context_lines,
        "deterministic_hint": _deterministic_hint_lines,
        "log_templates": _template_lines,
        "pipeline_logs": _pipeline_window_lines,
        "change_context": _change_one_liner,
    }


def _profile_for(summary: Summary) -> list[str]:
    category = (summary.classification.category or "").strip().lower()
    return EVIDENCE_PROFILES.get(category, EVIDENCE_PROFILE_DEFAULT)


def _priority_sections(summary: Summary) -> list[tuple[str, bool, list[str]]]:
    """(key, mandatory, lines) in category-aware order. Stop optionals at cap."""
    builders = _section_builders()
    out: list[tuple[str, bool, list[str]]] = []
    for key in _profile_for(summary):
        builder = builders.get(key)
        if builder is None:
            continue
        out.append((key, key in _MANDATORY_SECTIONS, builder(summary)))
    return out


def _capped_section(key: str, header: str, body: str) -> list[str]:
    """A section trimmed to its SECTION_TOKEN_CAPS entry (header + body kept)."""
    if not body.strip():
        return []
    text = f"{header}\n{body}"
    cap = SECTION_TOKEN_CAPS.get(key)
    if cap:
        text, _trimmed, _before, _after = trim_middle(text, cap)
    return [text]


def _stack_trace_lines(summary: Summary) -> list[str]:
    job = _primary_job(summary)
    if job is None or not job.stack_traces:
        return []
    blocks = [trace.content for trace in job.stack_traces if (trace.content or "").strip()]
    return _capped_section("stack_traces", "### stack_traces", "\n".join(blocks))


def _junit_lines(summary: Summary) -> list[str]:
    report = summary.junit
    if report is None or not report.failures:
        return []
    parts: list[str] = []
    for failure in report.failures[:JUNIT_EVIDENCE_CAP]:
        name = (
            f"{failure.classname}::{failure.name}"
            if failure.classname
            else (failure.name or "")
        )
        if name:
            parts.append(name)
        if failure.message:
            parts.append(failure.message)
        if failure.body:
            parts.append(failure.body)
    return _capped_section("junit", "### junit", "\n".join(parts))


def _code_context_lines(summary: Summary) -> list[str]:
    ctx = summary.code_context
    if ctx is None or not ctx.hunks:
        return []
    parts: list[str] = []
    for hunk in ctx.hunks:
        parts.append(f"{hunk.path}:{hunk.start_line}-{hunk.end_line}")
        if hunk.content:
            parts.append(hunk.content)
    return _capped_section("code_context", "### code_context", "\n".join(parts))


def _primary_failure_line_lines(summary: Summary) -> list[str]:
    """First cause line inside the failed step. Tiny, mandatory, never trimmed."""
    job = _primary_job(summary)
    if job is None or not job.primary_failure_line:
        return []
    return ["### primary_failure_line", job.primary_failure_line]


def _excerpt_lines(summary: Summary) -> list[str]:
    job = _primary_job(summary)
    if job is None:
        return []
    excerpt = job.failed_step_excerpt
    name = (excerpt.name if excerpt is not None else None) or job.failed_step_name or ""
    lines = list(excerpt.lines if excerpt is not None else [])
    if not name and not lines:
        return []
    out = ["### failed_step_excerpt", f"failed_step: {name}"]
    out.extend(lines)
    return out


def _job_first_error_lines(summary: Summary) -> list[str]:
    job = _primary_job(summary)
    if job is None:
        return []
    first = [w for w in job.windows if w.label == "first_error"]
    if not first:
        first = [w for w in job.windows if w.label == "merged"]
    if not first or not first[0].content.strip():
        return []
    return ["### first_error_window", first[0].content]


def _template_lines(summary: Summary) -> list[str]:
    templates = []
    if summary.drain is not None:
        templates.extend(summary.drain.templates)
    chosen = [t for t in templates if t.tier in {"T1", "T2"}]
    if not chosen:
        return []
    lines = ["### log_templates"]
    for tmpl in chosen[:5]:
        lines.append(f"[{tmpl.tier}] {tmpl.template} count={tmpl.count}")
        if tmpl.representative_line:
            lines.append(tmpl.representative_line)
    return lines


def _pipeline_window_lines(summary: Summary) -> list[str]:
    job = _primary_job(summary)
    step = ""
    if job is not None:
        if job.failed_step_excerpt is not None:
            step = job.failed_step_excerpt.name or ""
        step = step or (job.failed_step_name or "")
    if not _PIPELINE_STEP.search(step):
        return []
    if not summary.pipeline_logs:
        return []
    # Prefer a non-teardown stream; teardown streams carry follow-on noise.
    ordered = sorted(
        summary.pipeline_logs,
        key=lambda s: 1 if s.phase == "teardown" else 0,
    )
    stream = ordered[0]
    windows = [w for w in stream.windows if w.label in {"first_error", "merged"}]
    if not windows:
        return []
    return ["### pipeline_logs", windows[0].content]


def _change_one_liner(summary: Summary) -> list[str]:
    changes = summary.changes
    if changes is None:
        return []
    classes = ",".join(changes.classes)
    return [
        "### change_context",
        f"range {changes.range_basis} {changes.base_sha}..{changes.head_sha} "
        f"classes={classes}",
    ]


def _primary_job(summary: Summary) -> FailedJob | None:
    return summary.failed_jobs[0] if summary.failed_jobs else None


def _deterministic_hint_lines(summary: Summary) -> list[str]:
    """Always-on compact hint. Deterministic output is input + fallback."""
    diag = summary.diagnosis
    verdict = summary.verdict
    cls = summary.classification
    rule = (diag.rule_id if diag is not None else "") or (verdict.reason or "")
    one = (diag.one_liner if diag is not None else "") or ""
    fix = (diag.fix_one_liner if diag is not None else None) or ""
    stage = (diag.suspected_stage if diag is not None else None) or ""
    files = ",".join(list(diag.suspected_files if diag is not None else [])[:8])
    short = verdict.short_circuit or ""
    lines = [
        "DETERMINISTIC_HINT:",
        f"  rule_id: {rule}",
        f"  category: {cls.category}",
        f"  one_liner: {one}",
        f"  suggested_fix: {fix}",
        f"  suspected_stage: {stage}",
        f"  suspected_files: {files}",
        f"  requires_analysis: {str(bool(verdict.requires_analysis)).lower()}",
        f"  short_circuit: {short}",
    ]
    if short in _INFRA_FLAKE_SHORT:
        lines.append(
            "  instruction: High-confidence infrastructure/flake verdict; "
            "do not invent a code bug."
        )
    return lines
