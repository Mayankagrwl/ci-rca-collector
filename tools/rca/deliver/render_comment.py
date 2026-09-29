"""PR / commit comment body (delivery spec v1.3 §7, §7.1–§7.3, §11). Pure.

No client, no HTTP, no clock, no file I/O: ``render_comment`` maps
(summary, record, context, decision) to the exact text Step 15 will post.

Safety, in order: every model/log-derived string is redacted, @mentions and
rule ids are defused, fork text is HTML-escaped outside the code fence, the
whole assembled body is redacted again, and only then is it cut to size by
structure (evidence lines first, then the whole ``<details>`` block). The
marker, heading, headline and footer are never cut.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

from ..extract import is_benign_line, is_exit_code_line
from ..models import AnalysisRecord, Summary
from ..prompt import failed_step_anchor_text
from ..redact import redact_text
from . import DeliveryContext, SuppressionDecision

MAX_BODY_CHARS = 4000
MAX_EVIDENCE_LINES = 8
MAX_HEADLINE_CHARS = 300
MAX_FIX_CHARS = 300
MAX_EVIDENCE_LINE_CHARS = 300
MAX_STEP_NAME_CHARS = 100
MARKER_PREFIX = "<!-- rca-bot:fp="

# Every category the collector can emit. A missing key renders _GENERIC_CATEGORY,
# never the raw key.
CATEGORY_WORDS: dict[str, str] = {
    "auth": "authentication or permission failure",
    "compile": "compilation error",
    "crash": "process crashed",
    "dependency": "dependency could not be installed or resolved",
    "disk_space": "runner ran out of disk space",
    "image_pull": "container image could not be pulled",
    "infra_runner": "runner infrastructure problem",
    "infra_widespread": "widespread infrastructure outage",
    "network_dns": "network or DNS failure",
    "oom": "out of memory",
    "release": "release version already published",
    "test_failure": "test failure",
    "timeout": "timed out",
    "unknown": "unclassified failure",
}
_GENERIC_CATEGORY = "unrecognised failure type"

UNVERIFIED_BANNER = (
    "> [!WARNING]\n"
    "> **Unverified** — this diagnosis is not fully grounded in the failed step's log. "
    "Treat it as a lead, not a confirmed cause."
)

_MODEL_SOURCES = frozenset({"ai", "hybrid", "mixed"})
_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9])")
_RULE_ID_RE = re.compile(r"\bR(\d+)\b")
_BACKTICK_RUN_RE = re.compile(r"`+")
_ZWSP = "​"


def category_words(category: str | None) -> str:
    return CATEGORY_WORDS.get((category or "").strip(), _GENERIC_CATEGORY)


def marker_line(fingerprint_coarse: str) -> str:
    """The sticky key: the exact first line of every RCA comment (v1.3 §7.3)."""
    return f"{MARKER_PREFIX}{fingerprint_coarse} -->"


def marker(summary: Summary) -> str:
    return marker_line(summary.fingerprint_coarse)


def render_comment(
    summary: Summary,
    record: AnalysisRecord | None,
    context: DeliveryContext,
    decision: SuppressionDecision,
) -> str:
    fork = bool(context.is_fork)
    headline, fix, confidence, needs_review = _headline(summary, record, decision)

    def prose(text: str | None, limit: int) -> str:
        return _prose(text, limit, fork=fork)

    job = summary.failed_jobs[0] if summary.failed_jobs else None
    step = prose((job.failed_step_name if job else "") or "", MAX_STEP_NAME_CHARS).replace("`", "")

    head: list[str] = [marker(summary)]
    if decision.unverified_banner or needs_review:
        head.append(UNVERIFIED_BANNER)
    head.append(f"### CI failure — {category_words(summary.classification.category)}")
    head.append("")
    if headline:
        head.append(f"**{prose(headline, MAX_HEADLINE_CHARS)}**")
    meta = [f"Confidence: {confidence}"]
    if step:
        meta.append(f"`{step}`")
    meta.append(_run_link(summary))
    head.append(" · ".join(meta))
    if fix:
        head.extend(["", f"**Suggested fix** — {prose(fix, MAX_FIX_CHARS)}"])

    seen = (summary.history.seen_count if summary.history is not None else 0) + 1
    footer = [
        "",
        "---",
        f"<sub>Seen {seen}× · [full report]({summary.run.html_url}) · "
        "reply `/resolved <what fixed it>`</sub>",
    ]

    evidence = [_evidence_line(line) for line in evidence_lines(summary, record)]
    evidence = [line for line in evidence if line]
    return _fit(head, evidence, footer)


# ---- headline (v1.3 §7.1) -----------------------------------------------------


def _headline(
    summary: Summary, record: AnalysisRecord | None, decision: SuppressionDecision
) -> tuple[str | None, str | None, str, bool]:
    """(headline, fix, confidence label, needs_review) — one source for all three."""
    from ..analyze import display_status  # lazy: analyze pulls in the STGPT client

    status = display_status(record) if record is not None else "deterministic"
    result = record.result if record is not None else None
    needs_review = status == "needs-review"
    model_answer = (
        result is not None
        and status in {"ok", "cached"}
        and (result.source or "").strip().lower() in _MODEL_SOURCES
    )
    # A result that *is* the deterministic card (skipped record, or the Step 2 gate
    # replacement). Model prose is never used outside rule 2.
    det_result = (
        result
        if result is not None
        and not model_answer
        and (result.source or "").strip().lower() == "deterministic"
        else None
    )

    if model_answer:
        confidence = result.confidence  # type: ignore[union-attr]
    elif det_result is not None:
        confidence = det_result.confidence
    else:
        confidence = summary.classification.confidence or "low"

    # 1. Below threshold: no root-cause claim from either source.
    if decision.omit_root_cause:
        return None, None, confidence, needs_review
    # 2. A validated model answer.
    if model_answer:
        return result.root_cause, result.suggested_fix, confidence, needs_review  # type: ignore[union-attr]
    # 3./4. The deterministic card (needs-review adds the banner, never "confirmed").
    diag = summary.diagnosis
    headline = diag.one_liner if diag is not None else None
    fix = diag.fix_one_liner if diag is not None else None
    if not headline and det_result is not None:
        headline, fix = det_result.root_cause, fix or det_result.suggested_fix
    return headline or None, fix or None, confidence, needs_review


# ---- evidence (v1.3 §7.2) -------------------------------------------------------


def evidence_lines(summary: Summary, record: AnalysisRecord | None) -> list[str]:
    """primary_failure_line, then non-benign excerpt lines, then anchored citations.

    Benign and exit-code lines are never evidence: neither explains a failure.
    """
    job = summary.failed_jobs[0] if summary.failed_jobs else None
    ordered: list[str] = []
    if job is not None and job.primary_failure_line:
        ordered.append(job.primary_failure_line)
    if job is not None and job.failed_step_excerpt is not None:
        ordered.extend(job.failed_step_excerpt.lines)
    anchor = _anchor_lines(summary)
    for quote in _citation_quotes(summary, record):
        # Only quotes from the failed step's anchor — never a template or a
        # pipeline line from a step that did not fail.
        ordered.extend(ln for ln in quote.splitlines() if ln.strip() in anchor)
    out: list[str] = []
    for raw in ordered:
        line = (raw or "").rstrip()
        if not line.strip() or is_benign_line(line) or is_exit_code_line(line) or line in out:
            continue
        out.append(line)
        if len(out) >= MAX_EVIDENCE_LINES:
            break
    return out


def _anchor_lines(summary: Summary) -> set[str]:
    try:
        text = failed_step_anchor_text(summary)
    except Exception:  # noqa: BLE001 — rendering must not crash on the anchor
        return set()
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("### ")}


def _citation_quotes(summary: Summary, record: AnalysisRecord | None) -> Iterable[str]:
    result = record.result if record is not None else None
    if result is not None and result.citations:
        ungrounded = set(record.grounding.ungrounded_citations) if (
            record is not None and record.grounding is not None
        ) else set()
        return [c.quote for c in result.citations if c.quote not in ungrounded]
    if summary.diagnosis is not None:
        return list(summary.diagnosis.citations)
    return []


# ---- safety -------------------------------------------------------------------------


def _defuse(text: str) -> str:
    """No pings, no rule ids: @user → @​user, R19 → R​19."""
    text = _MENTION_RE.sub("@" + _ZWSP, text)
    return _RULE_ID_RE.sub("R" + _ZWSP + r"\1", text)


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _prose(text: str | None, limit: int, *, fork: bool) -> str:
    redacted, _ = redact_text(_one_line(text or ""))
    out = _clip(_defuse(redacted), limit)
    return html.escape(out, quote=False) if fork else out


def _evidence_line(text: str) -> str:
    redacted, _ = redact_text(text)
    return _clip(_defuse(redacted.rstrip()), MAX_EVIDENCE_LINE_CHARS)


def _run_link(summary: Summary) -> str:
    run = summary.run
    link = f"[run {run.run_id}]({run.html_url})"
    return f"{link} (attempt {run.run_attempt})" if (run.run_attempt or 1) > 1 else link


def _fence(lines: list[str]) -> str:
    longest = max((len(m) for ln in lines for m in _BACKTICK_RUN_RE.findall(ln)), default=0)
    return "`" * max(3, longest + 1)


def _details(evidence: list[str]) -> list[str]:
    fence = _fence(evidence)
    return [
        "",
        "<details><summary>Evidence</summary>",
        "",
        f"{fence}text",
        *evidence,
        fence,
        "</details>",
    ]


def _assemble(head: list[str], evidence: list[str] | None, footer: list[str]) -> str:
    parts = list(head)
    if evidence:
        parts.extend(_details(evidence))
    parts.extend(footer)
    body = "\n".join(parts) + "\n"
    first, _, rest = body.partition("\n")
    redacted_rest, _ = redact_text(rest)  # whole body, before any size cut
    return f"{first}\n{redacted_rest}"


def _fit(head: list[str], evidence: list[str], footer: list[str]) -> str:
    """Cap at MAX_BODY_CHARS: shrink evidence, then drop <details>; never cut the rest."""
    lines = list(evidence)
    body = _assemble(head, lines, footer)
    while len(body) > MAX_BODY_CHARS and lines:
        lines.pop()
        body = _assemble(head, lines, footer)
    return body


__all__ = [
    "CATEGORY_WORDS",
    "MAX_BODY_CHARS",
    "UNVERIFIED_BANNER",
    "category_words",
    "evidence_lines",
    "marker",
    "marker_line",
    "render_comment",
]
