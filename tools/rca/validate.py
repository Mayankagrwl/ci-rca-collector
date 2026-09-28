"""Post-hoc grounding validation (eval-spec §4). No model calls; never crashes.

This is the *production* consolidation of the eval-spec grounding checks, mapped
onto the real model (``AnalysisResult.citations``). It is pure scoring only —
the retrying behaviour lives in ``analyze`` (the Step 2 focused re-ask), and the
failed-step anchor grounding (``analyze.is_grounded``) stays separate and drives
the Step 5 confidence cap. This module adds the anti-fabrication rate against the
sent prompt evidence, with §4.2 normalisation so whitespace/case/punctuation
differences no longer produce false-ungrounded citations.
"""

from __future__ import annotations

import logging
import re

from .models import AnalysisResult, GroundingResult, Summary

_LOG = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WS_RE = re.compile(r"\s+")
_STRIP_CHARS = " \t\r\n.,;:\"'`"
# A commit-ish hex token cited in prose (eval-spec §4.3).
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def normalise(text: str) -> str:
    """eval-spec §4.2: strip ANSI, collapse whitespace, trim ws/punct, lowercase.

    Applied to BOTH the citation quote and the evidence before comparing.
    """
    if not text:
        return ""
    out = _ANSI_RE.sub("", text)
    out = _WS_RE.sub(" ", out)
    out = out.strip().strip(_STRIP_CHARS).strip()
    return out.lower()


def check_grounding(
    result: AnalysisResult,
    prompt_evidence: str,
    summary: Summary,
) -> GroundingResult:
    """Score a result's citations against the sent evidence. Pure; no model call."""
    checks_run: list[str] = ["fabrication"]
    citations = list(result.citations or [])
    citation_count = len(citations)
    norm_evidence = normalise(prompt_evidence)

    ungrounded: list[str] = []
    found = 0
    for cite in citations:
        norm_quote = normalise(cite.quote or "")
        if norm_quote and norm_quote in norm_evidence:
            found += 1
        else:
            ungrounded.append((cite.quote or "")[:240])

    rate = (found / citation_count) if citation_count else 0.0
    grounded = citation_count > 0 and found == citation_count

    component = _component_grounded(result, summary, checks_run)
    shas = _shas_grounded(result, summary, checks_run)

    return GroundingResult(
        grounded=grounded,
        citation_count=citation_count,
        ungrounded_citations=ungrounded,
        grounding_rate=round(rate, 4),
        component_grounded=component,
        shas_grounded=shas,
        checks_run=checks_run,
    )


def display_action(grounding: GroundingResult) -> str:
    """eval-spec §4.4 → 'publish' | 'strip' | 'fallback' (never 'suppress')."""
    if grounding.citation_count > 0 and grounding.grounding_rate >= 1.0:
        return "publish"
    if grounding.citation_count > 0 and grounding.grounding_rate >= 0.5:
        return "strip"
    # TODO: eval-spec §4.4 literal 'suppress' is available as a future config
    # option; default is fallback (Step 5 "always render one section" holds).
    return "fallback"


def _component_grounded(
    result: AnalysisResult, summary: Summary, checks_run: list[str]
) -> bool | None:
    """§4.3: does a suspected component appear in changed files / stack / job name?

    None when there is no changes/stack/job data to check against (never False
    on absence of reference data).
    """
    files = list(summary.changes.files) if summary.changes is not None else []
    stack_text = "\n".join(
        f"{trace.headline or ''}\n{trace.content or ''}"
        for job in summary.failed_jobs
        for trace in job.stack_traces
    )
    job_names = [job.name for job in summary.failed_jobs if job.name]
    if not files and not stack_text.strip() and not job_names:
        return None
    checks_run.append("component")

    components = [c for c in (result.suspected_files or []) if c]
    if not components:
        return None

    norm_files = {f.replace("\\", "/").strip() for f in files if f}
    norm_stack = stack_text.replace("\\", "/")
    for comp in components:
        c = comp.replace("\\", "/").strip()
        if not c:
            continue
        base = c.rsplit("/", 1)[-1]
        for f in norm_files:
            if c == f or f.endswith("/" + c) or c.endswith("/" + f) or f.rsplit("/", 1)[-1] == base:
                return True
        if base and base in norm_stack:
            return True
        if any(comp in name or name in comp for name in job_names):
            return True
    return False


def _shas_grounded(
    result: AnalysisResult, summary: Summary, checks_run: list[str]
) -> bool | None:
    """§4.3: every hex SHA cited in root_cause/suggested_fix must be a real commit.

    None when there is no changes context, or no SHA was cited.
    """
    if summary.changes is None:
        return None
    checks_run.append("shas")
    commits = {
        commit.sha.lower()
        for commit in summary.changes.commits
        if commit.sha
    }
    text = f"{result.root_cause or ''}\n{result.suggested_fix or ''}".lower()
    cited = _SHA_RE.findall(text)
    if not cited:
        return None
    for sha in cited:
        if not any(
            cs == sha or cs.startswith(sha) or sha.startswith(cs[:7])
            for cs in commits
        ):
            return False
    return True
